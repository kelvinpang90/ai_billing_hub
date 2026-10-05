# RUNBOOK — 故障处置

> spec §136 要求 runbook 覆盖 18 个故障场景。**这份文件按场景逐个补，不预留空条目** ——
> 空标题会让人以为「已经有预案了」。
> 最后更新：2026-10-05

---

## 本文件的写法

每个场景固定五节：**怎么发现 · 影响什么 · 立刻做什么 · 恢复 · 绝不能做什么**。

「绝不能做什么」这一节不是客套。故障处置里最贵的错误都不是「没做对」，而是
「做了不该做的」—— 半夜被叫醒的人手边只有这份文档。

文件末尾的「配置项」一节不是故障场景，不套这五节：每个配置项固定写 **含义 · 默认值与取值规则 · 怎么改 ·
改完怎么生效 · 对已有数据的影响 · 绝不能做什么**。

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
   （来源：R2 `config/` 最新快照里的 `.last-good-deploy`；或 GitHub Actions → Deploy → 最后一次成功的那次 ——
   合并触发的看它的 commit，手动触发的看 `ref` 输入）。
   ⚠️ **不要直接用 `main` 的最新提交** —— 它未必部署过，灾难当天不是验证新代码的时候
5. **还原密钥与配置**：`secrets/` 下三个文件，然后
   ```bash
   chown 10001:10001 secrets/*        # 10001 = 容器运行 UID（ADR-0004）
   chmod 0400 secrets/*
   ```
   再写 `.env`（数据库口令、R2 凭据、备份口令、心跳地址等；键名清单见 R2 `config/` 里最新那份快照）。
   ⚠️ `BILLING_IMAGE` / `BILLING_FRONTEND_IMAGE` 两行**不要手写**，由第 9 步的 `deploy.sh` 部署成功后写入
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
   BILLING_IMAGE_REPO=ghcr.io/<owner>/<repo> \
   BILLING_FRONTEND_IMAGE_REPO=ghcr.io/<owner>/<repo>-frontend \
     deploy/deploy.sh <第 4 步那个 SHA>
   ```
   它会按那个 commit 拉两个不可变镜像、跑迁移、等七个服务 healthy、跑冒烟，把这一次记进 `.last-good-deploy`，
   并把两个镜像钉进 `.env`（[deployment.md](deployment.md) §9.7）。
   ⚠️ **两个 `*_REPO` 必须在命令行上给**：`deploy.sh` 只从 shell 环境读它们、不读 `.env`（平时是 CD workflow 传的）。
   漏了它不会报错，而是**落进演练模式**：不拉镜像、去找本地构建的 `acuven-billing-hub:local`，新主机上没有，栈起不来
   （2026-09-18 查实）。两个包是公开的，不需要 `docker login`。
   ⚠️ **不要在这里手工 `docker compose up -d`**（Codex 审查 PR #67 指出）：新主机的 `.env` 里还没有部署钉进去的镜像，
   compose 会退回 compose 文件里那个本地构建的默认标签 `acuven-billing-hub:local`，同样起不来。
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
3. 把这次的 RPO / RTO 实测值与偏差写进收尾 PR 描述的任务记录（[TODO](TODO.md) 只改勾选项）

### 绝不能做什么

- **不要在核对之前切流量。**切早了的话，新写入会落在一个可能不完整的库上 ——
  那时想退回去，连「退回哪个时刻」都说不清
- **不要用 `BILLING_RESTORE_FULL_ONLY=1` 跳过 binlog 重放**，除非你**已经确认**
  binlog 链断了并且**书面承认**要丢掉那一段。丢数据的决定必须是人做的、并且写下来
- **不要在新主机上生成新的 `master.key`。**那不是「重新初始化」，是**把所有 TOTP 注册一次性作废**
- **不要把 `.env`、密钥内容贴进聊天、工单或截图。**灾难当天最容易发生这件事
- **不要跳过第 4 步直接用 `main`。**灾难恢复不是发布新版本的时机
- **不要用 `docker compose up -d` 起应用**（起 mysql 除外）。理由见第 9 步：新主机的 `.env` 里还没有部署钉进去的镜像 ——compose 会去找一个本地构建的默认标签，然后失败。**起应用只走 `deploy/deploy.sh`**
- **恢复完成之前不要往 `main` 合并任何东西。**2026-09-18 起合并即部署、不经人工批准：`VPS_*` secret 指着哪台机器，
  一次合并就会对着它开火 —— 指着旧机器是一次红色的 CD，已经改指新机器则是一次没人计划过的发布

---

## 主密钥（`master.key`）丢失或要在新主机上重建

**首次编写：2026-09-16（T0.9）。这一节是 [deployment.md](deployment.md) §6 要求的「runbook 写入主密钥恢复流程（脱敏）」——⚠️ 具体的值只在 Bitwarden 里，本文件与仓库都不许出现。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| 部署失败：`bind source path does not exist` | 生产（Linux）上 secret 文件不存在时，创建容器直接失败，旧版本原样留着 |
| 日志里 TOTP 解密失败 / 启动时读不到密钥 | Docker Desktop 下 secret 文件不存在时，它把路径建成一个**目录**挂进去，读它抛 `OSError` —— 密钥静默变成空串 |
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
4. 起栈：`docker compose up -d`，等七个服务 healthy。
   ⚠️ 这只在**这台主机部署过**时成立：`deploy.sh` 部署成功后把两个镜像钉在 `.env` 里
   （[deployment.md](deployment.md) §9.7）。**灾难恢复的新主机上没有那两行** —— 在那里
   不要在这一步起栈，等灾难恢复第 9 步的 `deploy/deploy.sh`
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

---

## FX 汇率：拉取失败（`fx_fetch` 告警）

**首次编写：2026-10-04（AIH-TASK-043）。依据：设计闸门 #183 v3（[AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md) §2「两个告警维度」、§5）、
[currency-and-fx.md](currency-and-fx.md)、[deployment.md](deployment.md) §8.4。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub fx_fetch` 变 DOWN，正文 `P2 fx_fetch: USD no successful fetch in 72 hours (<n> failed)` | **主信号**。判据：某个配置币种最近 72 小时没有任何成功的拉取（`NEW_DRAFT` / `NO_NEW_QUOTE` / `NO_QUOTE_FOR_DATE`），且至少一条 `FAILED` —— 即连续 3 天、9 次拉取全部失败 |
| `GET /api/v1/admin/fx-rates/fetch-attempts?outcome=FAILED` | 每次失败一行，`error_code` 说明原因 |
| worker 日志 | 某个币种写库失败时的 ERROR（那一次不留拉取记录，任务以失败结束） |

⚠️ **周末、公众假期不会触发它**：BNM 那几天答「无记录」，记为 `NO_QUOTE_FOR_DATE`，算成功。
72 小时里**一条记录都没有**（Beat 停了、worker 没起）也**不是**这条告警 —— 那由 `fx_stale` 与「celery-beat 停止调度」兜住。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 计费：已发布的版本照旧生效，`resolve_fx_rate` 只读库、**热路径从不调 BNM**（ADR-0005 第 6 条） | 没有新草稿：管理员没有新的 BNM 价可发布 |
| 管理员手工录入、发布、退役 | 时间一长，`fx_stale` 会接着响（当前版本报价日超过 5 个日历日） |

**没有数据丢失，也没有计费失败。**拉取失败只影响草稿与告警。

### 立刻做什么

1. 看失败原因：`GET /api/v1/admin/fx-rates/fetch-attempts?outcome=FAILED&base_currency=USD`

   | `error_code` | 多半是 |
   | --- | --- |
   | `NETWORK` / `TIMEOUT` | worker 出不去或 BNM 不可达。在 worker 容器里试一次出站 HTTPS（SMTP 也走出站，可对比） |
   | `HTTP_5xx` / `HTTP_429` | BNM 自己的问题，等它恢复 |
   | `HTTP_404` | 404 但不是「无记录」那个响应体：BNM 改了路径 |
   | `BAD_PAYLOAD` | BNM 改了响应形状（设计 §10 假设 2）—— 要改适配器，开任务 |
   | `UNIT_NOT_EXACT` | `middle_rate / unit` 超过 10 位小数，同上 |

2. 手工触发一次确认现状（幂等，拉的永远是吉隆坡当天）：
   ```bash
   docker compose exec celery-worker python -c "from app.tasks.fx_fetch import fetch; print(fetch())"
   ```
3. 短期内恢复不了、而当前版本又快过期：管理员按 BNM 网站公布的中午场中间价**手工录入**草稿
   （`POST /api/v1/admin/fx-rates`，`source_reference` 写清出处）并发布 —— 这正是 ADR-0005 第 1 条留的通道

### 恢复

1. 下一次 Beat（吉隆坡 12:30 / 14:30 / 17:30）或手工触发出现成功记录，`fx_fetch` 下一轮巡检转绿
2. 拿到 `NEW_DRAFT` 后，管理员核对并发布（见下面「过期」一节的发布步骤）
3. 适配器要改（`BAD_PAYLOAD` / `UNIT_NOT_EXACT`）：开任务改 `app/core/fx_source.py`，不改设计语义；在那之前靠手工录入

### 绝不能做什么

- **不要在计费路径上加「拉不到就实时调 BNM」。**ADR-0005 第 6 条：外部接口的抖动不能变成计费失败
- **不要为了让告警安静把 72 小时窗口调大、或删拉取记录。**拉取记录只增（触发器拒绝改删），它是草稿的来源证明
- **不要让自动拉取直接发布**来「省掉人工那一步」。ADR-0005 第 3 条：发布是「这个数字要开始算钱了」的确认点

---

## FX 汇率：过期，或没有生效的汇率（`fx_stale` 告警）

**首次编写：2026-10-04（AIH-TASK-043）。依据同上一节。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub fx_stale` 变 DOWN，正文 `P2 fx_stale: USD quoted <报价日> (<n> calendar days)` | 当前生效版本的报价日距今**超过 5 个日历日**（吉隆坡日期相减；Kelvin 2026-09-29 定，ADR-0005 收口条件旁有注明）。手工版本取 `observed_at` 的吉隆坡日期 |
| 正文 `P2 fx_stale: USD no rate in effect` | 该币种**此刻没有任何生效的版本**：从没发布过、最后一个被退役了、或只有未来的预约 |
| 业务侧：非 MYR 事件判 `FX_RATE_ERROR` | 只在「没有生效的版本」时出现，计费 worker 落地之后（T-H）才有这个信号 |

三种常见原因：

1. **忘了发布** —— 拉取一直成功（`fx_fetch` 是绿的），草稿堆着没人发布。最常见
2. **BNM 持续「无记录」** —— 拉取记录全是 `NO_QUOTE_FOR_DATE`：长假（周末 + 两三天公众假期在 5 天内，偶尔会超）；
   或者**币种配错**（BNM 对不存在的币种代码也答「无记录」，所以不会触发 `fx_fetch`）—— 后者的表现是 `no rate in effect`
   且从来没有过该币种的 BNM 草稿，查 `BILLING_FX_CURRENCIES`
3. **拉取一直失败** —— 那 `fx_fetch` 应当先响过，按上一节处理

### 影响什么

| 情形 | 影响 |
| --- | --- |
| 过期（有生效版本，只是旧） | 计费照常，按旧汇率算。偏差由定价规则 markup 的 FX 缓冲吸收（ADR-0005「负面」） |
| 没有生效的版本 | 非 MYR 事件 `FX_RATE_ERROR`、**不扣费**；发布之后由 T-H 重新入队补算 |

### 立刻做什么

1. 看草稿：`GET /api/v1/admin/fx-rates?status=DRAFT&base_currency=USD`；看拉取：`GET /api/v1/admin/fx-rates/fetch-attempts?base_currency=USD`
2. 有 BNM 草稿：与 BNM 网站上**同一报价日**的中午场中间价核对，`POST /api/v1/admin/fx-rates/{fx_rate_id}/publish`，
   **不给 `effective_from`** —— 从发布那一刻（`t`）起生效，不从报价日回溯（Kelvin 2026-09-29）
3. 没有草稿（长假、BNM 不覆盖的币种）：手工录入草稿（`source_reference` 写出处）再发布
4. 币种配错：查容器里实际的值
   `docker compose exec api python -c 'from app.core.config import get_settings; print(get_settings().fx_currency_codes)'`。
   ⚠️ 截至 AIH-TASK-043，`docker-compose.yml` 的 `x-backend.environment` **没有转发** `BILLING_FX_CURRENCIES`，
   只改 `.env` 进不了容器（理由同「配置项」一节）；要改币种得先在 compose 里加转发，另开任务

发布返回 409 `EFFECTIVE_FROM_CONFLICT`：有一个尚未开始的预约版本（或同一秒刚发布过）。要么指定一个晚于预约起点的
`effective_from`，要么先退役那条预约（撤销），要么下一秒重发。

### 恢复

1. 下一轮巡检（5 分钟内）`fx_stale` 转绿，正文变成 `OK USD quoted <新报价日> (<n> calendar days)`
2. 「没有生效的版本」那种：时间线为空时不给时刻的发布得到 `[null, null)`、退役留下的空档由不给时刻的发布从退役时刻起补上
   （设计 §2 P1 / P4）—— 这段时间里 `FX_RATE_ERROR` 的事件此后取得到汇率，由 T-H 重新入队补算；已取到汇率的时刻结果不变

### 绝不能做什么

- **不要把 5 天的阈值调大来「让它别响」。**它是「拉到了但忘了发布」唯一的信号（ADR-0005 第 6 条第二条告警）
- **不要直接改库里的版本行**（改 `effective_from` / `rate`、把草稿 `UPDATE` 成 `PUBLISHED`）。触发器会拒绝大部分写法；
  侥幸过去的会绕开锁、区间计算与审计
- **不要不核对就发布 BNM 草稿。**发布之后这个数字就开始算钱，而且已被引用的版本不可修改

---

## FX 汇率：发布了错误的汇率（退役与补发）

**首次编写：2026-10-04（AIH-TASK-043）。依据：设计闸门 #183 v3 §2「退役」「发布」、§5「发布了错误汇率」。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| 管理员核对时发现已发布版本的 `rate` 与出处不符 | 例如手工录入时小数点错位、把买入价当成中间价 |
| 客户或财务对某段时间的 MYR 换算提出异议 | 查那段时间生效的版本：版本列表 `GET /api/v1/admin/fx-rates?base_currency=USD` 的 `effective_from` / `effective_to` |
| 拉取记录出现 `QUOTE_CHANGED` | BNM 更正了同一报价日的价；已有版本**不会**跟着变 |

### 影响什么

- 错误版本生效期间计费的事件按错误汇率算。⚠️ **已经计费的事件不会因为退役而改变**：一个（币种, 时刻）一旦取到某个
  版本，以后永远取到同一个（ADR-0005「同一事件重算得出相同 MYR」）。纠正已计费事件走 Phase 8 的 reprocess，不在这里
- 退役之后到补发之前，该币种的新事件 `FX_RATE_ERROR`、不扣费；补发把这段空档补上（见下），不会永久丢

### 立刻做什么

先看错误版本处在哪种状态（`GET /api/v1/admin/fx-rates/{fx_rate_id}`）：

| 错误版本 | 做法 |
| --- | --- |
| 还是草稿 | 丢弃：`POST …/{fx_rate_id}/discard`。BNM 草稿不可编辑，丢弃后手工录入 |
| 已发布、**正在生效**（未截断，起点已过） | 1. **先**建好正确的草稿（手工录入，`source_reference` 写清出处与「更正 <错误版本 id>」）<br>2. 退役错误版本：`POST …/{fx_rate_id}/retire`，`reason` 写清原因 —— 从退役时刻 `t` 起该币种取不到汇率<br>3. **立刻**发布正确草稿，**不给 `effective_from`**：按 P4 从退役时刻起首尾相接补上空档（设计 §2 P4、E9 ①） |
| 已发布、**尚未开始**（预约的） | 退役即撤销预约：它变成空区间、永不生效；前一个版本若仍是已发布、且正是被它截断的，恢复为未截断（R2；以退役结束的不恢复）。然后按需重新发布正确的版本 |
| 已发布、**已被截断**（历史版本） | 409 `FX_RATE_NOT_RETIRABLE`：它的区间已经结束，不能退役。只能走 Phase 8 reprocess 纠正已计费事件 |

先建草稿再退役，是为了把「退役 → 补发」之间无汇率的空档压到几秒；即使拖久了，补发不给时刻也会从退役时刻起补上。

### 恢复

1. 版本列表里：错误版本 `RETIRED [原起点, 退役时刻)`，正确版本 `PUBLISHED [退役时刻, null)`，两者首尾相接
2. `fx_stale` 保持绿（正确版本的报价日在 5 天内）
3. 审计里有 `FX_RATE_RETIRE`（带 `reason`）与 `FX_RATE_PUBLISH` 两条，按 `entity_type = fx_rate_version` 可查
4. 错误版本生效期间已计费的事件：列出受影响的时间段与币种，留给 Phase 8 reprocess（记进当次事故记录）

### 绝不能做什么

- **不要直接 `UPDATE` 已发布版本的 `rate`。**触发器会拒绝；即使绕过去，已按它计费的事件与快照里的 `fx_rate_applied`
  就对不上了 —— 这正是 INV-6 要防的
- **不要用「给一个过去的 `effective_from`」来回溯纠正。**F 早于 `t` 一律 422 `EFFECTIVE_FROM_IN_PAST`：只有「无汇率 →
  有汇率」可以覆盖过去，已取到汇率的时刻不许改
- **不要退役一个正在生效的版本而不马上补发。**退役之后该币种的新事件全部 `FX_RATE_ERROR`、不扣费，直到下一次发布

---

## FX 告警维度不上报（Healthchecks 报 `fx_fetch` / `fx_stale`「未上报」）

**首次编写：2026-10-04（AIH-TASK-043）。依据：设计闸门 #183 v3 §2「内部告警接口」、§5 最后两行；[deployment.md](deployment.md) §8.4。**

### 怎么发现

Telegram：`ai_billing_hub fx_fetch` 与 `ai_billing_hub fx_stale` **一起**变 DOWN，原因是「该到的 ping 没到」（Healthchecks
宽限期 15 分钟过了），而不是收到 `/fail`。`monitor.sh` 在这几种情况下**刻意不推**告警维度：

| 巡检日志（`journalctl -t billing-monitor`） | 原因 |
| --- | --- |
| `alerts endpoint is not answering 200` | API 挂了（`readyz` 维度应同时 DOWN）；或接口返回 404 / 401 / 503 |
| `the monitor token file is unreadable or empty` | 宿主机上读不到令牌文件（权限、路径） |
| `no BILLING_MONITOR_TOKEN_HOST_FILE configured` | `.env` 里没配宿主机路径 |
| 只有一个维度 DOWN，日志 `no BILLING_HEALTHCHECK_ALERT_…_URL configured` | 那个维度的地址没写进 `.env` |
| 所有检查一起 DOWN | cron 没跑或整台机器挂了 —— 按「celery-beat 停止调度」「整台 VPS 没了」处理 |

### 影响什么

只影响**告警**：两个 FX 维度此刻没人看着。计费、拉取、发布都不受影响。

### 立刻做什么

1. `readyz` 也 DOWN：先按 API / 数据库故障处理，告警维度会随之恢复
2. 只有 FX 两个 DOWN：在宿主机上看状态码（不带令牌）：
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<端口>/internal/alerts`

   | 状态码 | 含义 | 处理 |
   | --- | --- | --- |
   | 401 | nginx 放行、应用的令牌可用 —— 问题在宿主机那一侧 | 查 `BILLING_MONITOR_TOKEN_HOST_FILE` 指向的是不是 `secrets/monitor.token`、部署用户能不能读（下面「配置项」） |
   | 404 | 应用侧令牌不可用（文件读不出、少于 32 个字符）；应用日志每次请求一条 ERROR（不含路径与内容） | 修文件的属主 / 权限 / 内容 |
   | 403 | nginx 拒绝 —— 名单与来源对不上 | 核对 `deploy/nginx/billing.conf` 里 `/internal/alerts` 与 `/readyz` 的名单（应完全相同） |
   | 503 | 令牌通过但查库失败 | 看 API 日志 `Evaluating the alert dimensions failed` |

3. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

两个检查收到成功 ping 即转绿（或 `/fail`，那就是真有 FX 问题，按上面两节处理）。

### 绝不能做什么

- **不要把令牌写进 `.env`、crontab 或命令行**（`curl -H "Authorization: Bearer …"`）：同机任何用户 `ps` 都看得到，
  而这台 VPS 上还跑着另外八个项目。`.env` 里只放文件路径
- **不要为了让部署用户读到而把文件改成 `0444` 或复制第二份。**属主 `10001`、属组部署用户的主组、`0440` 就够；
  第二份要和第一份同步轮换，迟早对不上
- **不要把 `/internal/alerts` 的 nginx 名单放宽**来「先通了再说」：它与 `/readyz` 同一份，测试钉着

---

## 用量摄取：幂等冲突（`usage_event_conflicts` 告警）与摄取中断

**首次编写：2026-10-05（AIH-TASK-029）。依据：设计闸门 #176 v8（[AIH-TASK-029-usage-ingest.md](design/AIH-TASK-029-usage-ingest.md)
§2「告警」「nginx」、§5）；接口契约见 [api.md](api.md#集成用量事件摄取)。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub usage_event_conflicts` 变 DOWN，正文 `P2 usage_event_conflicts: <n> conflicts in 24 hours, earliest <UTC 时刻>` | **主信号**（spec §23 要求的数据完整性告警，P2）。判据：最近 24 小时里记下了任何一条冲突行。`monitor.sh` 按维度名读 `.env` 的 `BILLING_HEALTHCHECK_ALERT_USAGE_EVENT_CONFLICTS_URL`，脚本不用改；没配这个地址时巡检日志会写 `no BILLING_HEALTHCHECK_ALERT_USAGE_EVENT_CONFLICTS_URL configured` |
| 管理端审计：`GET /api/v1/admin/audit-logs?action=USAGE_EVENT_IDEMPOTENCY_CONFLICT` | 每条**新的**冲突一条审计（同一冲突请求重发不再加），`entity_id` 是被撞的原事件，`after_state` 有请求方的 `api_key`、`key_version` 与不符类型 |
| 管理端审计：`action=USAGE_EVENT_SCOPE_MISMATCH` | 集成方在载荷里报的 `tenant_id` / `project_id` 与凭据不符（403）。不进告警维度，查冲突时顺带看一眼：常与配置串了有关 |
| **摄取中断**（spec §120 的 Billing ingestion outage） | 平台侧能观测到的中断就是 API 或数据库不可用，**由现有的 `services`（`api` / `mysql` / `billing_nginx` 容器）与 `readyz` 两个检查覆盖**，不另设维度。应用侧「发不出去」由 Phase 3 的积压监控覆盖。⚠️ Redis 不可用**不是**摄取中断：nonce 层放行、事件照收（API 日志里每个请求一条 `nonce_store_unavailable` WARNING），按「Redis / Celery broker 不可用」处理 |

### 影响什么

| 情形 | 仍然正常 | 受影响 |
| --- | --- | --- |
| 幂等冲突 | 原事件一个字节都没改，照常计费；冲突请求**没有任何财务效果** | 冲突的那个请求被拒（409，`retryable = false`），在集成方那边进死信 |
| 摄取中断 | 集成方的 outbox 退避重试，事件留在应用侧，不丢（REQ-AVAIL-001：AI 服务本身不受影响） | 中断期间没有新事件落库，计费随之推迟 |

### 立刻做什么

1. 看冲突明细（只读）：
   ```bash
   docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
     SELECT c.received_at, c.event_id, c.api_key, c.mismatch, e.public_id, e.received_at
       FROM usage_event_conflicts c JOIN usage_events e ON e.id = c.usage_event_id
      ORDER BY c.id DESC LIMIT 20"'
   ```
2. 按 `mismatch` 判断：

   | `mismatch` | 多半是 |
   | --- | --- |
   | `FINGERPRINT` | 同一个 `api_key` 用同一个 `event_id` 发了不同的内容：集成方的 `event_id` 生成或 outbox 改写了已排队的载荷 |
   | `OWNERSHIP` | 另一个 `api_key`（另一个项目）用了别人已用过的 `event_id`，内容相同：两套系统共用了 ID 来源，或复制了载荷 |
   | `BOTH` | 同上，内容也不同 |

3. 联系 `api_key` 对应的集成方修复。冲突的事件在他们那边是死信：修好之后用**新的** `event_id` 重投。
4. 摄取中断：先按 `readyz` / `services` 的告警处理 API 或数据库故障；恢复后集成方自行补投，已经落库的事件重投得到
   `200 already_received`，不会重复计费。

### 恢复

- 冲突维度在最后一条冲突行满 24 小时后自动转绿；冲突行本身永久保留（只增），不需要也不能清理。
- 摄取中断随 API / 数据库恢复而结束，`readyz` 与 `services` 转绿即可。

### 部署后检查：超大请求经 nginx 返回 JSON 413

集成前缀 `/api/v1/integration/` 的 nginx 上限是 1 MiB，超过时由 nginx 返回与应用同一个 JSON 信封（`PAYLOAD_TOO_LARGE`、
`retryable: false`、`request_id: null`），而不是默认的 HTML。静态配置由 `tests/backend/test_compose.py` 钉着，**经过 nginx 的
行为只能部署后实测**：合并部署 AIH-TASK-029（以及以后每次改 `deploy/nginx/billing.conf` 的集成前缀块）之后，在 VPS 上：

```bash
head -c $((1024 * 1024 + 1)) /dev/zero \
  | curl -s -X POST -H 'Content-Type: application/json' --data-binary @- \
      -w '\nHTTP %{http_code}\n' http://127.0.0.1:<BILLING_HTTP_PORT>/api/v1/integration/usage-events
```

期望恰好是：

```text
{"success":false,"data":null,"error":{"code":"PAYLOAD_TOO_LARGE","message":"request body too large"},"retryable":false,"request_id":null}
HTTP 413
```

把这两行原样写进该次任务记录（PR 描述）。得到 HTML 或别的状态码：集成前缀块没生效（`nginx -t`、`docker compose exec
billing_nginx nginx -T | grep -A4 integration` 核对挂载的配置），按「绝不能做什么」最后一条处理，不要先调大上限。

### 绝不能做什么

- **不要删冲突行，也不要改原事件来「消告警」**：冲突表只增（触发器拒绝 UPDATE / DELETE），它是「这个 `event_id` 被别人
  用过」的证据，也是告警的依据；原事件不变是 INV-11 的保证
- **不要把原事件的状态改成 `IDEMPOTENCY_CONFLICT`**：冲突是那个请求的结果，不是原事件的状态；改了它，合法事件就不计费了
- **不要让集成方「改了内容、沿用同一个 `event_id` 再发」**：永远是冲突。更正一律用新的 `event_id`
- **不要为了「先通了再说」调大集成前缀的 `client_max_body_size`**：应用侧单条上限是 16 KiB、批量的另由批量摄取定，
  两层的上限与返回的信封要一起改

---

## 配置项

**首次编写：2026-09-26（AIH-TASK-013）。**这一节收「运维会去改的配置项」，一项一小节。
事实来源写在每一小节开头；与代码不一致时**以代码为准**，并回来改这份文档。

### `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS` —— 集成 API 凭据的轮换重叠期

**依据**：[AIH-TASK-012 设计](design/AIH-TASK-012-integration-access.md)第 2 节「配置」与第 4 节状态表、
`app/core/config.py` 的 `credential_rotation_overlap_seconds`、[api.md](api.md) 的「轮换」一节、
`app/services/integration_access.py` 的 `rotate_credential`、`docker-compose.yml` 的 `x-backend.environment`、
`.env.example` 的「集成 API 凭据」一段。

#### 含义

管理员**轮换**一个集成 API 凭据（`POST …/credentials/{api_key}/rotate`）之后，**旧版本还能继续用于签名校验多少秒**。
重叠期里新旧版本都能通过校验，集成方在这段时间里换上新 `secret`；到点之后旧版本自然不可用 ——
没有定时任务去改状态，校验时按 `valid_until` 判断（`now < valid_until` 才可用）。

| 属性 | 值 |
| --- | --- |
| 配置项（代码里） | `credential_rotation_overlap_seconds` |
| 环境变量 | `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS`（前缀 `BILLING_` 来自 `Settings` 的 `env_prefix`） |
| 默认值 | **`604800` 秒 = 7 天**（Kelvin 2026-09-25 定） |
| 取值规则 | **整数秒，且不小于 0**（`Field(ge=0)`） |
| `0` 的意思 | 轮换即让旧版本**立刻**失效 —— 旧版本的 `valid_until` 被写成轮换那一刻，而校验要求 `now < valid_until` |
| 谁用它 | 只有轮换这一个动作。建凭据、吊销、列表都不读它 |

⚠️ **单位是秒，不是天或小时。**写 `7`、`7d`、`1.5` 都不是你想要的：`7` 是 7 秒；
`7d`、`1.5` 这类非整数与负数过不了配置校验，进程**起不来**（`Settings` 在启动时解析一次，校验失败就抛错）。
常用换算：`3600` = 1 小时，`86400` = 1 天，`604800` = 7 天。

#### 怎么改

在部署目录的 `.env` 里加（或改）一行：

```bash
BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS=3600
```

- 容器里的值来自 `docker-compose.yml` 的 `x-backend.environment` 那一行
  `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS: ${BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS:-604800}`（#124 起）。
  compose 读 `.env` **只为做 `${…}` 插值**，不把 `.env` 整个注入容器 —— 本栈**没有** `env_file:`，
  只有 `environment` 里逐项列出的变量才进容器。这一行的作用就是把 `.env` 里的值转进 api / celery-worker /
  celery-beat 三个后端容器
- `.env` 里不写这一行（或删掉它）= 用 compose 里的默认值 `604800`，与代码默认值一致
- `docker-compose.prod.yml` 只覆盖 `BILLING_ENVIRONMENT` 与 `BILLING_SESSION_COOKIE_SECURE`，**不碰**这一项，
  所以生产与本地走的是同一条转发
- 不经 compose、直接跑 uvicorn 时：`Settings` 从进程环境变量读，也读工作目录下的 `.env`（`env_file=".env"`）。
  `.env` 不进镜像（`.dockerignore` 排除了它），所以**容器里只有 compose 转发这一条路**
- ⚠️ **不要去改 `docker-compose.yml` 里的默认值**来「改配置」。那是仓库文件，下次部署 checkout 会把它换回去；
  而且 `tests/backend/test_compose.py` 钉着那个默认值必须等于代码默认值

#### 改完怎么生效

**必须重建后端容器，只改 `.env` 不会生效。**两层原因：

1. 容器的环境变量是**创建时**定下的。`docker compose restart` 只重启进程、不重新创建容器，拿到的仍是旧值
2. 进程里配置只解析一次：`get_settings()` 带 `lru_cache`，`create_app` 启动时把它放进 `app.state.settings`，
   轮换接口读的就是这一份。运行中的进程不会重读 `.env` 或环境变量

所以改完 `.env` 之后：

```bash
docker compose up -d     # 只重建配置变了的服务：这里是 api / celery-worker / celery-beat
```

⚠️ 这条命令只在**这台主机部署过**时成立：部署成功后两个镜像钉在 `.env` 里（[deployment.md](deployment.md) §9.7）。
灾难恢复的新主机上还没有那两行，不要在那里手工 `up -d`（理由见上面「整台 VPS 没了」第 9 步）。

**确认生效**（只读，不需要真的轮换一次）：

```bash
docker compose exec api python -c \
  'from app.core.config import get_settings; print(get_settings().credential_rotation_overlap_seconds)'
```

打印的是新值才算生效。⚠️ 这条命令起的是一个**新的** Python 进程，它证明的是「容器环境里的值对了」；
API 进程本身是随容器重建的，所以两者一致 —— 前提是你用的是 `up -d` 重建，而不是 `restart`。

#### 对已轮换凭据的影响

⚠️ **改这个配置不回溯。**它只在**轮换那一刻**被读一次，算出 `valid_until = 轮换时刻 + 重叠期` 写进库里。
已经写进库的 `valid_until` 是固定的时刻，不存「重叠期是多少」，改配置、重建容器都不会动它。

| 情形 | 结果 |
| --- | --- |
| 已经轮换过、旧版本正在重叠期里 | **不变**。旧版本仍按当初写入的 `valid_until` 到点失效 —— 把配置从 7 天改成 1 小时，**不会**让它提前失效；改成 0 也不会 |
| 从来没轮换过的凭据（`valid_until` 为空） | 不变，照常可用。直到下一次轮换才会用到新配置 |
| 改配置之后的**下一次轮换** | 按新值算：`截止 = 此刻 + 新重叠期`。这个 `api_key` 下**所有未吊销的旧版本**里，`valid_until` 为空、或晚于这个截止的，一律改成这个截止；本来就早于它的不动。已吊销的版本不动。新版本没有截止 |

最后一行有一个**容易踩到的后果**：把重叠期改短之后再轮换，**上一轮还在重叠期里的更老版本也会被一并截短**。
例：版本 1 因为上一次轮换（重叠期 7 天）还剩 5 天；现在把配置改成 `3600` 再轮换一次 ——
版本 1 与版本 2 的 `valid_until` 都变成「此刻 + 1 小时」。改成 `0` 再轮换，则**所有**未吊销的旧版本当场失效。
反过来，把重叠期改长**不会延长**任何已写入的截止：只有「为空或晚于新截止」的才会被改。

要**提前结束**某个旧版本的重叠期，不是改这个配置 —— 那对它不起作用 —— 而是吊销那个版本
（`POST …/versions/{key_version}/revoke`，立即生效、写审计）。每次轮换改了哪些版本的截止，
都记在那条 `API_KEY_ROTATE` 审计的 `before_state` / `after_state` 里。

#### 绝不能做什么

- **不要把它改成 0 当作「吊销旧版本」的办法。**它不影响已经轮换过的版本，只会让**下一次**轮换对集成方
  毫无缓冲 —— 集成方还没来得及换 `secret`，签名就开始被拒。要撤销一个版本，用吊销接口
- **不要只改 `.env` 就当改好了。**不重建容器，线上还是旧值；而轮换的后果（旧版本何时失效）要到重叠期结束时才看得出来
- **不要在集成方正在切换 `secret` 的时候缩短它再轮换。**上面那张表的最后一行：更老的版本也会被截短

### `BILLING_MONITOR_TOKEN_HOST_FILE` —— 监控令牌文件的宿主机路径（及两个 FX 告警地址）

**依据**：设计闸门 #183 v3（[AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md) §2「内部告警接口」「两个告警维度」、
§6「密钥」、§8「合并后的运维步骤」）、`deploy/monitor.sh` 的 `check_alerts` / `publish_alerts`、`docker-compose.yml` 的
`billing_monitor_token` secret、[deployment.md](deployment.md) §8.4（完整步骤与验证命令）。

#### 含义

`deploy/monitor.sh` 读 `GET /internal/alerts` 要带 Bearer 令牌；令牌在宿主机文件 `secrets/monitor.token` 里，容器经 compose
secret 读**同一份**文件（容器内 `/run/secrets/billing_monitor_token`，compose 写死，不经 `.env`）。
`.env` 里的这一项只告诉**宿主机上的** `monitor.sh` 那个文件在哪。

| 属性 | 值 |
| --- | --- |
| 读它的 | 只有 `deploy/monitor.sh`（字面解析 `.env`，不 source）。应用不读它 |
| 默认值 | 无。不配 = `monitor.sh` 只记 `no BILLING_MONITOR_TOKEN_HOST_FILE configured`，不推告警维度 |
| 取值 | 令牌文件的**绝对路径**，例如 `<部署目录>/secrets/monitor.token` |
| 令牌文件 | `openssl rand -hex 32`（64 个字符）；属主 `10001`、**属组为部署用户的主组**、`0440` |
| 相关的两个键 | `BILLING_HEALTHCHECK_ALERT_FX_FETCH_URL`、`BILLING_HEALTHCHECK_ALERT_FX_STALE_URL`：两个 Healthchecks 检查的 Ping URL。通用规则 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL` |

⚠️ **为什么是 `0440` 而不是其他 secrets 的 `0400`**：`monitor.sh` 由 cron 以部署用户（不是 root）运行，`0400` 它读不到，
告警维度就永远推不出去。容器以 uid 10001 按属主读，部署用户按属组读，其他用户读不到。

#### 怎么改（首次配置 = 设计 §8 的合并后运维步骤）

1. 令牌文件：已有且不少于 32 个字符就不重新生成，只改属主与权限（`sudo chown 10001:"$(id -gn <部署用户>)"`、
   `sudo chmod 0440`）；没有或过短才 `openssl rand -hex 32` 生成。`.env` 加
   `BILLING_MONITOR_TOKEN_HOST_FILE=<部署目录>/secrets/monitor.token`
2. Healthchecks 新建 `ai_billing_hub fx_fetch`、`ai_billing_hub fx_stale` 两个检查（Simple，5 分钟，宽限 15 分钟，Telegram），
   Ping URL 写进 `.env` 的 `BILLING_HEALTHCHECK_ALERT_FX_FETCH_URL` / `BILLING_HEALTHCHECK_ALERT_FX_STALE_URL`
3. 在 worker 容器里手工触发一次拉取，确认 BNM 可达（「无记录」也算可达）：
   `docker compose exec celery-worker python -c "from app.tasks.fx_fetch import fetch; print(fetch())"`
4. 管理员发布第一个汇率版本（见上面「FX 汇率：过期，或没有生效的汇率」的「立刻做什么」）

逐条的命令、核对与验证在 [deployment.md](deployment.md) §8.4。

**轮换令牌**：重新生成文件内容（属主与权限照旧）即可。

#### 改完怎么生效

- `.env` 的这几项**不需要重建任何容器**：`monitor.sh` 每一轮（5 分钟）重新读 `.env`
- 令牌文件的内容**也不需要重启**：应用每次请求都重新读文件
- 验证：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`，日志里出现 `fx_fetch: …` / `fx_stale: …`，两个检查变绿

#### 对已有数据的影响

无。令牌与地址只决定告警能不能送达，不碰汇率版本、拉取记录或计费。

#### 绝不能做什么

- **不要把令牌本身写进 `.env`**，也不要 `cat` 它、贴进对话或截图。`.env` 里只放路径（设计 §6）
- **不要改成 `0444` 或复制第二份令牌**给部署用户：两份要同步轮换，迟早对不上
- **不要删掉文件来「停用」接口。**生产上 compose 的 `file:` secret 缺文件时**部署直接失败**（[deployment.md](deployment.md) §6）
- **不要把心跳地址写进仓库或文档。**知道地址就能伪造「成功」；`config_snapshot.sh` 上传前按键名模式
  `^BILLING_HEALTHCHECK_[A-Z0-9_]*_URL=` 自查，撞上就拒传
