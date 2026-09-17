#!/usr/bin/env bash
#
# 部署配置快照：把「在一台新主机上重建这个栈还需要知道什么」打包离机
# （spec §98.1 备份四层里的第三层「部署配置」；T0.9）。
#
# ⚠️ **这一层不是为了备份 compose / nginx 的文件内容** —— 那些在 git 里，
# 每个 clone 都有一份，再往 R2 抄一遍是做样子。真正**不在 git 里、又在灾难恢复时
# 必须知道**的是这些：
#
#   · 当时部署的是哪个 commit（`.last-good-deploy`）—— 它只在那台主机上
#   · `.env` 里**有哪些键**（值在 Bitwarden）—— 少填一个，栈起得来但行为不对
#   · cron 装成了什么样（几行、几点）
#   · 当时真正在跑的镜像摘要
#   · 那份 nginx 配置的校验和 —— 用来确认「git 里那一版就是线上那一版」
#
# ⚠️ **绝不包含任何密钥的值。**compose 用 `--no-interpolate` 导出，占位符保持
# `${VAR}` 原样（实测：把口令塞进环境变量再导出，值不会出现）；`.env` 只取键名。
# 快照本身仍然加密之后才上传 —— 键名与拓扑也算内部信息。
#
# 由 cron 每天跑一次（deploy/cron.d/ai_billing_hub），排在日志外送之后。
#
# 用法：
#     deploy/config_snapshot.sh            # 生成 + 上传
#     deploy/config_snapshot.sh --dry-run  # 生成 + 验证，但不上传（本地演练用）
#
# 配置与 backup.sh 同一套（R2 凭据、`BILLING_BACKUP_PASSPHRASE`）；
# 心跳用 `BILLING_HEALTHCHECK_CONFIG_URL`（docs/deployment.md §5.2.6）。

set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"
WORK_DIR="${BILLING_CONFIG_SNAPSHOT_DIR:-./backups}"
AWS_CLI_IMAGE="${BILLING_AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"
DEPLOY_STATE="${BILLING_DEPLOY_STATE_FILE:-./.last-good-deploy}"
CRON_FILE="${BILLING_CRON_FILE:-/etc/cron.d/ai_billing_hub}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ⚠️ 与 backup.sh 同一条：字面解析 `.env`，**不 source**。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

heartbeat() {
    local suffix="$1" body="${2:-}" url
    url="$(env_value BILLING_HEALTHCHECK_CONFIG_URL)"
    [ -n "$url" ] || return 0
    curl -fsS -m 10 --retry 2 -o /dev/null --data-raw "$body" "${url}${suffix}" \
        || log "heartbeat ping failed (${suffix:-success}); the monitor will treat this run as missing"
}

die() {
    log "ERROR: $*"
    if command -v logger >/dev/null 2>&1; then
        logger -p user.err -t billing-config -- "$*" || true
    fi
    if [ "$DRY_RUN" != "1" ]; then
        [ -n "$(env_value BILLING_HEALTHCHECK_CONFIG_URL)" ] \
            || log "no BILLING_HEALTHCHECK_CONFIG_URL configured: nobody will be told about this"
        heartbeat /fail "$*"
    fi
    exit 1
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
        -v "$(cd "$WORK_DIR" && pwd):/data" \
        "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" "$@"
}
s3() { aws_cli s3 "$@" --only-show-errors; }
s3api() { aws_cli s3api "$@"; }

# --------------------------------------------------------------------------
# 0. 准备
# --------------------------------------------------------------------------

mkdir -p "$WORK_DIR"

PASSPHRASE="$(env_value BILLING_BACKUP_PASSPHRASE)"
[ -n "$PASSPHRASE" ] || die "BILLING_BACKUP_PASSPHRASE is not set in $ENV_FILE"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PLAIN="${WORK_DIR}/billing-config-${STAMP}.txt"
CIPHER="${PLAIN}.enc"
VERIFY=""

# ⚠️ 明文快照里有键名与拓扑，用完就删 —— 成败都删。
cleanup() {
    [ -n "$VERIFY" ] && rm -f "$VERIFY"
    rm -f "$PLAIN"
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# 1. 采集
# --------------------------------------------------------------------------

log "collecting the deployment snapshot"
{
    printf '# ai_billing_hub deployment snapshot\n'
    printf 'generated_at: %s\n' "$STAMP"
    printf '\n## deployed commit (from %s)\n' "$DEPLOY_STATE"
    cat "$DEPLOY_STATE" 2>/dev/null || printf '(no record on this host)\n'

    printf '\n## .env keys (names only, values live in Bitwarden)\n'
    # ⚠️ **只取键名**：`cut -d= -f1` 之后不可能带出值。注释行与空行一并去掉。
    sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$ENV_FILE" 2>/dev/null | sort \
        || printf '(no .env on this host)\n'

    printf '\n## cron (%s)\n' "$CRON_FILE"
    cat "$CRON_FILE" 2>/dev/null || printf '(not installed / not readable)\n'

    printf '\n## nginx config checksums (proves which revision is live)\n'
    sha256sum deploy/nginx/* 2>/dev/null || printf '(not found)\n'

    printf '\n## images actually running\n'
    $COMPOSE images 2>/dev/null || printf '(compose not available)\n'

    printf '\n## compose config (NO interpolation: values stay as ${VAR} placeholders)\n'
    $COMPOSE config --no-interpolate 2>/dev/null || printf '(compose config failed)\n'
} > "$PLAIN"

[ -s "$PLAIN" ] || die "the snapshot is empty"

# ⚠️ **上传之前先自查一遍**：万一哪天有人把 `--no-interpolate` 去掉，或者往 `.env`
# 的键名里塞了值，这里要当场拦住 —— 一份带口令的快照传上去就收不回来了。
LEAKED=0
for key in BILLING_MYSQL_ROOT_PASSWORD BILLING_MYSQL_PASSWORD BILLING_BACKUP_PASSPHRASE \
           R2_SECRET_ACCESS_KEY R2_ACCESS_KEY_ID BILLING_SMTP_PASSWORD; do
    value="$(env_value "$key")"
    [ -n "$value" ] || continue
    if grep -qF -- "$value" "$PLAIN"; then
        log "ERROR: the snapshot contains the value of ${key}"
        LEAKED=1
    fi
done
[ "$LEAKED" = "0" ] || die "refusing to upload a snapshot that contains secret values"

# 心跳地址本身也是凭据（知道它的人能伪造「成功」），同样不许出现。
for key in BILLING_HEALTHCHECK_BINLOG_URL BILLING_HEALTHCHECK_BACKUP_URL \
           BILLING_HEALTHCHECK_DRILL_URL BILLING_HEALTHCHECK_SERVICES_URL \
           BILLING_HEALTHCHECK_READYZ_URL BILLING_HEALTHCHECK_DISK_URL \
           BILLING_HEALTHCHECK_LOGS_URL BILLING_HEALTHCHECK_CONFIG_URL; do
    value="$(env_value "$key")"
    [ -n "$value" ] || continue
    grep -qF -- "$value" "$PLAIN" && die "refusing to upload a snapshot that contains ${key}"
done

log "snapshot ok ($(wc -c < "$PLAIN") bytes)"

# --------------------------------------------------------------------------
# 2. 加密 + 验证
# --------------------------------------------------------------------------

printf '%s' "$PASSPHRASE" | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -in "$PLAIN" -out "$CIPHER" -pass stdin \
    || die "encryption failed"

VERIFY="$(mktemp)"
printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
    -in "$CIPHER" -out "$VERIFY" -pass stdin \
    || die "the encrypted snapshot cannot be decrypted — NOT uploading"
cmp -s "$PLAIN" "$VERIFY" || die "the decrypted snapshot differs from the source — NOT uploading"

# --------------------------------------------------------------------------
# 3. 上传
# --------------------------------------------------------------------------

R2_BUCKET="$(env_value BILLING_R2_BUCKET)"
R2_ENDPOINT="$(env_value BILLING_R2_ENDPOINT)"
KEY="config/$(basename "$CIPHER")"

if [ "$DRY_RUN" = "1" ]; then
    log "--dry-run: skipping the upload"
elif [ -z "$R2_BUCKET" ] || [ -z "$R2_ENDPOINT" ]; then
    die "R2 is not configured (BILLING_R2_BUCKET / BILLING_R2_ENDPOINT); the snapshot would stay on this host only, which does NOT satisfy spec §98.1"
else
    s3 cp "/data/$(basename "$CIPHER")" "s3://${R2_BUCKET}/${KEY}" \
        || die "upload failed; the snapshot is still at $CIPHER"
    REMOTE_SIZE="$(s3api head-object --bucket "$R2_BUCKET" --key "$KEY" \
        --query ContentLength --output text)" \
        || die "uploaded, but the object cannot be read back"
    LOCAL_SIZE="$(wc -c < "$CIPHER" | tr -d ' ')"
    [ "$REMOTE_SIZE" = "$LOCAL_SIZE" ] \
        || die "size mismatch after upload: local ${LOCAL_SIZE}, remote ${REMOTE_SIZE}"
    log "uploaded and confirmed (${REMOTE_SIZE} bytes)"
fi

# 本地只留最近几份：它很小，但没有理由在这台机器上堆着。
find "$WORK_DIR" -maxdepth 1 -name 'billing-config-*.txt.enc' -mtime +7 -delete 2>/dev/null || true

SUMMARY="config snapshot $(basename "$CIPHER")"
log "$SUMMARY"
[ "$DRY_RUN" = "1" ] || heartbeat "" "$SUMMARY"
