# RUNBOOK — 故障处置

> spec §136 要求 runbook 覆盖 18 个故障场景。**这份文件按场景逐个补，不预留空条目** ——
> 空标题会让人以为「已经有预案了」。
> 最后更新：2026-09-16

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

**告警**（2026-09-16 落地，见 [deployment.md](deployment.md) §8.1）：

```text
名称：billing_readiness_degraded_redis
条件：宿主机每 5 分钟 GET /readyz，响应体 data.status != "ok"
     （非 2xx 是另一回事：数据库不通或 API 挂了，按 P1 处理）
分级：P2（服务未中断，但投递在积压）
通知：Healthchecks.io 检查 `ai_billing_hub readyz` → Telegram；状态翻转才发，恢复再发一次
连续 30 分钟未恢复：升级 P1
```

⚠️ **判据是响应体，不是日志。**原先写的条件是「过去 5 分钟出现 >= 1 条 `message="Readiness degraded"` 且 `component="redis"` 的日志」—— 那需要日志聚合，而本阶段没有。日志那条判据等有了聚合仍然成立，**告警名不变**：它是这份文档与通知之间的契约。

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
| Telegram 收到 `ai_billing_hub services` 变 DOWN，正文 `P2 celery-beat health=unhealthy` | 上面那条探针的**送达途径**（2026-09-16，`deploy/monitor.sh` 每 5 分钟巡检一次）。 ⚠️ 探针红了没人看，等于没有探针 |
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

---

## 整台 VPS 没了（灾难恢复）

**首次编写：2026-09-16（T0.9）。§136 的 18 个场景里第三个落地的，也是 §98.1 要求的「书面的灾难恢复顺序与恢复后对账」。**

⚠️ **RTO ≤ 4 小时是这一节的预算，不是口号。**四小时要装下：发现 → 决策 → 起新主机 →
拉备份 → 解密 → 导入 → 重放 binlog → **对账** → 切流量。没演练过的流程，光「解密口令放在哪」
就能吃掉一小时。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| **binlog 心跳先停**（周期 1 分钟、宽限 5 分钟） | **最快的信号**，也正好是 RPO 的量级 |
| `ai_billing_hub services` / `readyz` / `disk` 三个检查同时 DOWN | 巡检每 5 分钟一次、宽限 15 分钟 |
| 站点 502 | 边缘 `infra_nginx` 找不到上游 |

⚠️ **告警本身跑在那台机器上 —— 它挂了就不会有任何「错误日志」。**能发现这件事的只有
外部服务的 dead man's switch：「该到的 ping 没到」。这就是每个脚本成功时也要 ping 的原因。

⚠️ **先分清楚是整机不可用，还是只有服务挂了。**后者按对应场景处理（重启栈、看 `journalctl`），
**不要**直接进灾难恢复 —— 重建一台新主机的代价远大于重启一次栈。

### 影响什么

| 仍然安全 | 已经失去 |
| --- | --- |
| R2 上的全量、binlog、月 / 年备份、日志归档 | 这台主机上的一切：容器、数据库文件、本地备份副本、`.env`、`secrets/` |
| Bitwarden 里的 `master.key` / `jwt.key` / 备份口令离线副本 | `.last-good-deploy`（⚠️ 它也在这台机器上 —— 改从 GitHub Actions 的 Deploy 运行记录里找最后一次成功的 `ref`） |
| GitHub 上的代码与镜像（ghcr） | 边缘代理指向这台机器的那条配置 |

**数据丢失窗口 = 最后一次成功的 binlog 心跳到故障的时间**，设计上 ≤ 5 分钟（RPO）。
这个数字要在对账时**实测出来**，不要假设。

### 立刻做什么

⚠️ **顺序是有讲究的**，下面每一步都依赖上一步的产出：

1. **确认故障范围**（主机控制台 / ping / 供应商状态页），决定「修」还是「重建」。写下决策时刻 ——
   RTO 从这一刻算
2. **取离线材料**（Bitwarden）：`master.key`、`jwt.key`、SMTP 密码、`BILLING_BACKUP_PASSPHRASE`、
   R2 的 `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`、以及 `.env` 里其余的口令
3. **起新主机**：同区域，≥ 2 核 / 4 GB / 48 GB 盘（现网规格见 [deployment.md](deployment.md) §3.1），
   装 Docker 与 git
4. **取代码**：`git clone` 之后 **checkout 上一次成功部署的那个 SHA**
   （来源：GitHub Actions → Deploy → 最后一次成功那次的 `ref` 输入）。
   ⚠️ **不要直接用 `main` 的最新提交** —— 它未必部署过，灾难当天不是验证新代码的时候
5. **还原密钥与配置**：`secrets/` 下三个文件，然后
   ```bash
   chown 10001:10001 secrets/*        # 10001 = 容器运行 UID（ADR-0004）
   chmod 0400 secrets/*
   ```
   再写 `.env`（数据库口令、R2 凭据、备份口令、心跳地址、`BILLING_IMAGE_REPO` 等）
6. **只起数据库**：`docker compose up -d mysql`，等它 healthy。
   ⚠️ 先起全栈会让应用对着一个空库跑。这一步不需要本项目的镜像 —— MySQL 用的是公共镜像，新主机上 compose 会自己拉
7. **恢复**：
   ```bash
   deploy/restore.sh full/<最新一份全量的 key> billing_restore
   ```
   脚本会拉回全量、解密、导入，再重放它之后**全部**的 binlog（PITR）。
   要恢复到误操作之前的某一刻就给第三个参数（UTC 时间）
8. **对账**（见下一节），**通过之后**再往下走
9. **切换并起全栈**：把恢复库切成生产库名（改名或改 `.env` 里的 `BILLING_MYSQL_DATABASE`），然后
   ```bash
   deploy/deploy.sh <第 4 步那个 SHA>
   ```
   它会按那个 commit 拉两个不可变镜像、跑迁移、等七个服务 healthy、跑冒烟，并把这一次记进 `.last-good-deploy`。
   ⚠️ **不要在这里手工 `docker compose up -d`**（Codex 审查 PR #67 指出）：`.env` 里只有 `BILLING_IMAGE_REPO`，**compose 不会自己拼出 `BILLING_IMAGE`** ——
   它会退回 compose 文件里那个本地构建的默认标签 `acuven-billing-hub:local`，而新主机上根本没有这个镜像，栈直接起不来。
   ⚠️ 迁移对一份刚恢复的库通常是空操作（dump 里带着 `alembic_version`）——**这是对的**，它同时也是「代码与表结构对得上」的一次检查
10. **切流量**：把边缘 `infra_nginx` / DNS 指到新主机
11. **恢复运维设施**：按 [deployment.md](deployment.md) §5.2.5 重装 cron（五行），确认四类心跳
    在下一个周期内全部回绿

### 恢复后对账（§98.1 要求）

⚠️ **「导入成功」不等于「数据对」。**下面这些**每一条都要留下实际数字**，写进当次事故记录：

```sql
-- 1. 表清单：应为 8 张（7 张业务表 + alembic_version）
SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE();

-- 2. 数据丢失窗口：最后一条审计记录的时间 vs 故障时刻
SELECT MAX(created_at) FROM audit_logs;

-- 3. 待投递的事件：beat 起来之后会自动补投，这里只看量级
SELECT status, COUNT(*) FROM domain_outbox GROUP BY status;

-- 4. 账号：与最近一次演练 / 月报的数字对得上
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM two_factor_settings;
```

- **主密钥可用性**：用恢复出来的 `master.key` 解开一条 TOTP 密文（做法见下一节）。
  ⚠️ 不做这一步的话，「能登录第一因子、过不了第二因子」要等第一个管理员上线才暴露
- **RPO / RTO 实测值**：把「最后一条审计记录」与「决策时刻」的差、以及第 1 步到第 10 步的耗时
  都记下来。⚠️ 这两个数字是下一次演练与容量决策的输入，事后补不出来
- ⚠️ **钱包余额 = 账本流水之和、用量事件计数、支付回调对账 —— 现在做不了**：
  这些表要到 Phase 1 才存在。写在这里是为了它们落地时**不会被忘掉**，不是假装已经做了

### 恢复

1. 观察一个完整周期：binlog 每分钟离机、巡检每 5 分钟、当晚 03:17 的全量与 03:47 的日志外送
2. 下一个周日的自动恢复演练照常跑通（`ai_billing_hub restore drill` 变绿）
3. 把这次的 RPO / RTO 实测值与偏差写进 [TODO](TODO.md) 的任务记录

### 绝不能做什么

- **不要在核对之前切流量。**切早了的话，新写入会落在一个可能不完整的库上 ——
  那时想退回去，连「退回哪个时刻」都说不清
- **不要用 `BILLING_RESTORE_FULL_ONLY=1` 跳过 binlog 重放**，除非你**已经确认**
  binlog 链断了并且**书面承认**要丢掉那一段。丢数据的决定必须是人做的、并且写下来
- **不要在新主机上生成新的 `master.key`。**那不是「重新初始化」，是**把所有 TOTP 注册一次性作废**
- **不要把 `.env`、密钥内容贴进聊天、工单或截图。**灾难当天最容易发生这件事
- **不要跳过第 4 步直接用 `main`。**灾难恢复不是发布新版本的时机
- **不要用 `docker compose up -d` 起应用**（起 mysql 除外）。理由见第 9 步：新主机上没有本项目的镜像，而 compose 不会从 `BILLING_IMAGE_REPO` 拼出标签 ——它会去找一个本地构建的默认标签，然后失败。**起应用只走 `deploy/deploy.sh`**

---

## 主密钥（`master.key`）丢失或要在新主机上重建

**首次编写：2026-09-16（T0.9）。这一节是 [deployment.md](deployment.md) §6 要求的「runbook 写入主密钥恢复流程（脱敏）」——⚠️ 具体的值只在 Bitwarden 里，本文件与仓库都不许出现。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| 日志里 TOTP 解密失败 / 启动时读不到密钥 | 文件不存在时 Docker 把 secret 挂成一个**空目录**，读它抛 `OSError` —— 密钥静默变成空串 |
| 第二因子全体失效：密码对、验证码一律不通过 | 密文还在库里，只是解不开 |
| 灾难恢复的第 5 步 | 这时不是「丢了」，而是**要把离线副本放回去** |

⚠️ **备份是另一把钥匙。**`BILLING_BACKUP_PASSPHRASE` 只管备份的加解密，
`master.key` 只管应用里的信封加密（ADR-0004）。**丢哪一把，后果完全不同**，别混。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 数据库、备份、日志归档 | **所有 TOTP 注册**：密文解不开，第二因子全废 |
| 密码登录的第一因子 | 管理员登录卡在第二步 |
| R2 上的备份（那把是备份口令） | 依赖信封加密的一切后续功能（Phase 1 起的凭据存储） |

⚠️ **真正丢了（离线副本也没有）= 不可恢复**：只能让每个管理员重新扫码注册，
并按 [ADR-0004](adr/ADR-0004-credential-encryption.md) 走一次密钥轮换。

### 立刻做什么

1. 从 **Bitwarden** 取离线副本（条目名与字段在私有金库里，本文件不记）
2. 写回文件 —— ⚠️ **格式是 `version:base64`，单行、结尾不要多余换行**：
   ```bash
   printf '%s' '<从 Bitwarden 粘贴>' > secrets/master.key
   chown 10001:10001 secrets/master.key
   chmod 0400 secrets/master.key
   ```
   ⚠️ `compose` 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 三个选项在普通
   compose 下**被忽略** —— 宿主机上的属主与权限原样带进容器，那条 `chown` 是唯一的生效途径
3. **先核对指纹，再起栈**：只比对哈希前缀，**不要把内容打印出来**
   ```bash
   sha256sum secrets/master.key | cut -c1-8      # 与 Bitwarden 里记的前缀比对
   ```
4. 起栈：`docker compose up -d`，等七个服务 healthy
5. **验证真的能解密**（这一步不能省）：在一个**断网**的一次性容器里，用这把密钥解开一条
   从数据库里取出来的 TOTP 密文，只打印 `decrypted` —— 做法与每周自动演练里那一段相同
   （见 [deployment.md](deployment.md) §5.2.7）。
   ⚠️ **「容器起来了」不等于「密钥是对的」**：错的密钥要等第一个管理员登录才暴露

### 恢复

1. 用验证器 App 走一次完整登录（第一因子 + 第二因子），确认 TOTP 真的通过
2. 把这次恢复的时刻、指纹前缀、耗时记进当次事故 / 演练记录
3. ⚠️ 如果这次是因为**离线副本也差点没有**才惊险过关，那就补一条：按 ADR-0004 第 3 节
   确认副本的存放位置与访问控制仍然成立（与数据库备份**分开存放**、两套访问控制）

**已经演练过**（2026-09-15）：只凭 Bitwarden 里的离线 `master.key`，在断网容器里解开恢复库里的
TOTP 密文、生成的验证码与验证器 App 一致、错误的密钥被拒绝；从恢复到解密用时 4 分 12 秒。

### 绝不能做什么

- **不要生成一把新的覆盖上去。**那不是修复，是**把所有 TOTP 注册一次性作废** ——
  而且覆盖之后，原来的密文永远解不开了
- **不要把老版本从钥匙串里删掉。**轮换之后老行仍然要用老密钥解开
  （重包裹的后台任务还没有，见 [TODO](TODO.md) 的 T0.9）
- **不要把密钥值写进 `.env`、仓库、聊天、工单或截图。**ADR-0004 定的是「只有路径进环境变量」
- **不要为了读到密钥把容器改回 root。**ADR-0004 明写；要改的是宿主机文件的属主
