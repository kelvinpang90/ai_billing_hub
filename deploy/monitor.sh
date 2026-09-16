#!/usr/bin/env bash
#
# 生产巡检：容器健康 + `/readyz` 降级 + 磁盘水位（spec §94 的磁盘告警、§95 的告警通道；T0.9）。
#
# ⚠️ **这个脚本补的是三个「不会有人发现」的故障**，三个都不会让任何请求报错：
#   1. celery-beat 停了 —— 周期任务全停，API 一切正常，日志里什么都没有
#      （探针早就有了，见 docker-compose.yml；缺的一直是**把 unhealthy 送到人手里**）
#   2. Redis 不可用 —— `/readyz` **刻意**返回 200（app/api/health.py 写了理由），
#      所以负载均衡不会发现，告警名 `billing_readiness_degraded_redis`（docs/runbook.md）
#   3. 磁盘被日志 / binlog 撑满 —— §94 明写：必须在**威胁到 MySQL / 文档存储之前**告警
#
# 由 cron 每 5 分钟跑一次（deploy/cron.d/ai_billing_hub）。
#
# ⚠️ **三个维度各自一个心跳检查，不合成一个。**合成一个的话，磁盘先红了之后
# MySQL 再挂就**不会再有第二条通知** —— 外部服务只在状态翻转时通知，而那时状态
# 已经是 down。分开之后每个维度自己翻转、自己通知、自己恢复。
#
# ⚠️ 每一个维度都是 dead man's switch：全绿时也要 ping。cron 没跑、整台 VPS 挂了、
# 脚本卡死时，**没有任何日志会写出来**，只有「该到的 ping 没到」能暴露。
#
# 用法：
#     deploy/monitor.sh            # cron 跑的就是这个
#     BILLING_MONITOR_RECHECK_SECONDS=0 deploy/monitor.sh   # 手工排查时不等复核
#
# 配置从部署目录的 `.env` 读（与 backup.sh / binlog_ship.sh 同一条：字面解析，不 source）。

set -euo pipefail

COMPOSE="${BILLING_COMPOSE:-docker compose}"
ENV_FILE="${BILLING_ENV_FILE:-.env}"

# ⚠️ 分级只体现在**通知正文的前缀**里：通道只有一条（Telegram），它没有分级概念。
# P1 = 客户直接受影响，P2 = 异步链路受影响但请求路径仍然正常。见 docs/deployment.md §8。
SERVICES_P1="${BILLING_MONITOR_SERVICES_P1:-mysql api billing_nginx frontend}"
SERVICES_P2="${BILLING_MONITOR_SERVICES_P2:-redis celery-worker celery-beat}"

# 磁盘水位。⚠️ 这两个数字的意义是「离撑满还有多远」，不是「现在用了多少」——
# 调高它们只会让告警来得更晚，而这条告警的全部价值就在于**来得早**（§94）。
DISK_WARN_PERCENT="${BILLING_MONITOR_DISK_WARN_PERCENT:-80}"
DISK_CRIT_PERCENT="${BILLING_MONITOR_DISK_CRIT_PERCENT:-90}"

# 发现问题后隔多久复核一次。⚠️ 不复核的话，**一次正常部署就会误报**：deploy.sh
# 换版本时容器会有半分钟左右不是 healthy，而那段时间照样可能撞上这个 cron。
# 复核只在已经发现问题时才付出等待。
RECHECK_SECONDS="${BILLING_MONITOR_RECHECK_SECONDS:-45}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ⚠️ 与 backup.sh / binlog_ship.sh 同一条：字面解析 `.env`，**不 source**。
# source 会执行里面的东西，而那个文件里有密码。
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

# 栈的 nginx 默认只绑 127.0.0.1（docker-compose.yml 里那段注释解释了为什么），
# 而 `/readyz` 只放行回环与私有网段 —— 宿主机自己是唯一探得到它的地方。
READYZ_PORT="$(env_value BILLING_HTTP_PORT)"
READYZ_URL="${BILLING_MONITOR_READYZ_URL:-http://127.0.0.1:${READYZ_PORT:-8080}/readyz}"

# ⚠️ cron 那一行把输出整个以 info 级别送进 syslog。异常必须**另外**以 err 级别写一条，
# 否则它和每 5 分钟一次的「all clear」混在一起，日志侧的告警规则无从区分。
alarm() {
    log "ALERT: $*"
    if command -v logger >/dev/null 2>&1; then
        logger -p user.err -t billing-monitor -- "$*" || true
    fi
}

# 心跳。⚠️ `-m 10`：ping 卡住会让整轮巡检拖过下一个 cron 周期。
# ping 失败只记一行、不改变巡检结论 —— 监控服务抖动不该变成「生产有问题」。
heartbeat() {
    local url_name="$1" suffix="$2" body="$3" url
    url="$(env_value "$url_name")"
    if [ -z "$url" ]; then
        [ "$suffix" = "/fail" ] && log "no ${url_name} configured: nobody will be told about this"
        return 0
    fi
    curl -fsS -m 10 --retry 2 -o /dev/null --data-raw "$body" "${url}${suffix}" \
        || log "heartbeat ping failed (${url_name}${suffix}); the monitor will treat this run as missing"
}

# --- 三个维度的检查 ----------------------------------------------------------
#
# 每个 check_* 把问题逐行写到 stdout（空 = 全绿），行首是分级。**不在里面 ping** ——
# 发现与通知分开，复核时才能把同一个检查原样再跑一遍。

check_services() {
    local svc line state health sev
    for svc in $SERVICES_P1 $SERVICES_P2; do
        case " $SERVICES_P1 " in
            *" $svc "*) sev=P1 ;;
            *) sev=P2 ;;
        esac
        line="$($COMPOSE ps --format '{{.Service}} {{.State}} {{.Health}}' "$svc" 2>/dev/null | head -1 || true)"
        if [ -z "$line" ]; then
            # ⚠️ `ps` 不带 -a 时列不出已停止的容器，所以「查不到」= 停了或根本没建，
            # 这是最严重的一种，不是「查询失败」。
            echo "$sev $svc is not running"
            continue
        fi
        state="$(echo "$line" | awk '{print $2}')"
        health="$(echo "$line" | awk '{print $3}')"
        if [ "$state" != "running" ]; then
            echo "$sev $svc state=$state"
        elif [ "$health" != "healthy" ]; then
            # ⚠️ 只认 healthy 一种通过：health 字段空了同样要报出来（compose 里七个
            # 服务**都**配了 healthcheck，空的意思是有人把它删了）。
            echo "$sev $svc health=${health:-none}"
        fi
    done
}

check_readyz() {
    local body redis
    if ! body="$(curl -fsS -m 10 "$READYZ_URL" 2>/dev/null)"; then
        # `/readyz` 在数据库不通时返回 503，`curl -f` 会失败；API 整个挂了也走这里。
        echo "P1 readyz is not answering 2xx ($READYZ_URL)"
        return 0
    fi
    case "$body" in
        *'"status":"ok"'*) ;;
        *)
            # ⚠️ 告警名是**稳定契约**：runbook 与 Telegram 里按它认这个故障。
            redis="$(printf '%s' "$body" | grep -o '"redis"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 || true)"
            echo "P2 billing_readiness_degraded_redis: ${redis:-redis status unknown}"
            ;;
    esac
}

check_disk() {
    local target line device used mount seen=""
    # ⚠️ 用 `df -P` 而不是 `df`：长设备名会让默认输出换行，字段就错位了。
    # 两个路径：根文件系统（容器日志、镜像、MySQL 数据都在这），以及部署目录
    # （binlog 与备份的落地处）。生产上这两个是同一个文件系统 —— 按**设备**去重，
    # 而不是按百分比：两块盘恰好同一个水位时，按百分比去重会漏掉一整块。
    for target in / "$PWD"; do
        line="$(df -P "$target" 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $1, $5, $6}')"
        [ -n "$line" ] || continue
        device="$(echo "$line" | awk '{print $1}')"
        used="$(echo "$line" | awk '{print $2}')"
        mount="$(echo "$line" | awk '{print $3}')"
        case " $seen " in *" $device "*) continue ;; esac
        seen="$seen $device"
        if [ "$used" -ge "$DISK_CRIT_PERCENT" ]; then
            echo "P1 disk ${mount} at ${used}% (crit ${DISK_CRIT_PERCENT}%)"
        elif [ "$used" -ge "$DISK_WARN_PERCENT" ]; then
            echo "P2 disk ${mount} at ${used}% (warn ${DISK_WARN_PERCENT}%)"
        fi
    done
}

# --- 巡检一轮 ----------------------------------------------------------------

SERVICES_OUT="$(check_services || true)"
READYZ_OUT="$(check_readyz || true)"
DISK_OUT="$(check_disk || true)"

if [ -n "${SERVICES_OUT}${READYZ_OUT}${DISK_OUT}" ] && [ "$RECHECK_SECONDS" -gt 0 ]; then
    log "problems found; re-checking in ${RECHECK_SECONDS}s (a deploy in flight looks exactly like this)"
    sleep "$RECHECK_SECONDS"
    SERVICES_OUT="$(check_services || true)"
    READYZ_OUT="$(check_readyz || true)"
    DISK_OUT="$(check_disk || true)"
fi

FAILED=0
publish() {
    local url_name="$1" dimension="$2" problems="$3" ok_summary="$4"
    if [ -n "$problems" ]; then
        FAILED=1
        alarm "$(printf '%s' "$problems" | tr '\n' ';')"
        heartbeat "$url_name" /fail "$problems"
    else
        log "$dimension: $ok_summary"
        heartbeat "$url_name" "" "$ok_summary"
    fi
}

publish BILLING_HEALTHCHECK_SERVICES_URL services "$SERVICES_OUT" \
    "all containers healthy ($(echo "$SERVICES_P1 $SERVICES_P2" | wc -w) services)"
publish BILLING_HEALTHCHECK_READYZ_URL readyz "$READYZ_OUT" "readyz ok (database + redis)"
publish BILLING_HEALTHCHECK_DISK_URL disk "$DISK_OUT" \
    "disk below ${DISK_WARN_PERCENT}% ($(df -P / | awk 'NR==2 {print $5}'))"

exit "$FAILED"
