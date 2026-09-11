# RUNBOOK — 故障处置

> spec §136 要求 runbook 覆盖 18 个故障场景。**这份文件按场景逐个补，不预留空条目** ——
> 空标题会让人以为「已经有预案了」。
> 最后更新：2026-09-12

---

## 本文件的写法

每个场景固定五节：**怎么发现 · 影响什么 · 立刻做什么 · 恢复 · 绝不能做什么**。

「绝不能做什么」这一节不是客套。故障处置里最贵的错误都不是「没做对」，而是
「做了不该做的」—— 半夜被叫醒的人手边只有这份文档。

---

## Redis / Celery broker 不可用

**首次编写：2026-09-12（T0.5）。这是 §136 的 18 个场景里第一个落地的。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| `GET /readyz` 返回 **200**，`data.redis == "unavailable"`、`data.status == "degraded"` | **主信号。** 注意状态码仍是 200 —— 见下 |
| 日志里 `logger=app.api.health`、`message="Readiness degraded"`、`component="redis"` 的 WARNING | 每次就绪探测各一条，可直接作为告警条件 |
| 日志里 `logger=app.core.broker`、`message="Redis probe failed"` 的 ERROR | 带异常栈（已脱敏），用于判断是连不上、超时还是认证问题 |

⚠️ **负载均衡不会替你发现这件事。**`/readyz` 在 Redis 不可用时**刻意**返回 200，
原因见下一节。因此这个故障**只能靠日志告警发现**，没有第二条路。

**必须配的告警**（尚未实现，见 [TODO](TODO.md) 的 T0.9）：

```text
名称：billing_readiness_degraded_redis
条件：过去 5 分钟内出现 >= 1 条 message="Readiness degraded" 且 component="redis" 的日志
分级：P2（服务未中断，但投递在积压）
连续 30 分钟未恢复：升级 P1
```

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 收用量事件、落库、返回 `202` | 异步投递**不发生**，事件停在数据库里 |
| `/healthz`、只读查询 | 周期任务（对账扫描、状态轮询）不执行 |
| 客户的 AI 服务 | 通知、Webhook 出站延迟 |

**没有数据丢失。** spec §74.6 规定 Redis/Celery 只承载投递触发，数据库 Outbox
才是可恢复的事实来源；Invariant 14 要求队列丢失不得摧毁已持久化的工作。
Redis 回来之后，欠下的投递由周期恢复补上。

积压的量级会随停机时长线性增长 —— 恢复后**要盯 worker 的消费速率**，别让补投
把数据库压垮。

### 立刻做什么

1. 确认是 broker 本身还是网络：在 API 容器里 `redis-cli -u "$BILLING_REDIS_URL" ping`
2. 看 Redis 进程 / 容器状态与内存（`maxmemory` 打满会让写入失败而 `ping` 仍然通 ——
   **这种情况 `/readyz` 会显示 `ok`，别被它骗了**）
3. **不要**动数据库，**不要**重放任何财务操作。事件都在库里，等投递恢复

### 恢复

1. 恢复 Redis 服务
2. 确认 `/readyz` 的 `data.redis` 变回 `"ok"`、`data.status` 变回 `"ok"`
3. 确认 worker 重新连上：`celery -A app.worker inspect ping`
4. 盯积压消化：队列长度回落、上期调整（`PRIOR_PERIOD_ADJUSTMENT`）笔数是否异常增长
   —— 跨了会计期 cut-off 的积压会变成上期调整（见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)）

### 绝不能做什么

- **不要把 `/readyz` 在 Redis 不可用时改成 503。**`REQ-AVAIL-001` 要求 Redis
  不可用不得成为终端 AI 请求路径上的同步依赖。改成 503 会让所有实例被摘出
  轮转 —— 那正是 Invariant 1 要防的中断，而且是我们自己造出来的
- **不要清空队列**（`FLUSHDB` / `FLUSHALL`）来「让它快点恢复」。队列里是尚未
  投递的触发；清掉之后要靠周期恢复重建，恢复窗口反而更长
- **不要为了赶进度手工重跑财务任务。**任务是幂等的，但手工触发绕过了幂等键的
  来源（Outbox 行），可能造出重复效果
