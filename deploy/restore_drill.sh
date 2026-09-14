#!/usr/bin/env bash
#
# 每周自动恢复演练（spec §98.1；T0.9；Kelvin 2026-09-15 定每周一次）。
#
# ⚠️ **「备份在跑」和「备份能恢复」是两件事，只有第二件算数。**全量与 binlog 每天、每分钟
# 都在报成功，但证明它们能恢复的只有真的恢复一次。手工演练一季度一次，中间三个月里
# 任何一处悄悄坏掉（口令被改、binlog 链断了、镜像拉不到、密文与主密钥对不上）都要等到
# 真出事那天才发现。这个脚本每周替人做一遍，失败立刻经心跳通知。
#
# 每一轮：
#   1. 从 **R2**（不是本机的 backups/）取最新全量 —— 验证的是离机那一份
#   2. 用 deploy/restore.sh 原样恢复进专用库 billing_autodrill（全量 + binlog 重放）
#   3. 核对：最新全量不超过 26 小时；表清单与生产一致；users 非空；
#      用生产主密钥在断网容器里解开一条 TOTP 密文
#   4. 无论成败：关 binlog 删库、删工作目录、删 mysqlbinlog 镜像
#   5. 成功 ping BILLING_HEALTHCHECK_DRILL_URL，失败 ping /fail
#
# 它**不**替代季度手工演练：离线副本（密码管理器里那份主密钥）与「新主机从零搭起」
# 只有人能验（docs/deployment.md §5.2.7）。
#
# 用法：
#     deploy/restore_drill.sh
#
# 由 cron 每周日马来西亚 04:47 跑（deploy/cron.d/ai_billing_hub）。

set -euo pipefail

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"
# ⚠️ 专用库名，写死：清理时会无条件 DROP 它，绝不能让配置把它指到别的库上。
DRILL_DB="billing_autodrill"
# 在 .gitignore 挡住的 /restore/ 下面，与手工恢复的 ./restore 分开，互不踩文件。
WORK_DIR="./restore/autodrill"
MASTER_KEY_FILE="${BILLING_MASTER_KEY_HOST_FILE:-./secrets/master.key}"
AWS_CLI_IMAGE="${BILLING_AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"
# 与 restore.sh 同一个默认值；清理时删掉它，理由见 cleanup。
MYSQLBINLOG_IMAGE="${BILLING_MYSQLBINLOG_IMAGE:-percona/percona-server:8.4.11-11}"
# 全量每天一次；超过 26 小时说明最近一次没跑或没传上去。
MAX_BACKUP_AGE_SECONDS=$((26 * 3600))

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ⚠️ 与 backup.sh 同一条：字面解析，**不 source**。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

# 心跳（docs/deployment.md §5.2.6）。第三个检查，与全量、binlog 分开。
HEARTBEAT_URL="$(env_value BILLING_HEALTHCHECK_DRILL_URL)"
heartbeat() {
    local suffix="$1" body="${2:-}"
    [ -n "$HEARTBEAT_URL" ] || return 0
    curl -fsS -m 10 --retry 2 -o /dev/null --data-raw "$body" "${HEARTBEAT_URL}${suffix}" \
        || log "heartbeat ping failed (${suffix:-success}); the monitor will treat this run as missing"
}

die() {
    log "ERROR: $*"
    [ -n "$HEARTBEAT_URL" ] || log "no BILLING_HEALTHCHECK_DRILL_URL configured: nobody will be told about this"
    heartbeat /fail "restore drill failed: $*"
    exit 1
}

# --------------------------------------------------------------------------
# 0. 准备
# --------------------------------------------------------------------------

command -v flock >/dev/null 2>&1 || die "flock is required (util-linux)"
mkdir -p "$(dirname "$WORK_DIR")"
exec 9>"$(dirname "$WORK_DIR")/.autodrill.lock"
flock -n 9 || die "a previous restore drill is still running"

ROOT_PW="$(env_value BILLING_MYSQL_ROOT_PASSWORD)"
[ -n "$ROOT_PW" ] || die "BILLING_MYSQL_ROOT_PASSWORD is not set in $ENV_FILE"
LIVE_DB="$(env_value BILLING_MYSQL_DATABASE)"
LIVE_DB="${LIVE_DB:-billing}"
# restore.sh 自己也拒绝，这里再挡一次：下面的清理会 DROP 演练库。
[ "$DRILL_DB" != "$LIVE_DB" ] || die "the drill database name equals the live database '${LIVE_DB}'"
R2_BUCKET="$(env_value BILLING_R2_BUCKET)"
R2_ENDPOINT="$(env_value BILLING_R2_ENDPOINT)"
[ -n "$R2_BUCKET" ] && [ -n "$R2_ENDPOINT" ] || die "R2 is not configured in $ENV_FILE"

mysql_q() {
    $COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" -N -e "$1" 2>/dev/null | tr -d '\r'
}

# ⚠️ DROP 必须关 binlog：否则这条语句进 binlog、被推到 R2，下次演练重放到它时
# 会把正在恢复的同名演练库删掉（2026-09-14 手工演练后记下的教训）。
drop_drill_db() {
    $COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" \
        -e "SET sql_log_bin=0; DROP DATABASE IF EXISTS \`${DRILL_DB}\`" 2>/dev/null
}

# ⚠️ 无论成败都收拾干净：演练库是整库副本，工作目录里有整库密文，mysqlbinlog 镜像
# 约 440 MB（磁盘只剩 12 GB）。每周重新拉一次镜像，本身也验证了真出事时拉得到。
cleanup() {
    drop_drill_db || log "WARNING: could not drop ${DRILL_DB}; drop it by hand (with SET sql_log_bin=0)"
    rm -rf "$WORK_DIR"
    docker image rm "$MYSQLBINLOG_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 上一轮若在恢复中途被杀，演练库会留下；restore.sh 遇到已存在的库会拒绝。
drop_drill_db || die "cannot drop a leftover ${DRILL_DB}"

aws_cli() {
    AWS_ACCESS_KEY_ID="$(env_value R2_ACCESS_KEY_ID)" \
    AWS_SECRET_ACCESS_KEY="$(env_value R2_SECRET_ACCESS_KEY)" \
    MSYS_NO_PATHCONV=1 \
    docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
        -e AWS_DEFAULT_REGION=auto \
        "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" "$@"
}

# --------------------------------------------------------------------------
# 1. 取 R2 上最新的全量
# --------------------------------------------------------------------------

KEYS="$(aws_cli s3api list-objects-v2 --bucket "$R2_BUCKET" --prefix "full/" \
    --query 'Contents[].Key' --output text)" || die "cannot list full backups in R2"
KEY="$(printf '%s\n' $KEYS | grep -E '^full/billing-[0-9]{8}T[0-9]{6}Z\.sql\.enc$' | sort | tail -1 || true)"
[ -n "$KEY" ] || die "no full backup found in R2"

STAMP="${KEY#full/billing-}"
STAMP="${STAMP%.sql.enc}"
TAKEN_AT="$(date -u -d "${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2} ${STAMP:9:2}:${STAMP:11:2}:${STAMP:13:2}" +%s)" \
    || die "cannot parse the time of ${KEY}"
AGE=$(( $(date +%s) - TAKEN_AT ))
[ "$AGE" -le "$MAX_BACKUP_AGE_SECONDS" ] \
    || die "the newest full backup ${KEY} is $((AGE / 3600))h old; the daily backup is not reaching R2"

# --------------------------------------------------------------------------
# 2. 恢复 —— 用的就是出事时会用的那个脚本
# --------------------------------------------------------------------------

log "restoring ${KEY} into ${DRILL_DB}"
started="$(date +%s)"
BILLING_RESTORE_DIR="$WORK_DIR" bash deploy/restore.sh "$KEY" "$DRILL_DB" \
    || die "deploy/restore.sh failed for ${KEY}"
elapsed=$(( $(date +%s) - started ))

# --------------------------------------------------------------------------
# 3. 核对
# --------------------------------------------------------------------------
#
# ⚠️ 不和生产逐表比行数 / 校验和：演练期间生产照常在写，比不出稳定的结论，而一个
# 每周误报的检查很快会被所有人无视。这里只查「恢复出来的东西能用」的硬条件。

tables_of() {
    mysql_q "SELECT table_name FROM information_schema.tables
             WHERE table_schema = '$1' AND table_type = 'BASE TABLE' ORDER BY table_name"
}
LIVE_TABLES="$(tables_of "$LIVE_DB")"
DRILL_TABLES="$(tables_of "$DRILL_DB")"
[ -n "$DRILL_TABLES" ] || die "${DRILL_DB} has no tables after restoring ${KEY}"
[ "$LIVE_TABLES" = "$DRILL_TABLES" ] \
    || die "the restored table set differs from ${LIVE_DB}: live [$(echo $LIVE_TABLES)] restored [$(echo $DRILL_TABLES)]"

USERS="$(mysql_q "SELECT COUNT(*) FROM \`${DRILL_DB}\`.users")"
case "$USERS" in ""|*[!0-9]*) die "cannot count users in ${DRILL_DB}" ;; esac
[ "$USERS" -gt 0 ] || die "${DRILL_DB}.users is empty after restoring ${KEY}"

# ⚠️ 主密钥与密文对得上，才说明恢复出来的库**能用**：密钥被换过、密文损坏时，库照样
# 导得进来，但所有集成凭据与 TOTP 注册全部作废（ADR-0004）。
# 容器断网、以应用用户 10001 运行、密钥只读挂入；**只打印 decrypted**，不打印任何码或密钥。
LIVE_2FA="$(mysql_q "SELECT COUNT(*) FROM \`${LIVE_DB}\`.two_factor_settings WHERE confirmed_at IS NOT NULL")"
case "$LIVE_2FA" in ""|*[!0-9]*) die "cannot count confirmed 2FA rows in ${LIVE_DB}" ;; esac
if [ "$LIVE_2FA" -gt 0 ]; then
    TOKEN="$(mysql_q "SELECT encrypted_totp_secret FROM \`${DRILL_DB}\`.two_factor_settings
                      WHERE confirmed_at IS NOT NULL LIMIT 1")"
    [ -n "$TOKEN" ] || die "${LIVE_DB} has confirmed 2FA rows but the restored copy has none"
    [ -f "$MASTER_KEY_FILE" ] || die "the master key file ${MASTER_KEY_FILE} does not exist"
    APP_IMAGE="${BILLING_APP_IMAGE:-}"
    if [ -z "$APP_IMAGE" ]; then
        API_CONTAINER="$($COMPOSE ps -q api 2>/dev/null || true)"
        [ -n "$API_CONTAINER" ] || die "the api container is not running; cannot tell which image to decrypt with"
        APP_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$API_CONTAINER")"
    fi
    KEY_ABS="$(cd "$(dirname "$MASTER_KEY_FILE")" && pwd)/$(basename "$MASTER_KEY_FILE")"
    export TOKEN
    RESULT="$(MSYS_NO_PATHCONV=1 docker run --rm --network none --user 10001:10001 \
        -e TOKEN -v "${KEY_ABS}:/drill/master.key:ro" \
        --entrypoint python "$APP_IMAGE" -c '
import os, types
from app.core.crypto import decrypt_secret, load_keyring
ring = load_keyring(types.SimpleNamespace(master_key_file="/drill/master.key"))
decrypt_secret(ring, os.environ["TOKEN"])
print("decrypted")
' 2>&1 | tail -1)" || true
    unset TOKEN
    [ "$RESULT" = "decrypted" ] \
        || die "the production master key cannot open a restored TOTP secret (${RESULT})"
    DECRYPT_NOTE="TOTP secret decrypted"
else
    DECRYPT_NOTE="no confirmed 2FA rows in ${LIVE_DB}; decryption not checked"
    log "⚠️ ${DECRYPT_NOTE}"
fi

SUMMARY="restored ${KEY} ($((AGE / 3600))h old) in ${elapsed}s; $(echo "$DRILL_TABLES" | wc -l | tr -d ' ') tables; users ${USERS}; ${DECRYPT_NOTE}"
log "restore drill passed: ${SUMMARY} (RTO budget 4h)"
heartbeat "" "$SUMMARY"
