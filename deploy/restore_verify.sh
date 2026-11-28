#!/usr/bin/env bash
#
# 恢复后对账（spec §98.1；docs/runbook.md「整台 VPS 没了（灾难恢复）」第 8 步）。
#
# 在恢复主机上、`deploy/restore.sh` 把备份恢复进一个新库之后跑：用**要部署的那个镜像**对恢复库做
# 只读核对（app/services/restore_verify.py）——迁移版本、每个钱包对账本、各表行数、每份收据与
# 对账单能否渲染、每条密文能否用主密钥解开、最晚一行的时刻（实测 RPO 用）。全部通过退出 0，
# 任何一项不通过退出 1。
#
# 用法（在部署目录里，`.env` 与 `secrets/` 已经还原、mysql 已经 healthy）：
#     BILLING_IMAGE=ghcr.io/<owner>/<repo>:<SHA> deploy/restore_verify.sh <恢复库>
#
# ⚠️ `BILLING_IMAGE` 要给**灾难恢复第 4 步选定的那个 SHA** 的镜像：核对的「迁移版本等于代码 head」
# 拿的是镜像里的代码。新主机的 `.env` 里还没有部署钉进去的镜像，所以通常要在命令行上给。
#
# ⚠️ 只起一个一次性的 api 容器、不带依赖（`--no-deps`）：不会顺带起 redis、worker、beat，恢复库
# 不会开始发信、推 webhook、跑计费。它只连已经在跑的 mysql。

set -euo pipefail

DATABASE="${1:-}"
COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# ⚠️ 与 backup.sh 同一条：字面解析，**不 source**。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

[ -n "$DATABASE" ] || die "usage: BILLING_IMAGE=<image> deploy/restore_verify.sh <restored-database>"
case "$DATABASE" in
    *[!A-Za-z0-9_]*) die "database name must be [A-Za-z0-9_]" ;;
esac

IMAGE="${BILLING_IMAGE:-$(env_value BILLING_IMAGE)}"
[ -n "$IMAGE" ] || die "BILLING_IMAGE is not set: give the image of the SHA you are about to deploy"
ROOT_PW="$(env_value BILLING_MYSQL_ROOT_PASSWORD)"
[ -n "$ROOT_PW" ] || die "BILLING_MYSQL_ROOT_PASSWORD is not set in $ENV_FILE"

# ⚠️ 口令只经环境变量传：`-e` 后面只写变量名，值从本进程的环境里取，不出现在 docker 的命令行上
# （`ps` 看得到命令行）。
export BILLING_IMAGE="$IMAGE"
export BILLING_MYSQL_ROOT_PASSWORD="$ROOT_PW"

log "verifying ${DATABASE} with ${IMAGE}"
$COMPOSE run --rm --no-deps -T -e BILLING_MYSQL_ROOT_PASSWORD api \
    python -m app.cli restore-verify --database "$DATABASE"
