"""fx rate lock, fx rate versions and fx fetch attempts, with their triggers

照设计闸门 #183 v3（docs/design/AIH-TASK-028-fx-rates.md）
§2「数据库」与 §8；本迁移是 §11 的 F1。

步骤
----

0. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（与 0006 同一个）。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**
   抛出明确的错误，`alembic_version` 仍是 0014。
1. `create_table fx_rate_locks`，写入唯一一行 `id = 1`：发布 / 退役的
   排他锁与计费的共享锁都锁这一行（与 0014 的锁表同一套推理）
2. `create_table fx_rate_versions`：`public_id` 唯一；
   `created_by` / `approved_by` → `users.id` RESTRICT；
   STORED 生成列 `open_slot` 与唯一索引
   `(base_currency, quote_currency, open_slot)`；唯一约束
   `(base_currency, quote_currency, source, source_quote_date)`
   （同一报价日的 BNM 草稿只有一条）；十条 CHECK
   （币种两条、汇率、来源、状态，外加设计 §2 的 CHECK 1–5）
3. `create_table fx_fetch_attempts`：
   `fx_rate_version_id` → `fx_rate_versions.id` RESTRICT；
   索引 `(base_currency, attempted_at)`；
   三条 CHECK（来源、结果、按结果的列组合）
4. 7 个触发器：
   - 锁表：BEFORE INSERT（已有一行时）/ BEFORE DELETE；
   - 版本：BEFORE INSERT（只能以草稿插入）；BEFORE UPDATE
     （已退役、已丢弃的不可改；草稿只能改成草稿 / 已发布 / 已丢弃；
     发布跃迁上尽头为空；发布后只许截断与 `PUBLISHED → RETIRED`；
     锁 `fx_rate_locks` 后检查同一币种对的区间不重叠，空区间跳过）；
     BEFORE DELETE（一律拒绝）；
   - 拉取记录：BEFORE UPDATE / BEFORE DELETE（只增）

§132 第 13 条分析
-----------------

**已有数据**：不 ALTER、不读写任何既有表；三张都是新表。外键只指向
`users` 与新表自己，建外键只在新表上建索引。

**锁表与停机**：只建新表、索引与触发器，外加锁表的一行。建外键时
MySQL 对 `users` 取短暂的元数据锁，不扫行。不需要停机。

**备份**：要求前置备份（与 0005–0014 同一口径）。本版不改动、不回填
任何既有数据，也不预置任何汇率（设计 §8：上线后由管理员发布第一个版本）。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不读新表，不受影响。
本任务不带服务、接口与拉取（F2–F6），部署后只有三张空表（锁表一行）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 预检失败：什么都没建，`alembic_version` 仍是 `0014_pricing_rules`。
  给账号打开 `log_bin_trust_function_creators` 后重新部署。
- 第 1 张表就失败：什么都没建，修好后重新部署即可。
- 之后任何一步失败，或进程在中途被杀：已建的表（可能带锁表的那一行、
  一部分触发器）存在而 `alembic_version` 没有前进，重跑会撞
  "Table already exists"。确认 `alembic_version` 仍是
  `0014_pricing_rules`、版本与拉取记录两张表为空后，按依赖倒序
  `DROP TABLE fx_fetch_attempts`、`DROP TABLE fx_rate_versions`、
  `DROP TABLE fx_rate_locks`（不存在的跳过；触发器随表删除），
  再重新部署。
- 本版只写锁表的那一行，所以「清场重跑」始终安全。

**回滚**：`downgrade` 按外键依赖倒序删表（拉取记录 → 版本 → 锁表），
触发器随表删除，不碰任何既有表。⚠️ 生产上发布过汇率之后回滚会连同
版本一起删掉 —— 那时应前滚修复而不是回滚（设计 §8）。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 相同）。

Revision ID: 0015_fx_rates
Revises: 0014_pricing_rules
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_fx_rates"
down_revision: str | None = "0014_pricing_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCKS = "fx_rate_locks"
_VERSIONS = "fx_rate_versions"
_ATTEMPTS = "fx_fetch_attempts"

# 依赖倒序：downgrade 按这个顺序删。
_DROP_ORDER = (_ATTEMPTS, _VERSIONS, _LOCKS)

# 锁表唯一一行的 id。
_LOCK_ID = 1
_INSERT_LOCK_ROW = f"INSERT INTO {_LOCKS} (id) VALUES ({_LOCK_ID})"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the fx rate triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 名字 → (表, 条件)。⚠️ 冻结的字面量：与 app/models/fx_rates.py
# 上的 CheckConstraint 逐条一致（空白除外），test_migrations.py 比对两边。
# `base_currency` 的格式 `^[A-Z]{3}$` 只在应用层校验（设计 v3：MySQL 的
# REGEXP 在默认排序规则下不分大小写，SQLite 没有 REGEXP）。
_CHECKS: dict[str, tuple[str, str]] = {
    "ck_fx_rate_versions_base_currency": (_VERSIONS, "base_currency <> 'MYR'"),
    "ck_fx_rate_versions_quote_currency": (_VERSIONS, "quote_currency = 'MYR'"),
    "ck_fx_rate_versions_rate": (_VERSIONS, "rate > 0"),
    "ck_fx_rate_versions_source": (_VERSIONS, "source IN ('BNM', 'MANUAL')"),
    "ck_fx_rate_versions_status": (
        _VERSIONS,
        "status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DISCARDED')",
    ),
    # 设计 §2 CHECK 1：草稿与丢弃的草稿没有区间、没有发布人。
    "ck_fx_rate_versions_unpublished": (
        _VERSIONS,
        "status NOT IN ('DRAFT', 'DISCARDED') OR (effective_from IS NULL AND effective_to IS NULL"
        " AND approved_by IS NULL AND approved_at IS NULL)",
    ),
    # CHECK 2：发布过的一定有发布人与发布时刻。
    "ck_fx_rate_versions_approved": (
        _VERSIONS,
        "status NOT IN ('PUBLISHED', 'RETIRED')"
        " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    ),
    # CHECK 3：区间不倒置；空区间只能出现在 RETIRED 行上。
    "ck_fx_rate_versions_period": (
        _VERSIONS,
        "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
        " OR (status = 'RETIRED' AND effective_from = effective_to)",
    ),
    # CHECK 4：停用必有尽头。
    "ck_fx_rate_versions_retired_end": (
        _VERSIONS,
        "status <> 'RETIRED' OR effective_to IS NOT NULL",
    ),
    # CHECK 5：BNM 的版本必有报价日。
    "ck_fx_rate_versions_bnm_quote_date": (
        _VERSIONS,
        "source <> 'BNM' OR source_quote_date IS NOT NULL",
    ),
    "ck_fx_fetch_attempts_source": (_ATTEMPTS, "source = 'BNM'"),
    "ck_fx_fetch_attempts_outcome": (
        _ATTEMPTS,
        "outcome IN ('NEW_DRAFT', 'NO_NEW_QUOTE', 'NO_QUOTE_FOR_DATE', 'FAILED')",
    ),
    # 设计 v3：按 outcome 的列组合。
    "ck_fx_fetch_attempts_outcome_columns": (
        _ATTEMPTS,
        "(outcome = 'NEW_DRAFT' AND quote_date IS NOT NULL AND fx_rate_version_id IS NOT NULL"
        " AND error_code IS NULL)"
        " OR (outcome = 'NO_NEW_QUOTE' AND quote_date IS NOT NULL AND fx_rate_version_id IS NULL"
        " AND (error_code IS NULL OR error_code = 'QUOTE_CHANGED'))"
        " OR (outcome = 'NO_QUOTE_FOR_DATE' AND quote_date IS NULL AND fx_rate_version_id IS NULL"
        " AND error_code IS NULL)"
        " OR (outcome = 'FAILED' AND error_code IS NOT NULL AND quote_date IS NULL"
        " AND fx_rate_version_id IS NULL)",
    ),
}

# 生成列表达式。同上，冻结的字面量，与模型的 Computed 比对。
# 未截断的已发布版本为 1，其余一律为 NULL（不是 0）：MySQL 唯一约束不管
# NULL，只有 1 参与唯一性（与 0013 的 `_OPEN_SLOT` 相同）。
_OPEN_SLOT = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"

# 触发器名 → 建触发器的语句。⚠️ 整条 CREATE TRIGGER 是**一条**语句：
# DELIMITER 只是 mysql 命令行客户端的东西，驱动把 BEGIN … END 原样发给
# 服务器（与 0006 同一写法）。
_TRIGGERS: dict[str, str] = {
    # 锁表恰好一行：迁移写入之后，再插入一律拒绝。
    "trg_fx_rate_locks_before_insert": """
CREATE TRIGGER trg_fx_rate_locks_before_insert
BEFORE INSERT ON fx_rate_locks
FOR EACH ROW
BEGIN
    IF (SELECT COUNT(*) FROM fx_rate_locks) > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'fx_rate_locks holds exactly one row';
    END IF;
END
""",
    "trg_fx_rate_locks_before_delete": """
CREATE TRIGGER trg_fx_rate_locks_before_delete
BEFORE DELETE ON fx_rate_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'fx_rate_locks holds exactly one row'
""",
    # 版本只能以草稿进入：「草稿 → 已发布」跃迁上的检查因此绕不过去。
    "trg_fx_rate_versions_before_insert": """
CREATE TRIGGER trg_fx_rate_versions_before_insert
BEFORE INSERT ON fx_rate_versions
FOR EACH ROW
BEGIN
    IF NOT (NEW.status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'an fx rate version starts as a draft';
    END IF;
END
""",
    # 五道检查，按顺序（设计 §2「触发器」）：
    # ① RETIRED、DISCARDED 是终态，拒绝任何改动（v2 收紧）；
    # ② 草稿只能改成草稿、已发布或已丢弃：不许 DRAFT → RETIRED；
    # ③ 「草稿 → 已发布」跃迁上尽头必须为空：新发布的永远是末尾；
    # ④ 已发布的行只许改 effective_to、PUBLISHED → RETIRED 与
    #    updated_at（INV-6）；
    # ⑤ 新状态是 PUBLISHED / RETIRED 且区间或状态有变化时：先锁
    #    fx_rate_locks（与服务层发布同一把锁）；本行为空区间时到此为止；
    #    否则查同一币种对里有没有另一个非空区间的 PUBLISHED / RETIRED
    #    版本与本行相交。③ 加 ⑤：起点为空的版本只可能在时间线为空时产生。
    # ⚠️ MySQL 的触发器不能用 NEW / OLD 引用生成列，所以不可变检查不比
    # open_slot（它只由被比较的列算出）。锁的结果用局部变量接（设计写的
    # 是 `@lock_id` 会话变量），语义相同、不污染会话。
    "trg_fx_rate_versions_before_update": """
CREATE TRIGGER trg_fx_rate_versions_before_update
BEFORE UPDATE ON fx_rate_versions
FOR EACH ROW
BEGIN
    DECLARE locked_id INT DEFAULT NULL;
    IF OLD.status IN ('RETIRED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a retired or discarded fx rate version is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft fx rate version is published or discarded';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'an fx rate version is published without an end';
    END IF;
    IF OLD.status = 'PUBLISHED' THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.base_currency <=> OLD.base_currency)
           OR NOT (NEW.quote_currency <=> OLD.quote_currency)
           OR NOT (NEW.rate <=> OLD.rate)
           OR NOT (NEW.source <=> OLD.source)
           OR NOT (NEW.source_reference <=> OLD.source_reference)
           OR NOT (NEW.source_quote_date <=> OLD.source_quote_date)
           OR NOT (NEW.observed_at <=> OLD.observed_at)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.approved_at <=> OLD.approved_at)
           OR NOT (NEW.created_at <=> OLD.created_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published fx rate version is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status) AND NOT (NEW.status <=> 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published fx rate version can only be retired';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_id
          FROM fx_rate_locks
         WHERE id = 1
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM fx_rate_versions o
             WHERE o.base_currency = NEW.base_currency
               AND o.quote_currency = NEW.quote_currency
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND COALESCE(o.effective_from, '1000-01-01')
                   < COALESCE(NEW.effective_to, '9999-12-31')
               AND COALESCE(NEW.effective_from, '1000-01-01')
                   < COALESCE(o.effective_to, '9999-12-31')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'fx rate periods overlap';
        END IF;
    END IF;
END
""",
    # 版本永不删除：会被用量事件的快照永久引用（INV-6），丢弃的也留痕。
    "trg_fx_rate_versions_before_delete": """
CREATE TRIGGER trg_fx_rate_versions_before_delete
BEFORE DELETE ON fx_rate_versions
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'fx rate versions are never deleted'
""",
    # 拉取记录只增：它是 BNM 草稿的来源证明，也是 `fx_fetch` 维度的依据。
    "trg_fx_fetch_attempts_before_update": """
CREATE TRIGGER trg_fx_fetch_attempts_before_update
BEFORE UPDATE ON fx_fetch_attempts
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'fx fetch attempts are append-only'
""",
    "trg_fx_fetch_attempts_before_delete": """
CREATE TRIGGER trg_fx_fetch_attempts_before_delete
BEFORE DELETE ON fx_fetch_attempts
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'fx fetch attempts are append-only'
""",
}


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(condition, name=name)
        for name, (owner, condition) in _CHECKS.items()
        if owner == table
    ]


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
        sa.Column("base_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("quote_currency", sa.CHAR(length=3), nullable=False),
        # 1 单位 base_currency = 多少 MYR；精确，不舍入。
        sa.Column("rate", sa.Numeric(precision=24, scale=10), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=False),
        sa.Column("source_quote_date", sa.Date(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("open_slot", sa.Integer(), sa.Computed(_OPEN_SLOT, persisted=True)),
        # BNM 拉取的草稿没有创建人（系统）。
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_fx_rate_versions_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_fx_rate_versions_approved_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_fx_rate_versions_public_id"),
        # 同一报价日的 BNM 草稿只有一条；手工录入的报价日可空，
        # MySQL 唯一约束不管 NULL。
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "source",
            "source_quote_date",
            name="uq_fx_rate_versions_quote_date",
        ),
        # 唯一索引允许多个 NULL：只有未截断的已发布版本在 open_slot 上有值。
        sa.Index(
            "ux_fx_rate_versions_open_slot",
            "base_currency",
            "quote_currency",
            "open_slot",
            unique=True,
        ),
        *_checks(_VERSIONS),
    )

    op.create_table(
        _ATTEMPTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("base_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=False),
        # `NO_QUOTE_FOR_DATE` 有 17 个字符：VARCHAR(32)（设计 v3）。
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("fx_rate_version_id", sa.BigInteger(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["fx_rate_version_id"],
            [f"{_VERSIONS}.id"],
            name="fk_fx_fetch_attempts_version",
            ondelete="RESTRICT",
        ),
        # `fx_fetch` 告警维度按币种查最近 72 小时。
        sa.Index("ix_fx_fetch_attempts_currency_time", "base_currency", "attempted_at"),
        *_checks(_ATTEMPTS),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 按外键依赖倒序：拉取记录 → 版本 → 锁表。触发器随表删除。
    # 不碰任何既有表。
    for table in _DROP_ORDER:
        op.drop_table(table)
