#!/usr/bin/env bash
#
# 把一个指定 commit 的镜像部署到生产栈（spec §99、§98.1；T0.9）。
#
# ⚠️ **逻辑放在这个脚本里、不放在 workflow 的 YAML 里**，理由只有一个：
# YAML 里的部署步骤**没有任何办法在生产之外跑一遍**，而这段逻辑恰恰是最不该
# 第一次就在生产上验的东西（迁移、健康等待、回滚）。脚本可以在本地栈上整套跑通。
#
# 用法：
#     deploy/deploy.sh <image-tag>
#
# `<image-tag>` 必须是不可变标签（commit SHA）。⚠️ 拒绝 `latest` —— §99 要求
# 「immutable image tags identify the exact commit」，而用 latest 的话「线上跑的是
# 哪个 commit」没有答案，回滚也无从谈起（回到哪一个 latest？）。
#
# 需要的环境变量（生产上由宿主机的 .env 提供，不进仓库）：
#     BILLING_IMAGE_REPO          例如 ghcr.io/<owner>/<repo>
#     BILLING_FRONTEND_IMAGE_REPO 例如 ghcr.io/<owner>/<repo>-frontend
#     以及 .env 里那些数据库口令等（见 .env.example）

set -euo pipefail

TAG="${1:-}"
COMPOSE="${BILLING_COMPOSE:-docker compose}"
# 健康等待的上限。⚠️ 比所有 healthcheck 的 start_period 都长：celery-beat 那个
# 就要 90 秒（它必须等过一个调度周期，见 docker-compose.yml）。
HEALTH_TIMEOUT_SECONDS="${BILLING_HEALTH_TIMEOUT_SECONDS:-180}"
SMOKE_URL="${BILLING_SMOKE_URL:-http://127.0.0.1:${BILLING_HTTP_PORT:-8080}}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# 等待指定服务（不给参数就是全部）变成 healthy。
wait_for_health() {
    want="$*"
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        # 容器还没被创建时 `ps` 什么都不输出。
        # ⚠️ 少了这一条会把「还没起来」当成「已就绪」—— 空输出下面那个 awk
        # 同样给出空的 bad，于是直接返回成功。
        if [ -z "$($COMPOSE ps --format '{{.Service}}' $want 2>/dev/null || true)" ]; then
            log "waiting: containers not created yet"
            sleep 5
            continue
        fi
        # ⚠️ 只认 `healthy` 一种通过。没有 health 字段的服务会落进 bad ——
        # 本栈七个服务都有探针（tests/backend/test_compose.py 钉着），
        # 所以那种情况只可能是配置被人删了，那本来就不该放行。
        bad="$($COMPOSE ps --format '{{.Service}} {{.Health}}' $want 2>/dev/null | awk '$2 != "healthy" { print $1 }' || true)"
        if [ -z "$bad" ]; then
            return 0
        fi
        log "waiting on: $(echo "$bad" | tr '\n' ' ')"
        sleep 5
    done
    return 1
}

# --------------------------------------------------------------------------
# 0. 参数检查
# --------------------------------------------------------------------------

[ -n "$TAG" ] || die "usage: deploy/deploy.sh <image-tag>"

case "$TAG" in
    latest|main|master|"")
        # ⚠️ 不是洁癖：可变标签会让「线上是哪个 commit」变成一个没有答案的问题。
        die "refusing a mutable tag '$TAG'; pass the commit SHA (spec §99)"
        ;;
esac

# --------------------------------------------------------------------------
# 1. 记下当前在跑的是什么 —— 回滚要用
# --------------------------------------------------------------------------
#
# ⚠️ 这一步必须在**任何改动之前**做。在别处记（比如部署成功后再记）的话，
# 第一次失败的部署就没有可回滚的目标了。

PREVIOUS_IMAGE="$($COMPOSE ps --format '{{.Image}}' api 2>/dev/null | head -1 || true)"
if [ -n "$PREVIOUS_IMAGE" ]; then
    log "currently running: $PREVIOUS_IMAGE"
else
    log "nothing running yet (first deploy) — there is no rollback target"
fi

# --------------------------------------------------------------------------
# 2. 取镜像
# --------------------------------------------------------------------------

if [ -n "${BILLING_IMAGE_REPO:-}" ]; then
    export BILLING_IMAGE="${BILLING_IMAGE_REPO}:${TAG}"
    export BILLING_FRONTEND_IMAGE="${BILLING_FRONTEND_IMAGE_REPO:-${BILLING_IMAGE_REPO}-frontend}:${TAG}"
    log "pulling $BILLING_IMAGE"
    $COMPOSE pull --quiet api frontend || die "pull failed; nothing has been changed yet"
else
    # 本地演练：用已经构建好的镜像，跳过 registry。
    # ⚠️ 这条路**只用于在本地验证这个脚本自己的逻辑**，生产上 BILLING_IMAGE_REPO
    # 一定是设着的（CD 传进来）。
    log "BILLING_IMAGE_REPO is unset — using locally built images (rehearsal mode)"
fi

# --------------------------------------------------------------------------
# 3. 迁移 —— 在新容器起来**之前**
# --------------------------------------------------------------------------
#
# ⚠️ 顺序是有讲究的（spec §98.1 要求 migration ordering 是书面的）：
# 先迁移、再换应用。这条顺序只对**向后兼容**的迁移成立 —— 加列、加表、加索引。
# 删列 / 改列类型必须拆成两次部署（先加、双写、再删），否则迁移跑完的那一刻，
# 还在跑的旧代码就开始对着一个它不认识的表结构工作。
#
# ⚠️ 迁移**不回滚**。失败了就停在这里，让旧版本继续跑着 —— 那是安全的状态。

# ⚠️ 先等数据库。演练时这一步是缺的，结果**迁移在 MySQL 还在初始化时就开跑**、
# 直接 Connection refused —— 而那会被当成「这次部署失败」，实际只是早了几秒。
# 正常部署里 MySQL 本来就在跑，所以这条只在首次部署与 MySQL 重启后才生效 ——
# 而那正是最容易手忙脚乱的两个时刻。
log "waiting for the database"
wait_for_health mysql || die "the database did not become healthy; nothing has been changed"

log "running migrations"
$COMPOSE run --rm --no-deps api alembic upgrade head \
    || die "migration failed; the previous version is still running and untouched"

# --------------------------------------------------------------------------
# 4. 换上新版本
# --------------------------------------------------------------------------

log "starting $TAG"
$COMPOSE up -d --no-build || die "compose up failed"

# --------------------------------------------------------------------------
# 5. 等健康
# --------------------------------------------------------------------------

log "waiting for health (up to ${HEALTH_TIMEOUT_SECONDS}s)"
HEALTHY=0
wait_for_health && HEALTHY=1

# --------------------------------------------------------------------------
# 6. 冒烟
# --------------------------------------------------------------------------
#
# ⚠️ 健康检查通过**不等于**这次部署是好的：每个容器只问自己活着没有，没有人
# 端到端走一遍。这里至少确认边缘能把请求送到 api 并拿回 200。

SMOKE=0
if [ "$HEALTHY" = "1" ]; then
    log "smoke: GET ${SMOKE_URL}/healthz"
    if curl -fsS --max-time 10 "${SMOKE_URL}/healthz" >/dev/null; then
        SMOKE=1
    else
        log "smoke test failed"
    fi
fi

# --------------------------------------------------------------------------
# 7. 成或者回滚
# --------------------------------------------------------------------------

if [ "$HEALTHY" = "1" ] && [ "$SMOKE" = "1" ]; then
    log "deployed $TAG"
    # ⚠️ 清掉悬空镜像。生产 VPS 磁盘很紧（12 GB 可用，见 docs/deployment.md §3.1），
    # 每次部署都会留下上一个版本的层，不清的话它会慢慢把磁盘吃光 ——
    # 而磁盘写满时 MySQL 直接停止写入。
    docker image prune -f >/dev/null 2>&1 || true
    exit 0
fi

log "deployment is not healthy"

if [ -z "$PREVIOUS_IMAGE" ]; then
    # ⚠️ 第一次部署没有可回滚的目标。**不要把栈停掉** —— 留着现场给人看，
    # 停掉只会让排障的人失去日志与容器状态。
    die "first deploy failed and there is nothing to roll back to; leaving the stack up for inspection"
fi

log "rolling back to $PREVIOUS_IMAGE"
export BILLING_IMAGE="$PREVIOUS_IMAGE"
$COMPOSE up -d --no-build || die "rollback failed — manual intervention required"

if wait_for_health; then
    # ⚠️ 回滚成功仍然以**非零**退出：这次部署没有成功，CD 必须是红的。
    # 回滚让服务恢复了，但「这个 commit 上不了线」这件事不能被绿色掩盖。
    die "rolled back to $PREVIOUS_IMAGE; the deploy of $TAG failed"
fi

die "rollback did not become healthy — manual intervention required"
