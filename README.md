# Acuven Central AI Billing Platform

给 Acuven 旗下所有 AI 应用做一套独立的中心化计费平台：**请求级用量计量 + 预付 MYR 钱包 + 不可变财务账本 + 支付网关充值 + 自动扣费与停复机**。

> **当前状态：Phase 0 进行中（T0.1–T0.6 已完成，后端骨架 + MySQL / Redis / Celery + Compose 栈已通）。**
> 详细进度与下一步见 [docs/HANDOFF.md](docs/HANDOFF.md)。

---

## 从哪里开始读

| 你想做什么 | 读这个 |
| --- | --- |
| 第一次接触这个项目 | [docs/PROJECT.md](docs/PROJECT.md) —— 项目总览与**完整文档地图** |
| 知道现在做到哪、下一步是什么 | [docs/HANDOFF.md](docs/HANDOFF.md) |
| 找具体任务与验收标准 | [docs/TODO.md](docs/TODO.md) |
| 写代码前理解架构与财务不变量 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 需要精确定义 | [spec](docs/Acuven_Central_AI_Billing_Platform_Spec.md) —— **唯一事实来源** |
| 开 PR / 做审查 | [docs/WORKFLOW.md](docs/WORKFLOW.md) |

## 本地跑后端

```bash
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload                    # http://127.0.0.1:8000/healthz
```

这样起的是**孤零零一个 API 进程**：没有数据库、没有 Redis，`/healthz` 照常应答，`/readyz` 会如实报未配置。要连着依赖一起跑，用下面那套。

## 本地起整套栈

```bash
cp .env.example .env      # 然后填 BILLING_MYSQL_ROOT_PASSWORD / BILLING_MYSQL_PASSWORD
docker compose up -d --build
docker compose run --rm api alembic upgrade head    # 迁移是显式一步，见下
curl http://127.0.0.1:8080/healthz
```

六个服务：`nginx` · `api` · `celery-worker` · `celery-beat` · `redis` · `mysql`。
spec §99 的清单是**七**个 —— `frontend` 还不存在（[TODO](docs/TODO.md) 的 T0.7 才建 React 骨架），
在那之前 `/` 返回一句说明用的 503。

几条不是随手选的默认：

- **只发布 nginx 一个端口**（默认 `8080`）。MySQL / Redis / api 都不对宿主机开放；
  要连库就 `docker compose exec mysql mysql -u billing -p billing`
- **密码留空就起不来**。`.env.example` 里那两个密码是空的，`docker compose up` 会
  直接报错并指名该设哪个变量 —— 而不是悄悄起一个空密码的数据库
- **迁移不在启动时自动跑**。多个 API 实例会同时迁移同一个库（spec §98、§100），
  所以它是上面那条显式命令
- **`/readyz` 只对私有网段开放**：它逐个报出依赖状态，等于公开内部拓扑
- 容器以非 root（UID `10001`）运行。这个数字是 [ADR-0004](docs/adr/ADR-0004-credential-encryption.md)
  要求 Phase 0 定下来的：宿主机的主密钥文件要按它设属主

停掉：`docker compose down`；连数据一起删：`docker compose down -v`。

## 提 PR 前必须本地跑过

命令清单只在 [docs/WORKFLOW.md §7](docs/WORKFLOW.md) 一处，这里不重复。

CI 跑五项：`docs` / `scripts` / `policy` / `backend` / `secret-scan`，都是 `main` 的必需检查。

## 开发流程

Claude Code 主力开发 · Codex 独立审查 · Kelvin 最终验收。碰钱的改动（钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机）要先过**设计闸门**，再过**实现闸门**。完整规则见 [docs/WORKFLOW.md](docs/WORKFLOW.md)。

## ⚠️ 本仓库是公开的

绝不提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价。仓库转公开的决策与代价见 [ADR-0001](docs/adr/ADR-0001-repository-visibility.md)。
