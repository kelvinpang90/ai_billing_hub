#!/usr/bin/env bash
#
# 从离机备份恢复（spec §98.1；T0.9）。
#
# ⚠️ **这个脚本存在的意义是被演练，而不是等出事那天才第一次跑。**§98.1 要求季度
# 恢复演练，而 RTO ≤ 4 小时只有在步骤演练过的前提下才有意义 —— 没演练过的流程，
# 光「解密口令放在哪」「哪个桶」「导进哪个库」就能吃掉一小时。
#
# 用法：
#     deploy/restore.sh <object-key> <target-database> ['YYYY-MM-DD HH:MM:SS']
#
# 例：
#     deploy/restore.sh full/billing-20260913T020000Z.sql.enc billing_restore_drill
#     deploy/restore.sh full/billing-20260913T020000Z.sql.enc billing_incident '2026-09-13 14:05:00'
#
# 全量导入之后会重放它之后的全部 binlog（PITR）。第三个参数给出**停止时间点（UTC）**，
# 用于「恢复到误操作之前那一刻」；不给就重放到最后一个推出去的 binlog。
#
# ⚠️ **目标库必须是一个新名字**，脚本拒绝恢复进正在用的那个库（见下）。
# 演练就恢复进一个一次性的库、核对完删掉；真出事时恢复进新库、核对完再切换。

set -euo pipefail

KEY="${1:-}"
TARGET_DB="${2:-}"
STOP_AT="${3:-}"

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

if [ -n "$STOP_AT" ] && [[ ! "$STOP_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]; then
    die "stop time must be 'YYYY-MM-DD HH:MM:SS' (UTC)"
fi

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

# 与 backup.sh 同一个容器化客户端，理由见那边。
aws_cli() {
    AWS_ACCESS_KEY_ID="$(env_value R2_ACCESS_KEY_ID)" \
    AWS_SECRET_ACCESS_KEY="$(env_value R2_SECRET_ACCESS_KEY)" \
    MSYS_NO_PATHCONV=1 \
    docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION=auto \
        -v "$(cd "$WORK_DIR" && pwd):/data" \
        "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" "$@"
}

log "fetching s3://${R2_BUCKET}/${KEY}"
aws_cli s3 cp "s3://${R2_BUCKET}/${KEY}" "/data/$(basename "$KEY")" --only-show-errors \
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
# 3. 找齐 binlog 链 —— RPO ≤ 5 分钟靠它（spec §98.1）
# --------------------------------------------------------------------------
#
# ⚠️ 只导全量的话，恢复点是**全量那一刻**，最多丢一整天。PITR = 从 dump 里记下的
# 锚点位置开始，按顺序重放它之后的**每一个** binlog。
#
# 链必须从锚点那个文件开始、编号连续不断。缺一个就停：重放到缺口为止的结果
# 看起来是一个完整的库，却悄悄少了缺口之后的全部数据。
#
# ⚠️ 链的检查排在**建库之前**：第一版是导入完才查，缺口一出现就留下一个导了一半的库，
# 还得人去删。能在动任何东西之前发现的问题，就在动之前发现。

UUID="$(sed -n 's/^-- billing-server-uuid: //p' "$PLAIN" | head -1)"
case "$UUID" in
    ""|*[!0-9a-f-]*) die "the dump carries no server_uuid; cannot locate its binlog chain" ;;
esac
ANCHOR_FILE="$(printf '%s' "$ANCHOR" | sed -n "s/.*SOURCE_LOG_FILE='\([A-Za-z0-9._-]*\)'.*/\1/p")"
ANCHOR_POS="$(printf '%s' "$ANCHOR" | sed -n 's/.*SOURCE_LOG_POS=\([0-9]*\).*/\1/p')"
[ -n "$ANCHOR_FILE" ] && [ -n "$ANCHOR_POS" ] || die "the dump carries no binlog anchor; cannot do PITR"

log "listing the binlog chain for server ${UUID}"
KEYS="$(aws_cli s3api list-objects-v2 --bucket "$R2_BUCKET" --prefix "binlog/${UUID}/" \
    --query 'Contents[].Key' --output text)" || die "cannot list binlogs"

ANCHOR_NUM=$((10#${ANCHOR_FILE##*.}))
CHAIN=""
expected="$ANCHOR_NUM"
for key in $(printf '%s\n' $KEYS | grep -E '/[A-Za-z0-9_-]+\.[0-9]+\.enc$' | sort); do
    name="$(basename "$key" .enc)"
    num=$((10#${name##*.}))
    [ "$num" -lt "$ANCHOR_NUM" ] && continue
    [ "$num" -eq "$expected" ] \
        || die "gap in the binlog chain: expected #${expected}, found ${name}; refusing a silently incomplete restore"
    CHAIN="${CHAIN} ${name}"
    expected=$((num + 1))
done
[ -n "$CHAIN" ] \
    || die "the anchor binlog ${ANCHOR_FILE} has not been shipped; run deploy/binlog_ship.sh on the source host first"

# --------------------------------------------------------------------------
# 4. 导入新库
# --------------------------------------------------------------------------
#
# ⚠️ 建库与导入都 `SET sql_log_bin=0`：演练就在生产那台 MySQL 上做，不关的话整份
# 导入会写进 binlog、被 binlog_ship.sh 推走 —— 下一次恢复进同名演练库时，
# 按库名过滤的重放会把上一次演练的建库与导入**再放一遍**。变异验证过：去掉这两处，
# 第二次演练在重放时直接失败（`ERROR 1007 Can't create database ... database exists`）。

log "creating ${TARGET_DB}"
$COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" \
    -e "SET sql_log_bin=0; CREATE DATABASE \`${TARGET_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci" 2>/dev/null \
    || die "cannot create ${TARGET_DB} (does it already exist?)"

log "importing"
{ printf 'SET sql_log_bin=0;\n'; cat "$PLAIN"; } \
    | $COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" "$TARGET_DB" 2>/dev/null \
    || die "import failed; ${TARGET_DB} is partially populated — drop it before retrying"

# --------------------------------------------------------------------------
# 5. 重放 binlog
# --------------------------------------------------------------------------

REPLAY_ERR="${WORK_DIR}/replay.err"
cleanup_pitr() {
    rm -f "$PLAIN" "$REPLAY_ERR" "${WORK_DIR}"/*.binlog.plain "${WORK_DIR}"/binlog.*.enc
}
trap cleanup_pitr EXIT

FILES=()
for name in $CHAIN; do
    aws_cli s3 cp "s3://${R2_BUCKET}/binlog/${UUID}/${name}.enc" "/data/${name}.enc" --only-show-errors \
        || die "fetch failed: ${name}"
    printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
        -in "${WORK_DIR}/${name}.enc" -out "${WORK_DIR}/${name}.binlog.plain" -pass stdin \
        || die "decryption failed: ${name}"
    FILES+=("/data/${name}.binlog.plain")
done

# ⚠️ **官方 `mysql:8.4` 镜像里没有 `mysqlbinlog`**（只装了 server-minimal，它的软件源里
# 也装不上 client 包）—— 第一版就是在生产那台 MySQL 容器里调它，演练时 exit 127。
# 所以解码放在一个一次性容器里：Percona Server 与我们的服务端**同为 8.4.11**，
# binlog 格式一致。它只读挂进来的文件、输出 SQL，**不连任何数据库**；SQL 仍然交给
# 我们自己那台 mysql 执行。版本钉死，升级 mysql 时这里跟着改。
# ⚠️ 镜像约 440 MB，只在恢复时才拉。磁盘只剩 12 GB，演练完可以 `docker image rm`。
MYSQLBINLOG_IMAGE="${BILLING_MYSQLBINLOG_IMAGE:-percona/percona-server:8.4.11-11}"

# ⚠️ 三个参数缺一不可：
#   --start-position    只作用于第一个文件（锚点），跳过 dump 已经包含的那部分
#   --rewrite-db        生产库名的事件改写进目标库；配合 --database 只放行这一个库
#                       （mysqlbinlog 先改写、再按改写后的名字过滤）
#   --disable-log-bin   重放本身不写 binlog，理由与上面导入时相同
#
# ⚠️ 停止时间里有空格，所以参数用数组传，不能拼成字符串再分词。
STOP_ARGS=()
if [ -n "$STOP_AT" ]; then
    STOP_ARGS=("--stop-datetime=${STOP_AT}")
    log "replaying binlogs from ${ANCHOR_FILE}:${ANCHOR_POS} up to ${STOP_AT} UTC:${CHAIN}"
else
    log "replaying every shipped binlog from ${ANCHOR_FILE}:${ANCHOR_POS}:${CHAIN}"
fi
#
# ⚠️ 失败时把 mysql 的报错打出来（滤掉命令行口令那条固定警告）。第一版整个丢进
# /dev/null，演练失败时只剩一句「replay failed」，原因要另外手工复现才看得到 ——
# 真出事的那个晚上没有这个余裕。
# MSYS_NO_PATHCONV：与 aws_cli 同一个 Git Bash 坑（容器内路径 /data 会被改写）。
if ! MSYS_NO_PATHCONV=1 docker run --rm \
        -v "$(cd "$WORK_DIR" && pwd):/data:ro" \
        --entrypoint mysqlbinlog "$MYSQLBINLOG_IMAGE" \
        --disable-log-bin \
        --rewrite-db="${LIVE_DB}->${TARGET_DB}" --database="$TARGET_DB" \
        --start-position="$ANCHOR_POS" "${STOP_ARGS[@]}" "${FILES[@]}" \
    | $COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" 2>"$REPLAY_ERR"; then
    grep -v 'Using a password on the command line' "$REPLAY_ERR" >&2 || true
    die "binlog replay failed; ${TARGET_DB} holds the full backup plus a partial replay — drop it before retrying"
fi
log "binlog replay complete (last file:${CHAIN##* })"

# --------------------------------------------------------------------------
# 6. 核对
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
