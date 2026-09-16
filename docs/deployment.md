# 部署与恢复方案 — T0.9

> ⚠️ **这份文件绝大多数内容尚未落地**，其中不少**只有在生产主机上才做得了**。
> 第 10 节的七件事**已由 Kelvin 拍板**（2026-09-13），答案已并入各节。
>
> 依据：spec §94、§95、§98、§98.1、§99、§112、§123；
> [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)、
> [ADR-0004](adr/ADR-0004-credential-encryption.md)。
> 容量数据来自 [perf-baseline.md](perf-baseline.md)（T0.10）。

---

## 1. 现在有什么、缺什么

| | 状态 |
| --- | --- |
| 七服务 compose 栈 | ✅ T0.6 / T0.7，本地可跑通 |
| 迁移、认证、密码重置、outbox | ✅ T0.1–T0.8 |
| CI（六项必需检查 + 受保护 `main`） | ✅ T0.2 起 |
| 容量基线 | ✅ T0.10 |
| **生产主机上的任何东西** | ❌ 一件都没有 |
| **部署流水线（CD）** | ⚠️ 骨架已写、脚本本地演练过，**但从未在真实 VPS 上跑过**（§9） |
| **备份 / 恢复 / RPO·RTO** | ⚠️ 全量（每日）+ binlog 离机（每分钟，破 RPO 以 err 级别报出）+ PITR 恢复 + cron 已落地，**本地整链演练通过**（§5.2.1、§5.2.3–§5.2.5）；**2026-09-14 在 VPS 上实测 binlog 离机 + PITR 恢复通过**（§5.2.4 末尾），主密钥真解密演练通过（2026-09-15）；每日全量的 cron 已自动跑出过一份（2026-09-14 19:17，按旧时间）；备份与演练的心跳已在生产接通（Healthchecks.io + Telegram，§5.2.6）；R2 保留期规则已配；每周自动恢复演练已上线（§5.2.7） |
| **生产日志与告警** | ❌ 第 7、8 节 |

~~⚠️ `docker-compose.yml` 目前没有任何资源限制~~ —— ✅ **已做**（见 §3.3），ADR-0002 那条收口条件关闭。原文留档：
单 VPS 上这不是洁癖：一个失控的 celery worker 能把 MySQL 挤出内存，而表现出来的是
「数据库莫名其妙重启」。

---

## 2. 生产拓扑（§98、§99）

spec §99 钉死了形态：**单 VPS + Docker Compose，七个服务，没有独立的 staging**。
ADR-0002 钉死了隔离：**专用 MySQL 与 Redis 容器，不接 `vps_infra` 的共享实例**。

所以拓扑本身没有可选项，要定的是**它怎么接到那台机器已有的东西上**：

```text
Internet
  │
  ▼
infra_nginx（vps_infra，*.acuventech.com 通配符证书，已在跑）   ← **决策 ①A（已定）**
  │  least-privilege 接口，§98 允许但要求「documented」
  ▼
billing nginx（本项目，现在监听 80）
  ├── /            → frontend
  ├── /api/、/healthz → api
  └── /readyz      → api（仅私有网段）
        │
        ├── mysql（专用容器 + 专用卷）
        └── redis（专用容器 + 专用卷）
```

### 2.1 接入 `proxy_net`（已落地并本地实测）

VPS 上的约定（`vps_infra` 与 `erp_os` 等项目）：`infra_nginx` 与各项目挂在共享网络
**`proxy_net`** 上，按**服务名**互相访问；TLS 在 `infra_nginx` 终止；各项目**不发布公网端口**。

本平台照这个接，三处改动：

| 改动 | 为什么 |
| --- | --- |
| 边缘服务改名 `nginx` → **`billing_nginx`** | compose 把服务名注册成所挂网络上的 DNS 别名。`vps_infra` 自己的服务就叫 `nginx`，同名会让 `proxy_net` 上一个名字解析到两个容器。`erp_os` 的 compose 里记着同一个坑 |
| **只有** `billing_nginx` 挂 `proxy_net` | 那张网上还有另外八个项目。api / mysql / redis 一挂上去，任何一个项目的容器被攻破都能直接够到我们的数据库和未经限流的 api |
| 宿主机端口**默认只绑 127.0.0.1** | ⚠️ **Docker 发布的端口会绕过 UFW**。绑所有网卡的话，上线后任何人都能用 `http://<VPS>:8080` 明文直连登录接口，完全绕开 HTTPS |

生产上的挂接放在 [`docker-compose.prod.yml`](../docker-compose.prod.yml)，本地开发不用它
（本地没有 `proxy_net`，而它是 external 的，缺了 `up` 会直接失败）。VPS 的 `.env` 加两行：

```
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
COMPOSE_PATH_SEPARATOR=:
```

⚠️ 第二行不是多余的：分隔符在 Windows 上默认是 `;`。本地实测不加的话整串被当成一个文件名。

⚠️ 覆盖文件里 `billing_nginx` 的 `networks:` **必须同时列出 `default`**。一个服务一旦写了
`networks:`，compose 就不再自动挂默认网络 —— 只写 `proxy_net` 的话，部署成功、健康检查
也过（它只问 nginx 自己），但每个请求都是 502。

**本地模拟实测**（建一个 `proxy_net`，用挂在上面的容器冒充 `infra_nginx`）：

| 检查 | 结果 |
| --- | --- |
| 按服务名访问 `billing_nginx/healthz` | ✅ 正常响应 |
| 直连 `api:8000` | ✅ `bad address` —— 解析都解析不到 |
| 直连 `mysql:3306` | ✅ 解析不到 |
| `proxy_net` 上的 `nginx` 这个名字 | ✅ 不存在，没有撞名 |
| 宿主机端口绑定 | ✅ `127.0.0.1:8080` |
| 带 `X-Forwarded-For: 203.0.113.77` 访问 | ✅ 访问日志记为 `203.0.113.77`，不是代理地址 |

⚠️ `infra_nginx` 那一侧的转发配置含**真实域名**，按仓库规则**不进这个公开仓库**，
由 Kelvin 放进 `vps_infra`。要点：`proxy_pass` 指向变量 `billing_nginx:80`（与其它项目
同一写法），并且在这一跳用 `$remote_addr` **覆盖**而不是追加 `X-Forwarded-For`。

### 2.2 为什么保留本项目自己的边缘 nginx（Kelvin 2026-09-14 选 A）

同一台 VPS 上的其它项目是 `infra_nginx` **直接**转发到后端容器。本平台多一层：

```text
浏览器 → Cloudflare → infra_nginx（共享：TLS、按域名分发）→ billing_nginx（本项目）→ api / frontend
```

首次部署后复议过要不要统一成「只用 `infra_nginx`」。两种做法的区别：

| | **A. 保留 billing_nginx（选定）** | B. 统一只用 infra_nginx |
| --- | --- | --- |
| 登录限流（spec §53 的主控） | 在 billing_nginx，配置进本仓库、有测试、走审查 | 挪进 infra_nginx；`limit_req_zone` 还必须写进**共享的** `nginx.conf` |
| `/readyz` 对外封锁、安全响应头、JSON 访问日志（§94）、上游超时、`real_ip` | 同上 | 同上，全部挪进 infra_nginx |
| 这些配置放在哪 | 本仓库，随 CD 部署 | `vps_infra/nginx/conf.d/` 的站点文件 —— ⚠️ **被 vps_infra 的 `.gitignore` 排除**：没有版本记录、没有测试、没有审查，只能在 VPS 上手工改 |
| 改错的影响面 | 只影响本项目；`deploy.sh` 先 `nginx -t` 再 reload，无效就回滚 | 共享的 infra_nginx；reload 失败会保留旧配置，但误用 restart 会让**同机所有站点一起挂** |
| 网络隔离 | 只有 billing_nginx 挂 `proxy_net` | api 与 frontend 都要挂 `proxy_net`：另外八个项目的容器能**绕过限流直连 API** |
| 代价 | 多一个容器（限额 64 MB）；多一跳代理（本机网络内，可忽略）；配置变更要 reload（见下） | 少一个容器；与其它项目写法一致 |

**选 A 的理由**：`infra_nginx` 只做各项目**共有**的事（TLS、按域名分发）；计费平台**特有**的安全控制
留在自己的仓库里。它们守的是钱包调整、定价发布、退款的前门，必须有版本、测试与审查 ——
放进一个不在 git 里的共享文件，下一次改错不会有任何人发现。这与 2026-09-13 的决策 ① 是同一条分工。

⚠️ **A 的一个后果，已处理**：配置文件是从部署目录挂进 billing_nginx 的，而它的镜像与 compose
定义很少变，`docker compose up -d` 不会重建它 —— **只改了 nginx 配置的部署，不 reload 就不会生效**。
首次加安全响应头时生产上就是这样：部署成功、冒烟通过，外网一个头都没有。现在 `deploy.sh`
每次部署都先 `nginx -t` 再 reload（无效算部署失败、走回滚），冒烟额外确认 `X-Frame-Options: DENY`
—— 漏掉 reload 时这是唯一能发现的地方。本地演练四种情况都验证过（见 §9.2）。

⚠️ **光 reload 还不够，第一版上线就被冒烟拦下回滚了**（run 34815465122）：`nginx -t` 与 reload
都成功，外网仍然没有安全头。原因是配置**按单个文件**挂进容器，而单文件 bind mount 绑的是
inode 不是路径 —— `git checkout` 用新文件替换旧文件后，容器里看到的永远是旧的，reload 读到的
也是旧内容。改成**挂整个 `deploy/nginx/` 目录**。Linux 上（docker-in-docker）按生产的顺序复现：
旧配置起 nginx → 按 git 的方式替换文件 → `nginx -t` + reload —— 挂文件的仍无 `X-Frame-Options`，
挂目录的出现 `DENY`。⚠️ **Windows 的 Docker Desktop 复现不出来**（挂文件也能读到新内容），
这正是本地演练没发现的原因；`tests/backend/test_compose.py` 钉着挂载方式。

### ⚠️ 这个拓扑有一个后果，不是可选的

**已定走 `infra_nginx`（①A），所以 `real_ip_header` + `set_real_ip_from`
不是「可以加」，是必须加**。否则本项目 nginx 看到的每个请求的对端都是
`infra_nginx`，于是：

- `limit_req`（`billing_auth`，10r/m）把**所有客户端算成同一个来源** —— 任何人打到
  第 21 个认证请求，**所有人**一起拿 429
- `/readyz` 的 `allow 10.0.0.0/8` 之类**形同虚设** —— 那层代理的地址本来就在私有网段里，
  于是等于对全网开放，而它的响应体逐个报出依赖状态
- 审计日志里的 `ip_address` 全是同一个值，取证时没用

应用侧已经准备好了（`app/core/clientip.py` + `BILLING_TRUSTED_PROXIES`，只在可信代理
后面才采信 `X-Forwarded-For`），**缺的就是边缘那一层**。三处一并收口。

---

## 3. 进程模型（T0.10 基线的直接输出）

现状：`Dockerfile` 的 CMD 是 `uvicorn app.main:app`，**没有 `--workers`**，也就是单进程。

### ⚠️ 结论：保持单进程，不加 `--workers`（决策 ②A，已定）

T0.10 在开发机上实测「每条路径都在并发 8 饱和、20 核用不上」，据此本来要考虑加
worker。**但生产 VPS 只有 1 核**（第 3.1 节），那条结论**不转移**：1 核上加 worker
几乎不提高吞吐，只会多占内存。

**这正是「基线必须在目标机器上重跑」的意义** —— 不重跑的话，我们会照着一台 20 核
机器的结论去配一台 1 核的机器。

下面关于兜底限流的分析仍然留着：将来机器升配、真要加 worker 时，它是前置知识。

### 3.1 ⚠️ 生产 VPS 的实测规格（2026-09-13）

| 项 | 值 |
| --- | --- |
| CPU | **1 核**（load 0.24–0.34，即已被现有负载占掉约 30%） |
| 内存 | 3.6 GB 总量，**可用 1.8 GB**；⚠️ **swap 已用 596 MB** |
| 磁盘 | 29 GB，**剩 12 GB**（Docker 镜像已占 7.8 GB，卷 1.5 GB） |
| 已有负载 | 同一台机器上还跑着 `vps_infra` 与另外七个项目 |

这个数把 ADR-0002 那条挂了很久的收口条件（「VPS 容量核算把多出来的一套算进去」）
补上了，而答案是：**按现状勉强装得下，余量很薄**。

本平台这一套的估算（调优后）：

| 容器 | 估算常驻 |
| --- | --- |
| mysql（`innodb_buffer_pool_size` 调到 128M） | ~450 MB |
| api（含 Argon2 峰值） | ~250 MB |
| celery-worker | ~180 MB |
| celery-beat | ~80 MB |
| redis | ~50 MB |
| nginx + frontend | ~40 MB |
| **合计** | **~1.05 GB** |

⚠️ 而 Argon2 每次并发哈希要 **64 MiB**（T0.10 实测）：**10 个人同时登录就是 640 MB**，
直接吃掉剩下的余量。机器本来就在 swap。

**决策 ③：升配 VPS 内存（选项 B，已定）。**在升配完成之前不要上线 ——
按现状硬上能跑，但登录高峰会打到 swap，而那时候的表现是「整台机器一起变慢」，
包括同机的另外七个项目。

✅ **已升配**（2026-09-15 核对）：**2 核 / 7.3 GB 内存**，swap 4 GB 只用 10 MB，可用内存约 4.2 GB。
上表那几行（1 核 / 3.6 GB / 已在 swap）留作升配前的记录。⚠️ 升配改变了 §3 的前提（1 核 → 2 核），
「保持单进程」的结论与容量基线都要在这台机器上重跑后再确认（§11 最后一条）。

⚠️ **磁盘只增不减，而增的那一部分是我们自己造的**（2026-09-16 实测）：根分区 19 GB / 29 GB
（**68%**，告警线 80%），Docker 镜像从 9-13 的 7.8 GB 涨到 **11.02 GB** —— 其中
`ai_billing_hub` 的 api + frontend 攒了 **8 个 commit SHA 版本**，只有 1 个在跑。

→ **两件事之后，同日复测**：`prune_old_images` 上线后本项目镜像从 **16 个降到 6 个**（3 代 × api + frontend），Docker 镜像总量 11.02 GB → **6.47 GB**；Kelvin 同时把根分区扩到 **48 GB**，于是水位从 68% 落到 **31%**。⚠️ 扩容只是把时间买回来，**单调增长的那一部分是被回收窗口挡住的**，不是被磁盘大小挡住的。

根因是 `deploy.sh` 里那句 `docker image prune -f` **清不掉它们**：prune 只清悬空（无标签）
镜像，而每次部署拉进来的都带 commit SHA 标签。也就是说，脚本里写着「不清的话它会
慢慢把磁盘吃光」的那道防护，拦的恰恰不是在长的那一类。已补 `prune_old_images`
（保留窗口 `BILLING_IMAGE_KEEP`，默认 3；当前版本、回滚目标、任何被容器引用的镜像
都额外再挡一道）。

⚠️ **这台机器上不止我们一个项目**，所以保留窗口只管本项目的两个镜像仓库。别的项目
的遗留镜像、以及一个 209 MB 的悬空卷（里面是 `ai_chatbot` 的 MySQL 数据目录，**不是
垃圾**）都不在这个脚本的职责范围内 —— 那要人来判断，绝不能交给 `docker system prune`。

### 3.2 ⚠️ 限额必须和进程自己的并发数配对钉死

加限额时实测抓到的，两处都是同一类：**限额是外面的天花板，而进程自己的并发数
决定它要多少内存 —— 两者不配对，换台机器就变成「随机重启」。**

| 进程 | 默认行为 | 后果 | 已钉成 |
| --- | --- | --- | --- |
| celery worker | 并发 = **宿主机 CPU 核数**，每个 prefork 子进程都 import 一遍应用 | 本地（20 核）加上 384m 限额后起了 20 个子进程，**崩溃重启 4 次**；而 `docker inspect` 的 `OOMKilled` 还是 **false**（被杀的是子进程，主进程自己退了 0）—— 一条完全指不回原因的现象 | `--concurrency=2` |
| MySQL | buffer pool 默认 128M，但换台大内存机器会自己长大 | 撞限额时表现成「数据库随机重启」 | `--innodb-buffer-pool-size=128M` |
| Redis | 不知道天花板，一直收到被 OOM kill | 进程整个消失；投递触发丢掉（INV-14 会补，但每次要等最多一分钟） | `maxmemory 64mb` + `allkeys-lru` |

⚠️ **`OOMKilled=false` 不等于「没有内存问题」** —— 它只说明**主进程**没被杀。
这一条值得记住：排障时看到 false 很容易就把内存排除掉了。

### 3.3 实测生效的限额

| 服务 | mem_limit | cpus |
| --- | ---: | ---: |
| mysql | 512m | 0.6 |
| api | 384m | 0.5 |
| celery-worker | 384m | 0.5 |
| celery-beat | 128m | 0.2 |
| redis | 96m | 0.2 |
| nginx | 64m | 0.3 |
| frontend | 64m | 0.2 |
| **合计** | **1632m** | |

⚠️ 合计 1.63 GB，而升配前可用只有 1.8 GB。**限额是上限不是预留**，正常占用远低于
这个数；但它说明升配这件事不是可选的（决策 ③）。

⚠️ 用 `mem_limit` / `cpus` 短式而**不是** `deploy.resources.limits`。两种写法在
compose v2 下**都真的生效**（实测 `docker inspect` 的 `HostConfig.Memory` 都被设上了），
选短式只是为了不和 `secrets` 那条「swarm-only 选项会被静默忽略」的教训混淆。

⚠️ 磁盘同样要留意：12 GB 要装本平台的镜像（~1.5 GB）+ MySQL 数据 + **binlog**。
binlog 是 RPO ≤ 5 分钟的前提，必须保留，所以**必须给它一个明确的上限**，
否则它会慢慢把磁盘吃光 —— 而磁盘写满时 MySQL 会直接停止写入。

### ⚠️ 但「加 `--workers`」不是一个免费的开关

进程内那层兜底限流（`app/core/ratelimit.py` 的 `TokenBucket`）的状态在**进程内存**里。
跑 N 个 worker，同一个来源就有 N 个独立的桶 —— **兜底额度直接变成 N 倍**。

主控那层在边缘 nginx（`limit_req`），不受影响，所以这不是阻断项。但它意味着：
**调进程数的同时必须知道自己把兜底削弱了多少**，而不是事后才发现。

三条路，见决策 ②：

| | 做法 | 代价 |
| --- | --- | --- |
| A | 接受兜底被稀释，只依赖 nginx 那层 | 「不经 nginx 直连 uvicorn」这条路上的保护按 N 倍放宽 |
| B | 把每 worker 的额度除以 N | 要多一个「我是几个 worker 之一」的配置项，配错就是限流过严或过松，而且**错法是静默的** |
| C | 限流状态放 Redis | **已被 `ratelimit.py` 明确否决**：Redis 丢了会 fail-open，一个丢了就失效的安全控制比没有更危险 |

另外还有**内存**这条硬约束：Argon2 每次并发哈希要 **64 MiB**（T0.10 实测）。
N 个 worker × 并发登录数 × 64 MiB 是个能把单 VPS 吃光的量，定 worker 数时要一起算。

---

## 4. 边缘代理与 TLS

三件 T0.6 / T0.7 / T0.8a 欠下的，一并收口：

| 项 | 现状 | 要做成 |
| --- | --- | --- |
| 真实客户端地址 | ~~无 `real_ip` 配置~~ | ✅ **已做**（容器内 `nginx -T` 实测生效）。⚠️ **刻意不开 `real_ip_recursive`**：默认 off 时取 X-Forwarded-For 的**最后一个**地址，那是上游代理亲自追加、客户端伪造不了的；开了 recursive 反而要依赖可信名单写得够准。⚠️ 当前填的是私有网段，**上线前按实际拓扑收窄** |
| 上游超时 | ~~`proxy_connect_timeout` 挂着默认 **60 秒**~~ | ✅ **已做**：`proxy_connect_timeout 2s` + `resolver_timeout 2s`。**实测 api 停机时从 3.96 秒降到 2.00 秒**，且稳定落在 resolver 超时上 —— 停机现在是「报错」而不是「卡住」 |
| TLS / 域名 | 只监听 80 | **①A 已定**：TLS 在 `infra_nginx` 那层终止，本层只收内网流量、不自己管证书 |
| nginx 运行用户 | 官方镜像默认（master 是 root） | **决策 ④：不换**（已定）。理由：选了 ①A 之后本层只收 `infra_nginx` 转进来的内网流量，风险面小。⚠️ 这是一次**有意识的选择**，不是忘了做 |

---

## 5. 备份与恢复（§98.1）

spec 已经给了硬指标，**不是我们来定的**：

```text
RPO <= 5 minutes      —— 最多容忍丢 5 分钟的已提交数据
RTO <= 4 hours        —— 从「发现挂了」到「重新可用」不超过 4 小时
```

### 5.1 从指标反推出来的架构

**RPO ≤ 5 分钟**只有一种满足方式（§98.1 也点名了）：**全量备份 + binlog 的
point-in-time recovery**，而 binlog 必须**至少每 5 分钟**离机一次。

**决策 ⑦（已定）：离机存储用 Cloudflare R2**（S3 兼容；**出流量免费**，这对要反复
拉全量的恢复演练是实打实的好处）。

| 层 | 频率 | 去处 |
| --- | --- | --- |
| MySQL 全量（`mysqldump --single-transaction`） | 每日一次 | 加密后传 R2 |
| MySQL binlog | **≤ 5 分钟一次**推送 | 加密后传 R2 |
| 部署配置（compose、nginx 配置） | 变更后 | 加密后传 R2。⚠️ **不含任何密钥** |
| 不可变文档文件 | 变更后 | R2，独立桶。Phase 3 才有，先留位 |
| **加密主密钥** | 轮换时 | ⚠️ **不放 R2** —— 见下面 |

**决策 ⑦a（已定）：上传前自己加密**，不依赖 R2 的服务端加密。理由：服务端加密的
密钥在 Cloudflare 手里，那样备份对它是明文。

**决策 ⑦b（已定）：主密钥不进 R2，走密码管理器 + 离线副本。**

⚠️ **主密钥和数据库备份放在一起，等于两者都没有加密**（ADR-0004 第 3 节）。
而「同一个 R2 账号换个桶」只是两个桶，**不是两套访问控制** —— 一份泄漏的 R2 凭据
同时拿到两样东西。

具体做法：

1. 密码管理器（Bitwarden 等）里存一条 Secure Note
2. **密码管理器自己的主密码写在纸上**，放实体位置 —— 否则是死循环
3. 再留一份离线副本（加密 U 盘 / 打印件放保险位置）

为什么要两份以上：R2 挂了还有密码管理器，密码管理器进不去还有离线副本。
⚠️ **三处都不在 VPS 上、也不在 R2 上。**

⚠️ 主密钥**绝不**进仓库、工单、聊天或任何共享文档。轮换之后**老密钥要继续留着** ——
`app/core/crypto.py` 支持多版本钥匙串，但**目前没有重包裹任务**，老数据还靠老密钥解。

**RTO ≤ 4 小时**要求恢复步骤是**演练过的**而不是写下来的。四小时看着宽，
但它要装下：发现 → 决策 → 起新主机 → 拉备份 → 解密 → 导入 → 重放 binlog →
核对 → 切流量。没演练过的流程，光「解密的密码在哪」就能吃掉一小时。

### 5.2 §98.1 要求而现在一件都没有的

- [x] 备份成功与**新鲜度**的自动监控（§95 的 `backup age and backup failure`）—— 2026-09-15：Healthchecks.io 三个检查（binlog / 全量 / 每周演练）已建并写入 VPS 的 `.env`，三个都收到生产上的真实心跳，Telegram 收到 DOWN / UP 测试通知（§5.2.6 末尾）
- [ ] **季度恢复演练**，且要验证钱包、账本、用量事件、支付、对账单、文档的完整性
- [x] 书面的**灾难恢复顺序**与恢复后对账 —— 2026-09-16 写进 [runbook](runbook.md)「整台 VPS 没了（灾难恢复）」：11 步顺序（含「先分清整机不可用还是服务挂了」）、对账的四条 SQL 与主密钥可用性验证、要留下 RPO / RTO 实测值。⚠️ 钱包 / 账本 / 用量 / 支付那几项对账**做不了**（表要到 Phase 1 才有），已在该节写明
- [x] 保留期（与 §112 的财务保留期挂钩，见决策 ⑤）—— R2 上每日全量与 binlog 35 天、月备份 12 个月、年备份永久（Kelvin 2026-09-14），见 §5.2.1 末尾

⚠️ **「备份在跑」和「备份能恢复」是两件事。**只有第二件算数，而它只能靠演练证明。

### 5.2.1 全量备份脚本（已落地）

[`deploy/backup.sh`](../deploy/backup.sh)：导出 → 加密 → **验证** → 上传 → 本地轮转。

⚠️ **验证那一步是这个脚本存在的理由。**它把刚加密出来的文件解回来、和原始 dump
**逐字节比对**，不通过就拒绝上传。一个口令配错或写盘出错的备份，和好的备份长得
一模一样：都是一个大小合理的文件静静躺在桶里 —— 直到真出事那天才发现打不开。

三道检查各挡不同的东西，缺一不可：

| 检查 | 挡什么 | 为什么不能省 |
| --- | --- | --- |
| `-- Dump completed` 标记 | dump 被截断（进程被杀 / 磁盘满 / 连接断） | ⚠️ **往返比对抓不到这一种** —— 截断发生在加密之前，明文和解回来的一致。变异验证过：去掉它，截断的 dump 一路畅通 |
| 解密 + `cmp` 逐字节 | 加密、写盘、存储层出错 | 变异验证过：验证用错口令时脚本拒绝上传并非零退出 |
| R2 未配置 → **显式失败** | 「只在本地留备份」 | 那满足不了 §98.1 的 off-VPS 要求，而它看起来一切正常 —— 每天都「成功」，直到机器整个没了 |

⚠️ `--source-data=2` 把**导出那一刻的 binlog 位置**写进 dump 的注释里，这是 PITR
的锚点。没有它，你有一份全量和一堆 binlog，却不知道该从哪一条开始重放 ——
少放会丢数据，多放会重复。本地实测记到的是
`SOURCE_LOG_FILE='binlog.000002', SOURCE_LOG_POS=8326`。

**本地实测结果**：dump 9952 字节 → 加密 → 解回来逐字节一致 → 独立解密确认含
8 张表、目标数据行、完整结束标记；错口令被拒绝。

⚠️ **上传确认之后 `FLUSH BINARY LOGS` 一次，关掉锚点所在的 binlog。**锚点落在正在写的文件上；
之后若一直没有写入，`binlog_ship.sh` 按设计不 FLUSH，那个文件就永远到不了 R2，**恢复最新这份
全量会被拒绝**（报 anchor binlog not shipped）。夜里、周末正是这种状态。VPS 恢复演练撞到过
（2026-09-15：19:17 那份全量的锚点 `binlog.000005:158`，之后无写入）。

**端到端验证**（真 MySQL 8.4 + MinIO，脚本本体原样运行）：写一行 → 推送 → 全量 → 不写入 → 推送两轮 → 恢复最新全量：

| | 旧版 | 修复后 |
| --- | --- | --- |
| 全量之后的推送 | 两轮都 `no writes … nothing to ship`，锚点 `binlog.000003` 留在本机 | 下一轮推走 `binlog.000003`、`000004`，再下一轮才空闲 |
| 恢复最新全量 | `the anchor binlog binlog.000003 is not in R2`，拒绝 | 重放 `000003`–`000004`，1 行，与线上一致 |
| 之后写第 2 行、推送、再恢复同一份 | 2 行 | 2 行 |

代价：每份全量多推两个几百字节的 binlog。

#### R2 上的保留期（Kelvin 2026-09-14 定）

| 前缀 | 内容 | 保留 | 谁删 |
| --- | --- | --- | --- |
| `full/billing-<UTC 时间戳>.sql.enc` | 每日全量 | **35 天** | R2 lifecycle |
| `binlog/<server_uuid>/binlog.NNNNNN.enc` | binlog | **35 天** | R2 lifecycle |
| `monthly/billing-YYYYMM.sql.enc` | 当月第一份成功的全量 | **366 天**（12 个月，闰年不少一天） | R2 lifecycle |
| `yearly/billing-YYYY.sql.enc` | 当年第一份成功的全量 | **永久** | 无规则 |
| `logs/billing-logs-<UTC 时间戳>.tar.gz.enc` | 每天一份的容器日志归档（§7.1） | **35 天** | R2 lifecycle |

⚠️ **R2 的 lifecycle 只能按「前缀 + 上传后天数」删，挑不出「1 号那一份」**，所以 `backup.sh`
在每日全量上传确认之后，把它在桶内复制到 `monthly/` / `yearly/`：

- **判据是「这个月 / 这一年还没有」，不是「今天是 1 号」**：1 号那一轮失败时第二天自动补上，不会整月缺一份
- **已存在就绝不覆盖**：lifecycle 按上传时间计天数，覆盖一次等于重新计时，还会把月初快照换成更晚的
- **「查有没有 + 复制」在脚本内的 `flock` 里做**（Codex #51 R1）：两轮同时跑（cron 那轮没完、有人手工再跑）会都查到「没有」、后者覆盖前者；cron 行上的锁管不到手工运行。拿不到锁就等（最多 5 分钟），不跳过。本地验证：两轮并发，只有先拿到锁的那轮 `kept as`，另一轮等锁后报 `already exists`
- **按马来西亚日期归属**（固定 +8 小时，不依赖主机 tzdata）：03:17 那一轮 = UTC 前一天 19:17，按 UTC 算「10 月的月备份」会晚一天、多带一整天。所以 `monthly/billing-202610` 是马来西亚 10 月 1 日凌晨那份，内容是**9 月的完整月末**
- 存在与否用 `list-objects-v2` 按精确 key 过滤判断，查询出错就停，不会把「查不到」当成「不存在」去覆盖。⚠️ 第一版用 `--query KeyCount`：aws-cli 自动分页后这个字段恒为 `None`，**月备份一次都没建出来，每轮却都报 already exists** —— 本地 MinIO 演练抓到

⚠️ **35 天删 binlog 不会让 35 天内的每日全量失去 PITR**：一份全量需要的 binlog 都是在它**之后**才上传的，全量还在时它们一定也在。
月 / 年备份的 binlog 则一定早已过期，恢复它们要显式只恢复全量（§5.2.4）。

⚠️ **备份脚本不删 R2 上的任何东西**，也不配 lifecycle：一个每天自动运行的东西不该有删除备份、改桶设置的权限。
规则由人在 Cloudflare 控制台配（R2 → 桶 → Settings → Object lifecycle rules → Add rule，
条件选 **Prefix**、动作选 **Delete uploaded objects after N days**）：

| 规则名 | Prefix | 天数 |
| --- | --- | --- |
| `daily-full-35d` | `full/` | 35 |
| `binlog-35d` | `binlog/` | 35 |
| `monthly-full-366d` | `monthly/` | 366 |
| `logs-35d` | `logs/` | 35 |

⚠️ **不要建一条前缀为空、或覆盖 `yearly/` 的规则**：那会连永久保留的年备份一起删掉。

**生产上已配**（2026-09-15 三条，2026-09-16 补上 `logs-35d`；Kelvin 在控制台配、截图核对）：`full/` 35 天、`binlog/` 35 天、`monthly/` 366 天、`logs/` 35 天，
外加 R2 自带的「未完成分片上传 7 天中止」；`yearly/` 不受任何规则影响。⚠️ 第一次配时 `monthly` 的前缀被填成了
`monthly/366 days`（天数混进了前缀框）—— 那条规则一个对象都匹配不上，月备份会永远累积。**配完要逐条看前缀一栏**。
同日生产上第一次按新脚本跑全量：建出 `monthly/billing-202609.sql.enc` 与 `yearly/billing-2026.sql.enc`，第二次报 `already exists`。

**本地验证**（MinIO 冒充 R2、替身 compose 冒充 MySQL）：首轮建出 `monthly/` 与 `yearly/` 且大小回读一致；第二轮报 `already exists`、两者 ETag 不变；
跨月边界 UTC `2026-09-30T15:59:59Z` → `202609`、`16:00:00Z` → `202610`、`2026-12-31T19:17:00Z` → `202701`。

### 5.2.2 binlog 的磁盘约束

⚠️ **升配内存并没有加磁盘** —— 还是 12 GB 可用。MySQL 8.4 默认保留 binlog
**30 天**、单文件 **1 GB**（实测确认），按默认值走它迟早把磁盘吃光，而
**磁盘写满时 MySQL 直接停止写入**。

改成本地留 **3 天**、单文件 **128 MB**：本地只需要够从最近一次全量做一次 PITR
（全量每天一次，3 天有两天余量），**长期保留是 R2 那一侧的事**。

⚠️ Phase 2 的用量摄取接上来之后 binlog 的增速会完全不同，那时要重新算这个数。

### 5.2.3 binlog 离机（已落地，本地演练通过）

⚠️ **RPO ≤ 5 分钟靠的是它，不是全量。**第一版只有全量脚本，而且只能手工跑 ——
Codex #42 R1 指出：机器没了就丢最多一整天的计费数据。

[`deploy/binlog_ship.sh`](../deploy/binlog_ship.sh)，每一轮：

1. `FLUSH BINARY LOGS` —— 让正在写的 binlog 关闭。⚠️ 不 FLUSH 的话 128 MB 的文件要写满才轮转，写入少时那是好几天
2. 已关闭、未推过的文件逐个：从容器取出 → **与 MySQL 记录的大小比对** → 加密 → 解回来逐字节比对 → 上传 → 回读远端大小 → 进度前进
3. 编号**断档就显式失败**（最常见成因：脚本停了超过 3 天，文件在推出去之前被 MySQL 清掉）

⚠️ 远端按 **`server_uuid` 分目录**（`binlog/<uuid>/binlog.000123.enc`）。换主机（恢复之后正是
这种情况）编号从 000001 重来，不分目录会覆盖旧链。全量 dump 里也写进这一行 uuid，
恢复时靠它找链。

### 5.2.4 按时间点恢复（已落地，本地演练通过）

[`deploy/restore.sh`](../deploy/restore.sh) `<全量对象> <新库名> ['YYYY-MM-DD HH:MM:SS' UTC]`：

1. 拉回全量、解密、读出锚点与 uuid
2. **在动任何库之前**列出 binlog 链，必须从锚点那个文件开始、编号连续 —— 缺一个就拒绝
3. 建新库、导入全量（`sql_log_bin=0`）
4. 从锚点位置重放整条链，给了停止时间就停在那一刻

**恢复月备份 / 年备份**：它们的 binlog 早已过了 35 天保留期，默认模式会拒绝（报错里带着下面这个开关的提示）。
接受「只恢复到备份那一刻」时显式加开关：

```bash
BILLING_RESTORE_FULL_ONLY=1 bash deploy/restore.sh monthly/billing-202610.sql.enc billing_incident
```

⚠️ **binlog 链不全时脚本绝不自动退化成只恢复全量。**真出事时如果 binlog 推送早就悄悄坏了，
自动退化会跳过重放、照样报「恢复完成」，静默丢掉最多一整天 —— 丢数据的决定必须是人做的。
开关与停止时间不能同时给（没有 binlog 就停不到某一刻）；只接受 `0` / `1`；开头与结尾各打一行警告写明恢复点。

本地验证：默认模式恢复月备份被拒、**目标库没被创建**；开关 + 停止时间被拒；`BILLING_RESTORE_FULL_ONLY=yes` 被拒；
开关模式恢复成功、全程没有调用 `mysqlbinlog`。

⚠️ **官方 `mysql:8.4` 镜像里没有 `mysqlbinlog`**（只装了 server-minimal，它的源里也装不上
client 包）。解码放在一次性的 `percona/percona-server:8.4.11-11` 容器里 —— 与服务端同为
8.4.11，**只读文件、不连数据库**，SQL 仍交给我们自己那台 mysql 执行。镜像约 440 MB，
只在恢复时才拉。⚠️ 升级 mysql 版本时这个标签要跟着改。

**本地整链演练**（空栈部署 → MinIO 冒充 R2）：

| 场景 | 结果 |
| --- | --- |
| 全量之前写 1 行、之后写 1 行、停止点之后再写 1 行并改掉第 1 行 | —— |
| 恢复到最新 | ✅ 3 行，含那次 UPDATE |
| 恢复到停止点 | ✅ 2 行，UPDATE **没有**生效 |
| 恢复期间生产库 | ✅ 未受影响 |
| 同名演练库再演练一次 | ✅ 仍是 3 行 |
| 删掉链中间一个 binlog | ✅ 拒绝恢复，**目标库根本没被创建** |
| 残留明文 | ✅ 没有（只剩密文与进度记录） |

演练抓到三个缺陷，全部是「每一轮都报成功」的那种：

- ⚠️ 推送循环从 stdin 读清单，而循环体里的 `docker compose exec` **吞掉了 stdin** —— 每轮只推第一个文件，积压越来越多
- ⚠️ 第一版在生产 MySQL 容器里调 `mysqlbinlog`，exit 127（见上）
- ⚠️ 导入不关 `sql_log_bin` 的话，演练本身的导入会进 binlog；变异验证过：同名演练第二次在重放时失败（`ERROR 1007`）

**VPS 实测**（2026-09-14，生产主机、真 R2，恢复进一次性库 `billing_restore_drill`）：

| 项 | 结果 |
| --- | --- |
| binlog 离机 cron | ✅ 每分钟一轮：首轮推 `000001`、`000002`；之后三轮无写入、不 FLUSH；有写入的那轮推 `000003`。`journalctl -t billing-binlog -p err` 近 10 分钟无条目 |
| 恢复链 | ✅ 锚点 `binlog.000002:14835` → 链连续 → 导入 → 重放 `000002`、`000003` |
| 耗时 | ✅ **60 秒**（RTO 预算 4 小时）。重放那一步 49 秒，大头是首次拉 `percona/percona-server` 镜像 |
| 与生产库逐表比对 | ✅ `CHECKSUM TABLE` 对 `users`、`audit_logs`、`refresh_tokens`、`recovery_codes`、`two_factor_settings` 五张有数据的表，演练库与 `billing` **逐对一致** |

⚠️ **`restore.sh` 打出来的行数本身不是证据**：它没有参照物，看不出重放有没有生效。
这次锚点之后 `000002` 只剩 44 字节，全量之后的写入基本都在 `000003` 里 ——
与生产库逐表 `CHECKSUM TABLE` 一致，才证明那部分确实被重放进来了。

⚠️ 重放时 `mysqlbinlog` 打出的 `--database … will include the GTIDs in any case` 警告**无害**：
生产 MySQL 没开 `gtid_mode`（8.4 默认关），事件的 GTID 是 ANONYMOUS，不存在「同一实例上
被当成已执行而静默跳过」的问题。**哪天开了 `gtid_mode`，在同一实例上恢复就会静默丢掉重放**，
那时这个脚本必须改。

**密钥那一半**（同日）：密码管理器里 `master.key` 与 `BILLING_BACKUP_PASSPHRASE` 的离线副本，
与 VPS 上正在用的那两份比对 SHA-256 前 16 位，**两把都一致**。据此推出离线副本可用：
`BILLING_BACKUP_PASSPHRASE` 的线上值就是这次解开 R2 全量与 binlog 的那一份；`master.key` 的线上值
正在解密管理员的 TOTP 注册（两步登录一直可用）。⚠️ 这是「副本与线上逐字节相同」推出的结论，
**没有**在一台干净的机器上只凭离线副本走一遍 ADR-0004 第 6 节「恢复到临时位置 → 解一条已知密文」。
所以当时 §11 那条**没勾**：钥匙对不等于步骤对（文件格式、属主、挂载路径、重启顺序），而 RTO 算的正是步骤。
比对前记得：输入整行（含 `1:` 版本前缀），末尾那个换行由命令补上。

**真解密演练**（2026-09-15，补上上面的缺口）：

| 步骤 | 结果 |
| --- | --- |
| 恢复 09:03 那份全量 + 重放 `000002`–`000004` 进 `billing_keydrill` | ✅ 63 秒，行数与 9-14 那次一致 |
| 取出 `two_factor_settings` 的一条已确认密文 | ✅ 168 字节 |
| 离线副本写成密钥文件，`chown 10001:10001` + `chmod 0400` | ✅ |
| 一次性容器：`--network none`、`--user 10001:10001`、**只挂离线副本**，调 `app.core.crypto` 解密 | ✅ `decrypted OK` |
| 用解出的密钥生成当前 TOTP 码，与管理员手机上的验证器 App 比对 | ✅ **一致**（首轮忘了比对、恢复库已清理；同日从生产库取**同一行**的密文、同样的容器与离线副本重跑后比对） |
| 反向对照：随机生成的错误密钥 | ✅ `DecryptionFailed`，没有输出码 |
| 从开始恢复到解密成功 | **4 分 12 秒**（含手工输入），RTO 预算 4 小时 |
| 清理：`shred` 密钥文件、`SET sql_log_bin=0` 后删演练库 | ✅ |

⚠️ 仍然**不是一台新主机**：这次在生产 VPS 上起断网容器，验证了密钥格式、属主权限、只凭离线副本解密；
「新主机装 Docker、拉镜像、配 `.env`」那段没演练，留给季度演练（§6）。

⚠️ **最新那份全量（19:17 cron 自动跑出的）这次恢复被拒**，改用了 09:03 那份：备份之后没有写入，锚点 binlog
一直没离机。已由 #53 修复（§5.2.1）。

⚠️ **清理演练库时要关 binlog**：`SET sql_log_bin=0; DROP DATABASE billing_restore_drill;`。
这次是直接 `DROP` 的，那条语句进了 binlog —— 以后若拿**这天或更早**的全量、恢复进**同名**演练库，
重放到它会把正在恢复的库删掉。真出事时目标库不同名，不受影响。

这次**没有**覆盖的：§98.1 要求的钱包 / 账本 / 用量 / 支付 / 对账单 / 文档
完整性核对（那些表还不存在）；每日全量的 cron 自动跑出的备份（这次用的全量是手工触发的）。

### 5.2.5 调度

[`deploy/cron.d/ai_billing_hub`](../deploy/cron.d/ai_billing_hub)：binlog **每分钟**、全量每天一次，
日志进 syslog（`journalctl -t billing-binlog`）。

⚠️ **调度保证不了 RPO，只能缩小它、并让它破掉时不可能不被发现**（Codex #42 R2）。
离机时刻 = 下一轮开始 + 那一轮的上传耗时；上传本身慢过 5 分钟时，没有哪种调度拉得回来。
第一版是 `*/4` + cron 行上的 `flock -n`：上一轮没跑完时本轮**静默跳过**，离机间隔变成 8 分钟。
现在：

| 机制 | 挡什么 |
| --- | --- |
| 每分钟一次，锁在**脚本里** | 被跳过的代价是一分钟而不是一个周期 |
| 每一轮（**含被跳过的**）先查心跳：binlog 上一次成功离机距今超过 300 秒 → `err` 级别写 syslog | 卡死、变慢、连续失败，一分钟内冒出来。⚠️ 锁在 cron 行上的话，卡死的那一轮会让之后每一轮在进脚本前就被挡掉，检查永远不跑 |
| **还没成功过一次**（新部署 / 心跳文件丢失）时，从第一次看守的时刻起算 | ⚠️ 第一版此时直接不查（Codex #42 R3）：首次推送卡住就永远不报 —— 而首次上线恰恰最容易出事 |
| 任何失败也以 `err` 级别写 | 与每分钟的 info 输出分开：`journalctl -t billing-binlog -p err` |
| 心跳记 **FLUSH 的时刻**，不是本轮结束的时刻 | 结束时刻会把暴露窗口少算一次上传耗时 —— 恰好在上传变慢时少算最多 |
| 空闲不 FLUSH；「空闲」要求位置没变**且**最新已关闭文件就是推过的最后一个；位置只在整轮成功后记 | 每分钟不造空文件；两道判据各自变异验证过：缺了会在一次上传失败后把积压当成空闲、留在本机 |

**验证**：Windows 本地栈 + MinIO（上表各路径，含「R2 断掉 → 恢复后无新写入 → 积压仍被推走」）；
Ubuntu 容器里用真 `flock`：锁被占且心跳 10 分钟前 → 跳过**并**发出
`logger -p user.err … RPO breached`；心跳新鲜 → 安静跳过；失败 → `err` 告警、exit 1。

⚠️ 装进 `/etc/cron.d` 而不是 `crontab <file>` —— 后者会**整个替换**该用户的 crontab，
同机其它项目的定时任务一并抹掉。安装命令在那个文件开头。

⚠️ **`err` 级别的 syslog 不是告警。**它得被送到人手里 —— 两个备份任务现在走外部心跳（§5.2.6）。
§95 其余指标的告警通道仍未做。

### 5.2.6 心跳监控（Kelvin 2026-09-15 定：Healthchecks.io + Telegram）

⚠️ **最危险的故障不写日志**：cron 没跑、整台 VPS 挂了、脚本卡死 —— 任何基于日志的告警都发不出来。
所以用 dead man's switch：脚本**成功时**向外部服务 ping 一次，**失败时**立刻 ping `/fail`；
外部服务在「该到的 ping 没到」或「收到 fail」时通知人，恢复后再通知一次。

| 任务 | `.env` 变量 | 成功 ping | 立刻 `/fail` | 不 ping |
| --- | --- | --- | --- | --- |
| `binlog_ship.sh` | `BILLING_HEALTHCHECK_BINLOG_URL` | 推送完成、空闲（无写入） | 任何失败、**破 RPO**（含被锁跳过的那轮） | 被锁跳过的那轮 —— 否则一次卡死的推送会被每分钟的「成功」盖住 |
| `backup.sh` | `BILLING_HEALTHCHECK_BACKUP_URL` | 整轮完成（带上文件名） | 任何失败 | `--dry-run`（没有上传） |
| `restore_drill.sh`（§5.2.7） | `BILLING_HEALTHCHECK_DRILL_URL` | 恢复与全部核对通过（带摘要） | 任何失败 | — |
| `log_ship.sh`（§7.1） | `BILLING_HEALTHCHECK_LOGS_URL` | 归档上传确认（带行数与归档目录大小） | 任何失败 | `--dry-run` |

- **两个检查分开**：频率差 1440 倍，合成一个的话 binlog 每分钟的心跳会把「全量三天没跑」盖住
- ⚠️ **生产巡检另有三个检查**（容器健康 / `/readyz` / 磁盘），同一个账号、同一条 Telegram，见 §8.1
- ping 带 `-m 10` 超时，失败只记一行、不让本轮失败：数据已经离机，监控服务抖动不该变成「推送失败」；
  binlog 那边卡住的 ping 还会一直占着锁
- 没配地址时照常运行，但失败时多打一行 `nobody will be told about this`
- ⚠️ 心跳地址**不进仓库**：知道它的人都能伪造「成功」

**为什么是 Telegram 不是 WhatsApp**：Healthchecks.io 的 WhatsApp 通知按条计费，定价页上只有
US$20/月的 Business 档起才有额度（「50 SMS & WhatsApp credits」），免费档没有；Telegram 属于普通
聊天集成。也不走自家的 `whatsapp_gateway`：它和本平台在同一台 VPS 上，**机器挂了告警一起挂**，
而那正是最需要告警的时候。

**配置步骤**（Kelvin 做；⚠️ 先部署并按 §5.2.5 重装 cron，否则全量仍在 19:17 跑，检查会一直报迟到）：

1. 注册 healthchecks.io（免费档：20 个检查）
2. Integrations → 添加 **Telegram**，按页面提示在 Telegram 里给它的 bot 发消息完成绑定
3. 新建检查 `ai_billing_hub binlog`：Schedule 选 **Simple**，Period **1 分钟**，Grace **5 分钟**（= RPO）
4. 新建检查 `ai_billing_hub full backup`：Schedule 选 **Cron**，表达式 `17 3 * * *`，时区 **Asia/Kuala_Lumpur**，Grace **1 小时**
5. 两个检查都勾上 Telegram 通知
6. 把两个检查各自的 Ping URL 写进 VPS 的 `.env`：
   ```
   BILLING_HEALTHCHECK_BINLOG_URL=https://hc-ping.com/<binlog 检查的 uuid>
   BILLING_HEALTHCHECK_BACKUP_URL=https://hc-ping.com/<全量检查的 uuid>
   ```
7. 验证：一分钟内 binlog 检查变绿；手工跑一次 `bash deploy/backup.sh`，全量检查变绿
8. 第三个检查 `ai_billing_hub restore drill`：Cron `47 4 * * 0`，时区 **Asia/Kuala_Lumpur**，Grace **2 小时**；
   地址写进 `.env` 的 `BILLING_HEALTHCHECK_DRILL_URL`；手工跑一次 `bash deploy/restore_drill.sh` 验证变绿（§5.2.7）
9. 日志外送的检查 `ai_billing_hub logs`：Cron `47 3 * * *`，时区 **Asia/Kuala_Lumpur**，Grace **2 小时**；
   地址写进 `.env` 的 `BILLING_HEALTHCHECK_LOGS_URL`；手工跑一次 `bash deploy/log_ship.sh` 验证变绿（§7.1）

**本地验证**（真 MySQL 8.4 + MinIO + 一个记录请求的假心跳服务）：

| 场景 | 收到的 ping |
| --- | --- |
| binlog 推送两个文件 | `POST /hc-binlog` |
| binlog 空闲 | `POST /hc-binlog` |
| 锁被占 + 上次成功在 10 分钟前 | 只有 `POST /hc-binlog/fail`，正文 `RPO breached: … 600s ago (budget 300s)` |
| 全量成功 | `POST /hc-backup`，正文是文件名 |
| 全量 `--dry-run` | 无 |
| 全量失败（R2 连不上） | `POST /hc-backup/fail`，正文 `upload failed; …`，exit 1 |
| 心跳服务连不上 | 无；记 `heartbeat ping failed`，binlog 推送仍 exit 0 |
| 没配地址 + 全量失败 | 无；多打 `nobody will be told about this` |

**生产上已接通**（2026-09-15）：三个检查经 Healthchecks.io 管理 API 建出（名字、周期、宽限与上面配置步骤一致，时区
Asia/Kuala_Lumpur，挂 Telegram 与 email），ping 地址直接写入 VPS 的 `.env`、没有经过任何文档或对话。验证：

| 检查 | 触发 | 状态 |
| --- | --- | --- |
| binlog | cron 每分钟 | up，几分钟内收到 3 次 |
| full backup | 手工 `bash deploy/backup.sh` | up |
| restore drill | 手工 `bash deploy/restore_drill.sh`（`restore drill passed … in 60s; 8 tables; users 1; TOTP secret decrypted`） | up |
| 通知 | 对 full backup 发 `/fail` → down，再发成功 → up | Telegram 收到 DOWN 与 UP |
| services / readyz / disk | 2026-09-16 巡检上线（§8.1） | 三个都 up；磁盘那条做过一次真实 DOWN / UP |
| logs | 2026-09-16 日志外送上线（§7.1） | up（手工跑第一轮，1475 行 / 30 496 字节） |

建检查用的 read-write API Key 事后已删除（再调用返回 401）；ping 地址不依赖它。

### 5.2.7 每周自动恢复演练（Kelvin 2026-09-15 定每周一次）

⚠️ **全量与 binlog 每天、每分钟都在报成功，但证明它们能恢复的只有真的恢复一次。**手工演练一季度一次，
中间三个月里口令被改、binlog 链断了、镜像拉不到、主密钥与密文对不上，都要等到出事那天才发现。

[`deploy/restore_drill.sh`](../deploy/restore_drill.sh)，cron **每周日马来西亚 04:47**（排在当天 03:17 的全量之后）：

1. 从 **R2** 取最新全量（验的是离机那一份，不是本机 `backups/`）；超过 26 小时就失败 —— 说明全量没到 R2
2. 用 `deploy/restore.sh` **原样**恢复（全量 + binlog 重放）进专用库 `billing_autodrill`，工作目录 `./restore/autodrill`
3. 核对：
   - 表清单与生产库一致
   - `users` 非空
   - 生产有已确认的 2FA 时：在 `--network none`、`--user 10001:10001`、主密钥只读挂入的容器里，用**生产主密钥**解开一条恢复出来的 TOTP 密文。**只打印 `decrypted`**，不打印任何码或密钥（输出进 syslog）
4. **无论成败**：`SET sql_log_bin=0` 后删演练库、删工作目录、删 mysqlbinlog 镜像（约 440 MB；每周重新拉一次，本身也验证了真出事时拉得到）
5. 成功 ping `BILLING_HEALTHCHECK_DRILL_URL`（带「哪份、多旧、多少秒、几张表、解密是否通过」的摘要），失败 ping `/fail`

刻意**不做**的：

- **不和生产逐表比行数 / 校验和**：演练期间生产照常在写，比不出稳定结论；每周误报的检查很快会被所有人无视
- **不替代季度手工演练**：离线副本（密码管理器里那份主密钥）与「新主机从零搭起」只有人能验
- **不自动恢复线上**：恢复到哪一刻、要不要切流量是人的判断，自动做可能拿旧数据盖住仍然正确的数据

已知的误报面：生产在演练前一分钟内做过 DDL（建表 / 删表）时，那条 binlog 还没离机，表清单会对不上。
只在部署迁移时出现，而部署不会排在周日凌晨。

演练库名写死、再与生产库名比一次：清理会无条件 `DROP` 它。

**本地验证**（真 MySQL 8.4 + MinIO + 本地应用镜像的真 `app.core.crypto` + 假心跳服务）：

| 场景 | 结果 | 心跳 |
| --- | --- | --- |
| 正常（带一个上一轮残留的 `billing_autodrill`） | `restore drill passed: … in 7s; 2 tables; users 1; TOTP secret decrypted` | `POST /hc-drill`，正文即摘要 |
| 主机上的主密钥与数据对不上 | `the production master key cannot open a restored TOTP secret (…DecryptionFailed…)`，exit 1 | `/fail` |
| 生产有一张没离机的新表 | `the restored table set differs from billing: live [late_table …] restored […]`，exit 1 | `/fail` |
| R2 上最新全量是 2026-01-01 的 | `the newest full backup … is 6158h old; the daily backup is not reaching R2`，exit 1 | `/fail` |

四个场景结束后：演练库 0 个、工作目录不存在、镜像已删。

### 5.3 恢复之后必须做的对账

INV-14 的设计（DB 是事实来源、Redis/Celery 只承载触发）在这里兑现：恢复之后
**不需要**重放队列，`domain_outbox` 里状态为 `PENDING` 的行会被周期恢复任务自然捡起。

⚠️ 但这意味着**恢复点之后已投递、却因回滚而重新变回 `PENDING` 的行会被重投**。
outbox 的投递语义本来就是 at-least-once，密码重置无害；**Phase 2 接上用量事件与
钱包之后，重投必须是幂等的** —— 那是 Phase 1/2 建表时的事，这里先记下依赖。

---

## 6. 加密密钥的宿主机那一半（ADR-0004）

应用侧 T0.8b 已经做完（信封加密、多版本钥匙串、文件注入）。**缺的全在宿主机上**：

- [x] 主密钥文件 `chown 10001:10001` + `chmod 0400`
      （`10001` = `Dockerfile` 里的 `APP_UID`，T0.6 定的）—— 2026-09-15 在 VPS 上核对：`secrets/` 下三个文件（`jwt.key`、`master.key`、`smtp.password`）都是 `10001:10001 400`
- [x] ⚠️ **不得为了读密钥把容器改回 root** —— ADR-0004 明写。2026-09-15 核对：api / celery-worker / celery-beat 里 `id -u` 都是 `10001`
- [x] runbook 写入主密钥恢复**流程**（脱敏），具体值进私有附录 —— 2026-09-16 写进 [runbook](runbook.md)「主密钥（`master.key`）丢失或要在新主机上重建」：文件格式与属主、**只比对哈希前缀不打印内容**、断网容器里真解一条 TOTP 密文才算数；值只在 Bitwarden 里
- [ ] 季度恢复演练包含主密钥恢复

⚠️ **Compose 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 三个选项只在 swarm
下生效、普通 compose 下被忽略** —— 宿主机文件的属主与权限原样带进容器。所以上面
那条 `chown` 不是建议，是唯一的生效途径。

⚠️ **还有一条 T0.8d 实测确认的**：文件不存在时 **`docker compose up` 照样起得来** ——
Docker 把缺失的 `file:` secret 挂成一个**空目录**，读它抛 `OSError`，于是密钥静默变成
空串。别指望 compose 替你挡住「忘了建文件」。

---

## 7. 生产日志（§94）

T0.3 做的是日志的**内容与格式**（结构化、request id、脱敏）。§94 还要求下面这些，
**全是部署侧的** —— 2026-09-16 由 [`deploy/log_ship.sh`](../deploy/log_ship.sh) 一并落地：

| 要求 | 现状 | 怎么做到的 |
| --- | --- | --- |
| 轮转 | compose 的 json-file driver：`max-size 10m` / `max-file 3` | ⚠️ 它**按大小轮转，不按时间** —— 那只是「堵住把磁盘写满」，不是保留期 |
| 保留期 | ✅ `BILLING_LOG_RETENTION_DAYS`，默认 **30 天**（决策 ⑤） | 每天把容器日志抓出来打包加密成归档，**保留期落在归档上**：本机 30 天、R2 35 天 |
| 磁盘上限 | ✅ `BILLING_LOG_ARCHIVE_CAP_MB`，默认 **512 MB** | 先按保留期删、再按总量从最老删；撞上限**另外**以 err 级别写一条 syslog。加上容器日志那 210 MB，主机层面的日志总量口径 ≈ **0.7 GB** |
| 安全删除 | ✅ `shred -u`（§112） | ⚠️ 边界在脚本注释里写明：日志式文件系统、SSD 磨损均衡、快照 / 写时复制之下**都不保证**覆盖到原物理块。真正兜底的是**归档本身是加密的** |
| 异地留存 | ✅ R2 的 `logs/` 前缀，35 天 lifecycle | 与备份同一个桶、同一个口令、同一套「上传后回读核对大小」 |
| **磁盘告警** | ✅ 巡检每 5 分钟看水位，≥ 80% P2 / ≥ 90% P1 | §8.1。§94 明写的「必须在威胁到 MySQL / 文档存储之前告警」 |

### 7.1 日志外送（`deploy/log_ship.sh`）

每天马来西亚 03:47 由 cron 跑一次（排在 03:17 的全量之后）：
逐服务抓 → 打包 → 加密 → **解回来逐字节验证** → 传 R2 → 清理本机归档。

⚠️ **为什么必须外送，而不是「把保留期调大」**：json-file 按大小轮转，写得快的那天
可能只剩几个小时的历史；而事故调查要看的恰恰是**出事前那几天**。保留期这件事在
本机表达不了，它是**离机那一侧**的属性。

几条与「日志能不能用」直接相关的取舍：

- **`--timestamps` 不是可选的**：compose 默认不打印时间戳，而没有时间戳的日志
  在事故调查里几乎没用 —— 对不上时间线，也拼不起多个服务
- **逐服务一个文件**：混在一起的话，某个服务刷屏会把别人的淹掉
- **窗口从上一次成功那一刻起，宁可重叠不留缝**：重复的行无害，缺掉的那一段没有
  第二个地方能补。⚠️ 状态文件丢失时窗口被夹到 `BILLING_LOG_MAX_WINDOW_HOURS`（默认 7 天），
  否则一次例行外送会变成一次事故
- **状态只在上传确认之后推进**：提前推进的话，一次失败的外送会让那段窗口**再也不会
  被抓第二次** —— 而它多半正是出问题的那一段
- **成功和失败都清理本机归档**：失败的那一轮照样留下一份加密归档，R2 挂一周就攒一周。
  `die` 里也调一次保留期与上限（清理本身出错不盖住真正的失败原因），⚠️ 但**本轮那一份不删** ——
  失败时它是那段窗口目前唯一的副本
- **没配 R2 是显式失败**，不是静默跳过：只留在本机的日志不满足异地留存，而机器没了
  的那一刻正是最需要它的时候
- **全部服务零行只警告、不失败**：一台没人访问的机器就是这样；让它失败会把安静的
  周末变成每周一次的假警报

⚠️ **一条诚实的上限**：归档的完整性取决于抓取那一刻 json-file 里还剩多少 ——
某个服务一天之内写爆 30 MB 时，抓到的就是被截断的一段。频率定成每天而不是每周就是
为了压小这个风险；**要根治得上日志聚合**，那不在 T0.9 的范围里（§8.3）。

**本地演练**（假 docker 提供 `compose logs` 与 aws-cli，真 openssl / tar，八个场景）：

| 场景 | 结果 |
| --- | --- |
| 正常一轮 | 七个服务各 2 行 → 打包加密 → 验证 → 上传 → 回读大小一致 → 状态推进 → 心跳成功，exit 0 |
| `--dry-run` | 抓取 + 加密 + 验证照做，**不上传、不推进状态、不 ping** |
| 上传后大小对不上 | `size mismatch after upload: local 464, remote 463`，`/fail`，exit 1 |
| 没配 R2 | `does NOT satisfy spec §94`，`/fail`，exit 1 |
| 保留期 | 两个 40 天前的归档被安全删除 |
| 总量上限（临时设 3 MB） | 从**最老**开始删到低于上限，err 级 syslog；**本轮刚传的那份绝不删** |
| 状态文件很旧 | `clamping the window`，窗口被夹到 7 天 |
| 所有服务零行 | `WARNING: every service returned zero lines`，照常上传，exit 0 |
| **连续失败**（R2 一直不可用，连跑三轮） | 每轮都 `/fail` 并 exit 1，但**保留期与上限照跑**：过期归档被删、总量被压回上限内。⚠️ Codex #64 R1 指出的缺口：原来 `die` 直接退出，失败留下的归档会一直攒着 —— 而那正是 §94 要防的那件事 |

**归档可读性验证**：把桶里那份密文用口令解开 → `tar tzf` 列出七个 `<服务>.log` →
抽样看到带 RFC3339 时间戳的原始行。⚠️ 与备份同一条：**能传上去不算数，能解开才算**。

**生产上已接通**（2026-09-16）：部署 `308017d`（Deploy run 35109948596）→ 重装 cron 为五行 →
建第四个检查 `ai_billing_hub logs`（Cron `47 3 * * *`、时区 Asia/Kuala_Lumpur、宽限 2 小时，
渠道与另外三个一致）→ Kelvin 在 Cloudflare 加上 `logs-35d` 规则（前缀 `logs/`，35 天）。
建检查用的 read-write API Key 事后已撤销（再调用返回 401）。

| 步骤 | 结果 |
| --- | --- |
| 手工跑第一轮 | mysql 16 / redis 1048 / api 34 / celery-worker 33 / celery-beat 15 / frontend 29 / nginx 300 = **1475 行**；上传确认 **30 496 字节**；`enforcing retention` 照跑；exit 0 |
| **读回演练** | 从 R2 把那份密文拉回来 → 用生产口令解开 → `tar tzf` 列出七个 `<服务>.log` → `api.log` 34 行、首行 `api-1 | 2026-09-16T14:41:35.755734883Z …` |
| 心跳 | `ai_billing_hub logs` 变绿；七个检查（binlog / 全量 / 演练 / services / readyz / disk / logs）全部 up |

⚠️ **一个顺带的观察**：redis 一天 1048 行，占了这一轮的三分之二 —— 它是目前最吵的服务。
现在还在 512 MB 的归档上限之内，但真正要压缩日志量时，先看它。


---

## 8. 监控与告警（§95）

§95 列了 17 项指标，并要求**上线前每一项都有阈值、分级、通知对象、抑制规则和
runbook 链接**。⚠️ **17 项仍未齐** —— 那需要一套指标管道（采集 + 存储 + 规则表达式），
本阶段没有。先落地的是**三条「不落地就永远没人发现」的故障**，它们的共同点是
**不会让任何请求报错**：缺了告警就等于缺了唯一的发现途径。

### 8.1 已落地的三条（2026-09-16）

实现是一个巡检脚本 [`deploy/monitor.sh`](../deploy/monitor.sh)，由 cron **每 5 分钟**
跑一次，结果发到 §5.2.6 那套 Healthchecks.io + Telegram 上（与备份心跳同一条通道）。

| 告警 | 判据 | 阈值 / 分级 | 通知 | 抑制 | runbook |
| --- | --- | --- | --- | --- | --- |
| 容器不健康（含 **celery-beat 停止调度**） | `docker compose ps` 的 `State` / `Health`，七个服务逐个查 | 非 `running` 或非 `healthy`；`mysql` / `api` / `billing_nginx` / `frontend` = **P1**，`redis` / `celery-worker` / `celery-beat` = **P2** | Telegram（检查 `ai_billing_hub services`） | 状态翻转才通知，红着的期间不重复 | [celery-beat 停止调度](runbook.md) |
| **`billing_readiness_degraded_redis`** | 宿主机 `GET /readyz`，**读响应体**：`data.status != "ok"` | P2；非 2xx（数据库不通 / API 挂）= **P1** | Telegram（检查 `ai_billing_hub readyz`） | 同上 | [Redis / Celery broker 不可用](runbook.md) |
| 磁盘水位（§94 点名的那条） | `df -P` 查根文件系统与部署目录，按设备去重 | ≥ **80%** = P2，≥ **90%** = P1（`BILLING_MONITOR_DISK_*_PERCENT` 可调） | Telegram（检查 `ai_billing_hub disk`） | 同上 | §7 的保留期与上限（仍未做完） |

⚠️ **分级只体现在通知正文的前缀里**（`P1 mysql is not running`）。通道只有一条，
它没有分级概念 —— P1 与 P2 的区别是**人该多快起身**，不是消息走哪条路。

四条设计上的取舍，每一条都对应一种会让告警失效的形态：

1. **三个维度各自一个检查，不合成一个。**外部服务只在**状态翻转**时通知人：
   磁盘先红了之后 MySQL 再挂，检查早已是 down，**不会再有第二条通知**
2. **全绿时也 ping**（dead man's switch）。cron 没跑、整台 VPS 挂了、脚本卡死 ——
   这些情况**不写任何日志**，只有「该到的 ping 没到」能暴露
3. **发现问题先隔 45 秒复核一次。**一次正常部署看起来和故障一模一样：换版本时
   容器有半分钟不是 `healthy`。不复核的话每次部署误报一条，**而被误报训练过的人
   不会再看告警**
4. **cron 那一行不加 flock。**巡检是只读的，重叠无害；而 flock 会让一次卡死把之后
   每一轮都挡在脚本外面 —— 那种状态下没有任何一轮真的检查过（与 `binlog_ship.sh`
   同一条教训）

`/readyz` 只放行回环与私有网段，而栈的 nginx 默认只绑 `127.0.0.1` —— **宿主机自己
是唯一探得到它的地方**，这也是巡检必须跑在 VPS 上、而不是挂在外部监控服务里的原因。

**配置步骤**（与 §5.2.6 同一个 Healthchecks 账号）：

1. 新建三个检查，Schedule 选 **Simple**，Period **5 分钟**，Grace **15 分钟**
   （容得下一次复核加一次错过）：`ai_billing_hub services` / `ai_billing_hub readyz` /
   `ai_billing_hub disk`
2. 三个都勾上 Telegram
3. 把三个 Ping URL 写进 VPS 的 `.env`：
   ```
   BILLING_HEALTHCHECK_SERVICES_URL=https://hc-ping.com/<services 检查的 uuid>
   BILLING_HEALTHCHECK_READYZ_URL=https://hc-ping.com/<readyz 检查的 uuid>
   BILLING_HEALTHCHECK_DISK_URL=https://hc-ping.com/<disk 检查的 uuid>
   ```
4. 按 §5.2.5 重装 cron（那个文件是**复制**进 `/etc/cron.d` 的，改完不重装不生效）
5. 验证：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh` 手工跑一次，三个变绿

**本地演练**（假 docker + 一个记录请求的假心跳服务，九个场景）：

| 场景 | 收到的 ping | 退出码 |
| --- | --- | --- |
| 全绿 | 三个检查各一次成功 ping | 0 |
| celery-beat `unhealthy` | `services/fail`，正文 `P2 celery-beat health=unhealthy` | 1 |
| mysql 容器不在了 | `services/fail`，正文 `P1 mysql is not running` | 1 |
| `/readyz` 200 但 `degraded` | `readyz/fail`，正文 `P2 billing_readiness_degraded_redis: "redis":"unavailable"` | 1 |
| `/readyz` 503 | `readyz/fail`，正文 `P1 readyz is not answering 2xx` | 1 |
| 磁盘越过警戒线 | `disk/fail`，正文点名挂载点与百分比 | 1 |
| 第一轮红、复核时已恢复 | 三个都是成功 ping | 0 |
| 没配 ping 地址 且有问题 | 无 ping，多打一行 `nobody will be told about this` | 1 |
| 心跳服务连不上 | 无；记 `heartbeat ping failed`，巡检结论不变 | 1 |

**生产上已接通**（2026-09-16）：三个检查经 Healthchecks.io 管理 API 建出（周期 300 秒、宽限 900 秒、
通知渠道与既有三个备份检查完全一致），ping 地址直接写入 VPS 的 `.env`、**没有经过任何文档或对话**；
建检查用的 read-write API Key 事后已撤销（再调用返回 401）。

| 步骤 | 结果 |
| --- | --- |
| 部署 `7ae606e`（Deploy run 35053358264） | VPS 工作副本到位，`deploy/monitor.sh` 可执行 |
| 重装 `/etc/cron.d/ai_billing_hub` | 四行：binlog 每分钟、全量 `17 3`、**巡检 `*/5`**、演练 `47 4 * * 0` |
| 手工跑一次（`BILLING_MONITOR_RECHECK_SECONDS=0`） | `all containers healthy (7 services)` / `readyz ok (database + redis)` / `disk below 80% (68%)`，exit 0；三个检查全部变绿 |
| **真实告警演练**：把警戒线临时压到 1% 跑一次 | exit 1；`journalctl -t billing-monitor -p err` 出现 `P2 disk / at 68% (warn 1%)`；`ai_billing_hub disk` 翻成 **down**，Telegram 收到通知 |
| 恢复：按默认阈值再跑一次 | 三个检查回到 up |
| cron 自动跑 | 13:00:02 (MYT) 那一轮自动执行，三条结论与手工一致 |

⚠️ **这条告警上线当天就派上了用场**：接通后顺手查了一眼磁盘现状 —— 根分区 29 GB、已用 **68%**，
离 80% 的警戒线只剩约 3.5 GB，而占大头的是一代代攒下来的部署镜像。那个缺口已由 PR #62 堵住
（`deploy.sh` 的 `prune_old_images`，详见 §3.1 末尾）。**先有会响的告警，才会有人去查这件事** ——
在此之前磁盘涨到 80% 不会有任何信号。

### 8.2 原来钉住的两条（现状）

> ⚠️ **`billing_readiness_degraded_redis`**（T0.5 派生，PR #28 审查指出）：
> `/readyz` 在 Redis 不可用时**刻意**返回 200（理由见 `app/core/config.py`：
> Redis 挂了就把 API 摘出轮转，恰好制造出 INV-1 要防的那种中断）。
> 代价是**负载均衡永远发现不了这个故障，它只能靠日志告警发现**。
> ~~告警落地之前，Redis 静默不可用是一个**已知的、被接受的检测缺口**。~~

→ **已关闭**（2026-09-16）。⚠️ 实现方式与 runbook 原先写的条件**不一样**：没有日志聚合，
判据不是「日志里出现那条 WARNING」，而是**宿主机每 5 分钟直接读一次 `/readyz` 的响应体**。
告警名不变 —— 它是 runbook 与通知之间的稳定契约。日志那条判据等有了日志聚合仍然成立。

> ⚠️ **celery-beat 没有存活探针。**`celery inspect ping` 问的是 worker，够不着 beat。
> T0.8d 之后 beat 已经有了真实的周期任务（outbox 恢复），所以**它崩了就是静默故障**：
> outbox 不再补投、API 一切正常、没有任何报错。

→ **探针**在 T0.6 就补上了（`docker-compose.yml` 里按调度状态文件的 mtime 判活），
**缺的一直是把 `unhealthy` 送到人手里** —— 那一半由 §8.1 的服务维度关闭。

### 8.3 还没有的（诚实清单）

- §95 的 17 项**业务指标**（用量摄取速率、钱包余额异常、支付回调失败率……）：
  需要指标管道，**不在 T0.9 的范围里**，Phase 1 起随功能补
- 日志聚合，以及基于日志内容的告警规则（§94 的异地留存也卡在这一条上）
- 告警的**值班与升级路径**：现在只有一个人、一条 Telegram。`P1 30 分钟未响应升级`
  这类规则等有第二个人再谈 —— 写在 runbook 里而不是配置里

---

## 9. 部署流水线（CD）

决策 ⑥ 把它划进了 T0.9。spec §123 的 Phase 0 验收要求「CI gates merge and
**deploys an immutable commit image**」，§99 给了具体要求。

### 9.1 形状：薄 workflow + 可演练的脚本

| 文件 | 干什么 | 验证到什么程度 |
| --- | --- | --- |
| [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) | 构建镜像 → 推 ghcr → ssh 上去跑脚本 | ⚠️ **从未在真实 VPS 上执行过** |
| [`deploy/deploy.sh`](../deploy/deploy.sh) | 等库 → 迁移 → 换版本 → 等健康 → 冒烟 → 失败回滚 | ✅ **本地栈上整套演练过**，成功与回滚两条路都跑通 |

⚠️ **逻辑放脚本里不放 YAML 里，只有一个理由**：YAML 里的步骤没有任何办法在生产
之外跑一遍，而迁移顺序、健康等待、回滚恰恰是最不该第一次就在生产上验的东西。

⚠️ **镜像在 GitHub Actions 上构建，不在 VPS 上。**那台机器只有 1–2 核、12 GB 可用
磁盘，上面还跑着另外八个项目 —— 在它上面构建会把同机的别人一起拖慢。

### 9.2 本地演练的结果

```
成功路径：等库 → 迁移 → 启动 → 七服务健康 → 冒烟 GET /healthz → exit 0
失败路径：冒烟指向一个不通的端口 → 回滚到上一个镜像 → exit 1
```

⚠️ **回滚成功仍然以非零退出。**回滚让服务恢复了，但「这个 commit 上不了线」这件事
不能被一个绿色的 CD 掩盖 —— 那样下一个人会以为它已经上线了。

⚠️ 演练本身抓到一个缺陷：第一版**没等数据库就跑迁移**，MySQL 还在初始化就
Connection refused，被当成「部署失败」而实际只是早了几秒。首次部署与 MySQL 重启后
各会中一次 —— 而那正是最容易手忙脚乱的两个时刻。

⚠️ 那次修的时候只加了「等」没加「拉」，而演练用的栈上 MySQL 本来就在跑，于是没暴露：
**真正的首次部署时 mysql 容器还没被创建**，脚本会一直等到超时（Codex #42 R1）。
现在先 `up -d mysql` 再等。空栈上验证过两边：带着这一行 exit 0；去掉它卡在
`containers not created yet` 直到超时、exit 1。

⚠️ 手动触发时填的 `ref` **必须是完整的 40 位 commit SHA**。workflow 的第一步就校验它，
而且只经环境变量进 shell —— 那是个触发者随手填的文本框，原样拼进脚本就是命令注入
（Codex #42 R1）。短 SHA、分支名、`latest`、带引号或换行的输入全部被拒，本地逐个试过。

⚠️ **边缘 nginx 的配置变更要 reload 才生效**（§2.2）。本地栈上按顺序演练的四种情况：

| 场景 | 结果 |
| --- | --- |
| 以不带安全头的旧配置从零起栈 | 冒烟拦下 `not sending its security headers`，首次部署无回滚目标，exit 1 |
| 磁盘上换成新配置，**去掉 reload 的变异** `deploy.sh`（= 生产上首次加安全头时的状态） | 冒烟拦下并回滚，exit 1 |
| 同一份新配置，真正的 `deploy.sh` | `nginx -t` → reload → `X-Frame-Options: DENY` 出现，exit 0 |
| 磁盘上是写错的配置 | `nginx -t` 失败、不 reload，回滚，exit 1；**线上继续以上一份配置服务，安全头仍在** |

### 9.3 GitHub secret 与 VPS 上的准备

**沿用本工作区其它八个项目的约定**（`rs-roof-pms`、`crm_os`、`erp_os` 等）：
`appleboy/ssh-action` + `VPS_*` 四个 secret + VPS 上一份 git 工作副本 + 路径写死
`/opt/<项目名>`。Kelvin 2026-09-13 已按这套配好前四个。

| Secret | 状态 |
| --- | --- |
| `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` / `VPS_PORT` | ✅ 已配（仓库级） |
| **`VPS_FINGERPRINT`** | ⚠️ **还没有，必须补** —— 见下 |

#### 与其它项目**刻意不同**的两处

**① 主机指纹必须校验。**其它八个项目的 deploy workflow **都没有**做这一条。
`appleboy/ssh-action` 的 `fingerprint` 留空时是**静默跳过**校验，而不校验主机指纹
的 SSH 等于把部署私钥交给任何能在中间做手脚的人。所以这里补一个 `VPS_FINGERPRINT`，
并在前面单独检查它非空 —— 没配就让 job 直接红，不让它退化成「不校验」。

取值（**在 VPS 上**执行，要的是 `SHA256:` 开头的那一段）：

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub | awk '{print $2}'
```

⚠️ **必须是 ECDSA 那把，不是 ED25519。**`appleboy/ssh-action@v1.0.3` → `drone-ssh:1.7.3` →
`golang.org/x/crypto v0.17.0`，它不指定主机密钥算法，按库里的偏好顺序
`ECDSA256 → ECDSA384 → ECDSA521 → RSA → … → ED25519` 协商 —— 服务器同时有 ECDSA 与
ED25519 时出示的是 ECDSA，指纹就按那把比。第一版文档写的是 ED25519，首次部署时连续两次
`ssh: handshake failed: ssh: host key fingerprint mismatch`（2026-09-14），换成 ECDSA 后通过。
比对发生在登录认证之前，所以这类失败不会触发 fail2ban，VPS 上也没有执行任何东西。

⚠️ 升级 `appleboy/ssh-action` 版本时这条要重新确认：偏好顺序跟着 `x/crypto` 版本走，后续版本
里有调整过。换版本后第一次部署如果报 fingerprint mismatch，先查新版本协商的是哪种密钥。

**② 检出这次构建的 commit，不 `git pull`。**其它项目用 `git pull --ff-only`，那拿到的
是 main 的最新 HEAD —— 构建完成后又合进来一个 commit 的话，VPS 上跑的部署脚本与
compose 就和镜像不是同一个版本。§99 要求镜像标签对应确切 commit，那条对部署脚本
本身同样成立。

#### ⚠️ 仓库级 secret 与 environment secret 的一处真实差别

`VPS_SSH_KEY` 现在是**仓库级**的。deploy job 引用了 `environment: production`，
所以照样读得到、人工批准也照样生效 —— **但批准只拦住了部署 job，没拦住那把私钥**：
仓库级 secret 对仓库里**任何** workflow 都可读，不需要经过批准。

实际风险不高：来自 fork 的 PR 拿不到 secret（GitHub 不会传给它们），能加 workflow 的
只有有写权限的人。与其它项目一致起见先保持仓库级；想收紧的话把 `VPS_SSH_KEY` 挪到
`production` 环境里即可，workflow 不用改。

#### VPS 上的一次性准备

`/opt/ai_billing_hub` 里已经有 `.env` 和 `secrets/`，所以**不能** `git clone`（它要求空目录）。
改成就地初始化 —— `.env` 与 `secrets/` 都被 `.gitignore` 挡着，不会冲突：

```bash
cd /opt/ai_billing_hub
git init
git remote add origin https://github.com/kelvinpang90/ai_billing_hub.git
git fetch origin
git checkout main
```

宿主机那一侧还要准备好（**都不在仓库里**）：

- `.env`（按 [`.env.example`](../.env.example) 填，含数据库口令、`BILLING_FRONTEND_BASE_URL`、SMTP、R2）
- `secrets/jwt.key`、`secrets/master.key`、`secrets/smtp.password`
- ⚠️ 主密钥文件 `chown 10001:10001` + `chmod 0400`（见 §6）

### 9.4 第一次跑之前

- [x] VPS 内存升配完成（决策 ③）—— 2026-09-15 核对：2 核 / 7.3 GB，swap 4 GB 只用 10 MB（§3.1）
- [x] `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` / `VPS_PORT` 已配（2026-09-13）
- [x] ⚠️ **`VPS_FINGERPRINT` 补上**（没有它 job 会直接失败，这是刻意的）—— 已补（2026-09-14，见 §9.3 的 ECDSA 那条）；此后 Deploy 多次成功（最近 run 34952597111）
- [x] `production` environment 开了人工批准与「只允许 main」—— 2026-09-15 经 GitHub API 核对：1 条 required reviewers 规则，deployment branch policy 只有 `main`；run 34952597111 实际停在等待批准
- [x] VPS 上 `/opt/ai_billing_hub` 已 `git init` 并检出 main —— 是一份指向本仓库的 git 工作副本，Deploy 按 SHA 检出（当前 `6ea9dc5`）
- [x] 宿主机的 `.env` 与三个密钥文件就位 —— 2026-09-15 核对三个密钥文件存在且权限正确（§6）；`.env` 就位（部署、备份、心跳都在读它）。SMTP 四项 2026-09-15 补上（§11）
- [ ] ghcr 的包可见性确认过（公开仓库默认公开；镜像里没有密钥，与 ADR-0001 一致）
- [ ] ⚠️ **先手工跑一次 `deploy/deploy.sh`**，别让第一次执行是由一次 push 触发的
- [x] 装上 `deploy/cron.d/ai_billing_hub`（§5.2.5），并看到第一轮 binlog 推送与第一份全量在 R2 里 —— 2026-09-14 首装；2026-09-15 部署 `6ea9dc5` 后重装为三行（binlog / 全量 `17 3` / 演练 `47 4 * * 0`）
- [x] 在 VPS 上对 R2 里的真实备份跑一次 `deploy/restore.sh`（§5.2.4）—— 2026-09-14、09-15 手工，09-15 起每周自动（§5.2.7）
- [ ] 上面全部关闭之后，才把 `push: branches: [main]` 触发器加回 workflow

⚠️ **现在这个 workflow 只能手动触发（`workflow_dispatch`）。**挂上 push 触发器的话，
它会在这个 PR 合并的那一刻开火 —— 而主机还没就绪、secret 也没配。后果不只是一次
红色的 CD：build 那一步会**真的把镜像推到 ghcr**，那是个对外的副作用，不该由一次
「先把代码合进去」顺带触发。有一条用例（`test_deploying_is_a_deliberate_act...`）
钉着这件事，加回触发器的人必须同时改掉它 —— 那一刻他会读到为什么。

---

### 9.5 回滚（T0.9 2026-09-16 补上两个缺口）

自动回滚在 `deploy.sh` 的失败分支里：健康检查或冒烟没过 → 把上一版镜像换回来 →
等健康 → **仍然以非零退出**（这次部署没成功，CD 必须是红的）。

补上的两件事：

| 缺口 | 后果 | 现在怎么做 |
| --- | --- | --- |
| 回滚只换后端镜像 | 前端留在坏版本上，栈停在「后端旧、前端新」。⚠️ **这种不匹配是静默的**：两个容器都健康、日志干净，只有用户点到某个新页面才发现它在打一个不存在的接口 | 部署开始时把 `frontend` 正在跑的镜像一并记下，回滚时两个一起换回去。前端此刻没起来时（`ps` 列不出停掉的容器），按后端那一版的标签推出来 —— 两个镜像同一个 commit 构建 |
| 主机上没有「上一个成功部署是哪个 commit」 | 手工回滚要在 workflow_dispatch 里填 `ref`，而唯一的来源是 `docker compose ps` 里正在跑的标签 —— **需要回滚的时候，正在跑的恰恰是坏的那个**。`git log` 也答不了：`main` 上最新那条未必部署过 | 成功部署之后写 `.last-good-deploy`（tag、两个镜像、UTC 时间），并往 `.last-good-deploy-history` 追加一行。下一次部署开始时会把它打进日志 |

⚠️ **记录只在健康检查与冒烟都过了之后写**：记早了的话，一次失败的部署会把「上一个好的」
覆盖成那个坏的 —— 而这份记录是出事那天唯一的依据。

⚠️ **写不下去只是警告，不是一次失败的部署**：那时服务已经健康，把它扔成失败会触发回滚，
而那才是真正制造停机的那一步。

手工回滚的步骤：

```bash
# 在 VPS 上看上一个好的是哪个 commit
cat /opt/ai_billing_hub/.last-good-deploy
# 那一版本身也坏掉时，往前再找一个
tail -5 /opt/ai_billing_hub/.last-good-deploy-history
```

然后用 GitHub Actions 的 **Deploy** workflow（`workflow_dispatch`），`ref` 填那个 SHA。

⚠️ **迁移不回滚**：`deploy.sh` 只换镜像。所以破坏性的迁移必须拆成两次部署
（先加、双写、再删）—— 否则回滚之后，旧代码面对的是一个它不认识的表结构。

## 10. 七件事的决定（Kelvin，2026-09-13）

| # | 问题 | 决定 | 落在哪一节 |
| --- | --- | --- | --- |
| ① | nginx 接 `infra_nginx` 还是自己面对公网 | **A：走 `infra_nginx`** | §2、§4 |
| ② | 几个 uvicorn worker，兜底限流怎么办 | **A：依赖 nginx 那层**；⚠️ 因 1 核，动作是**不加 `--workers`** | §3 |
| ③ | VPS 规格与余量 | 1 核 / 3.6 GB / 剩 12 GB，已在 swap → **升配内存后再上线** | §3.1 |
| ④ | nginx 换非 root 镜像 | **不换**（①A 之后只收内网流量）。⚠️ 有意识的选择，不是遗漏 | §4 |
| ⑤ | 保留期 | 财务记录先永久保存；**运营日志做成环境变量、暂定 30 天** | §7、下方 |
| ⑥ | CD 单开编号还是算进 T0.9 | **算进 T0.9** | §9 |
| ⑦ | 备份放哪 | **R2**，上传前自己加密；**主密钥不进 R2** | §5.1 |
| ①′ | （2026-09-14 复议）要不要去掉 billing_nginx，统一只用 infra_nginx | **A：保留**。计费平台特有的安全控制留在本仓库 | §2.2 |

### ⚠️ 「先永久保存」有一处推迟、一处不适用

- **推迟**：§112 要求个人数据「不再需要时删除或不可逆匿名化」，与永久保存冲突。
  但 Phase 0 只有管理员账号，**真正的个人数据要等 Phase 4 的客户门户** ——
  所以这条推迟到那时定，不是忽略
- **不适用**：运营日志不能永久保存（磁盘），已单独定为 30 天

### ⚠️ ~~还差一个事实~~ —— **已查实**（2026-09-16），`set_real_ip_from` 已按它收窄

`infra_nginx` 与本平台 nginx 在**同一张 docker 网络 `proxy_net` 上直连容器**，
不经宿主机发布端口（`docker network inspect proxy_net`：本栈 nginx 与 infra_nginx 都在
`172.19.0.0/16` 上）。所以三处一起收窄成：

| 处 | 收窄前 | 现在 | 为什么 |
| --- | --- | --- | --- |
| nginx `set_real_ip_from` | `10.0.0.0/8` + `172.16.0.0/12` + `192.168.0.0/16` + `127.0.0.0/8` | **`172.19.0.0/16`**（proxy_net） | 能够到本栈 nginx 的只有这张网 |
| nginx `/readyz` 的 allow | 同上四段 | `127.0.0.0/8` + **本栈网段** + proxy_net | 经 infra_nginx 进来的请求在这里已经是**客户端真实 IP**，公网一律 deny |
| 应用 `BILLING_TRUSTED_PROXIES` | `172.16.0.0/12,10.0.0.0/8,192.168.0.0/16` | **本栈网段** | api 的直连对端只可能是本栈 nginx |

**本栈网段被钉死成 `10.201.0.0/24`**（`docker-compose.yml` 的 `networks.default.ipam`，
可用 `BILLING_STACK_SUBNET` 覆盖）。⚠️ 不钉的话 docker 每次随手分一个 172.x，
「可信代理是谁」就成了每台机器、每次重建都不一样的东西 —— 那正是原来只能拿三段 RFC1918
兜着的原因。选 10.201 是因为它**在 docker 默认分配池（172.17–172.31）之外**，
而且生产 VPS 上 10.x 一个都没用（2026-09-16 实测 `ip -4 route`）。

⚠️ **还剩一层有意保留的信任**：`proxy_net` 上挂着同机另外八个项目的容器，它们仍在
`set_real_ip_from` 的范围里 —— 其中任何一个被攻陷都能伪造 `X-Forwarded-For`。
要再窄一层得让 `infra_nginx` 拿一个**固定 IP**，那是 `vps_infra` 仓库的改动，已记进
[TODO](TODO.md)。

⚠️ **`proxy_net` 的网段由 `vps_infra` 定，不是我们定的。**它变了而这里没跟着改，
后果是**静默失效**：所有请求的来源又变回 infra_nginx 自己，限流与审计 IP 一起失真。
换主机 / 重建 proxy_net 时要先 `docker network inspect proxy_net` 看一眼。

## 11. 收口条件

⚠️ 下面这些**在生产主机可用之前一条都做不了**，本文件只负责把它们说清楚。

- [x] 决策 ①–⑦ 有答案，本文件已按答案补完（2026-09-13）
- [x] ⚠️ **VPS 内存升配完成**（决策 ③）—— 在此之前不要上线。2026-09-15 核对：2 核 / 7.3 GB，swap 几乎未用
- [x] `set_real_ip_from` 按实际拓扑收窄 —— 2026-09-16 查实 infra_nginx 走 `proxy_net` 直连容器，三处一起收窄（nginx 的 `set_real_ip_from` 与 `/readyz` 名单、应用的 `BILLING_TRUSTED_PROXIES`），并把本栈网段钉死成 `10.201.0.0/24`，见 §10 末尾。⚠️ proxy_net 上还有同机另外八个项目的容器，那一层信任是**有意保留**的（要 vps_infra 给 infra_nginx 固定 IP）
- [ ] binlog 的磁盘上限定下来（写满时 MySQL 直接停止写入） —— ⚠️ 2026-09-16 实测：根分区 29 GB / 已用 68%，余量约 9 GB。**镜像那一半已由 `prune_old_images` 堵住**（§3.1 末尾），binlog 自己的上限仍未定
- [x] compose 补上资源限制（ADR-0002 收口条件）—— 2026-09-13，见 §3.3
- [x] 边缘 nginx 的 `real_ip` 与上游超时 —— 2026-09-13，见 §4
- [ ] 边缘 nginx 的 TLS / 域名（①A：在 `infra_nginx` 那层，本层不做）
- [x] 进程模型定案（保持单进程，见 §3）
- [ ] 备份四层全部在跑，且有新鲜度监控
- [x] **做过一次恢复演练**，含主密钥恢复，并记录实际耗时 vs RTO 4 小时 —— 2026-09-14 数据库 60 秒恢复、与生产逐表校验和一致；2026-09-15 真解密演练：只凭离线 `master.key` 在断网容器里解开恢复库的 TOTP 密文，生成的码与验证器 App 一致，错误密钥被拒，从恢复到解密 4 分 12 秒（§5.2.4 末尾）。这正是 [REVIEW-LOG](REVIEW-LOG.md) 里 #52 分歧选 B 时要求补的那一步
- [x] 宿主机主密钥文件 `chown 10001:10001` + `chmod 0400` —— 2026-09-15 核对（§6）
- [ ] §95 的 17 项指标各有阈值、分级、通知对象、抑制规则、runbook 链接 —— ⚠️ 未做完：**三条被点名的已落地**（§8.1），其余 17 项业务指标要等指标管道，见 §8.3
- [x] `billing_readiness_degraded_redis` 与 celery-beat 存活探针落地 —— 2026-09-16：探针 T0.6 就有（beat 按调度状态文件 mtime 判活），本次补的是**把它送到人手里**：`deploy/monitor.sh` 每 5 分钟巡检容器健康 + `/readyz` 响应体 + 磁盘水位，三个维度各自一个 Healthchecks 检查 + Telegram（§8.1）
- [x] 日志轮转 / 保留 / 上限 / 安全删除 / 异地 / 磁盘告警 —— 2026-09-16：六项都有实现（§7 的表），生产上部署、重装 cron、第四个心跳检查、R2 的 `logs-35d` 规则四件都做完，**手工跑通一轮并从 R2 读回解开核对**（§7.1 末尾）。⚠️ 轮转仍是 json-file 的按大小窗口，保留期由归档承载；根治日志完整性要上日志聚合（§8.3）
- [x] **上线前配好 SMTP**，否则密码重置的信发不出去（outbox 会重试到死信）—— 2026-09-15：Google Workspace（`smtp.gmail.com:587` STARTTLS + 应用专用密码，发信邮箱 `developer@acuventech.com`，显示名 `Acuven Billing`）。生产端到端：`/password/forgot` → outbox 行 `SENT`（第 1 次尝试，约 3 秒）→ 管理员收到信、链接能打开重置页。SPF / DKIM / DMARC 全部 pass，**但 Outlook.com 仍判进垃圾箱**（SCL 5，`SpamFilterAuthJ`）—— 属发信信誉与内容判定，不是配置问题；Phase 4 给客户发信前要重新评估传输（ADR-0009 备选 A）
- [ ] 容量基线在**升配后的**生产机上重跑一次（[perf-baseline.md](perf-baseline.md) 第 6 节）
- [x] 部署流水线在真实 VPS 上跑通一次 —— 2026-09-14 起在真实 VPS 上多次成功；其间 run 34815465122 被冒烟拦下并**自动回滚**（§2.2），也算实地走过一次回滚路径。最近一次是 run 34952597111，部署 `6ea9dc5`。⚠️ §9.4 里「ghcr 包可见性」「先手工跑一次 `deploy.sh`」两条事后无法核实，仍未勾；push 触发器仍未加回
