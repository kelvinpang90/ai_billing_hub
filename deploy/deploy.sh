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
# 每个镜像仓库在本机保留几个版本。⚠️ 至少要 2：当前这个，加上回滚目标。
# 默认 3 多留一个缓冲，让「回滚之后再回滚」也还有落脚点。
IMAGE_KEEP="${BILLING_IMAGE_KEEP:-3}"
# 「上一个成功部署的是哪个 commit」落盘的地方。
#
# ⚠️ **出事那天要用它填 workflow_dispatch 的 `ref`。**在此之前，唯一的来源是
# `docker compose ps` 里正在跑的那个标签 —— 而需要回滚的时候，正在跑的恰恰就是
# 坏的那一个。`git log` 也答不了：`main` 上最新那条未必部署过。
DEPLOY_STATE="${BILLING_DEPLOY_STATE_FILE:-./.last-good-deploy}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# 记下这一次成功部署。⚠️ 只在**健康检查与冒烟都过了之后**调用：
# 记早了的话，一次失败的部署会把「上一个好的」覆盖成那个坏的。
record_last_good() {
    local now tmp
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    tmp="${DEPLOY_STATE}.tmp"
    # ⚠️ 先写临时文件再 `mv`：直接覆写时，写到一半被打断就留下一个残缺的记录，
    # 而它恰恰是出事时唯一的依据。
    {
        printf 'tag=%s\n' "$TAG"
        printf 'api_image=%s\n' "${BILLING_IMAGE:-}"
        printf 'frontend_image=%s\n' "${BILLING_FRONTEND_IMAGE:-}"
        printf 'deployed_at=%s\n' "$now"
    # ⚠️ `2>/dev/null` 必须写在 `>` **前面**。重定向按从左到右建立，写在后面时
    # 「目录不存在」那条报错是 shell 在建立 `>` 的当场打出来的，还没轮到它 ——
    # 于是一次成功的部署日志里会平白多出一行 shell 报错。用例真踩到过。
    } 2>/dev/null > "$tmp" && mv -f "$tmp" "$DEPLOY_STATE" 2>/dev/null \
        || { log "WARNING: could not record the last good deploy at $DEPLOY_STATE"; return 0; }
    # 再追加一行历史：回滚目标本身也坏掉时，要往前再找一个。
    printf '%s %s\n' "$now" "$TAG" >> "${DEPLOY_STATE}-history" 2>/dev/null || true
    log "recorded as the last good deploy: $TAG"
    return 0
}

# 把数据库拉起来。正常情况下就是一句 `up -d mysql`。
#
# ⚠️ **但网络定义变了的时候，这一句会把生产搞停**（2026-09-16 在生产上真发生了）：
# compose 为了按新定义重建项目网络，会**先停掉它要起的那个容器**（mysql），再删网 ——
# 而另外六个容器还挂在那张网上，删除报 `network ... has active endpoints` 而失败。
# 结果是：**数据库停了、应用还在跑、迁移没跑、也没有回滚**（失败发生在健康检查之前，
# 那条路径按设计「保持现场不动」）。当时 `/readyz` 直接 503。
#
# 所以这里识别那一个特征：整栈先离开那张网，再重试。
# ⚠️ **`stop` 不是 `down`**：`down` 会把容器删掉，连带 `PREVIOUS_IMAGE` 指向的那份
# 运行时信息一起没了 —— 那正是回滚的落脚点。stop 之后容器还在，回滚照样走得通。
# ⚠️ 这一停是**有意的停机**（几十秒），它是「改网络定义」这件事本身的代价，
# 不是这段代码引入的。迁移仍然排在应用容器之前，顺序没有被打乱。
start_database() {
    local out
    out="$($COMPOSE up -d --no-build mysql 2>&1)" && { printf '%s\n' "$out"; return 0; }
    printf '%s\n' "$out"
    case "$out" in
        *"has active endpoints"*|*"needs to be recreated"*) ;;
        *) return 1 ;;
    esac
    log "the project network must be recreated; stopping the whole stack first (this is a brief, intended outage)"
    $COMPOSE stop || return 1
    $COMPOSE up -d --no-build mysql || return 1
    return 0
}

# 把本项目两个镜像仓库里过老的版本删掉，只留最近 IMAGE_KEEP 个。
#
# ⚠️ **这件事 `docker image prune -f` 做不到。**那条命令只清悬空（无标签）镜像，
# 而每次部署拉进来的都是带 commit SHA 标签的 —— 它永远不会被清掉。2026-09-16 在
# 生产上实测这个漏洞的后果：攒了 8 个版本的 api + frontend 镜像，只有 1 个在跑，
# 占掉约 2.4 GB；根分区已到 68%（告警线 80%，见 monitor.sh）。
# 磁盘写满时 MySQL 直接停止写入，所以保留窗口必须是**有界**的。
#
# ⚠️ 清理失败绝不能让一次成功的部署变成失败 —— 出错只记日志，不改退出码。
prune_old_images() {
    local in_use repo img stale keep zeros digits
    # 演练模式（BILLING_IMAGE_REPO 未设）下不知道仓库名，什么都不动。
    [ -n "${BILLING_IMAGE_REPO:-}" ] || return 0
    case "$IMAGE_KEEP" in ''|*[!0-9]*) return 0 ;; esac

    # ⚠️ **必须 `10#` 强制十进制。**带前导零的值（人写 `08` 是想要 8）能过
    # 上面那一条「只有数字」与下面的 `-ge`（`test` 按十进制读），但 bash 的**算术
    # 展开**把它当八进制：`$(( 08 + 1 ))` 报 value too great for base。
    #
    # ⚠️ 实测的后果比「部署变红」**更难查**：碍于算术展开出错，bash 把函数
    # 惄无声息地提前结束并**返回 0**，于是 `set -e` 不会触发 —— 清理根本没做，
    # 而部署日志里除了一行 stderr 什么异常都看不出来。守卫用例因此钉的是
    # **stderr 为空**，不是退出码。
    #
    # `010` 又是另一种坏法：不报错，**静默地**变成 8。两处各自解析同一个值
    # 是问题的根，所以只解析一次，后面统一用 `keep`。（Codex 审查 PR #62 R2）
    keep=$(( 10#$IMAGE_KEEP ))

    # ⚠️ **得查回绕。**bash 的整数是 64 位且溢出不报错，而回绕的结果可以是
    # 一个**很小的正数**：`10#18446744073709551619` 就是 `3`。于是一个「想多留点」
    # 的配置反而变成只留 3 个，**真的去删镜像** —— 这是这个函数里唯一一类
    # 不可逆的后果，宁可不清也不能删错。（Codex 审查 PR #62 R3 指出；
    # 上一轮自查只抽样了两个大数，恰好都落在安全的一边，结论下错了。）
    #
    # 判法是**往返比对**，不是拍一个上限：把解析结果跟「去掉前导零的原值」
    # 比一次，只要解析丢了信息就不清。这样不用拿一个拍脑袋的数当阈值，
    # 也能连回绕成负数那一种一并拦下。
    zeros="${IMAGE_KEEP%%[!0]*}"      # 前导零（全零时就是它自己）
    digits="${IMAGE_KEEP#"$zeros"}"
    [ -n "$digits" ] || digits=0
    [ "$keep" = "$digits" ] || return 0

    # ⚠️ 少于 2 就没有回滚目标了。配歪了宁可不清，也不能把能回滚的版本删掉。
    [ "$keep" -ge 2 ] || return 0

    # ⚠️ 被**任何**容器引用的镜像都不能动，包括已退出的容器和本栈之外的容器
    # （这台机器上还跑着别的项目）。在用的镜像 docker 自己会拒绝删，但那会在部署
    # 日志里留下一串吓人的报错 —— 先排除掉，日志才有信噪比。
    # ⚠️ 必须 `tr` 成**一行**：下面用 `case " $in_use "` 做整词匹配，而多行字符串里
    # 每个镜像后面跟的是换行不是空格 —— 那个 pattern 永远匹配不上，此刻
    # 排除在用镜像这道防线形同虚设（2026-09-16 写这个函数时真踩了）。
    in_use="$(docker ps -a --format '{{.Image}}' 2>/dev/null | tr '\n' ' ' || true)"

    for repo in "$BILLING_IMAGE_REPO" "${BILLING_FRONTEND_IMAGE_REPO:-${BILLING_IMAGE_REPO}-frontend}"; do
        # ⚠️ 按 CreatedAt 显式排序，不靠 `docker images` 的默认顺序 —— 默认确实是新的
        # 在前，但那是没有文档保证的实现细节，而排错了就会删掉在跑的版本。
        # CreatedAt 的前缀是 `YYYY-MM-DD HH:MM:SS`，字典序即时间序。
        #
        # ⚠️ 用 `awk 'NR > k'` 而不是 `tail -n "+$(( keep + 1 ))"`：后者多一次加法，
        # 而 `keep` 取到 2^63-1 时那个加法溢出成负数，`tail` 报
        # invalid number of lines 到 stderr。上面的往返比对拦不住它 —— 2^63-1
        # 本身是合法十进制。**干脆把这个加法去掉**，awk 按浮点比大小，
        # 大到离谱的 k 只是一行都不输出。（awk 本脚本 wait_for_health 已在用。）
        #
        # ⚠️ **先收进变量、带 `|| true`，不能直接把管道接给 `while`。**本脚本开着
        # `set -euo pipefail`：枚举这一步任一环节出错，整条管道就是非零，于是
        # `set -e` 把一次**健康检查与冒烟都已经过了**的部署扔成失败，CD 变红。
        # （Codex 审查 PR #62 指出；已用假 docker 复现：修之前函数后面的语句
        # 根本执行不到，脚本直接 exit 1。）
        stale="$(
            docker images --filter "reference=${repo}:*" \
                          --format '{{.CreatedAt}}	{{.Repository}}:{{.Tag}}' 2>/dev/null \
                | sort -r \
                | awk -v k="$keep" 'NR > k' \
                | cut -f2 \
                || true
        )"
        [ -n "$stale" ] || continue

        while read -r img; do
            [ -n "$img" ] || continue
            # ⚠️ 这一次的标签与回滚目标额外再挡一道。正常情况下它们就落在最近
            # keep 个里，但「回滚到一个很旧的版本」会让回滚目标掉出窗口。
            case " $in_use " in *" $img "*) continue ;; esac
            if [ "$img" = "${repo}:${TAG}" ] \
                || [ "$img" = "$PREVIOUS_IMAGE" ] \
                || [ "$img" = "${PREVIOUS_FRONTEND_IMAGE:-}" ]; then
                continue
            fi
            log "removing old image $img"
            docker image rm "$img" >/dev/null 2>&1 \
                || log "could not remove $img; leaving it in place"
        done <<< "$stale"
    done

    # ⚠️ 显式 `return 0`，不让函数的退出码取决于最后一句碰巧是什么。
    # 这个函数在「部署已经成功」之后才跑，**它的成败不是部署的成败**。
    return 0
}

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
# ⚠️ **前端要单独记一份。**前端是独立的镜像（`BILLING_FRONTEND_IMAGE`），回滚时
# 只换回后端的话，栈会停在「后端旧、前端新」的混搭状态 —— 而这种状态不会有任何
# 报错：两个容器都健康，只有用户点到某个新页面才发现它在打一个不存在的接口。
PREVIOUS_FRONTEND_IMAGE="$($COMPOSE ps --format '{{.Image}}' frontend 2>/dev/null | head -1 || true)"
if [ -n "$PREVIOUS_IMAGE" ]; then
    log "currently running: $PREVIOUS_IMAGE / ${PREVIOUS_FRONTEND_IMAGE:-frontend not running}"
else
    log "nothing running yet (first deploy) — there is no rollback target"
fi
if [ -f "$DEPLOY_STATE" ]; then
    log "last good deploy on record: $(sed -n 's/^tag=//p' "$DEPLOY_STATE" | head -1)"
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
#
# ⚠️ **等之前先把它拉起来。**只等不拉的话，首次部署时 mysql 容器根本还没被创建，
# 这里会一直等到超时然后退出 —— 首个生产部署永远走不到迁移那一步（Codex #42 R1）。
# 对已经在跑的 mysql，`up -d` 在配置没变时什么也不做。
log "starting the database"
start_database || die "cannot start the database; the application containers are untouched"

log "waiting for the database"
wait_for_health mysql || die "the database did not become healthy; the application containers are untouched"

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
# 5b. 让边缘 nginx 读到新配置
# --------------------------------------------------------------------------
#
# ⚠️ **只改了 nginx 配置的部署，不做这一步就永远不会生效。**配置文件是从部署目录
# 挂进容器的：部署前的 `git checkout` 已经换掉了磁盘上的文件，但 nginx 只在启动或
# reload 时读它；而 billing_nginx 的镜像与 compose 定义没变，`up -d` 不会重建它。
# T0.9 首次加安全响应头时就是这样：部署成功、冒烟通过，外网一个头都没有。
#
# 先 `nginx -t` 再 reload：配置有错时 reload 本身会保留旧配置继续跑，但那样这次
# 部署会被当成成功 —— 所以把「配置无效」算作部署失败，走下面的回滚。
# ⚠️ 回滚只换回镜像；磁盘上的配置仍是这次检出的版本，nginx 内存里的是上一次成功
# reload 的版本（没被换掉）。修好配置之后重新部署即可。
if [ "$HEALTHY" = "1" ]; then
    log "checking and reloading the edge proxy configuration"
    if ! NGINX_TEST="$($COMPOSE exec -T billing_nginx nginx -t 2>&1)"; then
        printf '%s\n' "$NGINX_TEST"
        log "the edge proxy configuration is invalid; not reloading"
        HEALTHY=0
    elif ! $COMPOSE exec -T billing_nginx nginx -s reload; then
        log "the edge proxy reload failed"
        HEALTHY=0
    fi
fi

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
        # ⚠️ 顺带确认边缘 nginx 跑的是**带安全响应头的配置**。只看 200 的话，一个
        # 没 reload 上的边缘照样返回 200 —— 上面 5b 那一步漏掉时，这里是唯一能发现的地方。
        # reload 是异步的（master 收到信号后才换 worker），所以给几秒重试。
        for _ in 1 2 3 4 5; do
            if curl -fsS --max-time 10 -o /dev/null -D - "${SMOKE_URL}/healthz" \
                | tr -d '\r' | grep -qi '^x-frame-options: *DENY$'; then
                SMOKE=1
                break
            fi
            sleep 2
        done
        [ "$SMOKE" = "1" ] || log "smoke test failed: the edge proxy is not sending its security headers"
    else
        log "smoke test failed"
    fi
fi

# --------------------------------------------------------------------------
# 7. 成或者回滚
# --------------------------------------------------------------------------

if [ "$HEALTHY" = "1" ] && [ "$SMOKE" = "1" ]; then
    log "deployed $TAG"
    # 生产 VPS 磁盘很紧（见 docs/deployment.md §3.1），而磁盘写满时 MySQL 直接
    # 停止写入，所以每次成功部署都要把自己造出来的垃圾收干净。两步缺一不可：
    #
    # ⚠️ `image prune` **只清悬空（无标签）镜像**。它拦不住我们真正的增长源：
    # 每次部署拉进来的那个带 commit SHA 标签的镜像。旧版本的回收在
    # prune_old_images 里，按保留窗口做（它头上那段注释记着这个洞在生产上的后果）。
    docker image prune -f >/dev/null 2>&1 || true
    prune_old_images
    record_last_good
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

# ⚠️ **前端必须跟着一起回滚。**不换它的话 `BILLING_FRONTEND_IMAGE` 还指着这一次
# 那个坏标签，`compose up` 会把后端换回去、前端原地不动 —— 回滚看起来成功了，
# 栈却停在「后端旧、前端新」，而这种不匹配是**静默**的。
if [ -z "$PREVIOUS_FRONTEND_IMAGE" ] && [ -n "${BILLING_IMAGE_REPO:-}" ] && [ -n "$PREVIOUS_IMAGE" ]; then
    # 前端容器此刻可能压根没起来（`ps` 列不出停掉的），但两个镜像同一个 commit
    # 构建，所以能从后端那一版的标签推出来。
    PREVIOUS_FRONTEND_IMAGE="${BILLING_FRONTEND_IMAGE_REPO:-${BILLING_IMAGE_REPO}-frontend}:${PREVIOUS_IMAGE##*:}"
    log "the frontend was not running; deriving its rollback target from the api tag"
fi
if [ -n "$PREVIOUS_FRONTEND_IMAGE" ]; then
    export BILLING_FRONTEND_IMAGE="$PREVIOUS_FRONTEND_IMAGE"
    log "rolling the frontend back to $PREVIOUS_FRONTEND_IMAGE"
else
    # 演练模式下没有仓库名，推不出来。**说出来**，不要让人以为整栈都回去了。
    log "WARNING: no previous frontend image is known; the frontend stays on $TAG"
fi

$COMPOSE up -d --no-build || die "rollback failed — manual intervention required"

if wait_for_health; then
    # ⚠️ 回滚成功仍然以**非零**退出：这次部署没有成功，CD 必须是红的。
    # 回滚让服务恢复了，但「这个 commit 上不了线」这件事不能被绿色掩盖。
    die "rolled back to $PREVIOUS_IMAGE; the deploy of $TAG failed"
fi

die "rollback did not become healthy — manual intervention required"
