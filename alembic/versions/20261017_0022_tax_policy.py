"""tax policy lock and tax policy versions, with their triggers

照设计闸门 #332 v3（docs/design/AIH-TASK-077-tax-policy.md）§2「数据库」与 §8；
本迁移是 §11 的 077。只建结构，**不写任何政策**（不种子，设计 §9）。

步骤
----

0. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（与 0006 / 0015 同一个）。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**
   抛出明确的错误，`alembic_version` 仍是 0021。`--sql` 离线模式跳过。
1. `create_table tax_policy_locks`，写入唯一一行 `id = 1`：服务层发布与触发器的区间检查
   都锁这一行（与 0015 的 `fx_rate_locks` 同一套推理）
2. `create_table tax_policy_versions`：`public_id` 唯一；
   `created_by` / `approved_by` → `users.id` RESTRICT；
   STORED 生成列 `open_slot` 与唯一索引 `(jurisdiction, policy_type, open_slot)`；
   九条 CHECK（辖区、政策类型、税率、状态，外加草稿、发布人、起点、区间、退役尽头）
3. 6 个触发器：
   - 锁表：BEFORE INSERT（已有一行时）/ BEFORE UPDATE（一律，照 0016）/ BEFORE DELETE；
   - 版本：BEFORE INSERT（只能以草稿插入）；BEFORE UPDATE（规则 1–5 与发布跃迁上的
     税务一致性 3b）；BEFORE DELETE（一律拒绝）

§132 第 13 条分析
-----------------

**已有数据**：不 ALTER、不读写任何既有表；两张都是新表。外键只指向 `users`。

**锁表与停机**：只建新表、索引、触发器与锁表的一行。建外键时 MySQL 对 `users` 取短暂的
元数据锁，不扫行。不需要停机。

**备份**：要求前置备份（与 0005–0021 同一口径）。本版不改动、不回填任何既有数据，
也不预置政策（设计 §8：首条政策由运维在部署后经 CLI 发布）。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不读新表，不受影响。本任务不带仓储、
服务与 CLI（078 / 079），部署后只有两张空表（锁表一行），没有写入方。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 预检失败：什么都没建，`alembic_version` 仍是 `0021_ai_module`。
  给账号打开 `log_bin_trust_function_creators` 后重新部署。
- 之后任何一步失败，或进程在中途被杀：可能留下 `tax_policy_locks`（含那一行）及部分
  触发器 / 版本表，而 `alembic_version` 没有前进，重跑会撞 "Table already exists"。
  确认 `alembic_version` 仍是 `0021_ai_module`、`tax_policy_versions` 为空后，依次
  `DROP TABLE tax_policy_versions`、`DROP TABLE tax_policy_locks`（不存在的跳过；
  触发器随表删除），再重新部署。此时没有写入方，清场不丢业务数据。

**回滚**：`downgrade` 先查 `SELECT 1 FROM tax_policy_versions LIMIT 1`，有行就在任何 DDL
之前拒绝 —— 首条政策发布之后它就是被支付、收据快照引用的版本，删表等于删历史；空表时按
`tax_policy_versions` → `tax_policy_locks` 删表，触发器随表删除，不碰任何既有表。
`--sql` 离线模式读不到数据，跳过这一查、只输出 DDL（与 0021 相同）。生产只前滚。

**残余风险**：`TRUNCATE` / `DROP` / `DROP TRIGGER` 是 DDL，不经触发器（与 0006 / 0015 /
0016 相同），归「迁移账号与运行账号拆分」。

Revision ID: 0022_tax_policy
Revises: 0021_ai_module
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_tax_policy"
down_revision: str | None = "0021_ai_module"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCKS = "tax_policy_locks"
_VERSIONS = "tax_policy_versions"

# 依赖倒序：downgrade 按这个顺序删。
_DROP_ORDER = (_VERSIONS, _LOCKS)

# 锁表唯一一行的 id。
_LOCK_ID = 1
_INSERT_LOCK_ROW = f"INSERT INTO {_LOCKS} (id) VALUES ({_LOCK_ID})"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the tax policy triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 降级前的数据检查：有任何版本行就拒绝（设计 §8「回滚」）。
_LOSSY = "SELECT 1 FROM tax_policy_versions LIMIT 1"

# 名字 → 条件，都在 tax_policy_versions 上。⚠️ 冻结的字面量：与 app/models/tax_policy.py
# 上的 CheckConstraint 逐条一致（空白除外），test_migrations.py 比对两边。
_CHECKS: dict[str, str] = {
    "ck_tax_policy_versions_jurisdiction": "jurisdiction IN ('MY')",
    "ck_tax_policy_versions_policy_type": "policy_type IN ('SST')",
    "ck_tax_policy_versions_rate": "rate IS NULL OR (rate >= 0 AND rate < 1)",
    "ck_tax_policy_versions_status": "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')",
    # 草稿没有区间、没有发布人。
    "ck_tax_policy_versions_unpublished": (
        "status <> 'DRAFT' OR (effective_from IS NULL AND effective_to IS NULL"
        " AND approved_by IS NULL AND approved_at IS NULL)"
    ),
    # 发布过的一定有发布人与发布时刻。
    "ck_tax_policy_versions_approved": (
        "status NOT IN ('PUBLISHED', 'RETIRED')"
        " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)"
    ),
    # 税务政策没有「一直以来」：发布过的一定有具体起点（设计 §9）。
    "ck_tax_policy_versions_published_start": (
        "status NOT IN ('PUBLISHED', 'RETIRED') OR effective_from IS NOT NULL"
    ),
    # 区间不倒置；空区间只能出现在 RETIRED 行上（与 0015 逐字相同）。
    "ck_tax_policy_versions_period": (
        "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
        " OR (status = 'RETIRED' AND effective_from = effective_to)"
    ),
    # 停用必有尽头。
    "ck_tax_policy_versions_retired_end": "status <> 'RETIRED' OR effective_to IS NOT NULL",
}

# 生成列表达式。同上，冻结的字面量，与模型的 Computed 比对；与 0015 逐字相同。
# 未截断的已发布版本为 1，其余一律为 NULL（不是 0）：唯一索引不管 NULL。
_OPEN_SLOT = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"

# 触发器名 → 建触发器的语句。⚠️ 整条 CREATE TRIGGER 是**一条**语句（与 0006 / 0015 同一写法）。
_TRIGGERS: dict[str, str] = {
    # 锁表恰好一行：迁移写入之后，再插入一律拒绝（照 0015）。
    "trg_tax_policy_locks_before_insert": """
CREATE TRIGGER trg_tax_policy_locks_before_insert
BEFORE INSERT ON tax_policy_locks
FOR EACH ROW
BEGIN
    IF (SELECT COUNT(*) FROM tax_policy_locks) > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'tax_policy_locks holds exactly one row';
    END IF;
END
""",
    # 锁行只该被锁、永远不该被写：无条件拒绝（照 0016）。
    "trg_tax_policy_locks_before_update": """
CREATE TRIGGER trg_tax_policy_locks_before_update
BEFORE UPDATE ON tax_policy_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'tax_policy_locks holds exactly one row'
""",
    "trg_tax_policy_locks_before_delete": """
CREATE TRIGGER trg_tax_policy_locks_before_delete
BEFORE DELETE ON tax_policy_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'tax_policy_locks holds exactly one row'
""",
    # 版本只能以草稿进入：「草稿 → 已发布」跃迁上的检查因此绕不过去。
    "trg_tax_policy_versions_before_insert": """
CREATE TRIGGER trg_tax_policy_versions_before_insert
BEFORE INSERT ON tax_policy_versions
FOR EACH ROW
BEGIN
    IF NOT (NEW.status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a tax policy version starts as a draft';
    END IF;
END
""",
    # 按顺序（设计 §2「触发器」，照 0015 的五道）：
    # ① RETIRED 是终态，拒绝任何改动；
    # ② 草稿只能改成草稿或已发布：不许 DRAFT → RETIRED；
    # ③ 「草稿 → 已发布」跃迁上尽头必须为空：新发布的永远是末尾；
    # ③b 同一跃迁上的税务一致性（v2）：两个标志都是 JSON 布尔、未注册不得收税、
    #    不收税 ⇔ 没有税率。已发布行之后不能再改这三处（④），只在这里查一次；
    # ④ 已发布的行只许改 effective_to、PUBLISHED → RETIRED 与 updated_at（INV-6）；
    # ⑤ 新状态是 PUBLISHED / RETIRED 且区间或状态有变化时：先锁 tax_policy_locks
    #    （与服务层同一把锁）；本行为空区间时到此为止；否则查同一（辖区, 政策类型）里
    #    有没有另一个非空区间的 PUBLISHED / RETIRED 版本与本行相交。
    # ⚠️ MySQL 的触发器不能用 NEW / OLD 引用生成列，所以不可变检查不比 open_slot
    # （它只由被比较的列算出）。`treatment_json` 是 JSON 列，`<=>` 按 JSON 值比较。
    "trg_tax_policy_versions_before_update": """
CREATE TRIGGER trg_tax_policy_versions_before_update
BEFORE UPDATE ON tax_policy_versions
FOR EACH ROW
BEGIN
    DECLARE locked_id INT DEFAULT NULL;
    DECLARE reg JSON DEFAULT NULL;
    DECLARE chg JSON DEFAULT NULL;
    IF OLD.status = 'RETIRED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a retired tax policy version is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft tax policy version can only be published';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a tax policy version is published without an end';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' THEN
        SET reg = JSON_EXTRACT(NEW.treatment_json, '$.sst_registered');
        SET chg = JSON_EXTRACT(NEW.treatment_json, '$.tax_charged');
        IF NOT (JSON_TYPE(reg) <=> 'BOOLEAN')
           OR NOT (JSON_TYPE(chg) <=> 'BOOLEAN')
           OR (JSON_UNQUOTE(reg) = 'false' AND JSON_UNQUOTE(chg) = 'true')
           OR ((JSON_UNQUOTE(chg) = 'false') <> (NEW.rate IS NULL)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a tax policy treatment contradicts itself or its rate';
        END IF;
    END IF;
    IF OLD.status = 'PUBLISHED' THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.jurisdiction <=> OLD.jurisdiction)
           OR NOT (NEW.policy_type <=> OLD.policy_type)
           OR NOT (NEW.rate <=> OLD.rate)
           OR NOT (NEW.treatment_json <=> OLD.treatment_json)
           OR NOT (NEW.source_reference <=> OLD.source_reference)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.approved_at <=> OLD.approved_at)
           OR NOT (NEW.created_at <=> OLD.created_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published tax policy version is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status) AND NOT (NEW.status <=> 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published tax policy version can only be retired';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_id
          FROM tax_policy_locks
         WHERE id = 1
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM tax_policy_versions o
             WHERE o.jurisdiction = NEW.jurisdiction
               AND o.policy_type = NEW.policy_type
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND COALESCE(o.effective_from, '1000-01-01')
                   < COALESCE(NEW.effective_to, '9999-12-31')
               AND COALESCE(NEW.effective_from, '1000-01-01')
                   < COALESCE(o.effective_to, '9999-12-31')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'tax policy periods overlap';
        END IF;
    END IF;
END
""",
    # 版本永不删除：会被支付、收据、对账单的快照永久引用（INV-6）。
    "trg_tax_policy_versions_before_delete": """
CREATE TRIGGER trg_tax_policy_versions_before_delete
BEFORE DELETE ON tax_policy_versions
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'tax policy versions are never deleted'
""",
}


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，建了一半
    # 的表要人工清场。`--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    op.create_table(
        _LOCKS,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # 唯一一行。排在触发器之前（此刻表是空的，BEFORE INSERT 本来也放行）。
    op.execute(_INSERT_LOCK_ROW)

    op.create_table(
        _VERSIONS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("jurisdiction", sa.String(length=16), nullable=False),
        sa.Column("policy_type", sa.String(length=16), nullable=False),
        # 小数表示（0.08 = 8%）；未注册、不适用时为 NULL。
        sa.Column("rate", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("treatment_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("open_slot", sa.Integer(), sa.Computed(_OPEN_SLOT, persisted=True)),
        sa.Column("source_reference", sa.String(length=255), nullable=False),
        # 政策只能由人建，没有系统写入方：非空（与汇率不同）。
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_tax_policy_versions_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_tax_policy_versions_approved_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_tax_policy_versions_public_id"),
        # 唯一索引允许多个 NULL：只有未截断的已发布版本在 open_slot 上有值。
        sa.Index(
            "ux_tax_policy_versions_open_slot",
            "jurisdiction",
            "policy_type",
            "open_slot",
            unique=True,
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _CHECKS.items()),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # ⚠️ 在任何 DDL 之前：有版本行就拒绝，删表等于删被凭证引用的历史。
    # `--sql` 离线模式读不了数据：跳过这一查，DDL 照旧。
    if not op.get_context().as_sql:
        if op.get_bind().execute(sa.text(_LOSSY)).first():
            raise RuntimeError(
                "Cannot downgrade tax policy after a tax policy version was written;"
                " roll forward instead"
            )
    # 按外键依赖倒序：版本 → 锁表。触发器随表删除。不碰任何既有表。
    for table in _DROP_ORDER:
        op.drop_table(table)
