#!/usr/bin/env bash
#
# 从离机备份恢复（spec §98.1；T0.9）。
#
# ⚠️ **这个脚本存在的意义是被演练，而不是等出事那天才第一次跑。**§98.1 要求季度
# 恢复演练，而 RTO ≤ 4 小时只有在步骤演练过的前提下才有意义 —— 没演练过的流程，
# 光「解密口令放在哪」「哪个桶」「导进哪个库」就能吃掉一小时。
#
# 用法：
#     deploy/restore.sh <object-key> <target-database>
#
# 例：
#     deploy/restore.sh full/billing-20260913T020000Z.sql.enc billing_restore_drill
#
# ⚠️ **目标库必须是一个新名字**，脚本拒绝恢复进正在用的那个库（见下）。
# 演练就恢复进一个一次性的库、核对完删掉；真出事时恢复进新库、核对完再切换。

set -euo pipefail

KEY="${1:-}"
TARGET_DB="${2:-}"

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"
WORK_DIR="${BILLING_RESTORE_DIR:-./restore}"
AWS_CLI_IMAGE="${BILLING_AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# ⚠️ 与 backup.sh 同一条：字面解析，**不 source**。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

[ -n "$KEY" ] && [ -n "$TARGET_DB" ] || die "usage: deploy/restore.sh <object-key> <target-database>"

LIVE_DB="$(env_value BILLING_MYSQL_DATABASE)"
LIVE_DB="${LIVE_DB:-billing}"

# ⚠️ **拒绝覆盖正在用的库。**恢复是「导入一份 dump」，而 dump 里是一连串
# DROP TABLE / CREATE TABLE —— 对着正在服务的库跑，等于在事故现场再制造一次
# 事故，而且会把「恢复前那一刻的数据」一起抹掉，那可能正是你要找回来的东西。
[ "$TARGET_DB" != "$LIVE_DB" ] \
    || die "refusing to restore over the live database '${LIVE_DB}'; restore into a new database and switch over after checking it"

# 库名会拼进 SQL。只允许字母数字下划线，别给注入留口子。
case "$TARGET_DB" in
    *[!A-Za-z0-9_]*) die "database name must be [A-Za-z0-9_]" ;;
esac

PASSPHRASE="$(env_value BILLING_BACKUP_PASSPHRASE)"
[ -n "$PASSPHRASE" ] || die "BILLING_BACKUP_PASSPHRASE is not set in $ENV_FILE"
ROOT_PW="$(env_value BILLING_MYSQL_ROOT_PASSWORD)"
[ -n "$ROOT_PW" ] || die "BILLING_MYSQL_ROOT_PASSWORD is not set in $ENV_FILE"
R2_BUCKET="$(env_value BILLING_R2_BUCKET)"
R2_ENDPOINT="$(env_value BILLING_R2_ENDPOINT)"
[ -n "$R2_BUCKET" ] && [ -n "$R2_ENDPOINT" ] || die "R2 is not configured in $ENV_FILE"

mkdir -p "$WORK_DIR"
CIPHER="${WORK_DIR}/$(basename "$KEY")"
PLAIN="${CIPHER%.enc}"
# ⚠️ 明文 dump 是整个库。退出时一定删掉，无论成败。
trap 'rm -f "$PLAIN"' EXIT

started="$(date +%s)"

# --------------------------------------------------------------------------
# 1. 拉回
# --------------------------------------------------------------------------

log "fetching s3://${R2_BUCKET}/${KEY}"
AWS_ACCESS_KEY_ID="$(env_value R2_ACCESS_KEY_ID)" \
AWS_SECRET_ACCESS_KEY="$(env_value R2_SECRET_ACCESS_KEY)" \
MSYS_NO_PATHCONV=1 \
docker run --rm \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION=auto \
    -v "$(cd "$WORK_DIR" && pwd):/data" \
    "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" \
    s3 cp "s3://${R2_BUCKET}/${KEY}" "/data/$(basename "$KEY")" --only-show-errors \
    || die "fetch failed"

# --------------------------------------------------------------------------
# 2. 解密
# --------------------------------------------------------------------------

log "decrypting"
printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
    -in "$CIPHER" -out "$PLAIN" -pass stdin \
    || die "decryption failed — wrong passphrase, or the object is corrupt"

grep -q "^-- Dump completed" "$PLAIN" || die "the decrypted dump is truncated"

ANCHOR="$(grep -m1 '^-- CHANGE REPLICATION SOURCE TO' "$PLAIN" || true)"
[ -n "$ANCHOR" ] && log "binlog anchor for PITR: ${ANCHOR#-- }"

# --------------------------------------------------------------------------
# 3. 导入新库
# --------------------------------------------------------------------------

log "creating ${TARGET_DB}"
$COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" \
    -e "CREATE DATABASE \`${TARGET_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci" 2>/dev/null \
    || die "cannot create ${TARGET_DB} (does it already exist?)"

log "importing"
$COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" "$TARGET_DB" < "$PLAIN" 2>/dev/null \
    || die "import failed; ${TARGET_DB} is partially populated — drop it before retrying"

# --------------------------------------------------------------------------
# 4. 核对
# --------------------------------------------------------------------------
#
# ⚠️ 「导入没报错」不等于「恢复是对的」。这里逐表给出**真实行数**，给人一个可以
# 和生产比对的数字。§98.1 要求的完整性核对（钱包、账本、用量、支付、对账单、文档）
# 要等 Phase 1–4 那些表建出来之后逐个补进来 —— 那时这一节必须跟着长。
#
# ⚠️ **不能用 `information_schema.tables.table_rows`。**它对 InnoDB 只是个估计值，
# 刚导入的表读出来是 **0**。第一版就是这么写的，演练时把一张确实有数据的 `users`
# 报成了 0 行（`SELECT COUNT(*)` 是 1）。一个会把有数据报成空的核对，比没有核对
# 更糟：真出事时看到 0 行的人会以为备份是空的，而平时大家会学会无视这组数字。
#
# 真 COUNT(*) 在大表上会慢 —— 但这是一次性的恢复核对，**准确比快重要**。

log "row counts in ${TARGET_DB} (exact COUNT(*), not the InnoDB estimate):"
TABLES="$($COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" -N -e "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = '${TARGET_DB}' AND table_type = 'BASE TABLE'
    ORDER BY table_name" 2>/dev/null | tr -d '\r')"

[ -n "$TABLES" ] || die "${TARGET_DB} has no tables after import — the dump restored nothing"

for table in $TABLES; do
    # 表名来自 information_schema，但照样只放行安全字符再拼进 SQL。
    case "$table" in *[!A-Za-z0-9_]*) die "unexpected table name: $table" ;; esac
    count="$($COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" -N -e \
        "SELECT COUNT(*) FROM \`${TARGET_DB}\`.\`${table}\`" 2>/dev/null | tr -d '\r')"
    printf '    %-28s %s\n' "$table" "$count"
done

elapsed=$(( $(date +%s) - started ))
log "restore complete in ${elapsed}s (RTO budget is 4h — spec §98.1)"
log "⚠️ ${TARGET_DB} is NOT the live database. Check it, then drop it (drill) or switch to it (incident)."
