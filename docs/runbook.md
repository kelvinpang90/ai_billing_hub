# RUNBOOK — 故障处置

> spec §136 要求 runbook 覆盖 18 个故障场景。**这份文件按场景逐个补，不预留空条目** ——
> 空标题会让人以为「已经有预案了」。
> 最后更新：2026-09-13

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

---

## celery-beat 停止调度

**首次编写：2026-09-13（T0.9）。§136 的 18 个场景里第二个落地的。**

⚠️ **这是本平台最安静的一种故障。**beat 不服务任何请求，所以它死了之后：API 正常、
`/healthz` 正常、`/readyz` 正常、worker 正常、日志里没有任何错误。唯一的现象是
**周期任务不再发生** —— 而「没发生」是没有信号的。

T0.8d 之后 beat 有了真实职责（outbox 的周期恢复），所以它停了就意味着：投递触发丢失
之后**再也没有人补投**，信静静地躺在 `domain_outbox` 里，状态永远是 `PENDING`。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| `docker compose ps` 里 `celery-beat` 显示 **unhealthy** | **主信号**（T0.9 加的探针） |
| `domain_outbox` 里 `status='PENDING'` 且 `next_retry_at` 早于现在的行在累积 | 业务侧的表现，比探针慢但更贴近后果 |

探针的判据是 **beat 自己的调度状态文件有多久没动**：

```bash
docker compose exec celery-beat sh -c \
  'echo $(( $(date +%s) - $(stat -c %Y /var/lib/celery/beat-schedule) ))s'
```

⚠️ **`celery inspect ping` 对 beat 无效** —— 那问的是 worker。beat 不接受 inspect
调用，这就是为什么它需要一套单独的判据。

⚠️ **beat 每派发一次才落一次盘**（`beat_sync_every=1`，见 `app/core/celery_app.py`）。
默认值下是每 3 分钟才同步 —— 实测连续观察三个 60 秒周期文件时间戳纹丝不动，
拿默认行为做探针等于给它一个 3 分钟的盲区。改调度周期时，探针阈值要一起改。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 所有 API 端点、登录、密码重置的**受理** | **周期恢复停止**：投递触发一旦丢失就不再补投 |
| worker 仍在执行**被直接触发**的任务 | 密码重置邮件在触发丢失时永远发不出去 |
| 数据完整性 —— `domain_outbox` 是事实来源，行都在 | Phase 2 之后：对账扫描、状态轮询一并停摆 |

⚠️ **没有数据丢失**，这一点要先确认下来再动手。INV-14 的整套设计就是为了这个：
beat 停多久都不会毁掉已持久化的工作，恢复之后会自然补上。

### 立刻做什么

1. 确认是 beat 本身还是 broker：看 `redis` 与 `celery-worker` 两格的健康状态。
   ⚠️ Redis 挂了会让 worker 也红，但 **beat 的探针不经过 broker** —— 只有 beat
   红而其它绿，才是 beat 自己的问题
2. 看积压规模：

   ```sql
   SELECT status, COUNT(*) FROM domain_outbox GROUP BY status;
   ```

3. 重启 beat：`docker compose restart celery-beat`
4. ⚠️ **不要**手工去重放积压 —— 见下

### 恢复

1. beat 起来后确认探针转绿（起步宽限 90 秒，要等过一个调度周期）
2. 确认周期任务恢复：worker 日志里重新出现 `app.tasks.outbox.recover`
3. 盯 `PENDING` 行数回落。⚠️ 积压不会一口气全放出去：`beat_schedule` 的
   `expires` 比周期略短，攒下的重复触发会被丢弃 —— 这是设计，不是丢任务
4. 如果积压里有已经进 `FAILED` 的死信，那是**另一件事**，按投递失败单独处理

### 绝不能做什么

- **不要手工去 `UPDATE domain_outbox SET status='PENDING'`「帮它重试」。**
  周期恢复会自己捡起该捡的行；手工改状态会把已经进死信的行也放回去，
  而那些行是**重试到上限都发不出去**的，放回去只是让它们再烧一轮
- **不要把 beat 的探针阈值调大来「让它别报警」。**阈值的意义是「多久没派发算异常」，
  调大它只是把盲区变宽 —— 而这个故障本来就是靠这一个信号发现的
- **不要为了「快点恢复」同时起多个 beat。**两个 beat 会各自按自己的时钟派发，
  同一个周期任务被放进队列两次。任务本身幂等，但这会让积压排查更难读
- **不要在没确认 `domain_outbox` 行还在的情况下重建容器。**调度状态在命名卷
  `celery-beat-state` 里，`docker compose down -v` 会连它一起删掉
