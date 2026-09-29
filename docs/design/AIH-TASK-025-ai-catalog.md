# AIH-TASK-025 设计：供应商、模型与计量单位目录（已批准 v4）

> **来源**：设计闸门 Issue #163。本文件是 `APPROVED: design v4` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #163 上被批准的 v4 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-A」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-025`。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。目录本身不动钱，但它是定价的根：供应商价格（T-B）、定价规则（T-D）按它的 id 挂，计费 worker（T-H）靠它把上报的 provider / model 字符串解析成模型 —— 解析错了就是按错的价格扣钱。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v4`（v1：计量类型只读收窄 §58 → v2 改为可新建；v2：别名可改指向 → v3 按 `occurred_at` 分段；v3：边界在提交前确定、靠余量与解析错开 → v4 解析与改映射用供应商行锁串行，见 §12）
对应需求：spec §11、§12、§15.1、§58、§74、§84；`REQ-PRICE-001`（间接：价格与规则挂在本目录上）
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-A（PR #162）

## 1. 目标与边界

- **要解决的问题**：spec §74 要求 `ai_providers`、`ai_models`、`usage_meter_types` 三张表，但没给字段；§58 要求管理员能管理供应商、模型、计量单位；§84 要求用量里出现未知模型时「保留事件 → 标 `MODEL_UNKNOWN` → 管理员映射或新增模型 → 重新处理」。现在三样都没有，Phase 2 后面的价格、规则、计费都挂不上去。
- **可观察的完成标准**：
  1. 迁移后库里有 9 个计量类型（§12 的清单）与 12 个计价分量（§15.1 的清单），每个分量写明它从上报事件的哪个字段取数量。
  2. 管理员能新建计量类型（连同它的计价分量），能改计量类型的显示名与状态（§58「Metering unit」）；**新建的类型不改代码就能被上报、被计价**（§2「上报形态」）。
  3. 管理员能建 / 列 / 查 / 改供应商与模型，能把上报的模型字符串映射到模型（别名）、改映射、撤销映射，所有改动都有审计。
  4. 一个只读的解析函数 `resolve_model(provider_code, model_code, occurred_at)`：按「模型代码精确匹配 → 在 `occurred_at` 那一刻生效的别名」返回模型，都不中返回「未知」。**一个 `(供应商, 字符串, occurred_at)` 一旦解析到某个模型，以后永远解析到同一个模型**（§3「本任务自己的不变量」②）。T-H 调用它；本任务只提供函数与测试，不接到任何计费路径。
- **明确不做什么**：
  - 价格、定价规则、FX、用量事件表 —— T-B / T-D / T-C / T-E
  - 未知模型事件的重新入队 —— T-H（本任务只保证「映射一建好，下一次解析就能命中」）
  - 给已有计量类型追加分量、或新建多字段（像 `LLM_TOKEN` 那样）的计量类型 —— 那需要上报载荷加新字段，是代码改动（见 §2「上报形态」与 §10）
  - 删除任何目录行 —— 只停用，不删除（价格版本与用量事件会永久引用它们）
  - 前端 —— T-K
- **现有行为与问题证据**：`app/models/` 里没有任何供应商 / 模型相关的表；`docs/database-schema.md` 没有这几张表；最新迁移是 AIH-TASK-022 的 0011（本任务的迁移接在 020–022 之后，序号实现时定）。

## 2. 设计概要

```text
管理端请求 → Pydantic 校验（extra="forbid"）→ require_admin
  → services.ai_catalog.*（session_scope：一个事务）
      建模型 / 映射别名 / 撤销别名：锁供应商行（SELECT … FOR UPDATE）→ 查「代码与别名不撞」→ 截断当前段、开新段 → 审计 → commit
      建供应商 / 改名 / 停用：写 → 审计 → commit（唯一约束兜底）
      建计量类型：同一事务写类型行与它唯一的分量行 → 审计 → commit（唯一约束兜底）
  → success() 信封

计费（T-H 调用，本任务只提供）：
  repositories.ai_catalog.resolve_model(session, provider_code, model_code, occurred_at)
      必须在调用方（T-H）的计费事务里调用，锁持有到该事务提交：
      供应商行 SELECT … FOR SHARE（按 code 精确匹配；没有 → None）
      → 该供应商下模型 code 精确匹配（加锁读 FOR SHARE）
      → 该供应商下满足 effective_from ≤ occurred_at < effective_to 的别名段（加锁读 FOR SHARE）
      → 否则 None
```

### 数据库

五张新表。约定照 `docs/database-schema.md`「通用约定」：BigInteger 自增主键、对外 `public_id`（uuid4，CHAR(36)）、枚举存 VARCHAR + CHECK、时间 UTC 无时区截到整秒。**代码列一律 `utf8mb4_0900_bin`**（区分大小写、不忽略尾部空格），理由同 `integration_credentials.public_api_key`：库默认的 `utf8mb4_0900_ai_ci` 会把 `GPT-4o` 与 `gpt-4o` 当成同一个值。

#### 上报形态 —— 让新建的计量类型不用改代码

计价要知道「这个分量的数量从上报事件的哪个字段取」。上报载荷的字段是代码定死的（§11），所以计量类型按**上报形态**分两种，摄取（T-E）与计价（T-G）只认形态、不认具体类型：

| 上报形态 | 上报事件带什么 | 分量从哪取数 | 谁能建 |
| --- | --- | --- | --- |
| `LLM_TOKEN_FIELDS` | 四个 token 字段（非负整数），不带 `quantity` / `unit` | 每个分量对应一个 token 字段 | 只有迁移种子（`LLM_TOKEN`） |
| `QUANTITY` | `quantity`（JSON 字符串）+ `unit`，不带 token 字段 | 唯一一个分量，取 `quantity` | 种子，以及**管理员新建的所有类型** |

管理员新建一个 `QUANTITY` 类型（例如 `VIDEO_SECOND`）后：应用按 §11 的非 token 写法上报 `usage_type = VIDEO_SECOND`、`quantity`、`unit`；T-E 按该类型的 `unit` 与 `quantity_kind` 校验；T-G 取 `quantity` 乘该分量的价格 —— 全程不改代码。要新的**多字段**形态（例如分开上报两档缓存写入），才需要改载荷与代码。

**`usage_meter_types`**（计量类型：迁移种子 + 管理员新建）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `code` | VARCHAR(32) bin，UNIQUE | 即上报事件的 `usage_type`。种子：`LLM_TOKEN`、`EMBEDDING_TOKEN`、`AUDIO_SECOND`、`AUDIO_MINUTE`、`TTS_CHARACTER`、`IMAGE_GENERATION`、`OCR_PAGE`、`DOCUMENT_PAGE`、`CUSTOM`（§12）。建后不可改 |
| `display_name` | VARCHAR(255) | 可改 |
| `payload_shape` | VARCHAR(32)，CHECK `LLM_TOKEN_FIELDS` / `QUANTITY` | 见上。建后不可改 |
| `unit` | VARCHAR(16) bin | 上报的 `unit` 必须与它相等。种子：`TOKEN` / `SECOND` / `MINUTE` / `CHARACTER` / `IMAGE` / `PAGE` / `UNIT`。建后不可改（改单位等于重新解释历史数量） |
| `quantity_kind` | VARCHAR(16)，CHECK `INTEGER` / `DECIMAL` | 数量是整数还是小数（§11：token 为非负整数；音频秒数是小数）。建后不可改 |
| `status` | CHECK `ACTIVE` / `RETIRED` | 含义同供应商，见「停用的含义」 |
| `created_at`、`updated_at` | DATETIME | |

UNIQUE `(id, payload_shape)`：给分量表的复合外键用。另加 CHECK：`payload_shape = 'LLM_TOKEN_FIELDS'` 时 `unit = 'TOKEN'` 且 `quantity_kind = 'INTEGER'`。

**`usage_meter_components`**（计价分量）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `meter_type_id` | BIGINT | |
| `payload_shape` | VARCHAR(32) | 复合外键 `(meter_type_id, payload_shape)` → `usage_meter_types(id, payload_shape)`，RESTRICT：分量的形态**由数据库保证**与所属类型一致 |
| `component_code` | VARCHAR(64) bin，UNIQUE | 种子为 §15.1 的清单。建后不可改 |
| `quantity_field` | VARCHAR(64) | 这个分量从事件的哪个字段取数量 |
| `created_at` | DATETIME | |

- CHECK：`(payload_shape = 'QUANTITY' AND quantity_field = 'quantity') OR (payload_shape = 'LLM_TOKEN_FIELDS' AND quantity_field IN ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens'))`
- UNIQUE `(meter_type_id, quantity_field)`：同一类型下一个字段只对应一个分量，计价时不会把同一批数量算两次；加上面的 CHECK，**`QUANTITY` 类型至多一个分量**也由数据库保证。
- 「每个类型至少一个分量」数据库表达不了：由「建类型与建分量同一事务」保证（管理端没有单独建类型或单独建分量的路径）；万一出现没有分量的类型，T-G 按 `PRICING_ERROR` 处理，不按 0 计。

种子（9 个类型、12 个分量）：

| 计量类型 | 分量 | 数量取自 |
| --- | --- | --- |
| `LLM_TOKEN`（`LLM_TOKEN_FIELDS`，TOKEN，INTEGER） | `LLM_INPUT_TOKEN` | `input_tokens` |
| | `LLM_OUTPUT_TOKEN` | `output_tokens` |
| | `LLM_CACHE_WRITE_TOKEN` | `cache_creation_input_tokens` |
| | `LLM_CACHE_READ_TOKEN` | `cache_read_input_tokens` |
| `EMBEDDING_TOKEN`（`QUANTITY`，TOKEN，INTEGER） | `EMBEDDING_TOKEN` | `quantity` |
| `AUDIO_SECOND`（`QUANTITY`，SECOND，DECIMAL） | `AUDIO_SECOND` | `quantity` |
| `AUDIO_MINUTE`（`QUANTITY`，MINUTE，DECIMAL） | `AUDIO_MINUTE` | `quantity` |
| `TTS_CHARACTER`（`QUANTITY`，CHARACTER，INTEGER） | `TTS_CHARACTER` | `quantity` |
| `IMAGE_GENERATION`（`QUANTITY`，IMAGE，INTEGER） | `IMAGE_GENERATION` | `quantity` |
| `OCR_PAGE`（`QUANTITY`，PAGE，INTEGER） | `OCR_PAGE` | `quantity` |
| `DOCUMENT_PAGE`（`QUANTITY`，PAGE，INTEGER） | `DOCUMENT_PAGE` | `quantity` |
| `CUSTOM`（`QUANTITY`，UNIT，DECIMAL） | `CUSTOM` | `quantity` |

⚠️ 这两张表是**给下游的契约**：T-B 的价格分量、T-D 的 FIXED_RATE 分量按 `component_code` 挂在这里；T-E 按 `payload_shape`、`unit`、`quantity_kind` 校验上报；T-G 按 `quantity_field` 取数。「一个事件要哪些分量有价」的判定（例如缓存读取为 0 时要不要有价）归 T-G，本任务不定。

**`ai_providers`**

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `code` | VARCHAR(64) bin，UNIQUE | 与上报事件的 `provider` 精确比较，例如 `anthropic`、`openai`。建后不可改 |
| `display_name` | VARCHAR(255) | 可改 |
| `status` | VARCHAR(16)，CHECK `ACTIVE` / `RETIRED`，默认 `ACTIVE` | 见下文「停用的含义」 |
| `created_at`、`updated_at` | DATETIME | |

**`ai_models`**

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `provider_id` | FK → `ai_providers.id`，RESTRICT | |
| `code` | VARCHAR(128) bin | 与上报事件的 `model` 精确比较。建后不可改 |
| `display_name` | VARCHAR(255) | 可改 |
| `status` | 同供应商 | |
| `created_at`、`updated_at` | | |

UNIQUE `(provider_id, code)`；UNIQUE `(id, provider_id)`（给别名表的复合外键用，先例：`uq_projects_id_tenant`）。

**`ai_model_aliases`**（§84 的「映射」，按时间分段，照 §17 价格版本的做法）

每一行是「某个字符串在一段时间内指向某个模型」。一个字符串的所有段首尾相接、不重叠；改指向或撤销都只截断**当前**段、从「现在」起开新段，**已有的段从不修改指向、从不往过去延伸**。

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `provider_id` | BIGINT | |
| `model_id` | BIGINT | 复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)`，RESTRICT：别名只能指向**同一供应商**的模型，由数据库保证。建后不可改 |
| `alias` | VARCHAR(128) bin | 上报事件里出现、但不是模型代码的那个字符串（例如带日期后缀的模型名）。建后不可改 |
| `effective_from` | DATETIME NULL | `NULL` = 「一直以来」。只有这个字符串的**第一段**是 `NULL`（见下） |
| `effective_to` | DATETIME NULL | `NULL` = 当前仍生效。只能从 `NULL` 改成一个时刻（截断），截断后不再改 |
| `open_slot` | 生成列（STORED）：`effective_to IS NULL` 时为 1，否则 `NULL` | 只读 |
| `created_at`、`closed_at` | DATETIME | `closed_at` 与 `effective_to` 同时写 |

- UNIQUE `(provider_id, alias, open_slot)`：一个字符串**至多一段未截断**，由数据库保证（MySQL 的唯一约束不管 `NULL`，写法同 AIH-TASK-019 `webhook_secrets` 的 `active_slot`）。
- CHECK：`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`。
- 「各段首尾相接、不重叠」跨行，数据库表达不了：由服务层在供应商行锁内保证（新段的 `effective_from` 永远取当前段被截断的那个时刻，见下），并在每次写入后于同一事务里复查该字符串的全部段（排序后相邻段 `to == 下一段 from`、只有第一段 `from` 为 `NULL`），不满足就回滚。

**三个操作**（都在供应商行排他锁内；边界时刻 `t` 在**拿到锁之后**取：服务端当前时间**向上**取整到下一个整秒，见「时间」）：

| 操作 | 该字符串现状 | 做什么 |
| --- | --- | --- |
| 映射到模型 M | 从没有过段 | 插入第一段 `[NULL, NULL) → M`：**对过去全部生效**。过去这个字符串一律解析为「未知」、从未扣过钱，所以补上映射只会把「未知」变成 M，不会改变任何已经解析到模型的结果 |
| 映射到模型 M | 有未截断的段，且指向 M | 200，不写 |
| 映射到模型 M | 有未截断的段，指向别的模型 N | 截断当前段于 `t`，插入 `[t, NULL) → M`。`t` 之前发生的事件（不论处理了没有）永远按 N |
| 映射到模型 M | 所有段都已截断（撤销过），最后一段止于 `t0` | 插入 `[t0, NULL) → M`：补上撤销以来的空档。空档里的事件一直是「未知」、没扣过钱，补上是安全的 |
| 撤销 | 有未截断的段 | 截断于 `t`。`t` 起发生的事件解析为「未知」（进 `MODEL_UNKNOWN`，不扣费、可见） |
| 撤销 | 没有未截断的段 | 404 `AI_MODEL_ALIAS_NOT_FOUND` |

由此，映射表的每一次变动只可能：把某些时刻的结果从「未知」变成某个模型；或只影响 `t` 及以后发生的事件。**已经解析到某个模型的 `(字符串, 时刻)` 永远不会改成另一个模型。**

**「模型代码」与「别名」不能撞**：同一供应商下，一个字符串不能既是某个模型的 `code`、又在别名表里出现过（**不论哪一段、是否已截断**），否则解析顺序会让别名永远不生效，或让一个曾经解析到别名目标的字符串改解析到同名模型。跨两张表的约束数据库表达不了，由服务层在**供应商行锁内**检查（建模型、映射别名两条路径都先 `SELECT … FOR UPDATE` 供应商行），两条路径串行，不会同时插进撞车的行。

### 接口

全部只有 ADMIN，处理函数第一条语句 `require_admin`，加进 `EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES`。路径里的 id 一律是 `public_id`；响应不含内部自增 id。列表按 §108 分页（沿用 `Page[...]`、`page` / `page_size`），按 `code` 升序。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/v1/admin/usage-meter-types` | 计量类型列表（含各自的分量），可按 `status` 筛；分页，按 `code` 升序 |
| `POST /api/v1/admin/usage-meter-types` | 新建 `QUANTITY` 类型：`code`、`display_name`、`unit`、`quantity_kind`、`component_code`（同一事务建出它唯一的分量） |
| `GET /api/v1/admin/usage-meter-types/{meter_type_id}` | 计量类型详情（含分量） |
| `PATCH /api/v1/admin/usage-meter-types/{meter_type_id}` | 只收 `display_name`、`status` |
| `GET /api/v1/admin/ai-providers` | 供应商列表，可按 `status` 筛 |
| `POST /api/v1/admin/ai-providers` | 建供应商：`code`、`display_name` |
| `GET /api/v1/admin/ai-providers/{provider_id}` | 供应商详情 |
| `PATCH /api/v1/admin/ai-providers/{provider_id}` | 只收 `display_name`、`status` |
| `GET /api/v1/admin/ai-providers/{provider_id}/models` | 该供应商的模型列表，可按 `status` 筛 |
| `POST /api/v1/admin/ai-providers/{provider_id}/models` | 建模型：`code`、`display_name` |
| `GET /api/v1/admin/ai-providers/{provider_id}/models/{model_id}` | 模型详情（含当前指向它的别名） |
| `PATCH /api/v1/admin/ai-providers/{provider_id}/models/{model_id}` | 只收 `display_name`、`status` |
| `GET /api/v1/admin/ai-providers/{provider_id}/model-aliases` | 该供应商的别名段列表（全部历史），可按 `alias` 筛、可只看当前生效的；按 `alias`、`effective_from` 排序 |
| `POST /api/v1/admin/ai-providers/{provider_id}/model-aliases` | 映射：`alias`、`model_id`（模型的 `public_id`），按上表的三种情形处理；返回当前生效的那一段 |
| `POST /api/v1/admin/ai-providers/{provider_id}/model-aliases/{alias_id}/retire` | 撤销：`alias_id` 必须是当前未截断的那一段，否则 404 |

别名段没有 PATCH：段一旦写下就不改指向，改映射只能再 POST 一次（开新段）。

字段规则：

| 字段 | 规则 |
| --- | --- |
| 计量类型 `code`、`component_code` | `^[A-Z][A-Z0-9_]{1,31}$`（与种子同一写法；`component_code` 列宽 64，管理员新建时同样限 32） |
| `unit` | `^[A-Z][A-Z0-9_]{0,15}$` |
| `quantity_kind` | `INTEGER` / `DECIMAL` |
| 供应商 `code` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| 模型 `code`、`alias` | `^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$`（不含空白；覆盖 `claude-sonnet-4-5-20250929`、`gpt-4o-mini-transcribe`、`models/gemini-x` 这类写法） |
| `display_name` | 去首尾空白后 1–255 |
| `status` | `ACTIVE` / `RETIRED` |

- 新建计量类型不收 `payload_shape`（固定为 `QUANTITY`）；带 `payload_shape` 或 `quantity_field`：422。
- PATCH 带 `code` / `alias` / `provider_id` / `unit` / `quantity_kind` / `payload_shape` / `component_code` 或任何多余字段：422。代码不可改是为了让价格、规则、历史事件看到的永远是同一个字符串。
- PATCH 没有实际变化：200，不写审计（与编辑客户一致）。
- 映射请求的 `model_id` 必须是**同一供应商**下的模型，否则 404 `AI_MODEL_NOT_FOUND`（不泄露别家的模型是否存在）。

错误：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `USAGE_METER_TYPE_NOT_FOUND` | 计量类型不存在 | 否 |
| 404 | `AI_PROVIDER_NOT_FOUND` / `AI_MODEL_NOT_FOUND` / `AI_MODEL_ALIAS_NOT_FOUND` | 路径或请求体里的 id 不存在或不属于该供应商 | 否 |
| 409 | `USAGE_METER_TYPE_CODE_TAKEN` | 计量类型 `code` 已存在 | 否 |
| 409 | `USAGE_METER_COMPONENT_CODE_TAKEN` | `component_code` 已存在（任何类型下） | 否 |
| 409 | `AI_PROVIDER_CODE_TAKEN` | 供应商 `code` 已存在 | 否 |
| 409 | `AI_MODEL_CODE_TAKEN` | 同供应商下已有同名模型，**或该字符串在别名表里出现过** | 否 |
| 409 | `AI_MODEL_ALIAS_TAKEN` | 映射的字符串与同供应商下某个模型的 `code` 相同 | 否 |

### 停用（`RETIRED`）的含义

**停用只影响「以后选不选它」，不影响计费。**

- 管理端：列表默认显示全部，可筛；T-B / T-D 建新价格、新规则时不许选 `RETIRED` 的计量类型、供应商或模型（由那两个任务的闸门执行，本任务写成契约）。
- 计量类型：`RETIRED` **不影响**摄取与计价（T-E 照常接收该 `usage_type`，T-G 照常计价），理由同下一条。
- 解析：`resolve_model` **不看**供应商与模型的状态 —— 用量已经发生，钱照样要算；停用一个模型不能让它的在途事件变成 `MODEL_UNKNOWN`。别名没有状态，只有时间段：撤销 = 截断，只影响此后发生的事件。
- `ACTIVE ↔ RETIRED` 双向都允许，没有终态。

### 事务边界

每个写接口一个 `session_scope`：（需要时锁供应商行）→ 校验 → 写 → 审计 → 提交。任何一步失败整体回滚。只锁供应商行，不碰钱包与租户，不与 `post_transaction` 的钱包 → 租户顺序形成环。

### 外部系统与异步边界

无。不写 outbox：目录变化没有下游订阅者；未知模型事件的重新入队是 T-H 的事，T-H 从数据库扫描，不靠本任务发事件。

### 时间

`created_at` / `updated_at` / `closed_at` 取服务端 `now` 截到整秒，UTC 无时区（与 006 / 011 / 012 一致）。

**解析与改映射靠供应商行锁串行，不靠时间余量**（v4）：

- 改映射（映射 / 撤销）：`SELECT … FOR UPDATE` 锁供应商行 → **拿到锁之后**取 `now`，`t` = `now` 向上取整到下一个整秒（`now` 恰好是整秒时取 `now + 1s`）→ 写段 → 提交后才释放锁。
- 解析（T-H）：在计费事务里 `SELECT … FOR SHARE` 锁供应商行 → **拿到锁之后**取 `now_r` → 只有 `occurred_at ≤ now_r` 才解析（否则这条事件本轮不处理，留在 `RECEIVED`）→ 解析结果写进事件快照 → 计费事务提交后才释放锁。
- 两把锁互斥（共享锁与排他锁冲突，共享锁之间不冲突），所以任意一次解析与任意一次改映射在时间上严格先后：
  - **解析在前**：改映射要等计费事务提交才拿到锁，于是 `occurred_at ≤ now_r` < 计费提交 ≤ 改映射拿到锁 ≤ `now` < `t`，这条事件在新表里仍落在旧段，与它的快照一致。
  - **改映射在前**：解析要等改映射提交才拿到锁，读到的就是新表；`occurred_at ≥ t` 的得新模型，`< t` 的得旧模型，与此后任何时刻读到的结果都一致。
- ⚠️ **别名与模型必须用加锁读**（`FOR SHARE`）：MySQL 默认的 REPEATABLE READ 下，普通 `SELECT` 读的是本事务第一次一致性读时建立的快照。T-H 在锁供应商行之前很可能已经读过用量事件，此时普通 `SELECT` 可能读到改映射提交之前的旧段 —— 锁拿到了、数据却是旧的。加锁读总是读最新已提交版本。
- 所有时刻都取应用服务器的同一时钟（单主机）。`occurred_at` 与 `effective_from` / `effective_to` 同为 UTC 无时区，比较按半开区间 `[from, to)`。
- **锁顺序**：T-H 的计费事务先锁供应商行（共享），再按 `post_transaction` 的既有顺序锁钱包 → 租户；改映射只锁供应商行。没有任何路径先持钱包或租户锁再去锁供应商行，不形成环。共享锁之间不冲突，计费路径只与低频的管理端改映射 / 建模型争锁。

### 金额

不适用：五张表都没有金额列。

### 审计

| `action` | 什么时候 | `entity_type` / `entity_id` | 前后状态 |
| --- | --- | --- | --- |
| `USAGE_METER_TYPE_CREATE` | 新建计量类型 | `usage_meter_type` / `public_id` | 后：`code`、`display_name`、`payload_shape`、`unit`、`quantity_kind`、`status`、分量的 `component_code` |
| `USAGE_METER_TYPE_UPDATE` | 改名或改状态 | 同上 | 变化的字段前后值 |
| `AI_PROVIDER_CREATE` | 建供应商 | `ai_provider` / `public_id` | 后：`code`、`display_name`、`status` |
| `AI_PROVIDER_UPDATE` | 改名或改状态 | 同上 | 变化的字段前后值 |
| `AI_MODEL_CREATE` | 建模型 | `ai_model` / `public_id` | 后：供应商 `code`、`code`、`display_name`、`status` |
| `AI_MODEL_UPDATE` | 改名或改状态 | 同上 | 变化的字段前后值 |
| `AI_MODEL_ALIAS_MAP` | 映射（新建第一段、改指向、撤销后重新映射；「指向相同」不写） | `ai_model_alias` / 新段的 `public_id` | 前：被截断那一段的目标模型 `code` 与 `effective_to`（没有则空）；后：供应商 `code`、`alias`、新段的目标模型 `code`、`effective_from` |
| `AI_MODEL_ALIAS_RETIRE` | 撤销 | `ai_model_alias` / 被截断那一段的 `public_id` | 前：`effective_to` 为空；后：`effective_to`。指向一律用模型 `code` 表示，不用内部 id |

八个动作都不在 spec §66 的清单里，按 `PROJECT_CREATE`、`WEBHOOK_SECRET_*` 的先例补上并记进 `docs/TODO.md`。操作者带 ip 与 user agent。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| T-B / T-D | 价格分量、FIXED_RATE 分量用 `usage_meter_components.component_code`；不许给 `RETIRED` 的计量类型 / 供应商 / 模型建新版本或新规则 |
| T-E | 校验**按上报形态查表，不写死类型清单**：`usage_type` 查 `usage_meter_types`；`LLM_TOKEN_FIELDS` 要四个 token 字段、不许带 `quantity` / `unit`；`QUANTITY` 要 `quantity` 与 `unit`、`unit` 等于该类型的 `unit`、数量按 `quantity_kind` 校验、不许带 token 字段。`usage_type` 不在表里时是拒绝还是保留为错误状态，由 T-E 的闸门定。上报的 `provider` / `model` 原样存（计划第 1 节第 5 条的两列），长度上限不小于本任务的 64 / 128 |
| T-G | 分量从 `quantity_field` 取数，不按类型写分支；类型没有分量按 `PRICING_ERROR` |
| T-H | ① `resolve_model` 必须在**写事件快照、扣费的同一个事务**里调用，它加的供应商行共享锁持有到该事务提交；不许在单独的短事务里解析、再到另一个事务里用结果。② 只处理 `occurred_at ≤` 持锁后当前时间的事件，更晚的留在 `RECEIVED` 下一轮再取。③ 锁顺序：供应商（共享）→ 钱包 → 租户。④ T-B / T-D 按 `occurred_at` 选价格与规则时，发布方同样要与解析串行（由那两个闸门定，本任务只提出要求） |
| T-E | 把 `occurred_at` 允许的未来偏差限制在一个小值（由 T-E 定），使 T-H 第 ② 条的推迟只是短暂等待 |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 不在 AI 调用路径上 | — | — |
| INV-2 事件不重复扣费 | 否（间接） | 别名改指向后，已处理的事件被按新模型重算 | 解析结果写进事件快照（T-H 契约）；本任务不提供任何重算入口 | T-H 闸门验证 |
| INV-3 支付不重复入账 | 否 | — | — | — |
| INV-4 余额只经账本变动 | 否 | 不碰钱包 | 服务层不导入钱包模型 | — |
| INV-5 历史账本不可变 | 否 | — | — | — |
| INV-6 事件保留版本引用 | **是** | ① 删除或改代码后，价格版本与历史事件引用的计量类型 / 分量 / 供应商 / 模型变了或没了；改计量类型的单位让历史数量被重新解释。② 已上报、尚未处理的事件在别名改指向后被解析到新模型，按错的价格与规则计费 | ① 只停用不删除；外键 RESTRICT；`code` / `alias` / `component_code` / `unit` / `quantity_kind` / `payload_shape` 建后不可改（PATCH 422）。② 别名按 `occurred_at` 分段，已有段从不改指向、不往过去延伸；改映射与解析用供应商行锁串行（排他 / 共享），`t` 在拿到锁之后取；解析用加锁读；T-H 只处理 `occurred_at ≤` 持锁后当前时间的事件 | ① PATCH 带这些字段的 422 用例；数据库层删有引用的行被外键拒绝（MySQL）。② 见 §7「改指向不影响过去」「撤销与重新映射」「边界时刻」「解析与改映射并发」「快照读陷阱」 |
| INV-7 客户不可见成本毛利 | 否 | 目录没有成本字段；接口只有管理端 | 只有 `/admin` 路由 | 越权用例 |
| INV-8 租户不可互访 | 否 | 目录是全局数据，不属于任何租户 | 没有租户维度 | — |
| INV-9 对话内容不入库 | 否 | — | — | — |
| INV-10 金额用 Decimal | 否 | 无金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否（间接） | 同 INV-2 | 同 INV-2 | — |
| INV-12 定稿对账单不可变 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是**（审计） | 目录改了而审计没写 | 写与审计同一事务 | 审计写入抛错的回滚用例（SQLite 与 MySQL 各一次） |
| INV-14 队列丢失不毁持久工作 | 否 | 没有异步处理 | — | — |

**本任务自己的两条不变量**：① 一个计量类型的每个分量与它的上报形态一致、同一字段不对应两个分量（否则同一批数量被计两次）。控制：复合外键 + CHECK + 唯一约束，全在数据库层。验证：MySQL 上直接插入形态不符的分量、给 `QUANTITY` 类型插第二个分量，都被拒绝。② **一个 `(供应商, 字符串, 时刻)` 一旦解析到某个模型，以后永远解析到同一个模型**；映射表的变动只会把「未知」变成某个模型，或只影响边界 `t` 及以后。控制：模型代码建后不可改、不删除；别名段不改指向、不往过去延伸，只有第一段对过去生效且只在该字符串从没出现过时建；撤销后重新映射只补空档；模型代码与「出现过的任何别名」不撞（供应商行锁内检查）；至多一段未截断（生成列 + 唯一约束）；同一事务内复查段的首尾相接。验证：§7「解析的时间稳定性」性质用例，以及并发用例（真 MySQL）。

## 4. 状态与并发

| 对象 | 当前状态 | 目标 | 结果 |
| --- | --- | --- | --- |
| 计量类型 / 供应商 / 模型 | `ACTIVE` | `RETIRED` | 200，审计 |
| 同上 | `RETIRED` | `ACTIVE` | 200，审计 |
| 同上 | 与当前相同 | — | 200，不写 |

别名不是状态机，是时间段序列，三种操作见 §2 的表。

- **串行化**：建模型、映射别名、撤销别名都在供应商行排他锁内；计费解析在供应商行共享锁内，与前三者互斥、彼此之间不互斥；建供应商、建计量类型只靠唯一约束。
- **数据库保证的唯一性**：`usage_meter_types.code`；`usage_meter_components.component_code`；`(meter_type_id, quantity_field)`；`ai_providers.code`；`(provider_id, code)`；别名的 `(provider_id, alias, open_slot)`（至多一段未截断）。
- **幂等键**：无。重发建请求得到 409（已存在），不会建出两行；重发 PATCH 无变化即 200 不写。
- **相同 ID、不同载荷**：不适用。
- **重试层**：服务端不重试。
- **原子提交**：目录行与审计；新建计量类型时类型行、分量行与审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（代码格式、多余字段、PATCH 带 `code`） | 422 | 无改动 | 改请求后可 | — | 无 |
| 路径 id 不存在 / 别名指向别家模型 / 撤销的不是当前段 | 404 | 无改动 | 否 | — | 无 |
| 段首尾相接的复查不通过（不应发生，防实现错误） | 500 | 整体回滚 | 否 | 修代码 | 错误日志 |
| 代码或别名已被占用（含并发下唯一约束冲突） | 409 | 无改动 | 否 | — | 无 |
| 审计写入或提交失败 | 500 | 整体回滚 | 可 | — | 错误日志 |
| 锁等待超时 | 500 | 回滚 | 可 | — | 错误日志 |
| 提交成功、响应丢失 | 客户端超时 | 已写 | 重发建请求得 409，按详情接口确认 | — | 无 |
| 迁移种子与代码里的常量不一致 | 启动时无感，计价时才错 | — | — | — | 用例在迁移后逐行比对种子与代码常量 |
| Redis / Celery 丢失、外部服务超时 | 不适用 | 本任务不用 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：不适用，目录是全局数据；本任务没有任何客户侧接口。
- **鉴权主体**：只有 ADMIN（`require_admin`，角色以数据库为准）。
- **禁止返回的字段**：内部自增 id。目录没有成本、毛利或密钥字段。
- **日志 / 审计 / 异常**：审计前后状态只有代码、显示名、状态；不含个人数据。
- **密钥**：不涉及。
- **prompt / response 或客户数据**：不处理。
- **保留与删除**：不删除任何行。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 种子正确 | integration（迁移后） | 9 个计量类型、12 个分量逐行比对 §12 / §15.1 与代码常量（含形态、单位、数量类型、取数字段） | 完全一致 |
| 新建计量类型 | integration（API） | 建 `VIDEO_SECOND`（SECOND，DECIMAL，分量 `VIDEO_SECOND`）→ 详情 → 改名 → 停用 | 类型为 `QUANTITY`、恰好一个分量取 `quantity`；每步审计 |
| 形态由数据库保证 | integration（MySQL） | 直接插入：`QUANTITY` 类型下取 `input_tokens` 的分量；`QUANTITY` 类型的第二个分量；`LLM_TOKEN_FIELDS` 类型下取 `quantity` 的分量；形态与所属类型不符的分量；`LLM_TOKEN_FIELDS` 且单位不是 `TOKEN` 的类型 | 全部被 CHECK / 外键 / 唯一约束拒绝 |
| 新建计量类型的边界 | integration | 带 `payload_shape` / `quantity_field`；`code` 或 `component_code` 与已有重复（含与种子重复）；小写 `code`；`unit` 非法 | 422 / 409，不写库；409 时类型行与分量行都没写 |
| 新建事务中途失败 | integration（SQLite 与 MySQL 各一次） | 分量写入抛错、审计写入抛错 | 类型行也回滚 |
| 正常路径 | integration（API） | 建供应商 → 建模型 → 映射别名 → 改名 → 停用 → 复用 | 各 200 / 201；每次真实变化一条审计，前后状态正确 |
| 解析顺序 | unit | 模型代码命中；别名命中；`RETIRED` 模型仍命中；未知供应商 / 未知模型返回 None；大小写不同不命中 | 同左 |
| 第一次映射对过去生效 | integration | 字符串 X 从没映射过，映射到 M | `resolve_model(X, 很久以前)` 与 `(X, 现在)` 都得 M |
| 改指向不影响过去 | integration | X → M；在时刻 T 改指向 N | `occurred_at < t` 得 M、`≥ t` 得 N；旧段的 `model_id` 未变 |
| 撤销与重新映射 | integration | X → M；撤销于 t1；过一会儿重新映射到 N | `[.., t1)` 得 M；撤销期间发生的时刻在重新映射前得 None、之后得 N；`≥` 重新映射时刻得 N；共三段首尾相接 |
| 边界时刻 | unit | 冻结时钟在整秒与非整秒两种情况下改指向 | `t` 严格晚于 `now`、是整秒；`occurred_at = t - 1µs` 得旧、`= t` 得新 |
| 解析与改映射并发 | integration（真 MySQL，两个连接） | ① 改映射事务拿到锁、写完新段后**暂停不提交**；另一连接开计费事务调 `resolve_model`（`occurred_at ≥ t`） ② 反过来：计费事务解析后**暂停不提交**；另一连接改映射 | ① 解析阻塞到改映射提交，返回新模型 ② 改映射阻塞到计费事务提交，取到的 `t` 晚于被解析事件的 `occurred_at`；两种顺序下，提交后再解析同一 `(字符串, occurred_at)` 与快照一致 |
| 快照读陷阱 | integration（真 MySQL） | 计费事务先对别的表做一次普通读（建立快照）→ 另一连接改映射并提交 → 计费事务调 `resolve_model` | 返回新段的结果（加锁读），不是快照里的旧段 |
| 未来时刻的事件 | unit | `occurred_at` 晚于持锁后当前时间 | `resolve_model` 的调用方契约：T-H 不处理（本任务提供一个判定函数与用例，接线归 T-H） |
| 共享锁互不阻塞 | integration（真 MySQL） | 两个计费事务同时解析同一供应商 | 都不阻塞 |
| 解析的时间稳定性（性质用例） | unit | 随机生成一串「建模型 / 映射 / 改指向 / 撤销」操作，每步之后对一组固定的历史时刻（都早于该步的 `t`）解析 | 任何一个 `(字符串, 时刻)` 一旦得到模型 M，之后每一步都仍得 M |
| 指向相同 | integration | 已指向 M 时再映射到 M | 200，不写段、不写审计 |
| 撤销不存在的段 | integration | 撤销已截断的段；撤销从没映射过的 | 404，不写 |
| 代码与别名不撞 | integration | 建与别名表里**出现过**（含已撤销）的字符串同名的模型；映射与现有模型同名的字符串 | 409，不写 |
| 至多一段未截断 | integration（MySQL） | 直接插入同一字符串的第二个未截断段 | 唯一约束拒绝 |
| 段不能倒置 | integration（MySQL） | 直接插入 `effective_from ≥ effective_to` 的段 | CHECK 拒绝 |
| 并发撞车 | integration（真 MySQL） | 两个线程同时建同名模型；同时把同一字符串映射到不同模型；一个建模型一个映射同名字符串 | 串行完成：同名模型恰好一个成功；两次映射都成功但形成首尾相接的两段（或第二次「指向相同」）；模型与别名撞车的一方 409；库里段的不变量成立 |
| 别名跨供应商 | integration | 映射到另一供应商的模型；数据库层直接插入被复合外键拒绝（MySQL） | 404；外键错误 |
| 不可改代码 | integration | PATCH 带 `code` / `alias` / `provider_id`；计量类型 PATCH 带 `unit` / `quantity_kind` / `payload_shape` / `component_code` | 422，不写 |
| 边界值 | integration | 代码 64 / 65、128 / 129 字符；含空白；首字符非法；`display_name` 空白 / 256 | 422 |
| 幂等 | integration | PATCH 无变化 | 200，不写审计 |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 审计写入抛错、提交抛错 | 全部回滚 |
| 不可删除 | integration（MySQL） | 直接删有别名引用的模型 | 外键拒绝 |
| 鉴权与越权 | integration | 匿名 401、CUSTOMER 403；首条语句 `require_admin`（AST 用例）；路由枚举 | 同左 |
| 响应字段 | integration | 全部接口 | 没有内部 id |
| 迁移 | integration（MySQL） | upgrade 建表与种子；CHECK 拒绝非法取值；downgrade 删表 | 同左 |
| 金额精度、Redis / Celery | 不适用 | 无金额、无异步 | — |

## 8. 迁移与上线

- **数据迁移步骤**：建五张表；`usage_meter_types` 与 `usage_meter_components` 写入 §2 的种子。迁移序号取实现时的下一个（预计接在 022 的 0011 之后）。种子常量在迁移文件里写死，不 import 应用代码（迁移要能在代码后来改动后重放）。
- **锁表与性能影响**：全是新表，不锁已有表。
- **兼容窗口**：旧代码不读新表，无影响。
- **部署顺序**：合并即自动部署，先迁移后代码。
- **回滚或前滚**：downgrade 按外键依赖倒序删表。生产上若已录入供应商 / 模型，回滚会丢掉 —— 那时前滚修复。
- **部分部署时的行为**：迁移成功、代码未更新：新表空置无人读。代码更新、迁移失败：容器启动失败，旧容器继续服务。
- **生产上的初始数据**：本任务**不**预置任何供应商与模型；上线后由管理员经接口录入（试点要的是 `anthropic` 与 `openai`）。
- **监控指标和告警负责人**：无。未知模型告警归 T-H。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**（v2）：计量类型按上报形态分两种；管理员可新建 `QUANTITY` 类型（连同唯一的分量），形态与取数字段的搭配由数据库约束保证 | 满足 §58；新类型不改代码就能上报与计价；「同一字段算两遍」与「形态不符」都由数据库挡住 | 管理员可能建出没人上报的类型（无害，只是空置）；多字段形态仍要改代码 | — |
| v1：计量类型与分量只读（迁移种子） | 实现最小 | 收窄 §58，要写偏离 ADR 并改 spec | Codex 判 v1 阻断；Kelvin 选择满足 §58 |
| 管理员可任意建分量、自由填 `quantity_field` | 最灵活 | 能把同一字段挂到两个分量上重复计费；能挂到载荷里不存在的字段 | 取数字段由载荷决定，用形态约束，不开放自由填写 |
| 未知模型只允许「新增模型」，不做别名表 | 少一张表 | 供应商常给同一模型带日期后缀的多个名字；每个都建成独立模型，就要各自建价格，漏一个就 `PRICING_ERROR` | §84 明写「maps/adds」，映射就是别名 |
| 解析时忽略大小写或做规范化 | 应用侧写法不一致也能命中 | 静默合并本应不同的名字；错误的上报被掩盖 | 精确比较，未知就进 `MODEL_UNKNOWN` 让人看到，再用别名显式映射 |
| 停用的模型不参与解析 | 「停用」语义更强 | 在途用量变成 `MODEL_UNKNOWN`，已发生的用量收不到钱 | 停用只影响新价格 / 新规则的选择 |
| **采用**（v3 + v4）：别名按 `occurred_at` 分段；第一次映射与撤销后的空档可对过去生效；解析与改映射用供应商行锁串行，`t` 在拿到锁后取 | 与 §16 / §17「按 `occurred_at` 选、已发布不改」一致；已解析到模型的结果永不改变，不依赖任何时间余量；§84「映射后重新处理」对积压的未知事件照样有效 | 计费事务多持一把共享锁；T-H 要推迟未来时刻的事件；解析必须用加锁读 | — |
| v3：`t` 在提交前确定，靠 T-H 的时间余量错开 | 计费路径不加锁 | 改映射事务提交慢过余量时，worker 读到旧段并快照，提交后同一时刻改解析为新模型（Codex 判 v3 阻断） | 余量只是概率上的保护，锁是确定性的 |
| 不加锁，改映射把 `t` 设在很远的将来（例如一小时后） | 不碰计费路径 | 仍是时间上的赌；映射错误要等很久才纠正 | 同上 |
| v2：别名一行、可 PATCH 改指向 | 简单 | 已上报未处理的旧事件在改指向后按新模型计费（Codex 判 v2 阻断） | 见上 |
| 在摄取时（T-E）就解析模型并写死在事件上 | 改映射不影响已接收的事件 | 未知事件仍要在处理时重新解析，两处解析、两套时间语义；摄取路径多一次查询，而 §20 要求摄取尽量只做持久化 | 分段映射只有一处解析、一套语义 |
| 别名不可改指向，只能撤销 | 最简单 | 撤销后该字符串永远无法再映射（改指向与重新映射都做不到），映射错了就只能靠 Phase 8 reprocess | 分段映射把「改」表达成「截断 + 开新段」，不改旧段 |

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-09-28 两条都同意（原文「同意」），结论如下：
  1. **计量类型可管理**：v1 的「只读」被 Codex 判阻断后，Kelvin 选择满足 §58（原文「2」，即三个选项中的「改成真的能管理」）。v2 按 §2「上报形态」实现：管理员可新建 `QUANTITY` 类型；多字段形态仍随代码增加。
  2. **缓存写入只有一种价格**：Anthropic 的缓存写入按缓存时长（5 分钟 / 1 小时）有两档价，但 §11 的上报载荷只有一个 `cache_creation_input_tokens`，平台分不出是哪一档。V1 只设一个 `LLM_CACHE_WRITE_TOKEN`，价格按试点实际使用的那一档录入；若以后要用两档，要同时扩上报载荷（应用侧分开上报）与分量种子，那时另开任务。**已同意记为已知限制**，实现时写进 `docs/TODO.md`。
- **需要谁拍板**：已拍板（Kelvin）。
- **尚未验证的假设**：MySQL 8.4 上复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)` 需要被引用列上有唯一索引 —— 由 UNIQUE `(id, provider_id)` 提供，`projects` 已用同一做法；分量表的 `(meter_type_id, payload_shape)` → `usage_meter_types(id, payload_shape)` 同理，由 UNIQUE `(id, payload_shape)` 提供。MySQL 8.4 执行分量表那条带 `OR` 的 CHECK（8.0.16 起 CHECK 生效）。迁移测试在 CI 的 MySQL 上实跑。
- **如果假设或口径错误**：「上报形态」若被判不足以覆盖 §58 → 设计版本 +1 再送审；第 2 条不同意 → 本任务就拆出两档缓存写入分量，T-E 的载荷契约同步改。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- 设计版本一变，之前的 APPROVE 自动作废
- 实质修改：改了契约、表结构、事务边界、状态机、失败语义、不变量控制

### 设计闸门判定

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（批准后登记为 Worker 任务的 `allowed_change_paths`）

- `alembic/versions/<日期>_<序号>_ai_catalog.py`（实现时定名）
- `app/models/ai_catalog.py`（新）、`app/models/__init__.py`
- `app/models/auth.py`（八个审计动作）
- `app/repositories/ai_catalog.py`（新，含 `resolve_model`）
- `app/services/ai_catalog.py`（新）
- `app/schemas/ai_catalog.py`（新）
- `app/api/admin_ai_catalog.py`（新）、`app/main.py`（注册路由）
- `tests/backend/test_ai_catalog_api.py`、`tests/backend/test_ai_catalog_resolve.py`、`tests/backend/test_usage_meter_api.py`（新）
- `tests/backend/test_admin_customers_api.py`（`EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES` 在这里）、`tests/backend/test_migrations.py`
- `docs/api.md`、`docs/database-schema.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-28 | 初稿 | — |
| v2 | 2026-09-28 | 计量类型由只读改为管理员可新建：加「上报形态」（`payload_shape`）与复合外键 + CHECK，新建类型固定为 `QUANTITY` 且同一事务建出唯一分量；计量类型加 `public_id`、`display_name`、`status`、`updated_at`；加三个接口（新建、详情、PATCH）、两个审计动作、三个错误码；T-E / T-G 契约改为按形态校验与取数；测试与取舍相应补充 | Codex 判 v1 REQUEST_CHANGES：只读收窄 spec §58 且无 ADR；Kelvin 选择满足 §58 |
| v3 | 2026-09-28 | 别名表改为按 `occurred_at` 分段（`effective_from` / `effective_to` / `open_slot` 生成列 + 唯一约束 + CHECK + 事务内首尾相接复查）；去掉别名的 `status` 与 PATCH，改为「映射」与「撤销」两个 POST；两个别名审计动作改名为 `AI_MODEL_ALIAS_MAP` / `AI_MODEL_ALIAS_RETIRE`；`resolve_model` 加 `occurred_at`；模型代码与别名表里出现过的任何字符串不撞；T-H 契约加「不处理未来时刻的事件」，T-E 契约加「限制未来偏差」；测试与取舍相应补充 | Codex 判 v2 REQUEST_CHANGES：别名改指向后，已接收未处理的事件会按新模型与新价格计费 |
| v4 | 2026-09-28 | 解析与改映射改为用供应商行锁串行：`resolve_model` 在调用方的计费事务里对供应商行 `FOR SHARE` 并持有到提交，模型与别名用加锁读；改映射在拿到排他锁之后才取 `t`；T-H 契约由「时间余量」改为「同一事务解析 + 只处理 `occurred_at ≤` 持锁后当前时间 + 锁顺序」；补「解析与改映射并发」「快照读陷阱」「共享锁互不阻塞」三组用例 | Codex 判 v3 REQUEST_CHANGES：`t` 在提交前确定，提交慢过余量时 worker 会读到旧段并快照 |
