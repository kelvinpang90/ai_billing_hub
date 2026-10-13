# acuven_ai_api 源文件参考副本（b57c5b0）

设计 [AI-MODULE.md](../AI-MODULE.md)（设计闸门 #281 已批准 v8）要把 `acuven_ai_api` 的一部分代码与测试搬进本仓库的 `app/ai_api/`。OpenClaw Worker 在沙箱里不联网、读不到那个仓库，所以把要参考的文件按 **`b57c5b0`**（生产上最后运行的那一版，2026-10-12 下线）原样放在这里。

- **只读参考，不是本仓库的代码**：扩展名一律加 `.txt`，不会被 import、不会被 pytest 收集、不进 ruff。不要改这些文件。
- 搬进 `app/ai_api/` 时按设计改：import 换成本仓库的、调模型改成 async、身份从 `ClientBinding` 换成「租户 × 项目」、错误换成本仓库的信封。设计与副本冲突时**以设计为准**。
- 文件名是原路径把 `/` 换成 `_` 再加 `.txt`，例如 `app/billing_rules.py` → `app_billing_rules.py.txt`。
- 设计 v3 起不再有「能力注册表」与每个能力的专用路由，所以原仓库的 `app/config.py`（能力清单与客户端绑定）、`app/main.py`（两条能力路由）、`app/contact_summary.py` 与 `docs/CONTACT_SUMMARY_V1.md` 不放在这里；客户摘要的提示词与解析归 CRM 仓库。

| 副本 | 原文件 | 参考什么 | 哪个任务用 |
| --- | --- | --- | --- |
| `app_billing_rules.py.txt` | `app/billing_rules.py` | 签名规范串、HMAC、回执判定、错误判定（逐字搬为 `client_rules.py`；`status_allows_ai` 不搬，状态在进程内读） | AIH-TASK-060 |
| `app_billing.py.txt` | `app/billing.py` | 用量载荷字段、`signed_headers`、发送与回执处理（状态查询 `allows_ai` 不搬） | AIH-TASK-060、AIH-TASK-062 |
| `app_ids.py.txt` | `app/ids.py` | ULID 生成 | AIH-TASK-060 |
| `app_provider.py.txt` | `app/provider.py` | Anthropic Messages API 调用与 `usage` 读取（改成 async、只转发子集字段、不加系统提示） | AIH-TASK-060 |
| `app_service.py.txt` | `app/service.py` | 「调模型 → 写 outbox → 返回」的顺序 | AIH-TASK-061 |
| `app_db.py.txt` | `app/db.py` | 原 `usage_outbox` 表与 `DATETIME(6)` 的写法（本仓库的 `ai_usage_outbox` 列以设计 §2 为准） | AIH-TASK-056 |
| `app_worker.py.txt` | `app/worker.py` | 领取、发送、结算、退避 | AIH-TASK-062 |
| `tests_test_billing_rules.py.txt` | `tests/test_billing_rules.py` | 签名与回执规则的用例 | AIH-TASK-060 |
| `tests_test_ai_and_billing.py.txt` | `tests/test_ai_and_billing.py` | 供应商适配器、投递与结算的用例（能力路由与状态查询的用例不搬） | AIH-TASK-060、AIH-TASK-062 |

所有 AI 任务交付后，这个目录随设计文件一起保留，作为「从哪里搬来」的比对依据。
