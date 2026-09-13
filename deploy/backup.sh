#!/usr/bin/env bash
#
# 全量备份：导出 → 加密 → **验证** → 上传 → 轮转（spec §98.1；T0.9）。
#
# ⚠️ **「备份在跑」和「备份能恢复」是两件事，只有第二件算数。**所以这个脚本在
# 上传之前会把刚加密出来的文件**解回来验一遍** —— 一个加密口令配错、或者 dump
# 中途被截断的备份，看起来和好的备份一模一样：都是一个大小合理的文件静静躺在
# 桶里，直到真出事那天才发现打不开。
#
# 用法：
#     deploy/backup.sh            # 全量 + 上传 + 轮转
#     deploy/backup.sh --dry-run  # 全量 + 验证，但不上传（本地演练用）
#
# 配置从部署目录的 `.env` 读（Kelvin 2026-09-13 决定 R2 凭据也放那里）：
#     BILLING_R2_BUCKET / BILLING_R2_ENDPOINT
#     R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
#     BILLING_BACKUP_PASSPHRASE
#
# ⚠️ 两条与「凭据放 .env」绑定的注意事项：
#   1. 这个 .env 从此同时是「数据库口令」和「备份钥匙」—— 一次误贴同时交出
#      数据库和它的全部备份。别往任何地方贴
#   2. **往 .env 加的凭据不能含 `$`**。compose 会把 `.env` 里的 `$` 当变量展开，
#      展不出来就**静默截断**（实测 `abc$def$ghi` 变成 `abc`，不报错不告警）。
#      R2 的 key 是 hex/base64、口令用 `openssl rand -base64`，都不含 `$`

set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"
BACKUP_DIR="${BILLING_BACKUP_DIR:-./backups}"
# 本地留几天。⚠️ 长期保留是 R2 那一侧的事 —— 这台机器只剩 12 GB。
LOCAL_KEEP_DAYS="${BILLING_BACKUP_KEEP_DAYS:-3}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# S3 客户端跑在容器里，不装在宿主机上。
#
# ⚠️ 两个理由都很实在：生产 VPS 上**不一定有** `aws` 命令，而那台机器上确定有的
# 只有 Docker；而且版本钉死在镜像标签上，不会因为宿主机某次 apt upgrade 就换了行为。
#
# ⚠️ 凭据用 `-e NAME`（不带值）传进容器，**不是** `-e NAME=value`：后者会让
# 口令出现在 `ps` 能看到的命令行参数里，同机任何用户都读得到。
AWS_CLI_IMAGE="${BILLING_AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"

aws_cli() {
    # ⚠️ MSYS_NO_PATHCONV 只对 Windows 的 Git Bash 有意义（Linux 上是个无害的空变量）：
    # Git Bash 会把容器内路径 `/data/...` 改写成 `D:/Git/data/...`，于是上传报
    # 「路径不存在」。**只能加在 docker 这一条上** —— 全局关掉的话，mktemp 给出的
    # `/tmp/...` 反过来会让原生的 openssl 打不开。两种都实测踩过。
    #
    # ⚠️ **这段注释必须在命令外面。**写在下面那串反斜杠续行的中间，续行会在注释
    # 那一行断掉：前两个凭据变成当前 shell 里**没有导出**的变量，`docker run -e`
    # 拿不到，报的是 `Unable to locate credentials` —— 看着像凭据填错了。实测踩过。
    AWS_ACCESS_KEY_ID="$(env_value R2_ACCESS_KEY_ID)" \
    AWS_SECRET_ACCESS_KEY="$(env_value R2_SECRET_ACCESS_KEY)" \
    MSYS_NO_PATHCONV=1 \
    docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
        -e AWS_DEFAULT_REGION=auto \
        -v "$(cd "$BACKUP_DIR" && pwd):/data" \
        "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" "$@"
}
s3() { aws_cli s3 "$@" --only-show-errors; }
s3api() { aws_cli s3api "$@"; }

# 从 .env 取一个值。
# ⚠️ **刻意不用 `source`** —— 那等于执行这个文件，而它是一份凭据清单，
# 里面任何一行写错都会变成命令。这里只做字面解析。
env_value() {
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | head -1
}

# --------------------------------------------------------------------------
# 0. 准备
# --------------------------------------------------------------------------

PASSPHRASE="$(env_value BILLING_BACKUP_PASSPHRASE)"
[ -n "$PASSPHRASE" ] || die "BILLING_BACKUP_PASSPHRASE is not set in $ENV_FILE"

MYSQL_DATABASE="$(env_value BILLING_MYSQL_DATABASE)"
MYSQL_DATABASE="${MYSQL_DATABASE:-billing}"
MYSQL_ROOT_PASSWORD="$(env_value BILLING_MYSQL_ROOT_PASSWORD)"
[ -n "$MYSQL_ROOT_PASSWORD" ] || die "BILLING_MYSQL_ROOT_PASSWORD is not set in $ENV_FILE"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PLAIN="${BACKUP_DIR}/billing-${STAMP}.sql"
CIPHER="${PLAIN}.enc"

# ⚠️ 明文 dump 落在磁盘上的那段时间是这个脚本最脆弱的窗口。退出时一定删掉，
# 无论成功还是失败 —— 失败路径不清理的话，一次磁盘满就会留下一份明文的整库。
cleanup() { rm -f "$PLAIN"; }
trap cleanup EXIT

# --------------------------------------------------------------------------
# 1. 导出
# --------------------------------------------------------------------------
#
# ⚠️ `--single-transaction`：不锁表地取一个一致性快照（InnoDB）。少了它，
# 备份期间整个库对写入是阻塞的 —— 而备份是每天都要跑的事。
#
# ⚠️ `--source-data=2`：把**导出那一刻的 binlog 位置**写进 dump 的注释里。
# 这一条是 PITR 的锚点：没有它，你有一份全量和一堆 binlog，却不知道该从哪一条
# 开始重放 —— 少放会丢数据，多放会重复。

log "dumping ${MYSQL_DATABASE}"
$COMPOSE exec -T mysql mysqldump \
    -uroot -p"$MYSQL_ROOT_PASSWORD" \
    --single-transaction \
    --source-data=2 \
    --routines --events --triggers \
    "$MYSQL_DATABASE" > "$PLAIN" 2>/dev/null \
    || die "mysqldump failed"

[ -s "$PLAIN" ] || die "the dump is empty"

# ⚠️ 非空不等于完整。mysqldump 正常结束时会在末尾写一行 `-- Dump completed`；
# 中途被杀 / 磁盘满 / 连接断掉时**不会**。少了这条检查，一份被截断的 dump
# 会被照常加密上传，而它长得和好的一模一样。
grep -q "^-- Dump completed" "$PLAIN" || die "the dump is truncated (no completion marker)"

# 记下 binlog 锚点，方便人直接看见。
ANCHOR="$(grep -m1 '^-- CHANGE REPLICATION SOURCE TO' "$PLAIN" || true)"
log "dump ok ($(wc -c < "$PLAIN") bytes)"
[ -n "$ANCHOR" ] && log "binlog anchor: ${ANCHOR#-- }"

# --------------------------------------------------------------------------
# 2. 加密
# --------------------------------------------------------------------------
#
# ⚠️ **上传前自己加密**（决策 ⑦a），不依赖 R2 的服务端加密 —— 那样密钥在
# Cloudflare 手里，备份对它是明文。
#
# `-pbkdf2 -iter` 不是可选的：openssl 的老式派生（EVP_BytesToKey）对口令爆破
# 几乎没有阻力。

log "encrypting"
printf '%s' "$PASSPHRASE" | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -in "$PLAIN" -out "$CIPHER" -pass stdin \
    || die "encryption failed"

# --------------------------------------------------------------------------
# 3. ⚠️ 验证 —— 这一步是整个脚本存在的理由
# --------------------------------------------------------------------------
#
# 把刚写出来的密文解回来，确认它确实是那份 dump。
# 不验的话，一个配错的口令、一次写盘错误、一个被截断的上传，产出的都是一个
# 「大小合理、静静躺在桶里」的文件 —— 直到真出事那天才发现打不开。

log "verifying the ciphertext round-trips"
VERIFY="$(mktemp)"
trap 'rm -f "$PLAIN" "$VERIFY"' EXIT

printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
    -in "$CIPHER" -out "$VERIFY" -pass stdin \
    || die "the encrypted backup cannot be decrypted — NOT uploading"

# 逐字节比对，不是比大小。
cmp -s "$PLAIN" "$VERIFY" || die "the decrypted backup differs from the dump — NOT uploading"
log "verified"

# --------------------------------------------------------------------------
# 4. 上传
# --------------------------------------------------------------------------

R2_BUCKET="$(env_value BILLING_R2_BUCKET)"
R2_ENDPOINT="$(env_value BILLING_R2_ENDPOINT)"

if [ "$DRY_RUN" = "1" ]; then
    log "--dry-run: skipping the upload"
elif [ -z "$R2_BUCKET" ] || [ -z "$R2_ENDPOINT" ]; then
    # ⚠️ **没配 R2 不是「静默跳过」**。一个只在本地留备份的系统满足不了 §98.1
    # 的 off-VPS 要求，而它看起来一切正常 —— 所以这里必须是显式失败。
    die "R2 is not configured (BILLING_R2_BUCKET / BILLING_R2_ENDPOINT); the backup is local-only and does NOT satisfy spec §98.1"
else
    log "uploading to ${R2_BUCKET}"
    s3 cp "/data/$(basename "$CIPHER")" "s3://${R2_BUCKET}/full/$(basename "$CIPHER")" \
        || die "upload failed; the verified backup is still at $CIPHER"

    # ⚠️ 传完**拉一次元数据回来核对大小**。`cp` 返回 0 只说明客户端认为它发完了；
    # 一次被中间设备截断的上传，本地看起来同样是成功的。
    REMOTE_SIZE="$(s3api head-object --bucket "$R2_BUCKET" \
        --key "full/$(basename "$CIPHER")" --query ContentLength --output text)" \
        || die "uploaded, but the object cannot be read back"
    LOCAL_SIZE="$(wc -c < "$CIPHER" | tr -d ' ')"
    [ "$REMOTE_SIZE" = "$LOCAL_SIZE" ] \
        || die "size mismatch after upload: local ${LOCAL_SIZE}, remote ${REMOTE_SIZE}"
    log "uploaded and confirmed (${REMOTE_SIZE} bytes)"
fi

# --------------------------------------------------------------------------
# 5. 本地轮转
# --------------------------------------------------------------------------
#
# ⚠️ 只删**本地**的。R2 那一侧的保留期由桶的 lifecycle 规则管 —— 让备份脚本
# 去删远端历史，等于给一个每天自动运行的东西发了删除备份的权限。

log "pruning local copies older than ${LOCAL_KEEP_DAYS} days"
find "$BACKUP_DIR" -name 'billing-*.sql.enc' -mtime "+${LOCAL_KEEP_DAYS}" -delete 2>/dev/null || true

log "backup complete: $(basename "$CIPHER")"
