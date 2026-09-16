#!/usr/bin/env bash
#
# 日志外送：抓取 → 打包 → 加密 → **验证** → 上传 R2 → 本地保留期与总量上限
# （spec §94 的保留期 / 磁盘上限 / 安全删除 / 异地留存，§112 的安全删除；T0.9）。
#
# ⚠️ **为什么必须外送。**容器日志走 json-file 驱动，它**按大小轮转，不按时间**：
# 每容器 3 × 10 MB，写得快的那天可能只剩几个小时的历史。事故调查要看的恰恰是
# 「出事前那几天」，而那时候日志早被自己顶掉了。所以「保留 30 天」这件事在本机
# 表达不了 —— 它是**离机那一侧**的属性，由这个脚本产出的归档承载。
#
# ⚠️ **归档的完整性上限就是那个大小窗口。**某个服务在一天之内写爆 30 MB 时，
# 这次抓到的就是被截断的一段。频率定成每天而不是每周，就是为了压小这个风险；
# 真要根治得换日志驱动或上日志聚合，那不在 T0.9 的范围里（见 docs/deployment.md §8.3）。
#
# 由 cron 每天跑一次（deploy/cron.d/ai_billing_hub），排在全量备份之后。
#
# 用法：
#     deploy/log_ship.sh            # 抓取 + 上传 + 清理
#     deploy/log_ship.sh --dry-run  # 抓取 + 加密 + 验证，但不上传（本地演练用）
#
# 配置从部署目录的 `.env` 读（与 backup.sh 同一套 R2 凭据与加密口令）：
#     BILLING_R2_BUCKET / BILLING_R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
#     BILLING_BACKUP_PASSPHRASE
#     BILLING_HEALTHCHECK_LOGS_URL     心跳地址（docs/deployment.md §5.2.6），**独立的检查**

set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"
ARCHIVE_DIR="${BILLING_LOG_ARCHIVE_DIR:-./logs}"
AWS_CLI_IMAGE="${BILLING_AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"

# 保留期（决策 ⑤，2026-09-13 定）。⚠️ 这个数字管的是**本机归档**：R2 那一侧由
# 桶上的 lifecycle 规则删（docs/deployment.md §5.2.1 的表）。两边都要有，因为
# 「只留在本机」不满足异地留存，「只留在 R2」则让本机排查每次都要先下载。
RETENTION_DAYS="${BILLING_LOG_RETENTION_DAYS:-30}"
# 主机层面的总量口径（§94 要的那条）。⚠️ 保留期与总量上限**两个都要**：
# 保留期管「多久」，上限管「最坏能占多少」—— 日志量突然放大时，只有后者拦得住。
ARCHIVE_CAP_MB="${BILLING_LOG_ARCHIVE_CAP_MB:-512}"
# 一次最多回看多久。⚠️ 状态文件丢了的时候，没有这个上限就会一口气去抓几个月，
# 把一次例行外送变成一次事故。
MAX_WINDOW_HOURS="${BILLING_LOG_MAX_WINDOW_HOURS:-168}"
DEFAULT_WINDOW_HOURS="${BILLING_LOG_WINDOW_HOURS:-24}"

SERVICES="${BILLING_LOG_SERVICES:-mysql redis api celery-worker celery-beat frontend billing_nginx}"
STATE_FILE="${ARCHIVE_DIR}/.last-success"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ⚠️ 与 backup.sh 同一条：字面解析 `.env`，**不 source** —— 那等于执行一份凭据清单。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

heartbeat() {
    local suffix="$1" body="${2:-}" url
    url="$(env_value BILLING_HEALTHCHECK_LOGS_URL)"
    [ -n "$url" ] || return 0
    curl -fsS -m 10 --retry 2 -o /dev/null --data-raw "$body" "${url}${suffix}" \
        || log "heartbeat ping failed (${suffix:-success}); the monitor will treat this run as missing"
}

die() {
    log "ERROR: $*"
    if command -v logger >/dev/null 2>&1; then
        logger -p user.err -t billing-logs -- "$*" || true
    fi
    if [ "$DRY_RUN" != "1" ]; then
        [ -n "$(env_value BILLING_HEALTHCHECK_LOGS_URL)" ] \
            || log "no BILLING_HEALTHCHECK_LOGS_URL configured: nobody will be told about this"
        heartbeat /fail "$*"
    fi
    exit 1
}

# ⚠️ **安全删除（§112）。**日志里有脱敏之后仍属敏感的东西（租户名、请求路径、
# 金额量级）。`rm` 只是摘掉目录项，数据块还躺在盘上。
#
# ⚠️ 诚实地说明它的边界：`shred` 在**日志式文件系统、SSD 的磨损均衡、快照或
# 写时复制**之下都不保证覆盖到原来的物理块。这里能做到的是「尽力而为 + 归档
# 本身是加密的」—— 后者才是真正兜底的那一层。
secure_rm() {
    local path
    for path in "$@"; do
        [ -e "$path" ] || continue
        if command -v shred >/dev/null 2>&1; then
            shred -u -n 1 "$path" 2>/dev/null || rm -f "$path"
        else
            rm -f "$path"
        fi
    done
}

aws_cli() {
    # ⚠️ 凭据用不带值的 `-e NAME`：带值会让口令出现在 `ps` 的命令行里。
    # ⚠️ 这段注释必须在命令外面 —— 夹在续行中间会把续行截断（backup.sh 踩过）。
    AWS_ACCESS_KEY_ID="$(env_value R2_ACCESS_KEY_ID)" \
    AWS_SECRET_ACCESS_KEY="$(env_value R2_SECRET_ACCESS_KEY)" \
    MSYS_NO_PATHCONV=1 \
    docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
        -e AWS_DEFAULT_REGION=auto \
        -v "$(cd "$ARCHIVE_DIR" && pwd):/data" \
        "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" "$@"
}
s3() { aws_cli s3 "$@" --only-show-errors; }
s3api() { aws_cli s3api "$@"; }

# --------------------------------------------------------------------------
# 0. 准备
# --------------------------------------------------------------------------

mkdir -p "$ARCHIVE_DIR"

PASSPHRASE="$(env_value BILLING_BACKUP_PASSPHRASE)"
[ -n "$PASSPHRASE" ] || die "BILLING_BACKUP_PASSPHRASE is not set in $ENV_FILE"

NOW="$(date +%s)"
STAMP="$(date -u -d "@${NOW}" +%Y%m%dT%H%M%SZ)"

# 窗口：从上一次成功那一刻起。⚠️ 宁可重叠也不要留缝 —— 重复的日志行在事故调查里
# 无害，而缺掉的那一段没有第二个地方能补。
SINCE_EPOCH=""
if [ -f "$STATE_FILE" ]; then
    SINCE_EPOCH="$(cat "$STATE_FILE" 2>/dev/null | tr -cd '0-9')"
fi
[ -n "$SINCE_EPOCH" ] || SINCE_EPOCH=$((NOW - DEFAULT_WINDOW_HOURS * 3600))
OLDEST=$((NOW - MAX_WINDOW_HOURS * 3600))
if [ "$SINCE_EPOCH" -lt "$OLDEST" ]; then
    log "the last success is older than ${MAX_WINDOW_HOURS}h; clamping the window (older lines may already be gone)"
    SINCE_EPOCH="$OLDEST"
fi
SINCE="$(date -u -d "@${SINCE_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)"

WORK="$(mktemp -d)"
TAR="${ARCHIVE_DIR}/billing-logs-${STAMP}.tar.gz"
CIPHER="${TAR}.enc"
VERIFY=""

# ⚠️ 明文日志落盘的那一段是这个脚本最脆弱的窗口 —— 无论成败都要安全删掉。
cleanup() {
    [ -n "$VERIFY" ] && secure_rm "$VERIFY"
    secure_rm "$TAR"
    [ -d "$WORK" ] && { find "$WORK" -type f -exec shred -u -n 1 {} \; 2>/dev/null || true; rm -rf "$WORK"; }
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# 1. 抓取
# --------------------------------------------------------------------------
#
# ⚠️ `--timestamps` 不是可选的：compose 默认不打印时间戳，而**没有时间戳的日志
# 在事故调查里几乎没有用**（对不上时间线、拼不起多个服务）。
# ⚠️ 逐服务抓、逐服务落一个文件：混在一起的话，某个服务刷屏会把别人的淹掉。

log "collecting logs since ${SINCE}"
TOTAL_LINES=0
for svc in $SERVICES; do
    out="${WORK}/${svc}.log"
    $COMPOSE logs --no-color --timestamps --since "$SINCE" "$svc" > "$out" 2>/dev/null \
        || die "cannot read logs for ${svc}"
    lines="$(wc -l < "$out" | tr -d ' ')"
    TOTAL_LINES=$((TOTAL_LINES + lines))
    log "  ${svc}: ${lines} lines"
done

# ⚠️ 全部为空是可疑的，但**不是失败**：一台没人访问的机器就是这样。
# 报出来即可 —— 让它失败会让「安静的周末」变成每周一次的假警报。
[ "$TOTAL_LINES" -gt 0 ] || log "WARNING: every service returned zero lines for this window"

# --------------------------------------------------------------------------
# 2. 打包 + 加密
# --------------------------------------------------------------------------

log "packing"
tar -czf "$TAR" -C "$WORK" . || die "packing failed"

log "encrypting"
printf '%s' "$PASSPHRASE" | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -in "$TAR" -out "$CIPHER" -pass stdin \
    || die "encryption failed"

# --------------------------------------------------------------------------
# 3. ⚠️ 验证 —— 与 backup.sh 同一条理由
# --------------------------------------------------------------------------
#
# 一个配错的口令产出的文件，和好的那个长得一模一样：大小合理、静静躺在桶里，
# 直到需要它的那天才发现打不开。而需要日志的那天，通常没有第二份。

log "verifying the ciphertext round-trips"
VERIFY="$(mktemp)"
printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
    -in "$CIPHER" -out "$VERIFY" -pass stdin \
    || die "the encrypted archive cannot be decrypted — NOT uploading"
cmp -s "$TAR" "$VERIFY" || die "the decrypted archive differs from the source — NOT uploading"
log "verified"

# --------------------------------------------------------------------------
# 4. 上传
# --------------------------------------------------------------------------

R2_BUCKET="$(env_value BILLING_R2_BUCKET)"
R2_ENDPOINT="$(env_value BILLING_R2_ENDPOINT)"
KEY="logs/$(basename "$CIPHER")"

if [ "$DRY_RUN" = "1" ]; then
    log "--dry-run: skipping the upload"
elif [ -z "$R2_BUCKET" ] || [ -z "$R2_ENDPOINT" ]; then
    # ⚠️ 与备份同一条：没配 R2 不是「静默跳过」。只留在本机的日志**不满足 §94 的
    # 异地留存**，而机器没了的时候，恰恰是最需要那份日志的时候。
    die "R2 is not configured (BILLING_R2_BUCKET / BILLING_R2_ENDPOINT); logs would stay on this host only, which does NOT satisfy spec §94"
else
    log "uploading ${KEY}"
    s3 cp "/data/$(basename "$CIPHER")" "s3://${R2_BUCKET}/${KEY}" \
        || die "upload failed; the verified archive is still at $CIPHER"

    # ⚠️ 传完拉一次元数据回来核对大小：`cp` 返回 0 只说明客户端认为它发完了。
    REMOTE_SIZE="$(s3api head-object --bucket "$R2_BUCKET" --key "$KEY" \
        --query ContentLength --output text)" \
        || die "uploaded, but the object cannot be read back"
    LOCAL_SIZE="$(wc -c < "$CIPHER" | tr -d ' ')"
    [ "$REMOTE_SIZE" = "$LOCAL_SIZE" ] \
        || die "size mismatch after upload: local ${LOCAL_SIZE}, remote ${REMOTE_SIZE}"
    log "uploaded and confirmed (${REMOTE_SIZE} bytes)"

    # ⚠️ **只有上传确认之后才推进状态**。提前推进的话，一次失败的外送会让那段
    # 窗口再也不会被抓第二次 —— 而它多半正是出问题的那一段。
    printf '%s\n' "$NOW" > "$STATE_FILE"
fi

# --------------------------------------------------------------------------
# 5. 本地保留期与总量上限
# --------------------------------------------------------------------------
#
# ⚠️ 顺序是**先按保留期删，再按总量删**：反过来的话，一次日志暴涨会把还在保留期
# 内的旧归档挤掉，而保留期是对外承诺的那一个。

log "enforcing retention: ${RETENTION_DAYS} days, cap ${ARCHIVE_CAP_MB} MB"
while IFS= read -r old; do
    [ -n "$old" ] || continue
    log "  expired: $(basename "$old")"
    secure_rm "$old"
done <<EOF
$(find "$ARCHIVE_DIR" -maxdepth 1 -name 'billing-logs-*.tar.gz.enc' -mtime +"$RETENTION_DAYS" 2>/dev/null)
EOF

used_mb() {
    local total
    total="$(find "$ARCHIVE_DIR" -maxdepth 1 -name 'billing-logs-*.tar.gz.enc' -printf '%s\n' 2>/dev/null \
        | awk '{ sum += $1 } END { printf "%d", sum / 1048576 }')"
    printf '%s' "${total:-0}"
}

USED="$(used_mb)"
if [ "$USED" -gt "$ARCHIVE_CAP_MB" ]; then
    # ⚠️ 撞上限是**异常**，不是日常维护：要么日志量变了，要么外送停了。
    # 以 err 级别单独写一条 —— 巡检的磁盘告警只看百分比，看不见这一层。
    log "ERROR: the archive directory is over its cap (${USED} MB > ${ARCHIVE_CAP_MB} MB); deleting oldest first"
    command -v logger >/dev/null 2>&1 \
        && logger -p user.err -t billing-logs -- "log archive over cap: ${USED}MB > ${ARCHIVE_CAP_MB}MB" || true
    # ⚠️ **绝不删本轮刚传上去的那一个**：它是唯一一份还没被任何人看过的。
    while IFS= read -r old; do
        [ -n "$old" ] || continue
        [ "$USED" -gt "$ARCHIVE_CAP_MB" ] || break
        [ "$old" = "$CIPHER" ] && continue
        log "  over cap: $(basename "$old")"
        secure_rm "$old"
        USED="$(used_mb)"
    done <<EOF
$(find "$ARCHIVE_DIR" -maxdepth 1 -name 'billing-logs-*.tar.gz.enc' -printf '%T@ %p\n' 2>/dev/null | sort -n | cut -d' ' -f2-)
EOF
fi

SUMMARY="shipped ${TOTAL_LINES} lines since ${SINCE}; archive dir $(used_mb) MB"
log "$SUMMARY"
[ "$DRY_RUN" = "1" ] || heartbeat "" "$SUMMARY"
