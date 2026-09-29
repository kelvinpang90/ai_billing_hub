"""provider price versions and components, with their immutability triggers

照设计闸门 #177 v4（docs/design/AIH-TASK-026-provider-prices.md）§2「数据库」与 §8。

步骤
----

0. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（与 0006 同一个）。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**
   抛出明确的错误，`alembic_version` 仍是 0012。
1. `create_table provider_price_versions`：`public_id` 唯一；复合外键
   `(model_id, provider_id)` → `ai_models(id, provider_id)` RESTRICT；`created_by` /
   `approved_by` → `users.id` RESTRICT；STORED 生成列 `open_slot` 与唯一索引
   `(provider_id, model_id, open_slot)`；五条 CHECK
2. `create_table provider_price_components`：外键 → 版本、→ `usage_meter_components`，都是
   RESTRICT；`(provider_price_version_id, usage_meter_component_id)` 唯一；两条 CHECK
3. 6 个触发器：版本的 BEFORE INSERT（只能以草稿插入）/ BEFORE UPDATE（丢弃的不可改、草稿
   只能改成草稿 / 已发布 / 已丢弃、发布后只许截断与退役、发布跃迁上的分量完整性、锁供应商行
   后检查区间不重叠）/ BEFORE DELETE（一律拒绝）；分量的 BEFORE INSERT / UPDATE / DELETE
   （所属版本不是草稿就拒绝）

§132 第 13 条分析
-----------------

**已有数据**：不 ALTER、不读写任何既有表；两张都是新表。外键指向 `ai_models`、`users`、
`usage_meter_components`，建外键只在新表上建索引。

**锁表与停机**：只建新表、索引与触发器。建外键时 MySQL 对被引用表取短暂的元数据锁；
三张被引用表都很小（生产上目录只有几十行）。不需要停机。

**备份**：要求前置备份（与 0005–0012 同一口径）。本版不改动、不回填任何既有数据，也不写
种子（设计 §8：不预置价格）。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不读新表，不受影响。新代码的管理端价格接口
依赖新表；`resolve_provider_price` 本任务不接到任何计费路径。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 预检失败：什么都没建，`alembic_version` 仍是 `0012_ai_catalog`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 第 1 张表就失败：什么都没建，修好后重新部署即可。
- 第 2 步或第 3 步失败，或进程在中途被杀：已建的表（可能带一部分触发器）存在而
  `alembic_version` 没有前进，重跑会撞 "Table already exists"。确认 `alembic_version` 仍是
  `0012_ai_catalog`、两张表为空后，按依赖倒序 `DROP TABLE provider_price_components`、
  `DROP TABLE provider_price_versions`（不存在的跳过；触发器随表删除），再重新部署。
- 本版不写任何行，所以「清场重跑」始终安全。

**回滚**：`downgrade` 按外键依赖倒序删表（分量 → 版本），触发器随表删除，不碰任何既有表。
⚠️ 生产上录了价格之后回滚会连同价格一起删掉 —— 那时应前滚修复而不是回滚（设计 §8）。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 相同）。

Revision ID: 0013_provider_prices
Revises: 0012_ai_catalog
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_provider_prices"
down_revision: str | None = "0012_ai_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSIONS = "provider_price_versions"
_COMPONENTS = "provider_price_components"

# 依赖倒序：downgrade 按这个顺序删。
_DROP_ORDER = (_COMPONENTS, _VERSIONS)

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the provider price triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 名字 → (表, 条件)。⚠️ 冻结的字面量：与 app/models/provider_prices.py 上的
# CheckConstraint 逐条一致（空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, tuple[str, str]] = {
    "ck_provider_price_versions_source_type": (
        _VERSIONS,
        "source_type IN ('MANUAL', 'SYNC')",
    ),
    "ck_provider_price_versions_status": (
        _VERSIONS,
        "status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DISCARDED')",
    ),
    "ck_provider_price_versions_unpublished": (
        _VERSIONS,
        "status NOT IN ('DRAFT', 'DISCARDED') OR (effective_from IS NULL AND effective_to IS NULL"
        " AND approved_by IS NULL AND approved_at IS NULL)",
    ),
    "ck_provider_price_versions_approved": (
        _VERSIONS,
        "status NOT IN ('PUBLISHED', 'RETIRED')"
        " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    ),
    "ck_provider_price_versions_period": (
        _VERSIONS,
        "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
        " OR (status = 'RETIRED' AND effective_from = effective_to)",
    ),
    "ck_provider_price_components_unit_quantity": (_COMPONENTS, "unit_quantity > 0"),
    "ck_provider_price_components_rate_amount": (_COMPONENTS, "rate_amount > 0"),
}

# 生成列表达式。同上，冻结的字面量，与模型的 Computed 比对。
_OPEN_SLOT = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"

# 触发器名 → 建触发器的语句。⚠️ 整条 CREATE TRIGGER 是**一条**语句：DELIMITER 只是
# mysql 命令行客户端的东西，驱动把 BEGIN … END 原样发给服务器（与 0006 同一写法）。
_TRIGGERS: dict[str, str] = {
    # 版本只能以草稿进入：「草稿 → 已发布」跃迁上的检查因此绕不过去（设计 §2 v2）。
    "trg_provider_price_versions_before_insert": """
CREATE TRIGGER trg_provider_price_versions_before_insert
BEFORE INSERT ON provider_price_versions
FOR EACH ROW
BEGIN
    IF NOT (NEW.status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a provider price version starts as a draft';
    END IF;
END
""",
    # 五道检查，按顺序：
    # ① 丢弃的草稿不可改；
    # ①′ 草稿只能改成草稿、已发布或已丢弃：不许 DRAFT → RETIRED 绕过 ③ 的完整性检查（v4）；
    # ② 发布过的行只许改 effective_to、PUBLISHED → RETIRED 与 updated_at（INV-6）；
    # ③ 「草稿 → 已发布」跃迁上：至少一个分量，出现的计量类型的全部分量都在（§15.1）；
    # ④ 发布过的行区间或状态有变化时：先锁供应商行（与服务层发布同一把锁），再查同一
    #    （供应商, 模型）里有没有另一个非空区间与本行相交。空区间（撤销的预约）不参与判定。
    "trg_provider_price_versions_before_update": """
CREATE TRIGGER trg_provider_price_versions_before_update
BEFORE UPDATE ON provider_price_versions
FOR EACH ROW
BEGIN
    DECLARE locked_provider_id BIGINT DEFAULT NULL;
    IF OLD.status = 'DISCARDED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a discarded provider price version is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft provider price version is published or discarded';
    END IF;
    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.provider_id <=> OLD.provider_id)
           OR NOT (NEW.model_id <=> OLD.model_id)
           OR NOT (NEW.source_currency <=> OLD.source_currency)
           OR NOT (NEW.source_type <=> OLD.source_type)
           OR NOT (NEW.source_reference <=> OLD.source_reference)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.created_at <=> OLD.created_at)
           OR NOT (NEW.approved_at <=> OLD.approved_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status)
           AND NOT (OLD.status = 'PUBLISHED' AND NEW.status = 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version can only be retired';
        END IF;
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' THEN
        IF (SELECT COUNT(*)
              FROM provider_price_components
             WHERE provider_price_version_id = NEW.id) = 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a provider price version needs at least one component';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM usage_meter_components umc
             WHERE umc.meter_type_id IN (
                       SELECT m.meter_type_id
                         FROM provider_price_components c
                         JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
                        WHERE c.provider_price_version_id = NEW.id)
               AND umc.id NOT IN (
                       SELECT usage_meter_component_id
                         FROM provider_price_components
                        WHERE provider_price_version_id = NEW.id)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a provider price version misses a meter type component';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_provider_id
          FROM ai_providers
         WHERE id = NEW.provider_id
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM provider_price_versions o
             WHERE o.provider_id = NEW.provider_id
               AND o.model_id = NEW.model_id
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND COALESCE(o.effective_from, '1000-01-01')
                   < COALESCE(NEW.effective_to, '9999-12-31')
               AND COALESCE(NEW.effective_from, '1000-01-01')
                   < COALESCE(o.effective_to, '9999-12-31')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'provider price periods overlap';
        END IF;
    END IF;
END
""",
    # 版本永不删除：价格版本会被用量事件永久引用（INV-6），丢弃的草稿也留痕。
    "trg_provider_price_versions_before_delete": """
CREATE TRIGGER trg_provider_price_versions_before_delete
BEFORE DELETE ON provider_price_versions
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'provider price versions are never deleted'
""",
    # 分量只在所属版本是草稿时可增删改（草稿编辑是「整体替换」：删掉旧的、插入新的）。
    "trg_provider_price_components_before_insert": """
CREATE TRIGGER trg_provider_price_components_before_insert
BEFORE INSERT ON provider_price_components
FOR EACH ROW
BEGIN
    DECLARE version_status VARCHAR(16) DEFAULT NULL;
    SELECT status INTO version_status
      FROM provider_price_versions
     WHERE id = NEW.provider_price_version_id;
    IF NOT (version_status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'provider price components change only on a draft';
    END IF;
END
""",
    "trg_provider_price_components_before_update": """
CREATE TRIGGER trg_provider_price_components_before_update
BEFORE UPDATE ON provider_price_components
FOR EACH ROW
BEGIN
    DECLARE old_status VARCHAR(16) DEFAULT NULL;
    DECLARE new_status VARCHAR(16) DEFAULT NULL;
    SELECT status INTO old_status
      FROM provider_price_versions
     WHERE id = OLD.provider_price_version_id;
    SELECT status INTO new_status
      FROM provider_price_versions
     WHERE id = NEW.provider_price_version_id;
    IF NOT (old_status <=> 'DRAFT') OR NOT (new_status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'provider price components change only on a draft';
    END IF;
END
""",
    "trg_provider_price_components_before_delete": """
CREATE TRIGGER trg_provider_price_components_before_delete
BEFORE DELETE ON provider_price_components
FOR EACH ROW
BEGIN
    DECLARE version_status VARCHAR(16) DEFAULT NULL;
    SELECT status INTO version_status
      FROM provider_price_versions
     WHERE id = OLD.provider_price_version_id;
    IF NOT (version_status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'provider price components change only on a draft';
    END IF;
END
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


def _decimal_20_8() -> sa.Numeric:
    return sa.Numeric(precision=20, scale=8)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，建了一半的表要人工
    # 清场。`--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    op.create_table(
        _VERSIONS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("source_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("open_slot", sa.Integer(), sa.Computed(_OPEN_SLOT, persisted=True)),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # 版本只能挂在同一供应商的模型上。RESTRICT：模型删不掉（INV-6）。
        sa.ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_provider_price_versions_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_provider_price_versions_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_provider_price_versions_approved_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_provider_price_versions_public_id"),
        # 唯一索引允许多个 NULL：只有未截断的已发布版本在 open_slot 上有值。
        sa.Index(
            "ux_provider_price_versions_open_slot",
            "provider_id",
            "model_id",
            "open_slot",
            unique=True,
        ),
        sa.Index("ix_provider_price_versions_model", "model_id", "provider_id"),
        *_checks(_VERSIONS),
    )

    op.create_table(
        _COMPONENTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider_price_version_id", sa.BigInteger(), nullable=False),
        sa.Column("usage_meter_component_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_quantity", _decimal_20_8(), nullable=False),
        sa.Column("rate_amount", _decimal_20_8(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["provider_price_version_id"],
            [f"{_VERSIONS}.id"],
            name="fk_provider_price_components_version",
            ondelete="RESTRICT",
        ),
        # RESTRICT：被价格引用的计价分量删不掉（INV-6）。
        sa.ForeignKeyConstraint(
            ["usage_meter_component_id"],
            ["usage_meter_components.id"],
            name="fk_provider_price_components_meter_component",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider_price_version_id",
            "usage_meter_component_id",
            name="uq_provider_price_components_version_component",
        ),
        sa.Index("ix_provider_price_components_meter_component", "usage_meter_component_id"),
        *_checks(_COMPONENTS),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 按外键依赖倒序：分量 → 版本。触发器随表删除。不碰任何既有表。
    for table in _DROP_ORDER:
        op.drop_table(table)
