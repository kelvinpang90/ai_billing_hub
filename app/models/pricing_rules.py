"""Customer pricing rules: MARKUP and FIXED_RATE (design gate #178 v4, AIH-TASK-027).

spec §15、§16、§74.3；ADR-0008（含税定价）。三张表：

- `pricing_rule_locks`：只有一行（`id = 1`）、没有业务字段。发布与停用对它
  `SELECT … FOR UPDATE`，计费对它 `FOR SHARE`：五级规则里「客户默认」「全局默认」没有供应商，
  锁不到供应商行，所以另建这一把锁（设计 §2「为什么用一张单行锁表」）；
- `pricing_rules`：一条规则 = 一个范围 + 一种策略。范围列有四个：`tenant_id`、`project_id`
  （迁移 0030，设计闸门 #507 v4）、`provider_id`、`model_id`；库里的两条范围 CHECK 已是八级
  （三个项目级 `PROJECT_PROVIDER_MODEL`、`PROJECT_PROVIDER`、`PROJECT` 排在 §16 的五级之前），
  每级恰好一种 NULL 组合。⚠️ `PricingScope` 仍只有五个值：八级的枚举值由 AIH-TASK-137 加入，
  在那之前应用层建不出、也读不出项目级规则（`project_id` 恒为 NULL）。项目级规则的
  `(project_id, tenant_id)` 以复合外键指向 `projects (id, tenant_id)`：项目一定属于同一租户。
  策略与倍数一一对应（CHECK）。状态 `DRAFT → PUBLISHED → RETIRED`，草稿也可以
  `DISCARDED`。已发布（`PUBLISHED` / `RETIRED`）的规则在 `[effective_from, effective_to)` 内
  生效；同一范围（生成列 `scope_key`）的非空区间首尾相接、不重叠，至多一条未截断的已发布规则
  （生成列 `open_slot` + 唯一索引）；
- `pricing_rule_components`：FIXED_RATE 规则的分量，**MYR 含税单价**（ADR-0008）。每个分量
  引用 025 的一个计价分量。复合外键 `(pricing_rule_id, strategy)` → `pricing_rules(id,
  strategy)` 加上 `strategy = 'FIXED_RATE'` 的 CHECK：MARKUP 规则在数据库层挂不上分量。

⚠️ **发布后不可变**由 MySQL 触发器保证（迁移 0014）：锁表恰好一行；规则只能以草稿插入、草稿
只能改成草稿 / 已发布 / 已丢弃；已发布的行只许改 `effective_to`、`PUBLISHED → RETIRED` 与
`updated_at`（迁移 0030 重建 BEFORE UPDATE 触发器：不可变列加 `project_id`）；分量只在草稿阶段
可增删改；规则一律不许删除；「草稿 → 已发布」跃迁上 FIXED_RATE 的分量完整；锁
`pricing_rule_locks` 后检查同一范围（含项目）的区间不重叠。SQLite 上没有这些触发器，服务层
按同样的规则写。

⚠️ 两条范围 CHECK 与 `scope_key` 表达式同迁移 0030 逐字一致，其余条件文本与 `open_slot` 同迁移
0014 逐字一致（空白除外），test_migrations.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    DDL,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.functions import FunctionElement

from app.models.base import MONEY_PRECISION, MONEY_SCALE, Base

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36

# 设计 §2 写死的列宽。
PRIORITY_SCOPE_LENGTH: Final = 32
STRATEGY_LENGTH: Final = 16
STATUS_LENGTH: Final = 16
CURRENCY_LENGTH: Final = 3
# 最长的 `PROJECT_PROVIDER_MODEL` 22 个字符、四个冒号、四个 BIGINT（各至多 20 位）：106，
# 仍在 128 之内（迁移 0030 不改列宽）。
SCOPE_KEY_LENGTH: Final = 128

# 锁表唯一一行的 id。
PRICING_RULE_LOCK_ID: Final = 1
# 规则只用这一种币种（§74.3）。
RULE_CURRENCY: Final = "MYR"


class PricingScope(enum.StrEnum):
    """§16 的五级，**按优先顺序从高到低**声明：`resolve_pricing_rule` 就按这个顺序逐级查。

    库里的 CHECK 已放行八级（迁移 0030）；排在最前的三个项目级由 AIH-TASK-137 加入这里。
    """

    CUSTOMER_PROVIDER_MODEL = "CUSTOMER_PROVIDER_MODEL"
    CUSTOMER_PROVIDER = "CUSTOMER_PROVIDER"
    CUSTOMER = "CUSTOMER"
    GLOBAL_PROVIDER_MODEL = "GLOBAL_PROVIDER_MODEL"
    GLOBAL = "GLOBAL"

    @property
    def has_customer(self) -> bool:
        return self in _CUSTOMER_SCOPES

    @property
    def has_provider(self) -> bool:
        return self in _PROVIDER_SCOPES

    @property
    def has_model(self) -> bool:
        return self in _MODEL_SCOPES


_CUSTOMER_SCOPES: Final = frozenset(
    {PricingScope.CUSTOMER_PROVIDER_MODEL, PricingScope.CUSTOMER_PROVIDER, PricingScope.CUSTOMER}
)
_PROVIDER_SCOPES: Final = frozenset(
    {
        PricingScope.CUSTOMER_PROVIDER_MODEL,
        PricingScope.CUSTOMER_PROVIDER,
        PricingScope.GLOBAL_PROVIDER_MODEL,
    }
)
_MODEL_SCOPES: Final = frozenset(
    {PricingScope.CUSTOMER_PROVIDER_MODEL, PricingScope.GLOBAL_PROVIDER_MODEL}
)

# 解析的顺序（§16）。
SCOPE_ORDER: Final = tuple(PricingScope)


class PricingStrategy(enum.StrEnum):
    """§15：MARKUP = MYR 估算成本 × 倍数；FIXED_RATE = Acuven 自定的 MYR 分量价。"""

    MARKUP = "MARKUP"
    FIXED_RATE = "FIXED_RATE"


class PricingRuleStatus(enum.StrEnum):
    """设计 §4。`DISCARDED` 是 §74.3 之外补的：丢弃的草稿留痕、不删除。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"
    DISCARDED = "DISCARDED"


# 已发布过的两种状态：有区间、参与解析。
PERIOD_STATUSES: Final = (PricingRuleStatus.PUBLISHED, PricingRuleStatus.RETIRED)

# ⚠️ 这两条的条件文本与迁移 0030 逐字一致（空白除外），test_migrations.py 比对两边。
# 八级（设计闸门 #507 v4 §2「数据库」）：三个项目级在前，其后是 §16 的五级。
PRIORITY_SCOPE_CHECK: Final = (
    "priority_scope IN ('PROJECT_PROVIDER_MODEL', 'PROJECT_PROVIDER', 'PROJECT',"
    " 'CUSTOMER_PROVIDER_MODEL', 'CUSTOMER_PROVIDER', 'CUSTOMER',"
    " 'GLOBAL_PROVIDER_MODEL', 'GLOBAL')"
)
# 范围与四列的 NULL 组合一一对应（§74.3「不能有含糊的 NULL 组合」）：八级之外的组合写不进去。
# 三个项目级同样带 `tenant_id`；其余五级的 `project_id` 一律为 NULL。
SCOPE_COLUMNS_CHECK: Final = (
    "(priority_scope = 'PROJECT_PROVIDER_MODEL' AND tenant_id IS NOT NULL"
    " AND project_id IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
    " OR (priority_scope = 'PROJECT_PROVIDER' AND tenant_id IS NOT NULL"
    " AND project_id IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NULL)"
    " OR (priority_scope = 'PROJECT' AND tenant_id IS NOT NULL"
    " AND project_id IS NOT NULL AND provider_id IS NULL AND model_id IS NULL)"
    " OR (priority_scope = 'CUSTOMER_PROVIDER_MODEL' AND tenant_id IS NOT NULL"
    " AND project_id IS NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
    " OR (priority_scope = 'CUSTOMER_PROVIDER' AND tenant_id IS NOT NULL"
    " AND project_id IS NULL AND provider_id IS NOT NULL AND model_id IS NULL)"
    " OR (priority_scope = 'CUSTOMER' AND tenant_id IS NOT NULL"
    " AND project_id IS NULL AND provider_id IS NULL AND model_id IS NULL)"
    " OR (priority_scope = 'GLOBAL_PROVIDER_MODEL' AND tenant_id IS NULL"
    " AND project_id IS NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
    " OR (priority_scope = 'GLOBAL' AND tenant_id IS NULL"
    " AND project_id IS NULL AND provider_id IS NULL AND model_id IS NULL)"
)
# 以下条件文本与迁移 0014 逐字一致（空白除外）。
STRATEGY_CHECK: Final = "strategy IN ('MARKUP', 'FIXED_RATE')"
# MARKUP 必有正的倍数；FIXED_RATE 不许有倍数（混合策略在数据库层被拒绝）。
MARKUP_CHECK: Final = (
    "(strategy = 'MARKUP' AND markup_multiplier IS NOT NULL AND markup_multiplier > 0)"
    " OR (strategy = 'FIXED_RATE' AND markup_multiplier IS NULL)"
)
RULE_STATUS_CHECK: Final = "status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DISCARDED')"
# 草稿与丢弃的草稿没有区间、没有发布人。
UNPUBLISHED_CHECK: Final = (
    "status NOT IN ('DRAFT', 'DISCARDED') OR (effective_from IS NULL AND effective_to IS NULL"
    " AND approved_by IS NULL AND approved_at IS NULL)"
)
# 发布过的一定有发布人与发布时刻。
APPROVED_CHECK: Final = (
    "status NOT IN ('PUBLISHED', 'RETIRED')"
    " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)"
)
# 区间不能倒置；唯一允许的空区间是「撤销尚未生效的预约」留下的（设计 §2「停用」）。
PERIOD_CHECK: Final = (
    "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
    " OR (status = 'RETIRED' AND effective_from = effective_to)"
)
# 停用必有尽头（设计 v4）：没有尽头的 RETIRED 行会一直被解析命中，并让该范围之后的发布都撞上
# 区间不重叠触发器。服务层的停用一律写尽头（截断于 `t`，或撤销预约留下的空区间）。
RETIRED_END_CHECK: Final = "status <> 'RETIRED' OR effective_to IS NOT NULL"
COMPONENT_STRATEGY_CHECK: Final = "strategy = 'FIXED_RATE'"
UNIT_QUANTITY_CHECK: Final = "unit_quantity > 0"
RATE_AMOUNT_CHECK: Final = "rate_amount > 0"
CURRENCY_CHECK: Final = "currency = 'MYR'"

# 「同一范围」的非空键：MySQL 的唯一约束不管 NULL，全局范围的两条规则直接按四列建唯一索引
# 挡不住。迁移 0030 里是这一句（MySQL）：在 0014 的基础上加了项目一段，同一客户两个项目的
# 规则因此是两个范围；已有行（`project_id` 为 NULL）的键只多出一段 `:0`。
SCOPE_KEY_EXPRESSION: Final = (
    "CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(project_id, 0),"
    " ':', COALESCE(provider_id, 0), ':', COALESCE(model_id, 0))"
)
# SQLite 上的同一个值：`CONCAT` 要 SQLite 3.44 才有，单元测试的 SQLite 可能更老。
SQLITE_SCOPE_KEY_EXPRESSION: Final = (
    "priority_scope || ':' || COALESCE(tenant_id, 0) || ':' || COALESCE(project_id, 0)"
    " || ':' || COALESCE(provider_id, 0) || ':' || COALESCE(model_id, 0)"
)
# 只有未截断的已发布规则有值，其余是 NULL；唯一索引允许多个 NULL，所以「同一范围至多一条
# 未截断的已发布规则」由数据库保证（写法同 `provider_price_versions.open_slot`）。
OPEN_SLOT_EXPRESSION: Final = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"


class ScopeKeyExpression(FunctionElement[str]):
    """The generated `scope_key` expression, written per dialect (see the two constants)."""

    type = String(SCOPE_KEY_LENGTH)
    inherit_cache = True
    name = "scope_key"


@compiles(ScopeKeyExpression)
def _scope_key_default(_element: ScopeKeyExpression, _compiler: Any, **_kw: Any) -> str:
    return SCOPE_KEY_EXPRESSION


@compiles(ScopeKeyExpression, "sqlite")
def _scope_key_sqlite(_element: ScopeKeyExpression, _compiler: Any, **_kw: Any) -> str:
    return SQLITE_SCOPE_KEY_EXPRESSION


def _decimal_20_8() -> Numeric:
    # 与金额同一精度（DECIMAL(20,8)，INV-10）；单价、数量与倍数都不经过 float。
    return Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


class PricingRuleLock(Base):
    """The one-row lock that serialises publishing with billing (设计 §2).

    发布 / 停用：`FOR UPDATE`；计费：`FOR SHARE`，在 `resolve_model` 之后、锁钱包之前取，
    持有到计费事务提交。
    """

    __tablename__ = "pricing_rule_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)


# `Base.metadata.create_all`（单元测试的 SQLite）建表时同样写入那一行；生产上由迁移 0014 写入，
# 迁移不经过这里。
event.listen(
    PricingRuleLock.__table__,
    "after_create",
    DDL(f"INSERT INTO pricing_rule_locks (id) VALUES ({PRICING_RULE_LOCK_ID})"),
)


class PricingRule(Base):
    """One customer pricing rule for one scope of §16.

    `effective_from IS NULL` = 「一直以来」，只有 `GLOBAL` 范围第一条、未指定生效时刻时是它；
    `effective_to IS NULL` = 仍生效（或尚未发布）。发布后只有 `effective_to`（截断、撤销预约时
    恢复）、`status`（`PUBLISHED → RETIRED`）与 `updated_at` 会变。
    """

    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    priority_scope: Mapped[PricingScope] = mapped_column(
        Enum(PricingScope, native_enum=False, length=PRIORITY_SCOPE_LENGTH), nullable=False
    )
    # 四个范围列哪几个非空由 `priority_scope` 决定（CHECK）。
    tenant_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # 只有项目级规则有（迁移 0030，列序紧接 `tenant_id`）；本任务的应用层恒写 NULL。
    project_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    provider_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    model_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    strategy: Mapped[PricingStrategy] = mapped_column(
        Enum(PricingStrategy, native_enum=False, length=STRATEGY_LENGTH), nullable=False
    )
    # 只有 MARKUP 有；乘的是 MYR 估算成本，得到的就是含税计费额（ADR-0008）。
    markup_multiplier: Mapped[Decimal | None] = mapped_column(_decimal_20_8(), nullable=True)
    status: Mapped[PricingRuleStatus] = mapped_column(
        Enum(PricingRuleStatus, native_enum=False, length=STATUS_LENGTH), nullable=False
    )
    effective_from: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 两个生成列（STORED）：只读，ORM 从不写它们。
    scope_key: Mapped[str | None] = mapped_column(
        String(SCOPE_KEY_LENGTH), Computed(ScopeKeyExpression(), persisted=True)
    )
    open_slot: Mapped[int | None] = mapped_column(
        Integer, Computed(OPEN_SLOT_EXPRESSION, persisted=True)
    )
    created_by: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 发布人；发布时写。
    approved_by: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 发布时刻。
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 客户级规则以租户外键绑定（INV-8）。RESTRICT：有规则的租户删不掉。
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_pricing_rules_tenant",
            ondelete="RESTRICT",
        ),
        # 项目级规则的项目一定属于同一租户（INV-8，迁移 0030）；`project_id` 为 NULL 时 MySQL
        # 不检查这条外键。RESTRICT：有规则的项目删不掉。
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_pricing_rules_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_pricing_rules_provider",
            ondelete="RESTRICT",
        ),
        # 规则只能挂在同一供应商的模型上，由数据库保证。
        ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_pricing_rules_model",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_pricing_rules_created_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_pricing_rules_approved_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_pricing_rules_public_id"),
        # `id` 本来就唯一；这个约束只为让分量表的复合外键成立。
        UniqueConstraint("id", "strategy", name="uq_pricing_rules_id_strategy"),
        # 同一范围至多一条未截断的已发布规则。
        Index("ux_pricing_rules_open_slot", "scope_key", "open_slot", unique=True),
        # 解析时逐级查。
        Index(
            "ix_pricing_rules_resolve",
            "priority_scope",
            "tenant_id",
            "provider_id",
            "model_id",
            "effective_from",
        ),
        # 复合外键在子表上的索引，显式建出来（不让 MySQL 自动起名）。
        Index("ix_pricing_rules_model", "model_id", "provider_id"),
        Index("ix_pricing_rules_project", "project_id", "tenant_id"),
        CheckConstraint(PRIORITY_SCOPE_CHECK, name="ck_pricing_rules_priority_scope"),
        CheckConstraint(SCOPE_COLUMNS_CHECK, name="ck_pricing_rules_scope_columns"),
        CheckConstraint(STRATEGY_CHECK, name="ck_pricing_rules_strategy"),
        CheckConstraint(MARKUP_CHECK, name="ck_pricing_rules_markup"),
        CheckConstraint(RULE_STATUS_CHECK, name="ck_pricing_rules_status"),
        CheckConstraint(UNPUBLISHED_CHECK, name="ck_pricing_rules_unpublished"),
        CheckConstraint(APPROVED_CHECK, name="ck_pricing_rules_approved"),
        CheckConstraint(PERIOD_CHECK, name="ck_pricing_rules_period"),
        CheckConstraint(RETIRED_END_CHECK, name="ck_pricing_rules_retired_end"),
    )


class PricingRuleComponent(Base):
    """One component price of a FIXED_RATE rule: `rate_amount` MYR per `unit_quantity` units.

    ⚠️ `rate_amount` 是 **MYR 含税单价**（ADR-0008）：本任务不存税额、不引用税务政策版本。
    """

    __tablename__ = "pricing_rule_components"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    pricing_rule_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 永远是 FIXED_RATE（CHECK）：与父行的 strategy 一起构成复合外键。
    strategy: Mapped[PricingStrategy] = mapped_column(
        Enum(PricingStrategy, native_enum=False, length=STRATEGY_LENGTH), nullable=False
    )
    usage_meter_component_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 多少个计量单位对应一个 `rate_amount`（例如每 1 000 000 个 token）。
    unit_quantity: Mapped[Decimal] = mapped_column(_decimal_20_8(), nullable=False)
    rate_amount: Mapped[Decimal] = mapped_column(_decimal_20_8(), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LENGTH), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # MARKUP 规则在数据库层挂不上分量；有分量时父行的 strategy 改不掉（RESTRICT）。
        ForeignKeyConstraint(
            ["pricing_rule_id", "strategy"],
            ["pricing_rules.id", "pricing_rules.strategy"],
            name="fk_pricing_rule_components_rule",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["usage_meter_component_id"],
            ["usage_meter_components.id"],
            name="fk_pricing_rule_components_meter_component",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "pricing_rule_id",
            "usage_meter_component_id",
            name="uq_pricing_rule_components_rule_component",
        ),
        Index("ix_pricing_rule_components_rule", "pricing_rule_id", "strategy"),
        Index("ix_pricing_rule_components_meter_component", "usage_meter_component_id"),
        CheckConstraint(COMPONENT_STRATEGY_CHECK, name="ck_pricing_rule_components_strategy"),
        CheckConstraint(UNIT_QUANTITY_CHECK, name="ck_pricing_rule_components_unit_quantity"),
        CheckConstraint(RATE_AMOUNT_CHECK, name="ck_pricing_rule_components_rate_amount"),
        CheckConstraint(CURRENCY_CHECK, name="ck_pricing_rule_components_currency"),
    )


__all__ = [
    "APPROVED_CHECK",
    "COMPONENT_STRATEGY_CHECK",
    "CURRENCY_CHECK",
    "MARKUP_CHECK",
    "OPEN_SLOT_EXPRESSION",
    "PERIOD_CHECK",
    "PERIOD_STATUSES",
    "PRICING_RULE_LOCK_ID",
    "PRIORITY_SCOPE_CHECK",
    "RATE_AMOUNT_CHECK",
    "RETIRED_END_CHECK",
    "RULE_CURRENCY",
    "RULE_STATUS_CHECK",
    "SCOPE_COLUMNS_CHECK",
    "SCOPE_KEY_EXPRESSION",
    "SCOPE_ORDER",
    "SQLITE_SCOPE_KEY_EXPRESSION",
    "STRATEGY_CHECK",
    "UNIT_QUANTITY_CHECK",
    "UNPUBLISHED_CHECK",
    "PricingRule",
    "PricingRuleComponent",
    "PricingRuleLock",
    "PricingRuleStatus",
    "PricingScope",
    "PricingStrategy",
    "ScopeKeyExpression",
]
