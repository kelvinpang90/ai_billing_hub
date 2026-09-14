#!/usr/bin/env bash
#
# binlog 离机：关闭当前 binlog → 已关闭且未推过的逐个 取出 → 加密 → **验证** → 上传
# （spec §98.1 RPO ≤ 5 分钟；T0.9）。
#
# ⚠️ **RPO ≤ 5 分钟靠的是这个脚本，不是全量备份。**全量每天一次，只靠它的话机器
# 没了就丢最多一整天的计费数据。PITR = 最近一次全量 + 它之后**每一个** binlog。
#
# 由 cron **每分钟**跑一次（deploy/cron.d/ai_billing_hub）。
#
# ⚠️ **调度保证不了 RPO，只能缩小它、并让它破掉时不可能不被发现**（Codex #42 R2）。
# 数据离机的时刻 = 下一次运行开始 + 那一次上传的耗时；上传慢到超过 5 分钟时，
# 没有哪种调度能把它拉回来。所以这里做三件事：
#   1. 每分钟一次：上一轮还没跑完时本轮跳过，代价只是一分钟，而不是一整个周期
#   2. 每一轮（**包括被跳过的那轮**）先查「binlog 上一次成功离机是多久以前」，
#      超过 RPO 预算就以 err 级别写 syslog —— 卡死、变慢、连续失败都会在一分钟内冒出来
#   3. 任何失败同样以 err 级别报出
# ⚠️ 把 err 级别的日志**送到人手里**需要告警通道（§95），这个脚本不负责那一步。
#
# 用法：
#     deploy/binlog_ship.sh
#
# 配置与 backup.sh 相同，从部署目录的 `.env` 读。

set -euo pipefail

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"
# 工作目录兼进度记录。在 .gitignore 挡住的 backups/ 下面。
STATE_DIR="${BILLING_BINLOG_STATE_DIR:-./backups/binlog}"
AWS_CLI_IMAGE="${BILLING_AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"

# RPO 预算（spec §98.1）。binlog 距上一次成功离机超过它，RPO 就已经破了。
RPO_SECONDS="${BILLING_BINLOG_RPO_SECONDS:-300}"
LAST_OK="${STATE_DIR}/last-success"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ⚠️ cron 那一行把输出整个以 info 级别送进 syslog。失败与破 RPO 必须**另外**以 err
# 级别写一条，否则它们和每分钟一次的「shipped …」混在一起，告警规则无从区分。
alarm() {
    log "ERROR: $*"
    if command -v logger >/dev/null 2>&1; then
        logger -p user.err -t billing-binlog -- "$*" || true
    fi
}
die() { alarm "$*"; exit 1; }

check_freshness() {
    [ -f "$LAST_OK" ] || return 0
    last="$(cat "$LAST_OK")"
    case "$last" in
        ""|*[!0-9]*) alarm "cannot read ${LAST_OK}; binlog freshness is unknown"; return 0 ;;
    esac
    age=$(( $(date +%s) - last ))
    [ "$age" -le "$RPO_SECONDS" ] \
        || alarm "RPO breached: binlogs last left this host ${age}s ago (budget ${RPO_SECONDS}s)"
}

# 记下「这个时刻之前写入的一切都已离机」。
# ⚠️ 传进来的是 **FLUSH 的时刻，不是本轮结束的时刻**：FLUSH 之后写入的数据要等下一轮
# 才走，用结束时刻会把暴露窗口少算一整次上传的耗时 —— 恰好是上传变慢时少算得最多。
mark_success() {
    printf '%s\n' "$1" > "${LAST_OK}.tmp" && mv "${LAST_OK}.tmp" "$LAST_OK"
}

# --------------------------------------------------------------------------
# 锁 + 新鲜度 —— 排在读配置、连数据库**之前**
# --------------------------------------------------------------------------
#
# ⚠️ 顺序是刻意的：一次卡死在数据库或上传上的运行会一直占着锁，之后每一轮都走
# 「跳过」那条路。如果新鲜度检查排在后面，那种状态下它**永远不会执行** —— 而那
# 恰恰是最需要它报警的时候。
#
# ⚠️ 用 flock 不用 mkdir 锁：进程被杀时 flock 随文件描述符自动释放；mkdir 锁会留下
# 一个永远不删的目录，此后每一轮都「跳过」。

mkdir -p "$STATE_DIR"
command -v flock >/dev/null 2>&1 || die "flock is required (util-linux)"
exec 9>"${STATE_DIR}/.lock"
if ! flock -n 9; then
    check_freshness
    log "previous run still in progress; skipping this one"
    exit 0
fi
check_freshness

# ⚠️ 与 backup.sh 同一条：字面解析，**不 source**。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

# 与 backup.sh 同一个容器化客户端，理由见那边。
aws_cli() {
    AWS_ACCESS_KEY_ID="$(env_value R2_ACCESS_KEY_ID)" \
    AWS_SECRET_ACCESS_KEY="$(env_value R2_SECRET_ACCESS_KEY)" \
    MSYS_NO_PATHCONV=1 \
    docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
        -e AWS_DEFAULT_REGION=auto \
        -v "$(cd "$STATE_DIR" && pwd):/data" \
        "$AWS_CLI_IMAGE" --endpoint-url "$R2_ENDPOINT" "$@"
}

PASSPHRASE="$(env_value BILLING_BACKUP_PASSPHRASE)"
[ -n "$PASSPHRASE" ] || die "BILLING_BACKUP_PASSPHRASE is not set in $ENV_FILE"
ROOT_PW="$(env_value BILLING_MYSQL_ROOT_PASSWORD)"
[ -n "$ROOT_PW" ] || die "BILLING_MYSQL_ROOT_PASSWORD is not set in $ENV_FILE"
R2_BUCKET="$(env_value BILLING_R2_BUCKET)"
R2_ENDPOINT="$(env_value BILLING_R2_ENDPOINT)"
# ⚠️ 与 backup.sh 同一条：没配 R2 是**显式失败**，不是静默跳过。
[ -n "$R2_BUCKET" ] && [ -n "$R2_ENDPOINT" ] \
    || die "R2 is not configured (BILLING_R2_BUCKET / BILLING_R2_ENDPOINT); binlogs are not leaving this host and RPO is NOT met"

mysql_q() {
    $COMPOSE exec -T mysql mysql -uroot -p"$ROOT_PW" -N -e "$1" 2>/dev/null | tr -d '\r'
}

PLAIN=""
CIPHER=""
VERIFY=""
# ⚠️ binlog 里是逐行的业务数据，明文与密文副本退出时一律删掉，无论成败。
trap 'rm -f "$PLAIN" "$CIPHER" "$VERIFY"' EXIT

# --------------------------------------------------------------------------
# 0. 这台 MySQL 是谁
# --------------------------------------------------------------------------
#
# ⚠️ 远端按 server_uuid 分目录。换一台主机（恢复之后正是这种情况）binlog 编号会
# 从 000001 重新开始 —— 不分目录的话，新机器的 binlog.000001 会**覆盖**旧机器的，
# 而旧的那条链可能正是下一次恢复要用的。

SERVER_UUID="$(mysql_q 'SELECT @@server_uuid')" || die "cannot reach the database"
case "$SERVER_UUID" in
    ""|*[!0-9a-f-]*) die "unexpected server_uuid: '${SERVER_UUID}'" ;;
esac
[ "$(mysql_q 'SELECT @@log_bin')" = "1" ] || die "binary logging is off; PITR is impossible"

BINLOG_DIR="$(dirname "$(mysql_q 'SELECT @@log_bin_basename')")"
MARK_DIR="${STATE_DIR}/${SERVER_UUID}"
MARK="${MARK_DIR}/last-shipped"
mkdir -p "$MARK_DIR"

# --------------------------------------------------------------------------
# 1. 关闭当前文件
# --------------------------------------------------------------------------
#
# ⚠️ **正在写的那个 binlog 不能推**：它还在增长，推上去的是半个文件。FLUSH 让 MySQL
# 换一个新文件，于是刚才那个变成「已关闭」。不 FLUSH 的话，一个 128 MB 的文件要
# 写满才会轮转 —— 写入少的时候那可能是好几天，RPO 就成了好几天。

#
# ⚠️ **上一轮成功之后没有任何写入，就不 FLUSH。**每分钟无条件 FLUSH 的话，空闲时
# 一天也要造出 1440 个空 binlog、1440 次上传。判据是「正在写的文件与位置」和上一轮
# 成功结束时记下的一样。
# ⚠️ 那个位置**只在整轮成功之后**才记：一轮 FLUSH 了、却在上传时失败，位置就不会
# 被记下，下一轮照常把积压推完 —— 否则一次失败之后的空闲会把没推出去的文件永远
# 当成「已经处理过」。

IDLE_MARK="${MARK_DIR}/idle-at"
current_position() { mysql_q 'SHOW BINARY LOG STATUS' | awk 'NR == 1 { print $1 ":" $2 }'; }

#
# ⚠️ 光比位置还不够，还要确认**最新一个已关闭的文件就是推过的最后一个**。位置记录
# 一旦和实际不符（进程在两次写之间被杀、有人为了重推删过进度、……），只比位置会把
# 积压当成空闲，直到下一次有写入才被推走 —— 演练里用一个「FLUSH 后立刻记位置」的
# 变异复现过：R2 恢复之后那一轮报「nothing to ship」，积压的文件留在本机。
CHECKED_AT="$(date +%s)"
BEFORE="$(current_position)" || die "SHOW BINARY LOG STATUS failed"
[ -n "$BEFORE" ] || die "SHOW BINARY LOG STATUS returned nothing"
NEWEST_CLOSED="$(mysql_q 'SHOW BINARY LOGS' | awk '{ print $1 }' | sed '$d' | tail -1)"
if [ "$BEFORE" = "$(cat "$IDLE_MARK" 2>/dev/null || true)" ] \
    && [ "$NEWEST_CLOSED" = "$(cat "$MARK" 2>/dev/null || true)" ]; then
    mark_success "$CHECKED_AT"
    log "no writes since the last successful run; nothing to ship"
    exit 0
fi

FLUSHED_AT="$(date +%s)"
mysql_q 'FLUSH BINARY LOGS' >/dev/null || die "FLUSH BINARY LOGS failed"
AFTER_FLUSH="$(current_position)" || die "SHOW BINARY LOG STATUS failed"

# `SHOW BINARY LOGS` 按编号升序；最后一行是正在写的，其余都已关闭。
LOGS="$(mysql_q 'SHOW BINARY LOGS' | awk '{ print $1, $2 }')"
[ -n "$LOGS" ] || die "SHOW BINARY LOGS returned nothing"
CLOSED="$(printf '%s\n' "$LOGS" | sed '$d')"

LAST="$(cat "$MARK" 2>/dev/null || true)"
LAST_NUM=""
[ -n "$LAST" ] && LAST_NUM=$((10#${LAST##*.}))

# --------------------------------------------------------------------------
# 2. 逐个推
# --------------------------------------------------------------------------

shipped=0
# ⚠️ 清单从 **fd 3** 读，不是 stdin：循环体里的 `docker compose exec` 会把 stdin
# 整个吞掉，于是每次运行只推第一个文件、其余的悄悄留到下一轮 —— 积压越来越多，
# 而每一轮都报成功。演练时实测踩过（三次运行各推一个）。
while read -r -u 3 name size; do
    [ -n "$name" ] || continue
    case "$name" in
        *[!A-Za-z0-9._-]*) die "unexpected binlog name: $name" ;;
    esac
    num=$((10#${name##*.}))

    if [ -n "$LAST_NUM" ]; then
        [ "$num" -le "$LAST_NUM" ] && continue
        # ⚠️ **断档必须是显式失败。**中间缺一个文件，PITR 就只能恢复到缺口之前 ——
        # 而从缺口往后的每一次推送看起来都照常成功。最常见的成因是这个脚本停了
        # 超过 binlog 的本地保留期（3 天），文件在推出去之前就被 MySQL 清掉了。
        # 恢复办法：立刻跑一次 deploy/backup.sh 建立新锚点，再删掉 $MARK。
        [ "$num" -eq $((LAST_NUM + 1)) ] \
            || die "gap in the binlog chain: last shipped ${LAST}, next available ${name}; PITR past ${LAST} is broken — run deploy/backup.sh, then remove ${MARK}"
    fi

    PLAIN="${STATE_DIR}/${name}"
    CIPHER="${PLAIN}.enc"
    VERIFY="${PLAIN}.verify"
    KEY="binlog/${SERVER_UUID}/${name}.enc"

    # MSYS_NO_PATHCONV：与 aws_cli 同一个 Git Bash 坑，容器内路径会被改写成 D:/Git/...
    MSYS_NO_PATHCONV=1 $COMPOSE exec -T mysql cat "${BINLOG_DIR}/${name}" > "$PLAIN" \
        || die "cannot read ${name} from the database container"

    # ⚠️ 与 MySQL 自己记的大小比对：一次半途断掉的 `exec cat` 会留下一个截断的文件，
    # 而加密往返比对抓不到这一种（明文本身就是截断的）。
    actual="$(wc -c < "$PLAIN" | tr -d ' ')"
    [ "$actual" = "$size" ] || die "${name}: copied ${actual} bytes, MySQL reports ${size}"

    printf '%s' "$PASSPHRASE" | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
        -in "$PLAIN" -out "$CIPHER" -pass stdin \
        || die "${name}: encryption failed"
    printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
        -in "$CIPHER" -out "$VERIFY" -pass stdin \
        || die "${name}: the ciphertext cannot be decrypted — NOT uploading"
    cmp -s "$PLAIN" "$VERIFY" || die "${name}: decrypted copy differs — NOT uploading"

    aws_cli s3 cp "/data/$(basename "$CIPHER")" "s3://${R2_BUCKET}/${KEY}" --only-show-errors \
        || die "${name}: upload failed"
    remote="$(aws_cli s3api head-object --bucket "$R2_BUCKET" --key "$KEY" \
        --query ContentLength --output text)" \
        || die "${name}: uploaded, but the object cannot be read back"
    local_size="$(wc -c < "$CIPHER" | tr -d ' ')"
    [ "$remote" = "$local_size" ] \
        || die "${name}: size mismatch after upload: local ${local_size}, remote ${remote}"

    # ⚠️ 进度**只在远端核对通过之后**才前进；先写临时文件再 mv，写到一半断电也
    # 不会留下一个半截的进度。进度丢了也无妨：重推同名对象是覆盖，不会出错。
    printf '%s\n' "$name" > "${MARK}.tmp" && mv "${MARK}.tmp" "$MARK"
    LAST_NUM="$num"
    LAST="$name"

    rm -f "$PLAIN" "$CIPHER" "$VERIFY"
    shipped=$((shipped + 1))
    log "shipped ${name} (${size} bytes)"
done 3<<EOF
$CLOSED
EOF

printf '%s\n' "$AFTER_FLUSH" > "${IDLE_MARK}.tmp" && mv "${IDLE_MARK}.tmp" "$IDLE_MARK"
mark_success "$FLUSHED_AT"
log "binlog shipping complete: ${shipped} file(s); last shipped ${LAST:-none}"
