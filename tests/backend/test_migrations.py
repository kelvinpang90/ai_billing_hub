"""`alembic upgrade head` must actually execute (spec §123 Phase 0 acceptance).

**这条必须对着真 MySQL 跑。**SQLite 的 DDL 与 MySQL 差得远，在 SQLite 上跑通
的迁移证明不了生产上跑得通 —— 而迁移跑不通是部署时才发现的那种故障。

需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**数据库。CI 的
`backend` job 起了一个 MySQL service 并设了它，所以 CI 里这些用例**从不跳过**。
本地没设时会 skip，`pytest` 的输出里会明写 skipped —— 不要把 skipped 读成
passed。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.ai_catalog import (
    CODE_COLLATION,
    OPEN_SLOT_EXPRESSION,
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import AuditAction, AuditLog
from app.models.base import Base
from app.models.fx_rates import FxFetchAttempt, FxRateLock, FxRateVersion
from app.models.integration import (
    API_KEY_COLLATION,
    IntegrationCredential,
    ProjectWebhookSecret,
)
from app.models.pricing_rules import (
    SCOPE_KEY_EXPRESSION,
    SQLITE_SCOPE_KEY_EXPRESSION,
    PricingRule,
    PricingRuleComponent,
    PricingRuleLock,
)
from app.models.provider_prices import ProviderPriceComponent, ProviderPriceVersion
from app.models.tenancy import AccountStatus, BillingStatus, Project, Tenant
from app.models.usage import TEXT_COLLATION, UsageEvent, UsageEventConflict
from app.models.wallet import REFERENCE_ID_COLLATION, Wallet, WalletTransaction

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)


def test_alembic_ini_is_ascii_only() -> None:
    """Alembic 用 configparser 读它，而 configparser 用**平台默认编码**。

    Windows 上那是 cp1252，非 ASCII 字符直接 `UnicodeDecodeError`——而 Linux
    的 CI runner 是 UTF-8，跑得好好的。这个 bug 真发生过（本任务初版把中文
    注释写进了 `alembic.ini`），且只在开发机上炸、CI 看不见。
    """
    raw = pathlib.Path("alembic.ini").read_bytes()

    assert raw.decode("ascii")  # 解不出来就说明有人又往里写了非 ASCII


@pytest.fixture
def alembic_config(monkeypatch) -> Config:
    # env.py 从 app.core.config 读连接串，而 get_settings() 带 lru_cache，
    # 所以要清掉缓存再指过去。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    yield Config("alembic.ini")
    get_settings.cache_clear()


@needs_mysql
def test_upgrade_head_creates_the_version_table(alembic_config: Config) -> None:
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    inspector = inspect(create_engine(TEST_DATABASE_URL))
    assert "alembic_version" in inspector.get_table_names()


@needs_mysql
def test_downgrade_to_base_is_reversible(alembic_config: Config) -> None:
    """能升不能降的迁移链，出事时只能靠恢复备份。"""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    inspector = inspect(create_engine(TEST_DATABASE_URL))
    assert "alembic_version" in inspector.get_table_names()


@needs_mysql
def test_the_migrated_columns_are_as_wide_as_the_models_say(alembic_config: Config) -> None:
    """⚠️ **这条用例来自一次真的踩坑（T0.8d）。**

    `sa.Enum(native_enum=False)` 不写 `length=` 时，宽度按**建表那一刻最长的
    成员**推。`domain_outbox.status` 因此在真 MySQL 上建成了 `VARCHAR(7)`，而
    模型那边是 `VARCHAR(64)` —— 两边悄悄对不上。

    后果是以后加一个更长的枚举值（`CANCELLED` 就够）会在插入时报
    `Data too long`，而**整套单元测试全绿**：SQLite 根本不强制 VARCHAR 长度。
    `test_model_columns.py` 也抓不到 —— 它检查的是模型，不是迁移建出来的东西。

    所以这里比的是**库里真实的列宽**与模型声明的列宽。它只能对着真 MySQL 跑。
    """
    command.upgrade(alembic_config, "head")
    inspector = inspect(create_engine(TEST_DATABASE_URL))

    mismatched = []
    for table in Base.metadata.sorted_tables:
        actual = {c["name"]: c["type"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            declared = getattr(column.type, "length", None)
            if declared is None or column.name not in actual:
                continue
            in_db = getattr(actual[column.name], "length", None)
            if in_db is not None and in_db != declared:
                mismatched.append(f"{table.name}.{column.name}: 库里 {in_db} ≠ 模型 {declared}")

    assert mismatched == [], "迁移建出来的列宽与模型不一致：" + "; ".join(mismatched)


# ---------------------------------------------------------------------------
# 0005_tenants_projects（AIH-TASK-004）
#
# `test_tenancy_repository.py` 的表是 create_all 按模型建的，看不见**迁移**建出来的
# 外键 RESTRICT、public_id 唯一与列形状；SQLite 又默认不强制外键与 VARCHAR 长度。
# 这几样只能在这里验。
# ---------------------------------------------------------------------------

_BEFORE_0005 = "0004_password_reset_outbox"
_REVISION_0005 = "0005_tenants_projects"
_NOW = dt.datetime(2026, 9, 19, 8, 30, 0)

# 列名 → 是否可空（head 上）。⚠️ 比的是**完整集合**：`integration_status`、webhook 列谁被
# 顺手加进来，这里都会红 —— 它们各归一个后续任务（docs/database-schema.md「尚未建的列」）。
# `billing_status` 起三列是 0006 加的（AIH-TASK-005），`account_status` 是 0009 加的
# （AIH-TASK-020）。
_TENANT_COLUMNS_0006 = {"billing_status", "status_version", "low_balance_threshold"}
_EXPECTED_COLUMNS = {
    "tenants": {
        "id": False,
        "public_id": False,
        "company_name": False,
        "contact_name": True,
        "email": False,
        "phone": True,
        "created_at": False,
        "updated_at": False,
        "billing_status": False,
        "status_version": False,
        "low_balance_threshold": True,
        "account_status": False,
    },
    "projects": {
        "id": False,
        "public_id": False,
        "tenant_id": False,
        "name": False,
        "description": True,
        "created_at": False,
        "updated_at": False,
    },
}


_EXPECTED_UNIQUE_AT_HEAD = {
    "tenants": [["public_id"]],
    "projects": [["id", "tenant_id"], ["public_id"]],
}


def _table_names() -> set[str]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@contextmanager
def _rolled_back_connection() -> Iterator[Connection]:
    """⚠️ 库是共享的：这几条用例写进去的行一律回滚，不留给后面的用例。"""
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _insert_tenant(connection: Connection, public_id: str) -> int:
    result = connection.execute(
        insert(Tenant).values(
            public_id=public_id,
            company_name="Migration Test Sdn Bhd",
            email="ops@example.com",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return int(result.inserted_primary_key[0])


def _insert_project(connection: Connection, tenant_id: int, public_id: str) -> None:
    connection.execute(
        insert(Project).values(
            public_id=public_id,
            tenant_id=tenant_id,
            name="Migration Test Project",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


@needs_mysql
def test_0005_only_adds_and_drops_its_own_two_tables(alembic_config: Config) -> None:
    """不 ALTER、不删除任何已有表：升降前后的表集合只差这两张。"""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _BEFORE_0005)
    without = _table_names()
    assert "users" in without, "0004 的表应该都在，否则下面的比较没有意义"
    assert "tenants" not in without
    assert "projects" not in without

    command.upgrade(alembic_config, _REVISION_0005)
    assert _table_names() == without | {"tenants", "projects"}

    command.downgrade(alembic_config, _BEFORE_0005)
    assert _table_names() == without

    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0005_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        types = {}
        for table, expected in _EXPECTED_COLUMNS.items():
            columns = inspector.get_columns(table)
            assert {c["name"]: c["nullable"] for c in columns} == expected, table
            types[table] = {c["name"]: c["type"] for c in columns}

            assert isinstance(types[table]["id"], BigInteger), table
            assert isinstance(types[table]["public_id"], CHAR), table
            assert types[table]["public_id"].length == 36, table

            # public_id 是唯一的那一个；tenants.email 刻意**不**唯一。projects 在 head 上
            # 另有 0007 加的 (id, tenant_id)，只为凭据的复合外键（AIH-TASK-012）。
            unique = sorted(u["column_names"] for u in inspector.get_unique_constraints(table))
            assert unique == _EXPECTED_UNIQUE_AT_HEAD[table], table

        assert isinstance(types["projects"]["tenant_id"], BigInteger)
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("projects")}
        assert indexes.get("ix_projects_tenant_id") == ["tenant_id"]

        foreign_keys = [
            (fk["name"], fk["constrained_columns"], fk["referred_table"], fk["referred_columns"])
            for fk in inspector.get_foreign_keys("projects")
        ]
        assert foreign_keys == [("fk_projects_tenant_id", ["tenant_id"], "tenants", ["id"])]

        # 删除规则从 information_schema 读：它记的是建表时写的那个规则本身。
        with engine.connect() as connection:
            delete_rule = connection.execute(
                text(
                    "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
                    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
                    " AND CONSTRAINT_NAME = 'fk_projects_tenant_id'"
                )
            ).scalar_one()
        assert delete_rule == "RESTRICT"
    finally:
        engine.dispose()


@needs_mysql
def test_0005_tenant_with_projects_cannot_be_deleted(alembic_config: Config) -> None:
    """ON DELETE RESTRICT 的行为本身：删租户不会级联删掉它的项目。"""
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant_id = _insert_tenant(connection, str(uuid.uuid4()))
        _insert_project(connection, tenant_id, str(uuid.uuid4()))

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(delete(Tenant).where(Tenant.id == tenant_id))

        remaining = connection.execute(
            select(func.count()).select_from(Project).where(Project.tenant_id == tenant_id)
        ).scalar_one()
        assert remaining == 1


@needs_mysql
def test_0005_public_ids_are_unique(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant_public_id = str(uuid.uuid4())
        tenant_id = _insert_tenant(connection, tenant_public_id)
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                _insert_tenant(connection, tenant_public_id)

        # 同一个联系邮箱、不同的 public_id：必须插得进去。
        _insert_tenant(connection, str(uuid.uuid4()))

        project_public_id = str(uuid.uuid4())
        _insert_project(connection, tenant_id, project_public_id)
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                _insert_project(connection, tenant_id, project_public_id)


# ---------------------------------------------------------------------------
# 0006_wallets_ledger（AIH-TASK-005，设计闸门 #88 v6）
#
# 表、约束与触发器的**形状**在这里验；触发器与 CHECK 的**行为**（拒绝什么、放行
# 什么）在 test_wallet_repository.py。头两条不连库：一条比对迁移与模型的 CHECK，
# 一条钉住「预检排在任何 DDL 之前」。
# ---------------------------------------------------------------------------

_REVISION_0006 = "0006_wallets_ledger"
_MIGRATION_0006 = pathlib.Path("alembic/versions/20260919_0006_wallets_ledger.py")

_EXPECTED_0006_COLUMNS = {
    "wallets": {
        "id": False,
        "tenant_id": False,
        "currency": False,
        "balance": False,
        "version": False,
        "created_at": False,
        "updated_at": False,
    },
    "wallet_transactions": {
        "id": False,
        "public_id": False,
        "wallet_id": False,
        "tenant_id": False,
        "wallet_sequence": False,
        "transaction_type": False,
        "amount": False,
        "balance_before": False,
        "balance_after": False,
        "reference_type": False,
        "reference_id": False,
        "description": True,
        "metadata_json": True,
        "created_by": True,
        "created_at": False,
    },
}

# 触发器名 → (表, 时机, 事件)。设计 §2 的 6 个，一个不多一个不少。
_EXPECTED_TRIGGERS = {
    "trg_wallet_transactions_before_insert": ("wallet_transactions", "BEFORE", "INSERT"),
    "trg_wallet_transactions_after_insert": ("wallet_transactions", "AFTER", "INSERT"),
    "trg_wallet_transactions_before_update": ("wallet_transactions", "BEFORE", "UPDATE"),
    "trg_wallet_transactions_before_delete": ("wallet_transactions", "BEFORE", "DELETE"),
    "trg_wallets_before_insert": ("wallets", "BEFORE", "INSERT"),
    "trg_wallets_before_update": ("wallets", "BEFORE", "UPDATE"),
}

# 删除规则从 information_schema 读：它记的是建表时写的那个规则本身。
_DELETE_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND TABLE_NAME IN ('wallets', 'wallet_transactions')"
)
_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME IN ('tenants', 'wallets', 'wallet_transactions')"
)
_TRIGGERS_QUERY = text(
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)


def _load_0006() -> ModuleType:
    """Import the migration file directly; its name starts with a digit."""
    spec = importlib.util.spec_from_file_location("migration_0006", _MIGRATION_0006)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalised(condition: str) -> str:
    return " ".join(condition.split())


def _model_checks() -> dict[str, tuple[str, str]]:
    checks = {}
    for table in (Tenant.__table__, Wallet.__table__, WalletTransaction.__table__):
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                checks[str(constraint.name)] = (table.name, _normalised(str(constraint.sqltext)))
    return checks


def test_0006_checks_are_the_ones_the_models_declare() -> None:
    """迁移里是冻结的字面量，模型上是由类型映射生成的 —— 两边必须是同一组条件。

    ⚠️ 不比的话，改了模型那边的映射（加一种交易类型）而忘了写新迁移，repository
    会放行一行、数据库再拒绝它；反过来则是数据库比代码宽松，而没有任何东西报错。
    """
    migration = _load_0006()
    from_migration = {
        name: (table, _normalised(condition))
        for name, (table, condition) in migration._CHECKS.items()
    }
    # 0009 在 tenants 上加的那一条由 test_0009_checks_are_the_ones_the_model_declares 比对。
    later = set(_load_0009()._CHECKS)
    model = {name: check for name, check in _model_checks().items() if name not in later}

    assert from_migration == model
    # tenants 3 条、wallets 1 条、wallet_transactions 5 条。
    assert len(from_migration) == 9


class _FakeBind:
    """Answers the prerequisite query with fixed values."""

    def __init__(self, log_bin: int, trusted: int) -> None:
        self.row = (log_bin, trusted)

    def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(one=lambda: self.row)


class _RecordingOp:
    """Stands in for `alembic.op`: every DDL call is recorded instead of run."""

    def __init__(self, bind: _FakeBind) -> None:
        self.bind = bind
        self.calls: list[str] = []

    def get_context(self) -> SimpleNamespace:
        return SimpleNamespace(as_sql=False)

    def get_bind(self) -> _FakeBind:
        return self.bind

    def __getattr__(self, name: str):
        def record(*_args: object, **_kwargs: object) -> None:
            self.calls.append(name)

        return record


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0006_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    """设计 §8 第 0 步：binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419）。"""
    migration = _load_0006()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0006_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL 的 DDL 不参与事务：预检要是排在建表之后，失败时库里已经留下半截。

    CI 用 root 连库、开关也开着，真实的「开关关着」在 CI 里造不出来（设计 §7），
    所以把 `op` 换成记录器，直接看预检失败时有没有任何 DDL 被发出去。
    """
    migration = _load_0006()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    # 反过来：开关开着时同一个 upgrade 确实走到了 DDL —— 上面的空列表不是因为没走到。
    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    assert allowed.calls[:2] == ["create_table", "create_table"]
    # 6 个触发器 + 1 条回填。
    assert allowed.calls.count("execute") == 7


def _column_names(table: str) -> set[str]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


@needs_mysql
def test_0006_only_adds_its_two_tables_and_three_tenant_columns(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION_0005)
    without = _table_names()
    assert "tenants" in without, "0005 的表应该都在，否则下面的比较没有意义"
    assert not {"wallets", "wallet_transactions"} & without
    tenant_columns = _column_names("tenants")
    assert not _TENANT_COLUMNS_0006 & tenant_columns

    command.upgrade(alembic_config, _REVISION_0006)
    assert _table_names() == without | {"wallets", "wallet_transactions"}
    assert _column_names("tenants") == tenant_columns | _TENANT_COLUMNS_0006

    command.downgrade(alembic_config, _REVISION_0005)
    assert _table_names() == without
    assert _column_names("tenants") == tenant_columns

    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0006_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        types = {}
        for table, expected in _EXPECTED_0006_COLUMNS.items():
            columns = inspector.get_columns(table)
            assert {c["name"]: c["nullable"] for c in columns} == expected, table
            types[table] = {c["name"]: c["type"] for c in columns}
        types["tenants"] = {c["name"]: c["type"] for c in inspector.get_columns("tenants")}

        money = [
            types["wallets"]["balance"],
            types["wallet_transactions"]["amount"],
            types["wallet_transactions"]["balance_before"],
            types["wallet_transactions"]["balance_after"],
            types["tenants"]["low_balance_threshold"],
        ]
        for column_type in money:
            assert isinstance(column_type, Numeric)
            assert (column_type.precision, column_type.scale) == (20, 8)

        for name in ("id", "tenant_id", "version"):
            assert isinstance(types["wallets"][name], BigInteger), name
        for name in ("id", "wallet_id", "tenant_id", "wallet_sequence", "created_by"):
            assert isinstance(types["wallet_transactions"][name], BigInteger), name
        assert isinstance(types["tenants"]["status_version"], BigInteger)

        assert isinstance(types["wallets"]["currency"], CHAR)
        assert types["wallets"]["currency"].length == 3
        assert isinstance(types["wallet_transactions"]["public_id"], CHAR)
        assert types["wallet_transactions"]["public_id"].length == 36
        assert isinstance(types["wallet_transactions"]["metadata_json"], JSON)
        widths = {
            "transaction_type": 64,
            "reference_type": 32,
            "reference_id": 64,
            "description": 255,
        }
        for name, width in widths.items():
            assert types["wallet_transactions"][name].length == width, name
        # 来源 ID 按字节比较：只差大小写或尾部空格的两个来源不是同一个（审查 #92）。
        reference_id = types["wallet_transactions"]["reference_id"]
        assert reference_id.collation == REFERENCE_ID_COLLATION
        assert types["tenants"]["billing_status"].length == 64
    finally:
        engine.dispose()


@needs_mysql
def test_0006_keys_indexes_checks_and_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)

        def unique(table: str) -> set[tuple[str, ...]]:
            constraints = inspector.get_unique_constraints(table)
            return {tuple(u["column_names"]) for u in constraints}

        def foreign_keys(table: str) -> dict[str, tuple[list[str], str, list[str]]]:
            return {
                str(fk["name"]): (
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table)
            }

        assert unique("wallets") == {("tenant_id",), ("id", "tenant_id")}
        assert unique("wallet_transactions") == {
            ("public_id",),
            ("wallet_id", "wallet_sequence"),
            ("reference_type", "reference_id"),
        }
        ledger_indexes = inspector.get_indexes("wallet_transactions")
        indexes = {i["name"]: i["column_names"] for i in ledger_indexes}
        by_tenant = ["tenant_id", "wallet_sequence"]
        assert indexes["ix_wallet_transactions_tenant_sequence"] == by_tenant

        assert foreign_keys("wallets") == {
            "fk_wallets_tenant_id": (["tenant_id"], "tenants", ["id"]),
        }
        # 复合外键：账本行的租户必须与钱包一致（INV-8）。
        assert foreign_keys("wallet_transactions") == {
            "fk_wallet_transactions_wallet": (
                ["wallet_id", "tenant_id"],
                "wallets",
                ["id", "tenant_id"],
            ),
            "fk_wallet_transactions_created_by": (["created_by"], "users", ["id"]),
        }

        with engine.connect() as connection:
            delete_rules = dict(connection.execute(_DELETE_RULES_QUERY).all())
            checks = set(connection.execute(_CHECKS_QUERY).scalars())
            triggers = {row[0]: tuple(row[1:]) for row in connection.execute(_TRIGGERS_QUERY)}

        # ⚠️ RESTRICT：删租户、钱包或用户都不许连带删掉账本（财务记录永久保留）。
        assert delete_rules == {
            "fk_wallets_tenant_id": "RESTRICT",
            "fk_wallet_transactions_wallet": "RESTRICT",
            "fk_wallet_transactions_created_by": "RESTRICT",
        }
        assert checks == set(_model_checks())
        # 只比 0006 的两张表：audit_logs 上的两个由 0010 建，在 test_0010_* 里验；价格表上的
        # 六个由 0013 建，在 test_0013_* 里验；定价规则的八个由 0014 建，在 test_0014_* 里验；
        # 汇率的七个由 0015 建，在 test_0015_* 里验；两张锁表的 BEFORE UPDATE 由 0016 建，在
        # test_0016_* 里验；冲突表的两个由 0017 建，在 test_0017_* 里验；用量事件上的三个由
        # 0018 建，在 test_0018_* 里验。
        ledger_tables = {"wallets", "wallet_transactions"}
        ours = {name: row for name, row in triggers.items() if row[0] in ledger_tables}
        assert ours == _EXPECTED_TRIGGERS
        expected = {
            *_EXPECTED_TRIGGERS,
            *_EXPECTED_TRIGGERS_0010,
            *_EXPECTED_TRIGGERS_0013,
            *_EXPECTED_TRIGGERS_0014,
            *_EXPECTED_TRIGGERS_0015,
            *_EXPECTED_TRIGGERS_0016,
            *_EXPECTED_TRIGGERS_0017,
            *_EXPECTED_TRIGGERS_0018,
        }
        assert set(triggers) == expected
    finally:
        engine.dispose()


@needs_mysql
def test_0006_gives_every_existing_tenant_one_empty_wallet(alembic_config: Config) -> None:
    """设计 §8 第 5 步：迁移前已有的租户各得一个余额为 0 的钱包，状态是 SUSPENDED。"""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION_0005)
    public_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.begin() as connection:
            for public_id in public_ids:
                # 0005 的表形状：Tenant 模型已经多了三列，这里只能写裸 SQL。
                connection.execute(
                    text(
                        "INSERT INTO tenants (public_id, company_name, email, created_at,"
                        " updated_at) VALUES (:public_id, 'Backfill Sdn Bhd',"
                        " 'ops@example.com', :now, :now)"
                    ),
                    {"public_id": public_id, "now": _NOW},
                )

        command.upgrade(alembic_config, _REVISION_0006)

        backfilled = (
            select(Tenant.billing_status, Tenant.status_version, Wallet.balance, Wallet.version)
            .join(Wallet, Wallet.tenant_id == Tenant.id)
            .where(Tenant.public_id.in_(public_ids))
        )
        with engine.connect() as connection:
            rows = connection.execute(backfilled).all()
            ours = select(Tenant.id).where(Tenant.public_id.in_(public_ids))
            currencies = connection.execute(
                select(Wallet.currency).where(Wallet.tenant_id.in_(ours))
            ).scalars()
            assert list(currencies) == ["MYR", "MYR"]

        assert len(rows) == 2, "每个既有租户正好一个钱包"
        for row in rows:
            assert row.billing_status is BillingStatus.SUSPENDED
            assert row.status_version == 0
            assert row.balance == 0
            assert row.version == 0
    finally:
        command.upgrade(alembic_config, "head")
        # 空钱包（没有账本行）允许删除 —— 设计 §2 说的「回填和测试清理的需要」。
        with engine.begin() as connection:
            owned = select(Tenant.id).where(Tenant.public_id.in_(public_ids))
            connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(owned)))
            connection.execute(delete(Tenant).where(Tenant.public_id.in_(public_ids)))
        engine.dispose()


# ---------------------------------------------------------------------------
# 0007_integration_access（AIH-TASK-012，设计闸门 #118 v1）
#
# 表、约束、排序规则与复合外键的**形状与行为**都在这里验：SQLite 不强制外键，也没有
# utf8mb4_0900_bin。第一条不连库，比对迁移与模型的 CHECK。
# ---------------------------------------------------------------------------

_REVISION_0007 = "0007_integration_access"
_MIGRATION_0007 = pathlib.Path("alembic/versions/20260925_0007_integration_access.py")
_CREDENTIALS_TABLE = "integration_credentials"

_EXPECTED_0007_COLUMNS = {
    "id": False,
    "tenant_id": False,
    "project_id": False,
    "public_api_key": False,
    "key_version": False,
    "encrypted_secret": False,
    "encryption_key_version": False,
    "status": False,
    "valid_from": False,
    "valid_until": True,
    "last_used_at": True,
    "created_at": False,
    "revoked_at": True,
}

_CREDENTIAL_DELETE_RULE_QUERY = text(
    "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND CONSTRAINT_NAME = 'fk_integration_credentials_project'"
)
_CREDENTIAL_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME = 'integration_credentials'"
)

# MySQL 的错误号。
_ER_DUP_ENTRY = 1062
_ER_ROW_IS_REFERENCED_2 = 1451
_ER_NO_REFERENCED_ROW_2 = 1452
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

# 全零占位值（设计 §2「格式」）：仓库是公开的，测试里不写看起来像真的 key。
_ZERO_API_KEY = "ak_" + "0" * 32


def _load_0007() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0007", _MIGRATION_0007)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _credential_checks() -> dict[str, str]:
    return {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in IntegrationCredential.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0007_checks_are_the_ones_the_model_declares() -> None:
    migration = _load_0007()
    from_migration = {name: _normalised(rule) for name, rule in migration._CHECKS.items()}

    assert from_migration == _credential_checks()
    assert len(from_migration) == 3
    assert migration._API_KEY_COLLATION == API_KEY_COLLATION


def _unique_sets(table: str) -> set[tuple[str, ...]]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        constraints = inspect(engine).get_unique_constraints(table)
        return {tuple(u["column_names"]) for u in constraints}
    finally:
        engine.dispose()


@needs_mysql
def test_0007_only_adds_its_table_and_one_projects_constraint(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION_0006)
    without = _table_names()
    assert "wallets" in without, "0006 的表应该都在，否则下面的比较没有意义"
    assert _CREDENTIALS_TABLE not in without
    project_uniques = _unique_sets("projects")
    assert ("id", "tenant_id") not in project_uniques

    command.upgrade(alembic_config, _REVISION_0007)
    assert _table_names() == without | {_CREDENTIALS_TABLE}
    assert _unique_sets("projects") == project_uniques | {("id", "tenant_id")}

    # downgrade 先删表、再删约束；反过来 MySQL 会拒绝（外键还在引用那个索引）。
    command.downgrade(alembic_config, _REVISION_0006)
    assert _table_names() == without
    assert _unique_sets("projects") == project_uniques

    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0007_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        columns = inspector.get_columns(_CREDENTIALS_TABLE)
        assert {c["name"]: c["nullable"] for c in columns} == _EXPECTED_0007_COLUMNS
        types = {c["name"]: c["type"] for c in columns}

        for name in ("id", "tenant_id", "project_id"):
            assert isinstance(types[name], BigInteger), name
        # 两个版本号都是 INT，而且是两列（ADR-0004 §4：签名版本与主密钥版本不能混用）。
        for name in ("key_version", "encryption_key_version"):
            assert isinstance(types[name], Integer), name
            assert not isinstance(types[name], BigInteger), name
        assert isinstance(types["encrypted_secret"], Text)
        assert isinstance(types["public_api_key"], String)
        assert types["public_api_key"].length == 64
        # 按字节比较：只差大小写或尾部空格的两个 key 不是同一个。
        assert types["public_api_key"].collation == API_KEY_COLLATION
        assert isinstance(types["status"], String)
        assert types["status"].length == 16
        for name in ("valid_from", "valid_until", "last_used_at", "created_at", "revoked_at"):
            assert isinstance(types[name], DateTime), name
    finally:
        engine.dispose()


@needs_mysql
def test_0007_keys_indexes_and_checks(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        # (public_api_key, key_version)，不是 §74.4 字面的 public_api_key 单列唯一（设计 §2）。
        # head 上另有 0017 加的 (id, project_id, tenant_id)，只为用量事件的复合外键。
        assert _unique_sets(_CREDENTIALS_TABLE) == {
            ("public_api_key", "key_version"),
            ("id", "project_id", "tenant_id"),
        }
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes(_CREDENTIALS_TABLE)}
        assert indexes["ix_integration_credentials_project_id"] == ["project_id"]
        foreign_keys = {
            str(fk["name"]): (
                fk["constrained_columns"],
                fk["referred_table"],
                fk["referred_columns"],
            )
            for fk in inspector.get_foreign_keys(_CREDENTIALS_TABLE)
        }
        assert foreign_keys == {
            "fk_integration_credentials_project": (
                ["project_id", "tenant_id"],
                "projects",
                ["id", "tenant_id"],
            ),
        }

        with engine.connect() as connection:
            delete_rule = connection.execute(_CREDENTIAL_DELETE_RULE_QUERY).scalar_one()
            checks = set(connection.execute(_CREDENTIAL_CHECKS_QUERY).scalars())
        assert delete_rule == "RESTRICT"
        assert checks == set(_credential_checks())
    finally:
        engine.dispose()


def _credential_values(tenant_id: int, project_id: int, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "public_api_key": _ZERO_API_KEY,
        "key_version": 1,
        "encrypted_secret": "not-a-real-ciphertext",
        "encryption_key_version": 1,
        "status": "ACTIVE",
        "valid_from": _NOW,
        "created_at": _NOW,
    }
    values.update(overrides)
    return values


def _insert_credential(connection: Connection, **values: object) -> None:
    connection.execute(insert(IntegrationCredential).values(**values))


def _project_id(connection: Connection, public_id: str) -> int:
    return int(
        connection.execute(select(Project.id).where(Project.public_id == public_id)).scalar_one()
    )


def _refused(connection: Connection, **values: object) -> int:
    """Insert in a savepoint; return the MySQL error number it was refused with."""
    with pytest.raises(DBAPIError) as raised:
        with connection.begin_nested():
            _insert_credential(connection, **values)
    return int(raised.value.orig.args[0])


@needs_mysql
def test_0007_the_composite_foreign_key_refuses_a_mismatched_tenant(
    alembic_config: Config,
) -> None:
    """INV-8：凭据行的 tenant_id 必须是项目所属的租户，由数据库保证。"""
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        owner = _insert_tenant(connection, str(uuid.uuid4()))
        stranger = _insert_tenant(connection, str(uuid.uuid4()))
        project_public_id = str(uuid.uuid4())
        _insert_project(connection, owner, project_public_id)
        project = _project_id(connection, project_public_id)

        mismatched = _credential_values(stranger, project)
        assert _refused(connection, **mismatched) == _ER_NO_REFERENCED_ROW_2

        _insert_credential(connection, **_credential_values(owner, project))


@needs_mysql
def test_0007_a_project_with_credentials_cannot_be_deleted(alembic_config: Config) -> None:
    """RESTRICT：以后的用量事件要引用凭据行，项目删不掉就不会留下悬空引用（INV-6）。"""
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant = _insert_tenant(connection, str(uuid.uuid4()))
        project_public_id = str(uuid.uuid4())
        _insert_project(connection, tenant, project_public_id)
        project = _project_id(connection, project_public_id)
        _insert_credential(connection, **_credential_values(tenant, project))

        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                connection.execute(delete(Project).where(Project.id == project))
        assert int(raised.value.orig.args[0]) == _ER_ROW_IS_REFERENCED_2

        ours = IntegrationCredential.project_id == project
        remaining = connection.execute(
            select(func.count()).select_from(IntegrationCredential).where(ours)
        ).scalar_one()
        assert remaining == 1


@needs_mysql
def test_0007_uniqueness_is_per_key_and_version_and_byte_exact(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant = _insert_tenant(connection, str(uuid.uuid4()))
        project_public_id = str(uuid.uuid4())
        _insert_project(connection, tenant, project_public_id)
        project = _project_id(connection, project_public_id)
        _insert_credential(connection, **_credential_values(tenant, project))

        # 同一个 key 的同一个版本只有一行。
        assert _refused(connection, **_credential_values(tenant, project)) == _ER_DUP_ENTRY
        # 同一个 key 的下一个版本：轮换就是这样加行的。
        _insert_credential(connection, **_credential_values(tenant, project, key_version=2))
        # 只差大小写、只差尾部空格：utf8mb4_0900_bin 下是不同的 key。
        for variant in (_ZERO_API_KEY.upper(), _ZERO_API_KEY + " "):
            _insert_credential(
                connection, **_credential_values(tenant, project, public_api_key=variant)
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"key_version": 0},
        {"key_version": -1},
        {"status": "EXPIRED"},
        {"status": "REVOKED"},
        {"status": "ACTIVE", "revoked_at": _NOW},
    ],
    ids=[
        "version-0",
        "version-negative",
        "status-expired",
        "revoked-without-time",
        "active-with-time",
    ],
)
@needs_mysql
def test_0007_checks_refuse_bad_rows(alembic_config: Config, overrides: dict) -> None:
    """状态只有 ACTIVE / REVOKED；吊销时刻与状态同进退；版本号从 1 起。"""
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant = _insert_tenant(connection, str(uuid.uuid4()))
        project_public_id = str(uuid.uuid4())
        _insert_project(connection, tenant, project_public_id)
        project = _project_id(connection, project_public_id)

        bad = _credential_values(tenant, project, **overrides)
        assert _refused(connection, **bad) == _ER_CHECK_CONSTRAINT_VIOLATED

        # 合法的吊销行：状态与时刻都在。
        revoked = _credential_values(tenant, project, status="REVOKED", revoked_at=_NOW)
        _insert_credential(connection, **revoked)


# ---------------------------------------------------------------------------
# 0008_webhook_signing（AIH-TASK-019，设计闸门 #135 v1）
#
# 生成列、它们的唯一索引、CHECK 与复合外键的**形状与行为**在真 MySQL 上验（设计 §7「迁移」
# 「数据库兜底」）。头两条不连库：比对迁移与模型的 CHECK 与生成列表达式，钉住版本链。
# 并发与 SQLite 上的同一组数据库兜底在 test_webhook_signing_service.py。
# ---------------------------------------------------------------------------

_REVISION_0008 = "0008_webhook_signing"
_MIGRATION_0008 = pathlib.Path("alembic/versions/20260928_0008_webhook_signing.py")
_WEBHOOK_TABLE = "project_webhook_secrets"

_EXPECTED_0008_COLUMNS = {
    "id": False,
    "project_id": False,
    "tenant_id": False,
    "key_version": False,
    "status": False,
    "encrypted_secret": False,
    "encryption_key_version": False,
    # 生成列：非 ACTIVE / PENDING 的行上是 NULL。
    "active_slot": True,
    "pending_slot": True,
    "created_at": False,
    "activated_at": True,
    "retired_at": True,
}

_WEBHOOK_DELETE_RULE_QUERY = text(
    "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND CONSTRAINT_NAME = 'fk_project_webhook_secrets_project'"
)
_WEBHOOK_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME = 'project_webhook_secrets'"
)


def _load_0008() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0008", _MIGRATION_0008)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _webhook_checks() -> dict[str, str]:
    return {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in ProjectWebhookSecret.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0008_checks_and_slots_are_the_ones_the_model_declares() -> None:
    migration = _load_0008()
    from_migration = {name: _normalised(rule) for name, rule in migration._CHECKS.items()}

    assert from_migration == _webhook_checks()
    assert len(from_migration) == 3
    table = ProjectWebhookSecret.__table__
    for name, expression in migration._SLOTS.items():
        computed = table.c[name].computed
        assert computed is not None, name
        assert computed.persisted is True, name
        assert _normalised(str(computed.sqltext)) == _normalised(expression), name
    assert set(migration._SLOTS) == {"active_slot", "pending_slot"}


def test_0008_follows_0007() -> None:
    migration = _load_0008()

    assert migration.revision == _REVISION_0008
    assert migration.down_revision == _REVISION_0007


@needs_mysql
def test_0008_only_adds_and_drops_its_table(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION_0007)
    without = _table_names()
    assert _CREDENTIALS_TABLE in without, "0007 的表应该都在，否则下面的比较没有意义"
    assert _WEBHOOK_TABLE not in without
    project_uniques = _unique_sets("projects")

    command.upgrade(alembic_config, _REVISION_0008)
    assert _table_names() == without | {_WEBHOOK_TABLE}

    # downgrade 只删表，不碰 projects 上 0007 的约束。
    command.downgrade(alembic_config, _REVISION_0007)
    assert _table_names() == without
    assert _unique_sets("projects") == project_uniques

    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0008_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        columns = inspect(engine).get_columns(_WEBHOOK_TABLE)
        assert {c["name"]: c["nullable"] for c in columns} == _EXPECTED_0008_COLUMNS
        types = {c["name"]: c["type"] for c in columns}
        computed = {c["name"]: c.get("computed") for c in columns}

        for name in ("id", "project_id", "tenant_id", "active_slot", "pending_slot"):
            assert isinstance(types[name], BigInteger), name
        # 两个版本号都是 INT，而且是两列（ADR-0004 §4）。
        for name in ("key_version", "encryption_key_version"):
            assert isinstance(types[name], Integer), name
            assert not isinstance(types[name], BigInteger), name
        assert isinstance(types["encrypted_secret"], Text)
        assert isinstance(types["status"], String)
        assert types["status"].length == 16
        for name in ("created_at", "activated_at", "retired_at"):
            assert isinstance(types[name], DateTime), name

        # 两个生成列都是 STORED，只有它们两个是生成列。
        assert {name for name, value in computed.items() if value} == {
            "active_slot",
            "pending_slot",
        }
        for name, status in (("active_slot", "ACTIVE"), ("pending_slot", "PENDING")):
            assert computed[name]["persisted"] is True, name
            assert status in str(computed[name]["sqltext"]), name
    finally:
        engine.dispose()


@needs_mysql
def test_0008_keys_indexes_and_checks(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        assert _unique_sets(_WEBHOOK_TABLE) == {
            ("project_id", "key_version"),
            ("active_slot",),
            ("pending_slot",),
        }
        indexes = {i["name"]: i for i in inspector.get_indexes(_WEBHOOK_TABLE)}
        for name, column in (
            ("ux_project_webhook_secrets_active_slot", "active_slot"),
            ("ux_project_webhook_secrets_pending_slot", "pending_slot"),
        ):
            assert indexes[name]["column_names"] == [column], name
            assert indexes[name]["unique"], name
        foreign_keys = {
            str(fk["name"]): (
                fk["constrained_columns"],
                fk["referred_table"],
                fk["referred_columns"],
            )
            for fk in inspector.get_foreign_keys(_WEBHOOK_TABLE)
        }
        assert foreign_keys == {
            "fk_project_webhook_secrets_project": (
                ["project_id", "tenant_id"],
                "projects",
                ["id", "tenant_id"],
            ),
        }

        with engine.connect() as connection:
            delete_rule = connection.execute(_WEBHOOK_DELETE_RULE_QUERY).scalar_one()
            checks = set(connection.execute(_WEBHOOK_CHECKS_QUERY).scalars())
        assert delete_rule == "RESTRICT"
        assert checks == set(_webhook_checks())
    finally:
        engine.dispose()


def _webhook_values(tenant_id: int, project_id: int, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "key_version": 1,
        "status": "PENDING",
        "encrypted_secret": "not-a-real-ciphertext",
        "encryption_key_version": 1,
        "created_at": _NOW,
    }
    values.update(overrides)
    return values


def _insert_webhook_secret(connection: Connection, **values: object) -> None:
    connection.execute(insert(ProjectWebhookSecret).values(**values))


def _webhook_refused(connection: Connection, **values: object) -> int:
    """Insert in a savepoint; return the MySQL error number it was refused with."""
    with pytest.raises(DBAPIError) as raised:
        with connection.begin_nested():
            _insert_webhook_secret(connection, **values)
    return int(raised.value.orig.args[0])


def _new_project(connection: Connection) -> tuple[int, int]:
    """(tenant id, project id), both internal."""
    tenant = _insert_tenant(connection, str(uuid.uuid4()))
    public_id = str(uuid.uuid4())
    _insert_project(connection, tenant, public_id)
    return tenant, _project_id(connection, public_id)


@needs_mysql
def test_0008_the_composite_foreign_key_refuses_a_mismatched_tenant(
    alembic_config: Config,
) -> None:
    """INV-8：密钥行的 tenant_id 必须是项目所属的租户，由数据库保证。"""
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        owner, project = _new_project(connection)
        stranger = _insert_tenant(connection, str(uuid.uuid4()))

        mismatched = _webhook_values(stranger, project)
        assert _webhook_refused(connection, **mismatched) == _ER_NO_REFERENCED_ROW_2

        _insert_webhook_secret(connection, **_webhook_values(owner, project))


@needs_mysql
def test_0008_a_project_with_webhook_secrets_cannot_be_deleted(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant, project = _new_project(connection)
        _insert_webhook_secret(connection, **_webhook_values(tenant, project))

        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                connection.execute(delete(Project).where(Project.id == project))
        assert int(raised.value.orig.args[0]) == _ER_ROW_IS_REFERENCED_2


@needs_mysql
def test_0008_at_most_one_active_and_one_pending_per_project(alembic_config: Config) -> None:
    """设计 §7「数据库兜底」：绕过服务层直接插第二个 ACTIVE / PENDING、重复版本号，都被拒绝。

    同时验证设计 §10 假设 1：生成列上的唯一索引允许多个 NULL（多个 RETIRED 行）。
    """
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant, project = _new_project(connection)
        other_tenant, other_project = _new_project(connection)
        active = {"status": "ACTIVE", "activated_at": _NOW}
        retired = {"status": "RETIRED", "retired_at": _NOW}

        _insert_webhook_secret(connection, **_webhook_values(tenant, project, key_version=1))
        _insert_webhook_secret(
            connection, **_webhook_values(tenant, project, key_version=2, **active)
        )

        # 第二个 PENDING、第二个 ACTIVE、重复的版本号。
        second_pending = _webhook_values(tenant, project, key_version=3)
        second_active = _webhook_values(tenant, project, key_version=3, **active)
        duplicate_version = _webhook_values(tenant, project, key_version=1, **retired)
        for bad in (second_pending, second_active, duplicate_version):
            assert _webhook_refused(connection, **bad) == _ER_DUP_ENTRY

        # 多个 RETIRED：唯一索引上都是 NULL，可以并存。
        for version in (3, 4, 5):
            _insert_webhook_secret(
                connection, **_webhook_values(tenant, project, key_version=version, **retired)
            )
        # 别的项目有自己的一个 ACTIVE 与一个 PENDING，互不影响。
        _insert_webhook_secret(connection, **_webhook_values(other_tenant, other_project))
        _insert_webhook_secret(
            connection,
            **_webhook_values(other_tenant, other_project, key_version=2, **active),
        )

        slots = connection.execute(
            select(
                ProjectWebhookSecret.key_version,
                ProjectWebhookSecret.active_slot,
                ProjectWebhookSecret.pending_slot,
            )
            .where(ProjectWebhookSecret.project_id == project)
            .order_by(ProjectWebhookSecret.key_version)
        ).all()
        assert [tuple(row) for row in slots] == [
            (1, None, project),
            (2, project, None),
            (3, None, None),
            (4, None, None),
            (5, None, None),
        ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"key_version": 0},
        {"key_version": -1},
        {"status": "REVOKED"},
        {"status": "PENDING", "activated_at": _NOW},
        {"status": "PENDING", "retired_at": _NOW},
        {"status": "ACTIVE"},
        {"status": "ACTIVE", "activated_at": _NOW, "retired_at": _NOW},
        {"status": "RETIRED"},
        {"status": "RETIRED", "activated_at": _NOW},
    ],
    ids=[
        "version-0",
        "version-negative",
        "status-unknown",
        "pending-with-activated",
        "pending-with-retired",
        "active-without-activated",
        "active-with-retired",
        "retired-without-time",
        "retired-with-only-activated",
    ],
)
@needs_mysql
def test_0008_checks_refuse_bad_rows(alembic_config: Config, overrides: dict) -> None:
    """版本号从 1 起；状态只有三种；时间与状态一致（设计 §2）。"""
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        tenant, project = _new_project(connection)

        bad = _webhook_values(tenant, project, **overrides)
        assert _webhook_refused(connection, **bad) == _ER_CHECK_CONSTRAINT_VIOLATED

        # 合法的两种退役行：从 PENDING 直接退役（没有启用时刻）、从 ACTIVE 退役（两个都有）。
        retired = {"status": "RETIRED", "retired_at": _NOW}
        _insert_webhook_secret(connection, **_webhook_values(tenant, project, **retired))
        _insert_webhook_secret(
            connection,
            **_webhook_values(tenant, project, key_version=2, activated_at=_NOW, **retired),
        )


# ---------------------------------------------------------------------------
# 0009_tenant_account_status（AIH-TASK-020，设计闸门 #136 v2）
#
# 设计 §7「迁移」：upgrade 后已有行为 ENABLED；非法值被 CHECK 拒绝；downgrade 先删 CHECK
# 再删列。头两条不连库：比对迁移与模型的 CHECK、列宽与默认值，钉住版本链。
# ---------------------------------------------------------------------------

_REVISION_0009 = "0009_tenant_account_status"
_MIGRATION_0009 = pathlib.Path("alembic/versions/20260928_0009_tenant_account_status.py")
_ACCOUNT_STATUS_CHECK = "ck_tenants_account_status"

_TENANT_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME = 'tenants'"
)


def _load_0009() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0009", _MIGRATION_0009)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0009_checks_are_the_ones_the_model_declares() -> None:
    migration = _load_0009()
    from_migration = {
        name: ("tenants", _normalised(rule)) for name, rule in migration._CHECKS.items()
    }
    model = _model_checks()

    assert set(from_migration) == {_ACCOUNT_STATUS_CHECK}
    assert from_migration[_ACCOUNT_STATUS_CHECK] == model[_ACCOUNT_STATUS_CHECK]
    # CHECK 的取值恰好是枚举的四个成员（设计 §2「数据库」）。
    condition = from_migration[_ACCOUNT_STATUS_CHECK][1]
    assert {status.value for status in AccountStatus} == {
        "PENDING_ACTIVATION",
        "ENABLED",
        "DISABLED",
        "CLOSED",
    }
    for status in AccountStatus:
        assert f"'{status.value}'" in condition, status
    # 列宽与服务端默认值：迁移与模型一致。
    column = Tenant.__table__.c.account_status
    assert column.type.length == 32
    assert column.nullable is False
    assert migration._DEFAULT == column.server_default.arg == AccountStatus.ENABLED.value


def test_0009_follows_0008() -> None:
    migration = _load_0009()

    assert migration.revision == _REVISION_0009
    assert migration.down_revision == _REVISION_0008


def _tenant_checks() -> set[str]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            return set(connection.execute(_TENANT_CHECKS_QUERY).scalars())
    finally:
        engine.dispose()


@needs_mysql
def test_0009_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("tenants")}
        column = columns["account_status"]
        assert isinstance(column["type"], String)
        assert column["type"].length == 32
        assert column["nullable"] is False
        assert "ENABLED" in str(column["default"])
        # 不加索引：没有按账户状态筛选的查询（设计 §2「数据库」）。
        indexes = inspect(engine).get_indexes("tenants")
        assert not [index for index in indexes if "account_status" in index["column_names"]]
    finally:
        engine.dispose()
    assert _ACCOUNT_STATUS_CHECK in _tenant_checks()


@needs_mysql
def test_0009_backfills_enabled_refuses_bad_values_and_downgrades(alembic_config: Config) -> None:
    """已有行回填 ENABLED、版本不动；CHECK 拒绝四个取值以外的值；downgrade 删 CHECK 与列。"""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION_0008)
    tables = _table_names()
    assert "account_status" not in _column_names("tenants")
    assert _ACCOUNT_STATUS_CHECK not in _tenant_checks()
    public_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.begin() as connection:
            for public_id in public_ids:
                # 0008 的表形状：Tenant 模型已经多了 account_status，这里只能写裸 SQL。
                connection.execute(
                    text(
                        "INSERT INTO tenants (public_id, company_name, email, created_at,"
                        " updated_at) VALUES (:public_id, 'Account Status Sdn Bhd',"
                        " 'ops@example.com', :now, :now)"
                    ),
                    {"public_id": public_id, "now": _NOW},
                )

        command.upgrade(alembic_config, _REVISION_0009)

        # 只动 tenants：表集合不变，多一列、多一条 CHECK。
        assert _table_names() == tables
        assert _ACCOUNT_STATUS_CHECK in _tenant_checks()
        backfilled = select(Tenant.account_status, Tenant.status_version).where(
            Tenant.public_id.in_(public_ids)
        )
        with engine.connect() as connection:
            rows = connection.execute(backfilled).all()
        assert [tuple(row) for row in rows] == [(AccountStatus.ENABLED, 0)] * 2

        # 裸 SQL：绕过枚举类型，直接看数据库自己拒不拒绝。
        set_status = text("UPDATE tenants SET account_status = :value WHERE public_id = :id")
        with _rolled_back_connection() as connection:
            for bad in ("SUSPENDED", "ACTIVE", ""):
                with pytest.raises(DBAPIError) as raised:
                    with connection.begin_nested():
                        connection.execute(set_status, {"value": bad, "id": public_ids[0]})
                assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, bad
            # 四个取值都放行，包括本任务不产生的两个。
            for status in AccountStatus:
                connection.execute(set_status, {"value": status.value, "id": public_ids[0]})

        command.downgrade(alembic_config, _REVISION_0008)
        assert "account_status" not in _column_names("tenants")
        assert _ACCOUNT_STATUS_CHECK not in _tenant_checks()
        assert _table_names() == tables
    finally:
        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            connection.execute(delete(Tenant).where(Tenant.public_id.in_(public_ids)))
        engine.dispose()


# ---------------------------------------------------------------------------
# 0010_audit_logs_append_only（AIH-TASK-021）
#
# 触发器的形状与行为都在这里验：两个触发器存在；UPDATE / DELETE 被拒、SQLSTATE 是
# 45000；INSERT 照常；downgrade 删掉两个触发器。头几条不连库：钉住版本链、触发器语句
# 与 0006 同一写法、预检排在任何 DDL 之前。
# ---------------------------------------------------------------------------

_REVISION_0010 = "0010_audit_logs_append_only"
_MIGRATION_0010 = pathlib.Path("alembic/versions/20260928_0010_audit_logs_append_only.py")

# 触发器名 → (表, 时机, 事件)。
_EXPECTED_TRIGGERS_0010 = {
    "trg_audit_logs_before_update": ("audit_logs", "BEFORE", "UPDATE"),
    "trg_audit_logs_before_delete": ("audit_logs", "BEFORE", "DELETE"),
}

# SIGNAL 用户定义的 SQLSTATE 时 MySQL 报 1644（ER_SIGNAL_EXCEPTION）。SQLSTATE 本身驱动
# 不带回来，要用 GET DIAGNOSTICS 读。
_ER_SIGNAL_EXCEPTION = 1644
_APPEND_ONLY_MESSAGE = "audit_logs is append-only"
_GET_SQLSTATE = "GET DIAGNOSTICS CONDITION 1 @audit_sqlstate = RETURNED_SQLSTATE"
# CAST 成 CHAR：用户变量的字符集随来源而定，驱动可能把它当二进制交回 bytes。
_READ_SQLSTATE = text("SELECT CAST(@audit_sqlstate AS CHAR)")


def _load_0010() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0010", _MIGRATION_0010)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0010_follows_0009() -> None:
    migration = _load_0010()

    assert migration.revision == _REVISION_0010
    assert migration.down_revision == _REVISION_0009


def test_0010_triggers_are_written_like_the_ledger_ones() -> None:
    """写法照 0006 的账本 BEFORE UPDATE / BEFORE DELETE：只换表名（消息随之换）。"""
    ledger = _load_0006()._TRIGGERS
    audit = _load_0010()._TRIGGERS

    assert set(audit) == set(_EXPECTED_TRIGGERS_0010)
    for event in ("update", "delete"):
        statement = audit[f"trg_audit_logs_before_{event}"]
        expected = ledger[f"trg_wallet_transactions_before_{event}"]
        assert _normalised(statement) == _normalised(
            expected.replace("wallet_transactions", "audit_logs")
        )
        assert "SIGNAL SQLSTATE '45000'" in statement
        assert f"'{_APPEND_ONLY_MESSAGE}'" in statement


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0010_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    """与 0006 同一个预检：binlog 开着而开关关着时建不了触发器（ERROR 1419）。"""
    migration = _load_0010()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0010_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL 的 DDL 不参与事务：预检失败时不能已经建了一个触发器。"""
    migration = _load_0010()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    # 反过来：开关开着时同一个 upgrade 确实建了两个触发器 —— 上面的空列表不是因为没走到。
    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    assert allowed.calls == ["execute", "execute"]


def _audit_triggers() -> dict[str, tuple[str, ...]]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            rows = connection.execute(_TRIGGERS_QUERY).all()
    finally:
        engine.dispose()
    return {row[0]: tuple(row[1:]) for row in rows if row[1] == "audit_logs"}


def _insert_audit(connection: Connection, reason: str) -> int:
    result = connection.execute(
        insert(AuditLog).values(action=AuditAction.LOGIN, reason=reason, created_at=_NOW)
    )
    return int(result.inserted_primary_key[0])


def _count_audits(connection: Connection) -> int:
    return int(connection.execute(select(func.count()).select_from(AuditLog)).scalar_one())


@needs_mysql
def test_0010_audit_logs_refuse_update_and_delete_but_take_inserts(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    assert _audit_triggers() == _EXPECTED_TRIGGERS_0010

    with _rolled_back_connection() as connection:
        before = _count_audits(connection)
        audit_id = _insert_audit(connection, "migration test")
        # INSERT 照常。
        assert _count_audits(connection) == before + 1

        refused = {
            "UPDATE": "UPDATE audit_logs SET reason = 'edited' WHERE id = %s",
            "DELETE": "DELETE FROM audit_logs WHERE id = %s",
        }
        for event, statement in refused.items():
            # ⚠️ 不用 begin_nested：回滚到保存点会清掉诊断区，GET DIAGNOSTICS 就读不到了。
            # MySQL 里失败的语句只撤销它自己，外层事务照常；外层事务里 SQLAlchemy 也不会
            # 替我们回滚。
            with pytest.raises(DBAPIError) as raised:
                connection.exec_driver_sql(statement, (audit_id,))
            errno, message = raised.value.orig.args[:2]
            connection.exec_driver_sql(_GET_SQLSTATE)
            sqlstate = connection.execute(_READ_SQLSTATE).scalar_one()
            assert (int(errno), message, sqlstate) == (
                _ER_SIGNAL_EXCEPTION,
                _APPEND_ONLY_MESSAGE,
                "45000",
            ), event

        # 两次都被拒：那一行原样还在。
        reason = select(AuditLog.reason).where(AuditLog.id == audit_id)
        assert connection.execute(reason).scalar_one() == "migration test"
        assert _count_audits(connection) == before + 1


@needs_mysql
def test_0010_downgrade_drops_only_the_two_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    tables = _table_names()
    columns = _column_names("audit_logs")

    try:
        command.downgrade(alembic_config, _REVISION_0009)
        assert _audit_triggers() == {}
        # 只删触发器：表与列都不动（0012–0015 与 0017 的表随降级到 0009 一并删掉）。
        later = _TABLES_0012 | _TABLES_0013 | _TABLES_0014 | _TABLES_0015 | _TABLES_0017
        assert _table_names() == tables - later
        assert _column_names("audit_logs") == columns
        # 触发器没了，UPDATE / DELETE 又能执行（事务回滚，不留行）。
        with _rolled_back_connection() as connection:
            audit_id = _insert_audit(connection, "migration test")
            connection.exec_driver_sql(
                "UPDATE audit_logs SET reason = 'edited' WHERE id = %s", (audit_id,)
            )
            connection.exec_driver_sql("DELETE FROM audit_logs WHERE id = %s", (audit_id,))

        command.upgrade(alembic_config, _REVISION_0010)
        assert _audit_triggers() == _EXPECTED_TRIGGERS_0010
    finally:
        command.upgrade(alembic_config, "head")


# ---------------------------------------------------------------------------
# 0011_audit_logs_query_indexes（AIH-TASK-022）
#
# 头两条不连库：钉住版本链、迁移与模型声明的索引一致。真 MySQL 上验 head 的索引集合，以及
# downgrade 只删这三个、表与触发器不动。
# ---------------------------------------------------------------------------

_REVISION_0011 = "0011_audit_logs_query_indexes"
_MIGRATION_0011 = pathlib.Path("alembic/versions/20260928_0011_audit_logs_query_indexes.py")

# 0002 建表时就有的那一个。
_AUDIT_INDEXES_BEFORE_0011 = {"ix_audit_logs_actor_created": ["actor_user_id", "created_at"]}
_EXPECTED_INDEXES_0011 = {
    "ix_audit_logs_entity": ["entity_type", "entity_id", "id"],
    "ix_audit_logs_action": ["action", "id"],
    "ix_audit_logs_created_at": ["created_at"],
}


def _load_0011() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0011", _MIGRATION_0011)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0011_follows_0010() -> None:
    migration = _load_0011()

    assert migration.revision == _REVISION_0011
    assert migration.down_revision == _REVISION_0010


def test_0011_indexes_are_the_ones_the_model_declares() -> None:
    """迁移里是冻结的字面量；模型的 `__table_args__` 要声明同样的三个，一列不差。"""
    migration = _load_0011()
    model = {
        str(index.name): [column.name for column in index.columns]
        for index in AuditLog.__table__.indexes
    }

    assert migration._INDEXES == _EXPECTED_INDEXES_0011
    assert model == _AUDIT_INDEXES_BEFORE_0011 | _EXPECTED_INDEXES_0011
    assert not any(index.unique for index in AuditLog.__table__.indexes)


def test_0011_downgrade_drops_what_upgrade_creates(monkeypatch) -> None:
    migration = _load_0011()
    recorder = _RecordingOp(_FakeBind(log_bin=0, trusted=0))
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()
    migration.downgrade()

    assert recorder.calls == ["create_index"] * 3 + ["drop_index"] * 3


def _audit_indexes() -> dict[str, list[str]]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        indexes = inspect(engine).get_indexes("audit_logs")
    finally:
        engine.dispose()
    return {str(index["name"]): list(index["column_names"]) for index in indexes}


@needs_mysql
def test_0011_indexes_exist_at_head_and_downgrade_drops_only_them(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    assert _audit_indexes() == _AUDIT_INDEXES_BEFORE_0011 | _EXPECTED_INDEXES_0011
    tables = _table_names()
    columns = _column_names("audit_logs")

    try:
        command.downgrade(alembic_config, _REVISION_0010)
        assert _audit_indexes() == _AUDIT_INDEXES_BEFORE_0011
        # 只删索引：表、列与 0010 的触发器都不动（0012–0015 与 0017 的表随降级到 0010 一并删掉）。
        later = _TABLES_0012 | _TABLES_0013 | _TABLES_0014 | _TABLES_0015 | _TABLES_0017
        assert _table_names() == tables - later
        assert _column_names("audit_logs") == columns
        assert _audit_triggers() == _EXPECTED_TRIGGERS_0010

        command.upgrade(alembic_config, _REVISION_0011)
        assert _audit_indexes() == _AUDIT_INDEXES_BEFORE_0011 | _EXPECTED_INDEXES_0011
    finally:
        command.upgrade(alembic_config, "head")


# ---------------------------------------------------------------------------
# 0012_ai_catalog（AIH-TASK-025，设计闸门 #163 v4）
#
# 头几条不连库：钉住版本链；迁移与模型的 CHECK、生成列、排序规则一致；种子在迁移、代码常量
# 与设计 §2 的清单三方一致；downgrade 按外键依赖倒序删表。真 MySQL 上验五张表的形状、键、
# 外键删除规则、种子行、CHECK 拒绝非法取值，以及升降只增删这五张表。约束的**行为**（形态
# 不符的分量、第二个未截断段、跨供应商的别名、删有引用的行）在 test_usage_meter_api.py 与
# test_ai_catalog_resolve.py。
# ---------------------------------------------------------------------------

_REVISION_0012 = "0012_ai_catalog"
_MIGRATION_0012 = pathlib.Path("alembic/versions/20260929_0012_ai_catalog.py")
_CATALOG_MODELS = (UsageMeterType, UsageMeterComponent, AiProvider, AiModel, AiModelAlias)
_TABLES_0012 = {model.__tablename__ for model in _CATALOG_MODELS}
# 建表顺序；downgrade 必须恰好反过来（外键依赖）。
_CREATE_ORDER_0012 = [
    "usage_meter_types",
    "usage_meter_components",
    "ai_providers",
    "ai_models",
    "ai_model_aliases",
]

# 设计 §2「种子」那张表逐行手抄：code → (上报形态, 单位, 数量类型, {分量: 取数字段})。
# 不从代码或迁移里取 —— 两边各自对照这一份。
_DESIGN_SEED: dict[str, tuple[str, str, str, dict[str, str]]] = {
    "LLM_TOKEN": (
        "LLM_TOKEN_FIELDS",
        "TOKEN",
        "INTEGER",
        {
            "LLM_INPUT_TOKEN": "input_tokens",
            "LLM_OUTPUT_TOKEN": "output_tokens",
            "LLM_CACHE_WRITE_TOKEN": "cache_creation_input_tokens",
            "LLM_CACHE_READ_TOKEN": "cache_read_input_tokens",
        },
    ),
    "EMBEDDING_TOKEN": ("QUANTITY", "TOKEN", "INTEGER", {"EMBEDDING_TOKEN": "quantity"}),
    "AUDIO_SECOND": ("QUANTITY", "SECOND", "DECIMAL", {"AUDIO_SECOND": "quantity"}),
    "AUDIO_MINUTE": ("QUANTITY", "MINUTE", "DECIMAL", {"AUDIO_MINUTE": "quantity"}),
    "TTS_CHARACTER": ("QUANTITY", "CHARACTER", "INTEGER", {"TTS_CHARACTER": "quantity"}),
    "IMAGE_GENERATION": ("QUANTITY", "IMAGE", "INTEGER", {"IMAGE_GENERATION": "quantity"}),
    "OCR_PAGE": ("QUANTITY", "PAGE", "INTEGER", {"OCR_PAGE": "quantity"}),
    "DOCUMENT_PAGE": ("QUANTITY", "PAGE", "INTEGER", {"DOCUMENT_PAGE": "quantity"}),
    "CUSTOM": ("QUANTITY", "UNIT", "DECIMAL", {"CUSTOM": "quantity"}),
}

_EXPECTED_0012_COLUMNS = {
    "usage_meter_types": {
        "id": False,
        "public_id": False,
        "code": False,
        "display_name": False,
        "payload_shape": False,
        "unit": False,
        "quantity_kind": False,
        "status": False,
        "created_at": False,
        "updated_at": False,
    },
    "usage_meter_components": {
        "id": False,
        "meter_type_id": False,
        "payload_shape": False,
        "component_code": False,
        "quantity_field": False,
        "created_at": False,
    },
    "ai_providers": {
        "id": False,
        "public_id": False,
        "code": False,
        "display_name": False,
        "status": False,
        "created_at": False,
        "updated_at": False,
    },
    "ai_models": {
        "id": False,
        "public_id": False,
        "provider_id": False,
        "code": False,
        "display_name": False,
        "status": False,
        "created_at": False,
        "updated_at": False,
    },
    "ai_model_aliases": {
        "id": False,
        "public_id": False,
        "provider_id": False,
        "model_id": False,
        "alias": False,
        # NULL = 「一直以来」/「仍生效」；生成列在截断的段上是 NULL。
        "effective_from": True,
        "effective_to": True,
        "open_slot": True,
        "created_at": False,
        "closed_at": True,
    },
}

# (表, 列) → 列宽。这几列是代码，按字节比较（utf8mb4_0900_bin）。
_CODE_COLUMNS_0012 = {
    ("usage_meter_types", "code"): 32,
    ("usage_meter_types", "unit"): 16,
    ("usage_meter_components", "component_code"): 64,
    ("ai_providers", "code"): 64,
    ("ai_models", "code"): 128,
    ("ai_model_aliases", "alias"): 128,
}

# head 上的唯一约束：`usage_meter_types` 的最后一个由 0017 加，只为用量事件的复合外键。
_EXPECTED_UNIQUE_0012 = {
    "usage_meter_types": {
        ("public_id",),
        ("code",),
        ("id", "payload_shape"),
        ("id", "payload_shape", "unit", "quantity_kind"),
    },
    "usage_meter_components": {("component_code",), ("meter_type_id", "quantity_field")},
    "ai_providers": {("public_id",), ("code",)},
    "ai_models": {("public_id",), ("provider_id", "code"), ("id", "provider_id")},
    "ai_model_aliases": {("public_id",), ("provider_id", "alias", "open_slot")},
}

_EXPECTED_FOREIGN_KEYS_0012 = {
    "usage_meter_components": {
        "fk_usage_meter_components_type": (
            ["meter_type_id", "payload_shape"],
            "usage_meter_types",
            ["id", "payload_shape"],
        ),
    },
    "ai_models": {
        "fk_ai_models_provider": (["provider_id"], "ai_providers", ["id"]),
    },
    "ai_model_aliases": {
        "fk_ai_model_aliases_model": (
            ["model_id", "provider_id"],
            "ai_models",
            ["id", "provider_id"],
        ),
    },
}

_CATALOG_DELETE_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME IN ('usage_meter_types',"
    " 'usage_meter_components', 'ai_providers', 'ai_models', 'ai_model_aliases')"
)
_CATALOG_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME IN ('usage_meter_types', 'usage_meter_components', 'ai_providers',"
    " 'ai_models', 'ai_model_aliases')"
)


def _load_0012() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0012", _MIGRATION_0012)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog_checks() -> dict[str, tuple[str, str]]:
    return {
        str(constraint.name): (model.__tablename__, _normalised(str(constraint.sqltext)))
        for model in _CATALOG_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0012_follows_0011() -> None:
    migration = _load_0012()

    assert migration.revision == _REVISION_0012
    assert migration.down_revision == _REVISION_0011


def test_0012_checks_slot_and_collation_are_the_ones_the_models_declare() -> None:
    migration = _load_0012()
    from_migration = {
        name: (table, _normalised(condition))
        for name, (table, condition) in migration._CHECKS.items()
    }

    assert from_migration == _catalog_checks()
    # 类型 4 条、分量 2 条、供应商 1 条、模型 1 条、别名 1 条。
    assert len(from_migration) == 9
    computed = AiModelAlias.__table__.c.open_slot.computed
    assert computed is not None
    assert computed.persisted is True
    assert _normalised(str(computed.sqltext)) == _normalised(migration._OPEN_SLOT)
    assert _normalised(migration._OPEN_SLOT) == _normalised(OPEN_SLOT_EXPRESSION)
    assert migration._CODE_COLLATION == CODE_COLLATION


def test_0012_seed_is_the_design_list_in_the_migration_and_in_the_code() -> None:
    """设计 §5「迁移种子与代码里的常量不一致」：三方逐行一致，9 个类型、12 个分量。"""
    migration = _load_0012()
    from_migration = {
        code: (shape, unit, kind, dict(components))
        for code, _, shape, unit, kind, components in migration._SEED_METER_TYPES
    }
    from_code = {
        seed.code: (
            seed.payload_shape.value,
            seed.unit,
            seed.quantity_kind.value,
            dict(seed.components),
        )
        for seed in SEED_METER_TYPES
    }

    assert from_migration == _DESIGN_SEED
    assert from_code == _DESIGN_SEED
    assert len(from_migration) == 9
    assert sum(len(components) for *_, components in from_migration.values()) == 12
    # 顺序与显示名也一致。
    assert [(row[0], row[1]) for row in migration._SEED_METER_TYPES] == [
        (seed.code, seed.display_name) for seed in SEED_METER_TYPES
    ]


class _ArgsRecorder:
    """Stands in for `alembic.op` and keeps each call's positional arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        def record(*args: object, **_kwargs: object) -> None:
            self.calls.append((name, args))

        return record

    def first_args(self, name: str) -> list[object]:
        return [args[0] for called, args in self.calls if called == name]


def test_0012_downgrade_drops_the_five_tables_in_dependency_order(monkeypatch) -> None:
    migration = _load_0012()
    upgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert upgrade.first_args("create_table") == _CREATE_ORDER_0012
    # 种子：一次写 9 个类型，12 条 INSERT … SELECT 写分量。
    assert len(upgrade.first_args("bulk_insert")) == 1
    assert len(upgrade.first_args("execute")) == 12

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    assert downgrade.first_args("drop_table") == _CREATE_ORDER_0012[::-1]
    assert [name for name, _ in downgrade.calls] == ["drop_table"] * 5


@needs_mysql
def test_0012_only_adds_and_drops_its_five_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION_0011)
        without = _table_names()
        assert "audit_logs" in without, "0011 的表应该都在，否则下面的比较没有意义"
        assert not _TABLES_0012 & without

        command.upgrade(alembic_config, _REVISION_0012)
        assert _table_names() == without | _TABLES_0012

        command.downgrade(alembic_config, _REVISION_0011)
        assert _table_names() == without
    finally:
        command.upgrade(alembic_config, "head")


@needs_mysql
def test_0012_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        columns = {table: inspector.get_columns(table) for table in _EXPECTED_0012_COLUMNS}
        for table, expected in _EXPECTED_0012_COLUMNS.items():
            assert {c["name"]: c["nullable"] for c in columns[table]} == expected, table
        types = {(table, c["name"]): c["type"] for table, found in columns.items() for c in found}

        for (table, name), column_type in types.items():
            if name == "id" or (name.endswith("_id") and name != "public_id"):
                assert isinstance(column_type, BigInteger), (table, name)
            if name == "public_id":
                assert isinstance(column_type, CHAR), table
                assert column_type.length == 36, table
            if name.endswith("_at") or name.startswith("effective_"):
                assert isinstance(column_type, DateTime), (table, name)
        # 代码列：写死的宽度，按字节比较。其余文本列用库默认的排序规则。
        for key, width in _CODE_COLUMNS_0012.items():
            assert isinstance(types[key], String), key
            assert types[key].length == width, key
            assert types[key].collation == CODE_COLLATION, key
        for key, column_type in types.items():
            if key not in _CODE_COLUMNS_0012 and isinstance(column_type, String):
                assert getattr(column_type, "collation", None) != CODE_COLLATION, key
        widths = {
            ("usage_meter_types", "display_name"): 255,
            ("usage_meter_types", "payload_shape"): 32,
            ("usage_meter_types", "quantity_kind"): 16,
            ("usage_meter_types", "status"): 16,
            ("usage_meter_components", "payload_shape"): 32,
            ("usage_meter_components", "quantity_field"): 64,
            ("ai_providers", "status"): 16,
            ("ai_models", "status"): 16,
        }
        for key, width in widths.items():
            assert types[key].length == width, key

        # 状态默认 ACTIVE（设计 §2）。
        for table in ("usage_meter_types", "ai_providers", "ai_models"):
            [status] = [c for c in columns[table] if c["name"] == "status"]
            assert "ACTIVE" in str(status["default"]), table

        # 只有 open_slot 是生成列，而且是 STORED。
        computed = {
            (table, c["name"]): c.get("computed")
            for table, found in columns.items()
            for c in found
            if c.get("computed")
        }
        assert set(computed) == {("ai_model_aliases", "open_slot")}
        open_slot = computed[("ai_model_aliases", "open_slot")]
        assert open_slot["persisted"] is True
        assert "effective_to" in str(open_slot["sqltext"])
    finally:
        engine.dispose()


@needs_mysql
def test_0012_keys_indexes_foreign_keys_and_checks(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        for table, expected in _EXPECTED_UNIQUE_0012.items():
            assert _unique_sets(table) == expected, table
        for table in _TABLES_0012:
            foreign_keys = {
                str(fk["name"]): (
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table)
            }
            assert foreign_keys == _EXPECTED_FOREIGN_KEYS_0012.get(table, {}), table

        component_indexes = inspector.get_indexes("usage_meter_components")
        by_name = {i["name"]: i["column_names"] for i in component_indexes}
        assert by_name["ix_usage_meter_components_type_shape"] == [
            "meter_type_id",
            "payload_shape",
        ]
        alias_indexes = {i["name"]: i for i in inspector.get_indexes("ai_model_aliases")}
        open_slot = alias_indexes["ux_ai_model_aliases_open_slot"]
        assert open_slot["column_names"] == ["provider_id", "alias", "open_slot"]
        assert open_slot["unique"]
        assert alias_indexes["ix_ai_model_aliases_model"]["column_names"] == [
            "model_id",
            "provider_id",
        ]

        with engine.connect() as connection:
            delete_rules = dict(connection.execute(_CATALOG_DELETE_RULES_QUERY).all())
            checks = set(connection.execute(_CATALOG_CHECKS_QUERY).scalars())
        # ⚠️ RESTRICT：目录行删不掉，价格版本与用量事件会永久引用它们（INV-6）。
        assert delete_rules == {
            "fk_usage_meter_components_type": "RESTRICT",
            "fk_ai_models_provider": "RESTRICT",
            "fk_ai_model_aliases_model": "RESTRICT",
        }
        assert checks == set(_catalog_checks())
    finally:
        engine.dispose()


@needs_mysql
def test_0012_seeds_the_design_list(alembic_config: Config) -> None:
    """升到 0012 的那一刻，库里恰好是设计 §2 的 9 个类型与 12 个分量。"""
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        # 重建这五张表：别的用例在共享库里留下的目录行不算进来。
        command.downgrade(alembic_config, _REVISION_0011)
        command.upgrade(alembic_config, _REVISION_0012)
        with engine.connect() as connection:
            types = connection.execute(
                text(
                    "SELECT id, public_id, code, display_name, payload_shape, unit,"
                    " quantity_kind, status, created_at, updated_at FROM usage_meter_types"
                )
            ).all()
            components = connection.execute(
                text(
                    "SELECT meter_type_id, payload_shape, component_code, quantity_field"
                    " FROM usage_meter_components"
                )
            ).all()

        by_id = {row.id: row for row in types}
        seeded: dict[str, tuple[str, str, str, dict[str, str]]] = {
            row.code: (row.payload_shape, row.unit, row.quantity_kind, {}) for row in types
        }
        for component in components:
            owner = by_id[component.meter_type_id]
            # 分量的形态就是所属类型的形态。
            assert component.payload_shape == owner.payload_shape
            seeded[owner.code][3][component.component_code] = component.quantity_field

        assert seeded == _DESIGN_SEED
        assert len(components) == 12
        names = {seed.code: seed.display_name for seed in SEED_METER_TYPES}
        for row in types:
            assert row.display_name == names[row.code]
            assert row.status == "ACTIVE"
            assert str(uuid.UUID(row.public_id)) == row.public_id
            assert row.created_at == row.updated_at
            assert row.created_at.microsecond == 0
        assert len({row.public_id for row in types}) == 9
    finally:
        command.upgrade(alembic_config, "head")
        engine.dispose()


# 裸 SQL：绕过模型的枚举类型，直接看数据库自己拒不拒绝。
_METER_TYPE_INSERT = text(
    "INSERT INTO usage_meter_types (public_id, code, display_name, payload_shape, unit,"
    " quantity_kind, status, created_at, updated_at) VALUES (:public_id, 'MIGRATION_T',"
    " 'Migration test', :shape, :unit, :kind, :status, :now, :now)"
)
_PROVIDER_INSERT = text(
    "INSERT INTO ai_providers (public_id, code, display_name, status, created_at, updated_at)"
    " VALUES (:public_id, 'migration-test', 'Migration test', :status, :now, :now)"
)
_MODEL_INSERT = text(
    "INSERT INTO ai_models (public_id, provider_id, code, display_name, status, created_at,"
    " updated_at) VALUES (:public_id, :provider_id, 'migration-test', 'Migration test',"
    " :status, :now, :now)"
)


def _check_refused(connection: Connection, statement, **values: object) -> int:
    """Run in a savepoint; return the MySQL error number it was refused with."""
    params = {"public_id": str(uuid.uuid4()), "now": _NOW, **values}
    with pytest.raises(DBAPIError) as raised:
        with connection.begin_nested():
            connection.execute(statement, params)
    return int(raised.value.orig.args[0])


@pytest.mark.parametrize(
    "overrides",
    [
        {"shape": "OTHER"},
        {"kind": "FLOAT"},
        {"status": "DISABLED"},
        {"shape": "LLM_TOKEN_FIELDS", "unit": "SECOND", "kind": "INTEGER"},
        {"shape": "LLM_TOKEN_FIELDS", "unit": "TOKEN", "kind": "DECIMAL"},
    ],
    ids=[
        "shape-unknown",
        "kind-unknown",
        "status-unknown",
        "token-shape-not-token-unit",
        "token-shape-decimal",
    ],
)
@needs_mysql
def test_0012_meter_type_checks_refuse_bad_values(alembic_config: Config, overrides: dict) -> None:
    """形态、数量类型、状态的取值；`LLM_TOKEN_FIELDS` 只能是 TOKEN、整数（设计 §2）。"""
    command.upgrade(alembic_config, "head")
    valid = {"shape": "QUANTITY", "unit": "UNIT", "kind": "DECIMAL", "status": "ACTIVE"}
    with _rolled_back_connection() as connection:
        refused = _check_refused(connection, _METER_TYPE_INSERT, **{**valid, **overrides})
        assert refused == _ER_CHECK_CONSTRAINT_VIOLATED
        # 合法的 LLM_TOKEN_FIELDS 行放行：CHECK 不是一律拒绝这个形态。
        token = {"shape": "LLM_TOKEN_FIELDS", "unit": "TOKEN", "kind": "INTEGER"}
        connection.execute(
            _METER_TYPE_INSERT,
            {"public_id": str(uuid.uuid4()), "now": _NOW, **valid, **token},
        )


@needs_mysql
def test_0012_provider_and_model_status_checks(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    with _rolled_back_connection() as connection:
        refused = _check_refused(connection, _PROVIDER_INSERT, status="DISABLED")
        assert refused == _ER_CHECK_CONSTRAINT_VIOLATED

        # RETIRED 是合法取值：停用的供应商照样能挂模型。
        params = {"public_id": str(uuid.uuid4()), "now": _NOW, "status": "RETIRED"}
        provider = connection.execute(_PROVIDER_INSERT, params).lastrowid
        refused = _check_refused(
            connection,
            _MODEL_INSERT,
            provider_id=provider,
            status="DISABLED",
        )
        assert refused == _ER_CHECK_CONSTRAINT_VIOLATED


# ---------------------------------------------------------------------------
# 0013_provider_prices（AIH-TASK-026，设计闸门 #177 v3）
#
# 头几条不连库：钉住版本链；迁移与模型的 CHECK、生成列一致；触发器写法与预检照 0006，
# 预检排在任何 DDL 之前；downgrade 按外键依赖倒序删表。真 MySQL 上验两张表的形状、键、外键删除规则、
# CHECK 与触发器的集合，以及升降只增删这两张表。触发器与约束的**行为**（拒绝什么、放行什么）在
# test_provider_prices_service.py。
# ---------------------------------------------------------------------------

_REVISION_0013 = "0013_provider_prices"
_MIGRATION_0013 = pathlib.Path("alembic/versions/20260929_0013_provider_prices.py")
_PRICE_MODELS = (ProviderPriceVersion, ProviderPriceComponent)
_TABLES_0013 = {model.__tablename__ for model in _PRICE_MODELS}
# 建表顺序；downgrade 必须恰好反过来（外键依赖）。
_CREATE_ORDER_0013 = ["provider_price_versions", "provider_price_components"]

# 触发器名 → (表, 时机, 事件)。设计 §2「不可变」的六个，一个不多一个不少。
_EXPECTED_TRIGGERS_0013 = {
    "trg_provider_price_versions_before_insert": ("provider_price_versions", "BEFORE", "INSERT"),
    "trg_provider_price_versions_before_update": ("provider_price_versions", "BEFORE", "UPDATE"),
    "trg_provider_price_versions_before_delete": ("provider_price_versions", "BEFORE", "DELETE"),
    "trg_provider_price_components_before_insert": (
        "provider_price_components",
        "BEFORE",
        "INSERT",
    ),
    "trg_provider_price_components_before_update": (
        "provider_price_components",
        "BEFORE",
        "UPDATE",
    ),
    "trg_provider_price_components_before_delete": (
        "provider_price_components",
        "BEFORE",
        "DELETE",
    ),
}

_EXPECTED_0013_COLUMNS = {
    "provider_price_versions": {
        "id": False,
        "public_id": False,
        "provider_id": False,
        "model_id": False,
        "source_currency": False,
        "source_type": False,
        "source_reference": False,
        "status": False,
        # NULL = 草稿 / 「一直以来」/「仍生效」；生成列在截断、未发布的行上是 NULL。
        "effective_from": True,
        "effective_to": True,
        "open_slot": True,
        "created_by": False,
        "approved_by": True,
        "created_at": False,
        "updated_at": False,
        "approved_at": True,
    },
    "provider_price_components": {
        "id": False,
        "provider_price_version_id": False,
        "usage_meter_component_id": False,
        "unit_quantity": False,
        "rate_amount": False,
        "metadata_json": True,
        "created_at": False,
    },
}

_EXPECTED_UNIQUE_0013 = {
    "provider_price_versions": {("public_id",), ("provider_id", "model_id", "open_slot")},
    "provider_price_components": {("provider_price_version_id", "usage_meter_component_id")},
}

_EXPECTED_FOREIGN_KEYS_0013 = {
    "provider_price_versions": {
        "fk_provider_price_versions_model": (
            ["model_id", "provider_id"],
            "ai_models",
            ["id", "provider_id"],
        ),
        "fk_provider_price_versions_created_by": (["created_by"], "users", ["id"]),
        "fk_provider_price_versions_approved_by": (["approved_by"], "users", ["id"]),
    },
    "provider_price_components": {
        "fk_provider_price_components_version": (
            ["provider_price_version_id"],
            "provider_price_versions",
            ["id"],
        ),
        "fk_provider_price_components_meter_component": (
            ["usage_meter_component_id"],
            "usage_meter_components",
            ["id"],
        ),
    },
}

_PRICE_DELETE_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND TABLE_NAME IN ('provider_price_versions', 'provider_price_components')"
)
_PRICE_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME IN ('provider_price_versions', 'provider_price_components')"
)


def _load_0013() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0013", _MIGRATION_0013)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _price_checks() -> dict[str, tuple[str, str]]:
    return {
        str(constraint.name): (model.__tablename__, _normalised(str(constraint.sqltext)))
        for model in _PRICE_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0013_follows_0012() -> None:
    migration = _load_0013()

    assert migration.revision == _REVISION_0013
    assert migration.down_revision == _REVISION_0012


def test_0013_checks_and_slot_are_the_ones_the_models_declare() -> None:
    migration = _load_0013()
    from_migration = {
        name: (table, _normalised(condition))
        for name, (table, condition) in migration._CHECKS.items()
    }

    assert from_migration == _price_checks()
    # 版本 5 条、分量 2 条。
    assert len(from_migration) == 7
    computed = ProviderPriceVersion.__table__.c.open_slot.computed
    assert computed is not None
    assert computed.persisted is True
    assert _normalised(str(computed.sqltext)) == _normalised(migration._OPEN_SLOT)
    # 生成列只在「未截断的已发布版本」上有值。
    assert "'PUBLISHED'" in migration._OPEN_SLOT
    assert "effective_to IS NULL" in migration._OPEN_SLOT


def test_0013_triggers_are_the_six_of_the_design_written_like_0006() -> None:
    """每条都是一条完整的 CREATE TRIGGER，拒绝时 SIGNAL SQLSTATE '45000'（与 0006 同一写法）。"""
    triggers = _load_0013()._TRIGGERS

    assert set(triggers) == set(_EXPECTED_TRIGGERS_0013)
    for name, (table, timing, event) in _EXPECTED_TRIGGERS_0013.items():
        statement = _normalised(triggers[name])
        assert statement.startswith(f"CREATE TRIGGER {name} {timing} {event} ON {table}"), name
        assert "FOR EACH ROW" in statement, name
        assert "SIGNAL SQLSTATE '45000'" in statement, name
    # 区间不重叠：先锁供应商行（与服务层同一把锁），再查相交。
    update = _normalised(triggers["trg_provider_price_versions_before_update"])
    locked = update.index("FROM ai_providers WHERE id = NEW.provider_id FOR UPDATE")
    assert locked < update.index("provider price periods overlap")


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0013_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    """与 0006 同一个预检：binlog 开着而开关关着时建不了触发器（ERROR 1419）。"""
    migration = _load_0013()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0013_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL 的 DDL 不参与事务：预检失败时不能已经建了一张表。"""
    migration = _load_0013()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    # 反过来：开关开着时同一个 upgrade 确实走到了 DDL —— 上面的空列表不是因为没走到。
    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    assert allowed.calls == ["create_table", "create_table"] + ["execute"] * 6


class _OfflineArgsRecorder(_ArgsRecorder):
    """`_ArgsRecorder` in `--sql` mode: no bind, so the precheck is skipped."""

    def get_context(self) -> SimpleNamespace:
        return SimpleNamespace(as_sql=True)


def test_0013_downgrade_drops_the_two_tables_in_dependency_order(monkeypatch) -> None:
    migration = _load_0013()
    upgrade = _OfflineArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert upgrade.first_args("create_table") == _CREATE_ORDER_0013
    # 不写种子：只有六条 CREATE TRIGGER。
    assert len(upgrade.first_args("execute")) == 6
    assert not upgrade.first_args("bulk_insert")

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    assert downgrade.first_args("drop_table") == _CREATE_ORDER_0013[::-1]
    assert [name for name, _ in downgrade.calls] == ["drop_table"] * 2


@needs_mysql
def test_0013_only_adds_and_drops_its_two_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION_0012)
        without = _table_names()
        assert "ai_models" in without, "0012 的表应该都在，否则下面的比较没有意义"
        assert not _TABLES_0013 & without

        command.upgrade(alembic_config, _REVISION_0013)
        assert _table_names() == without | _TABLES_0013

        command.downgrade(alembic_config, _REVISION_0012)
        assert _table_names() == without
        # 触发器随表删除。
        assert not set(_EXPECTED_TRIGGERS_0013) & set(_all_triggers())
    finally:
        command.upgrade(alembic_config, "head")


def _all_triggers() -> dict[str, tuple[str, ...]]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            rows = connection.execute(_TRIGGERS_QUERY).all()
    finally:
        engine.dispose()
    return {row[0]: tuple(row[1:]) for row in rows}


@needs_mysql
def test_0013_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        columns = {table: inspector.get_columns(table) for table in _EXPECTED_0013_COLUMNS}
        for table, expected in _EXPECTED_0013_COLUMNS.items():
            assert {c["name"]: c["nullable"] for c in columns[table]} == expected, table
        types = {(table, c["name"]): c["type"] for table, found in columns.items() for c in found}

        for (table, name), column_type in types.items():
            if name == "id" or (name.endswith("_id") and name != "public_id"):
                assert isinstance(column_type, BigInteger), (table, name)
            if name.endswith("_at") or name.startswith("effective_"):
                assert isinstance(column_type, DateTime), (table, name)
        versions, components = "provider_price_versions", "provider_price_components"
        for name in ("created_by", "approved_by"):
            assert isinstance(types[(versions, name)], BigInteger), name
        assert isinstance(types[(versions, "public_id")], CHAR)
        assert types[(versions, "public_id")].length == 36
        assert isinstance(types[(versions, "source_currency")], CHAR)
        assert types[(versions, "source_currency")].length == 3
        widths = {"source_type": 16, "status": 16, "source_reference": 255}
        for name, width in widths.items():
            assert isinstance(types[(versions, name)], String), name
            assert types[(versions, name)].length == width, name
        for name in ("unit_quantity", "rate_amount"):
            column_type = types[(components, name)]
            assert isinstance(column_type, Numeric), name
            assert (column_type.precision, column_type.scale) == (20, 8), name
        assert isinstance(types[(components, "metadata_json")], JSON)

        # 只有 open_slot 是生成列，而且是 STORED、整数。
        computed = {
            (table, c["name"]): c.get("computed")
            for table, found in columns.items()
            for c in found
            if c.get("computed")
        }
        assert set(computed) == {(versions, "open_slot")}
        open_slot = computed[(versions, "open_slot")]
        assert open_slot["persisted"] is True
        assert "PUBLISHED" in str(open_slot["sqltext"])
        assert isinstance(types[(versions, "open_slot")], Integer)
        assert not isinstance(types[(versions, "open_slot")], BigInteger)
    finally:
        engine.dispose()


@needs_mysql
def test_0013_keys_foreign_keys_checks_and_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        for table, expected in _EXPECTED_UNIQUE_0013.items():
            assert _unique_sets(table) == expected, table
            foreign_keys = {
                str(fk["name"]): (
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table)
            }
            assert foreign_keys == _EXPECTED_FOREIGN_KEYS_0013[table], table

        indexes = {i["name"]: i for i in inspector.get_indexes("provider_price_versions")}
        open_slot = indexes["ux_provider_price_versions_open_slot"]
        assert open_slot["column_names"] == ["provider_id", "model_id", "open_slot"]
        assert open_slot["unique"]
        by_model = indexes["ix_provider_price_versions_model"]["column_names"]
        assert by_model == ["model_id", "provider_id"]

        with engine.connect() as connection:
            delete_rules = dict(connection.execute(_PRICE_DELETE_RULES_QUERY).all())
            checks = set(connection.execute(_PRICE_CHECKS_QUERY).scalars())
        # ⚠️ RESTRICT：价格版本与分量删不掉，被引用的模型、用户、计量分量也删不掉（INV-6）。
        expected_rules = {
            name: "RESTRICT"
            for foreign_keys in _EXPECTED_FOREIGN_KEYS_0013.values()
            for name in foreign_keys
        }
        assert delete_rules == expected_rules
        assert checks == set(_price_checks())
        ours = {name: row for name, row in _all_triggers().items() if row[0] in _TABLES_0013}
        assert ours == _EXPECTED_TRIGGERS_0013
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0014_pricing_rules（AIH-TASK-027，设计闸门 #178 v4）
#
# The first cases need no database: the revision chain; the migration's checks and generated
# columns equal the models' (`scope_key` per dialect); triggers written like 0006 with the 0006
# precheck before any DDL; downgrade drops the tables in dependency order. On a real MySQL:
# the shapes, keys, foreign keys and their rules, checks and triggers of the three tables, the
# lock table holding exactly one row, and that upgrade / downgrade add and drop only these
# tables. What the triggers and constraints refuse or let through is in
# test_pricing_rules_service.py.
# ---------------------------------------------------------------------------

_REVISION_0014 = "0014_pricing_rules"
_MIGRATION_0014 = pathlib.Path("alembic/versions/20260929_0014_pricing_rules.py")
_RULE_MODELS = (PricingRuleLock, PricingRule, PricingRuleComponent)
_TABLES_0014 = {model.__tablename__ for model in _RULE_MODELS}
# Creation order; downgrade must be exactly the reverse (foreign keys).
_CREATE_ORDER_0014 = ["pricing_rule_locks", "pricing_rules", "pricing_rule_components"]

# Trigger → (table, timing, event). The eight of design §2, no more, no fewer.
_EXPECTED_TRIGGERS_0014 = {
    "trg_pricing_rule_locks_before_insert": ("pricing_rule_locks", "BEFORE", "INSERT"),
    "trg_pricing_rule_locks_before_delete": ("pricing_rule_locks", "BEFORE", "DELETE"),
    "trg_pricing_rules_before_insert": ("pricing_rules", "BEFORE", "INSERT"),
    "trg_pricing_rules_before_update": ("pricing_rules", "BEFORE", "UPDATE"),
    "trg_pricing_rules_before_delete": ("pricing_rules", "BEFORE", "DELETE"),
    "trg_pricing_rule_components_before_insert": ("pricing_rule_components", "BEFORE", "INSERT"),
    "trg_pricing_rule_components_before_update": ("pricing_rule_components", "BEFORE", "UPDATE"),
    "trg_pricing_rule_components_before_delete": ("pricing_rule_components", "BEFORE", "DELETE"),
}

_EXPECTED_0014_COLUMNS = {
    "pricing_rule_locks": {"id": False},
    "pricing_rules": {
        "id": False,
        "public_id": False,
        "priority_scope": False,
        # Which of the three scope columns are set follows from `priority_scope` (CHECK).
        "tenant_id": True,
        "provider_id": True,
        "model_id": True,
        "strategy": False,
        "markup_multiplier": True,
        "status": False,
        "effective_from": True,
        "effective_to": True,
        "scope_key": True,
        "open_slot": True,
        "created_by": False,
        "approved_by": True,
        "created_at": False,
        "updated_at": False,
        "approved_at": True,
    },
    "pricing_rule_components": {
        "id": False,
        "pricing_rule_id": False,
        "strategy": False,
        "usage_meter_component_id": False,
        "unit_quantity": False,
        "rate_amount": False,
        "currency": False,
        "created_at": False,
    },
}

_EXPECTED_UNIQUE_0014 = {
    "pricing_rule_locks": set(),
    "pricing_rules": {("public_id",), ("id", "strategy"), ("scope_key", "open_slot")},
    "pricing_rule_components": {("pricing_rule_id", "usage_meter_component_id")},
}

_EXPECTED_FOREIGN_KEYS_0014 = {
    "pricing_rule_locks": {},
    "pricing_rules": {
        "fk_pricing_rules_tenant": (["tenant_id"], "tenants", ["id"]),
        "fk_pricing_rules_provider": (["provider_id"], "ai_providers", ["id"]),
        "fk_pricing_rules_model": (["model_id", "provider_id"], "ai_models", ["id", "provider_id"]),
        "fk_pricing_rules_created_by": (["created_by"], "users", ["id"]),
        "fk_pricing_rules_approved_by": (["approved_by"], "users", ["id"]),
    },
    "pricing_rule_components": {
        "fk_pricing_rule_components_rule": (
            ["pricing_rule_id", "strategy"],
            "pricing_rules",
            ["id", "strategy"],
        ),
        "fk_pricing_rule_components_meter_component": (
            ["usage_meter_component_id"],
            "usage_meter_components",
            ["id"],
        ),
    },
}

_RULE_REFERENTIAL_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE, UPDATE_RULE"
    " FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND TABLE_NAME IN ('pricing_rule_locks', 'pricing_rules', 'pricing_rule_components')"
)
_RULE_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME IN ('pricing_rule_locks', 'pricing_rules', 'pricing_rule_components')"
)


def _load_0014() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0014", _MIGRATION_0014)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rule_checks() -> dict[str, tuple[str, str]]:
    return {
        str(constraint.name): (model.__tablename__, _normalised(str(constraint.sqltext)))
        for model in _RULE_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0014_follows_0013() -> None:
    migration = _load_0014()

    assert migration.revision == _REVISION_0014
    assert migration.down_revision == _REVISION_0013


def test_0014_checks_and_generated_columns_are_the_ones_the_models_declare() -> None:
    migration = _load_0014()
    from_migration = {
        name: (table, _normalised(condition))
        for name, (table, condition) in migration._CHECKS.items()
    }

    assert from_migration == _rule_checks()
    # Rules 9 (v4 adds the retired rule's end), components 4.
    assert len(from_migration) == 13
    columns = PricingRule.__table__.c
    for name in ("scope_key", "open_slot"):
        assert columns[name].computed is not None, name
        assert columns[name].computed.persisted is True, name
    open_slot = str(columns.open_slot.computed.sqltext)
    assert _normalised(open_slot) == _normalised(migration._OPEN_SLOT)
    # `scope_key` is written per dialect: MySQL takes the migration's CONCAT literally; SQLite
    # (the unit tests) the same key with `||`.
    scope_key = columns.scope_key.computed.sqltext
    on_mysql = str(scope_key.compile(dialect=mysql.dialect()))
    on_sqlite = str(scope_key.compile(dialect=sqlite.dialect()))
    assert _normalised(on_mysql) == _normalised(migration._SCOPE_KEY)
    assert _normalised(migration._SCOPE_KEY) == _normalised(SCOPE_KEY_EXPRESSION)
    assert _normalised(on_sqlite) == _normalised(SQLITE_SCOPE_KEY_EXPRESSION)
    assert _normalised(str(scope_key)) == _normalised(SCOPE_KEY_EXPRESSION)


def test_0014_sqlite_tables_get_the_lock_row_and_a_non_null_scope_key() -> None:
    """`create_all` (the unit tests) writes the one lock row like the migration does, and the
    SQLite form of `scope_key` gives the same key as the MySQL one would."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            locks = connection.execute(select(PricingRuleLock.id)).scalars().all()
            key = connection.execute(
                text(
                    "SELECT 'GLOBAL' || ':' || COALESCE(NULL, 0) || ':' || COALESCE(7, 0)"
                    " || ':' || COALESCE(NULL, 0)"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert locks == [1]
    assert key == "GLOBAL:0:7:0"


def test_0014_triggers_are_the_eight_of_the_design_written_like_0006() -> None:
    """Each is one complete CREATE TRIGGER that refuses with SIGNAL SQLSTATE '45000' (as 0006)."""
    triggers = _load_0014()._TRIGGERS

    assert set(triggers) == set(_EXPECTED_TRIGGERS_0014)
    for name, (table, timing, event) in _EXPECTED_TRIGGERS_0014.items():
        statement = _normalised(triggers[name])
        assert statement.startswith(f"CREATE TRIGGER {name} {timing} {event} ON {table}"), name
        assert "FOR EACH ROW" in statement, name
        assert "SIGNAL SQLSTATE '45000'" in statement, name
        # MySQL triggers cannot refer to generated columns through NEW / OLD.
        for column in ("scope_key", "open_slot"):
            assert f"NEW.{column}" not in statement, name
            assert f"OLD.{column}" not in statement, name
    # No overlapping periods: lock pricing_rule_locks first (the service's lock), then check.
    update = _normalised(triggers["trg_pricing_rules_before_update"])
    locked = update.index("FROM pricing_rule_locks WHERE id = 1 FOR UPDATE")
    assert locked < update.index("pricing rule periods overlap")
    # v4: an empty start only for the first GLOBAL rule, checked under the same lock.
    assert locked < update.index("only the first global pricing rule starts with no time")
    assert "NEW.strategy = 'FIXED_RATE'" in update
    assert "NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED')" in update


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0014_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    migration = _load_0014()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0014_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL DDL is not transactional: a failed precheck must not leave a table behind."""
    migration = _load_0014()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    # The lock table, its one row, the two rule tables, then eight triggers.
    assert allowed.calls == ["create_table", "execute"] + ["create_table"] * 2 + ["execute"] * 8


def test_0014_downgrade_drops_the_three_tables_in_dependency_order(monkeypatch) -> None:
    migration = _load_0014()
    upgrade = _OfflineArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert upgrade.first_args("create_table") == _CREATE_ORDER_0014
    executed = upgrade.first_args("execute")
    assert executed[0] == "INSERT INTO pricing_rule_locks (id) VALUES (1)"
    assert len(executed) == 1 + 8

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    assert downgrade.first_args("drop_table") == _CREATE_ORDER_0014[::-1]
    assert [name for name, _ in downgrade.calls] == ["drop_table"] * 3


@needs_mysql
def test_0014_only_adds_and_drops_its_three_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION_0013)
        without = _table_names()
        assert "provider_price_versions" in without, "0013's tables must be there"
        assert not _TABLES_0014 & without

        command.upgrade(alembic_config, _REVISION_0014)
        assert _table_names() == without | _TABLES_0014

        command.downgrade(alembic_config, _REVISION_0013)
        assert _table_names() == without
        # Triggers go with their tables.
        assert not set(_EXPECTED_TRIGGERS_0014) & set(_all_triggers())
    finally:
        command.upgrade(alembic_config, "head")


@needs_mysql
def test_0014_the_lock_table_holds_exactly_one_row(alembic_config: Config) -> None:
    """§7 "migration": after upgrade the lock table holds exactly the row `id = 1`."""
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT id FROM pricing_rule_locks")).scalars().all()
    finally:
        engine.dispose()

    assert rows == [1]


@needs_mysql
def test_0014_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        columns = {table: inspector.get_columns(table) for table in _EXPECTED_0014_COLUMNS}
        for table, expected in _EXPECTED_0014_COLUMNS.items():
            assert {c["name"]: c["nullable"] for c in columns[table]} == expected, table
        types = {(table, c["name"]): c["type"] for table, found in columns.items() for c in found}

        locks, rules, components = _CREATE_ORDER_0014
        assert isinstance(types[(locks, "id")], Integer)
        assert not isinstance(types[(locks, "id")], BigInteger)
        for (table, name), column_type in types.items():
            if table == locks:
                continue
            if name == "id" or (name.endswith("_id") and name != "public_id"):
                assert isinstance(column_type, BigInteger), (table, name)
            if name.endswith("_at") or name.startswith("effective_"):
                assert isinstance(column_type, DateTime), (table, name)
        for name in ("created_by", "approved_by"):
            assert isinstance(types[(rules, name)], BigInteger), name
        assert isinstance(types[(rules, "public_id")], CHAR)
        assert types[(rules, "public_id")].length == 36
        widths = {
            (rules, "priority_scope"): 32,
            (rules, "strategy"): 16,
            (rules, "status"): 16,
            (rules, "scope_key"): 128,
            (components, "strategy"): 16,
        }
        for key, width in widths.items():
            assert isinstance(types[key], String), key
            assert types[key].length == width, key
        assert isinstance(types[(components, "currency")], CHAR)
        assert types[(components, "currency")].length == 3
        money = [(rules, "markup_multiplier"), (components, "unit_quantity")]
        for key in [*money, (components, "rate_amount")]:
            assert isinstance(types[key], Numeric), key
            assert (types[key].precision, types[key].scale) == (20, 8), key

        # Exactly two generated columns, both STORED.
        computed = {
            (table, c["name"]): c.get("computed")
            for table, found in columns.items()
            for c in found
            if c.get("computed")
        }
        assert set(computed) == {(rules, "scope_key"), (rules, "open_slot")}
        for column in computed.values():
            assert column["persisted"] is True
        assert "concat" in str(computed[(rules, "scope_key")]["sqltext"]).lower()
        assert "PUBLISHED" in str(computed[(rules, "open_slot")]["sqltext"])
        assert isinstance(types[(rules, "open_slot")], Integer)
    finally:
        engine.dispose()


@needs_mysql
def test_0014_keys_foreign_keys_checks_and_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        for table, expected in _EXPECTED_UNIQUE_0014.items():
            assert _unique_sets(table) == expected, table
            foreign_keys = {
                str(fk["name"]): (
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table)
            }
            assert foreign_keys == _EXPECTED_FOREIGN_KEYS_0014[table], table

        indexes = {i["name"]: i for i in inspector.get_indexes("pricing_rules")}
        assert indexes["ux_pricing_rules_open_slot"]["column_names"] == ["scope_key", "open_slot"]
        assert indexes["ux_pricing_rules_open_slot"]["unique"]
        assert indexes["ix_pricing_rules_resolve"]["column_names"] == [
            "priority_scope",
            "tenant_id",
            "provider_id",
            "model_id",
            "effective_from",
        ]
        assert indexes["ix_pricing_rules_model"]["column_names"] == ["model_id", "provider_id"]
        by_rule = {i["name"]: i for i in inspector.get_indexes("pricing_rule_components")}
        assert by_rule["ix_pricing_rule_components_rule"]["column_names"] == [
            "pricing_rule_id",
            "strategy",
        ]

        with engine.connect() as connection:
            referential = {
                row[0]: (row[1], row[2])
                for row in connection.execute(_RULE_REFERENTIAL_RULES_QUERY)
            }
            checks = set(connection.execute(_RULE_CHECKS_QUERY).scalars())
        # ⚠️ RESTRICT: rules and components are never deleted, nor what they refer to (INV-6);
        # a draft with components cannot change its strategy under them.
        names = {name for keys in _EXPECTED_FOREIGN_KEYS_0014.values() for name in keys}
        assert set(referential) == names
        assert {pair[0] for pair in referential.values()} == {"RESTRICT"}
        assert referential["fk_pricing_rule_components_rule"][1] == "RESTRICT"
        assert checks == set(_rule_checks())
        ours = {name: row for name, row in _all_triggers().items() if row[0] in _TABLES_0014}
        # At head the lock table also has 0016's BEFORE UPDATE.
        assert ours == _EXPECTED_TRIGGERS_0014 | _triggers_0016_on(_TABLES_0014)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0015_fx_rates（AIH-TASK-028，设计闸门 #183 v3 §11 的 F1）
#
# The first cases need no database: the revision chain; the migration's checks and `open_slot`
# equal the models'; triggers written like 0006 with the 0006 precheck before any DDL;
# downgrade drops the tables in dependency order. On a real MySQL: the shapes, keys, foreign
# keys and their rules, checks and triggers of the three tables, and that upgrade / downgrade
# add and drop only these tables. What the triggers and constraints refuse or let through is in
# test_fx_rates_db.py.
# ---------------------------------------------------------------------------

_REVISION_0015 = "0015_fx_rates"
_MIGRATION_0015 = pathlib.Path("alembic/versions/20260929_0015_fx_rates.py")
_FX_MODELS = (FxRateLock, FxRateVersion, FxFetchAttempt)
_TABLES_0015 = {model.__tablename__ for model in _FX_MODELS}
# Creation order; downgrade must be exactly the reverse (foreign keys).
_CREATE_ORDER_0015 = ["fx_rate_locks", "fx_rate_versions", "fx_fetch_attempts"]

# Trigger → (table, timing, event). The seven of design §2, no more, no fewer.
_EXPECTED_TRIGGERS_0015 = {
    "trg_fx_rate_locks_before_insert": ("fx_rate_locks", "BEFORE", "INSERT"),
    "trg_fx_rate_locks_before_delete": ("fx_rate_locks", "BEFORE", "DELETE"),
    "trg_fx_rate_versions_before_insert": ("fx_rate_versions", "BEFORE", "INSERT"),
    "trg_fx_rate_versions_before_update": ("fx_rate_versions", "BEFORE", "UPDATE"),
    "trg_fx_rate_versions_before_delete": ("fx_rate_versions", "BEFORE", "DELETE"),
    "trg_fx_fetch_attempts_before_update": ("fx_fetch_attempts", "BEFORE", "UPDATE"),
    "trg_fx_fetch_attempts_before_delete": ("fx_fetch_attempts", "BEFORE", "DELETE"),
}

_EXPECTED_0015_COLUMNS = {
    "fx_rate_locks": {"id": False},
    "fx_rate_versions": {
        "id": False,
        "public_id": False,
        "base_currency": False,
        "quote_currency": False,
        "rate": False,
        "source": False,
        "source_reference": False,
        # A manual version may have no quote date (CHECK 5 requires it for BNM).
        "source_quote_date": True,
        "observed_at": False,
        "status": False,
        "effective_from": True,
        "effective_to": True,
        "open_slot": True,
        # A BNM draft has no creator (the system).
        "created_by": True,
        "approved_by": True,
        "approved_at": True,
        "created_at": False,
        "updated_at": False,
    },
    "fx_fetch_attempts": {
        "id": False,
        "base_currency": False,
        "source": False,
        "requested_date": False,
        "outcome": False,
        "quote_date": True,
        "error_code": True,
        "fx_rate_version_id": True,
        "attempted_at": False,
    },
}

_EXPECTED_UNIQUE_0015 = {
    "fx_rate_locks": set(),
    "fx_rate_versions": {
        ("public_id",),
        ("base_currency", "quote_currency", "open_slot"),
        ("base_currency", "quote_currency", "source", "source_quote_date"),
    },
    "fx_fetch_attempts": set(),
}

_EXPECTED_FOREIGN_KEYS_0015 = {
    "fx_rate_locks": {},
    "fx_rate_versions": {
        "fk_fx_rate_versions_created_by": (["created_by"], "users", ["id"]),
        "fk_fx_rate_versions_approved_by": (["approved_by"], "users", ["id"]),
    },
    "fx_fetch_attempts": {
        "fk_fx_fetch_attempts_version": (["fx_rate_version_id"], "fx_rate_versions", ["id"]),
    },
}

_FX_REFERENTIAL_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE"
    " FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND TABLE_NAME IN ('fx_rate_locks', 'fx_rate_versions', 'fx_fetch_attempts')"
)
_FX_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME IN ('fx_rate_locks', 'fx_rate_versions', 'fx_fetch_attempts')"
)


def _load_0015() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0015", _MIGRATION_0015)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fx_checks() -> dict[str, tuple[str, str]]:
    return {
        str(constraint.name): (model.__tablename__, _normalised(str(constraint.sqltext)))
        for model in _FX_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0015_follows_0014() -> None:
    migration = _load_0015()

    assert migration.revision == _REVISION_0015
    assert migration.down_revision == _REVISION_0014


def test_0015_checks_and_open_slot_are_the_ones_the_models_declare() -> None:
    migration = _load_0015()
    from_migration = {
        name: (table, _normalised(condition))
        for name, (table, condition) in migration._CHECKS.items()
    }

    assert from_migration == _fx_checks()
    # Versions 10 (currencies two, rate, source, status, then design §2 CHECK 1–5), attempts 3.
    assert len(from_migration) == 13
    computed = FxRateVersion.__table__.c.open_slot.computed
    assert computed is not None
    assert computed.persisted is True
    assert _normalised(str(computed.sqltext)) == _normalised(migration._OPEN_SLOT)
    # The design writes the expression out (v2): 1 for an untruncated published version and
    # NULL — not 0 — for every other row, the same as 0013's.
    expected = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"
    assert _normalised(migration._OPEN_SLOT) == expected
    assert migration._OPEN_SLOT == _load_0013()._OPEN_SLOT
    # No format CHECK on the currency (v3): MySQL's REGEXP ignores case, SQLite has none.
    assert not any("REGEXP" in condition for _, condition in from_migration.values())


def test_0015_sqlite_tables_get_the_lock_row_and_the_open_slot() -> None:
    """`create_all` (the unit tests) writes the one lock row like the migration does, and
    SQLite computes `open_slot` as 1 only for an untruncated published version. SQLite does not
    enforce foreign keys here, so the approver is a placeholder id."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            locks = connection.execute(select(FxRateLock.id)).scalars().all()
            for number, (status, end, approved) in enumerate(
                [
                    ("DRAFT", None, False),
                    ("PUBLISHED", None, True),
                    ("PUBLISHED", _NOW, True),
                    ("RETIRED", _NOW, True),
                    ("DISCARDED", None, False),
                ]
            ):
                connection.execute(
                    insert(FxRateVersion).values(
                        public_id=f"00000000-0000-0000-0000-{number:012d}",
                        base_currency="USD",
                        quote_currency="MYR",
                        rate=Decimal("1.1111111111"),
                        source="MANUAL",
                        source_reference="fictional test rate",
                        observed_at=_NOW,
                        status=status,
                        effective_from=_NOW - dt.timedelta(days=1) if approved else None,
                        effective_to=end,
                        approved_by=1 if approved else None,
                        approved_at=_NOW if approved else None,
                        created_at=_NOW,
                        updated_at=_NOW,
                    )
                )
            slots = connection.execute(
                select(FxRateVersion.status, FxRateVersion.open_slot).order_by(FxRateVersion.id)
            ).all()
    finally:
        engine.dispose()

    assert locks == [1]
    assert [slot for _, slot in slots] == [None, 1, None, None, None]


def test_0015_triggers_are_the_seven_of_the_design_written_like_0006() -> None:
    """Each is one complete CREATE TRIGGER that refuses with SIGNAL SQLSTATE '45000' (as 0006)."""
    triggers = _load_0015()._TRIGGERS

    assert set(triggers) == set(_EXPECTED_TRIGGERS_0015)
    for name, (table, timing, event) in _EXPECTED_TRIGGERS_0015.items():
        statement = _normalised(triggers[name])
        assert statement.startswith(f"CREATE TRIGGER {name} {timing} {event} ON {table}"), name
        assert "FOR EACH ROW" in statement, name
        assert "SIGNAL SQLSTATE '45000'" in statement, name
        # MySQL triggers cannot refer to generated columns through NEW / OLD.
        assert "NEW.open_slot" not in statement, name
        assert "OLD.open_slot" not in statement, name
    update = _normalised(triggers["trg_fx_rate_versions_before_update"])
    # No overlapping periods: lock fx_rate_locks first (the service's lock), then check.
    locked = update.index("FROM fx_rate_locks WHERE id = 1 FOR UPDATE")
    assert locked < update.index("fx rate periods overlap")
    # v2: retired and discarded rows are final; no DRAFT → RETIRED; no end on the publish
    # transition.
    assert "OLD.status IN ('RETIRED', 'DISCARDED')" in update
    assert "NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED')" in update
    assert "NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL" in update


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0015_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    migration = _load_0015()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0015_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL DDL is not transactional: a failed precheck must not leave a table behind."""
    migration = _load_0015()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    # The lock table, its one row, the two other tables, then seven triggers.
    assert allowed.calls == ["create_table", "execute"] + ["create_table"] * 2 + ["execute"] * 7


def test_0015_downgrade_drops_the_three_tables_in_dependency_order(monkeypatch) -> None:
    migration = _load_0015()
    upgrade = _OfflineArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert upgrade.first_args("create_table") == _CREATE_ORDER_0015
    executed = upgrade.first_args("execute")
    assert executed[0] == "INSERT INTO fx_rate_locks (id) VALUES (1)"
    assert len(executed) == 1 + 7
    # No rates are seeded (design §8: an administrator publishes the first version).
    assert not upgrade.first_args("bulk_insert")

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    assert downgrade.first_args("drop_table") == _CREATE_ORDER_0015[::-1]
    assert [name for name, _ in downgrade.calls] == ["drop_table"] * 3


@needs_mysql
def test_0015_only_adds_and_drops_its_three_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION_0014)
        without = _table_names()
        assert "pricing_rules" in without, "0014's tables must be there"
        assert not _TABLES_0015 & without

        command.upgrade(alembic_config, _REVISION_0015)
        assert _table_names() == without | _TABLES_0015

        command.downgrade(alembic_config, _REVISION_0014)
        assert _table_names() == without
        # Triggers go with their tables.
        assert not set(_EXPECTED_TRIGGERS_0015) & set(_all_triggers())
    finally:
        command.upgrade(alembic_config, "head")


@needs_mysql
def test_0015_the_lock_table_holds_exactly_one_row(alembic_config: Config) -> None:
    """§7 "migration": after upgrade the lock table holds exactly the row `id = 1`."""
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT id FROM fx_rate_locks")).scalars().all()
    finally:
        engine.dispose()

    assert rows == [1]


@needs_mysql
def test_0015_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        columns = {table: inspector.get_columns(table) for table in _EXPECTED_0015_COLUMNS}
        for table, expected in _EXPECTED_0015_COLUMNS.items():
            assert {c["name"]: c["nullable"] for c in columns[table]} == expected, table
        types = {(table, c["name"]): c["type"] for table, found in columns.items() for c in found}

        locks, versions, attempts = _CREATE_ORDER_0015
        assert isinstance(types[(locks, "id")], Integer)
        assert not isinstance(types[(locks, "id")], BigInteger)
        for key in [(versions, "id"), (versions, "created_by"), (versions, "approved_by")]:
            assert isinstance(types[key], BigInteger), key
        for key in [(attempts, "id"), (attempts, "fx_rate_version_id")]:
            assert isinstance(types[key], BigInteger), key
        for key in [
            (versions, "observed_at"),
            (versions, "effective_from"),
            (versions, "effective_to"),
            (versions, "approved_at"),
            (versions, "created_at"),
            (versions, "updated_at"),
            (attempts, "attempted_at"),
        ]:
            assert isinstance(types[key], DateTime), key
        for key in [
            (versions, "source_quote_date"),
            (attempts, "requested_date"),
            (attempts, "quote_date"),
        ]:
            assert isinstance(types[key], Date), key
            assert not isinstance(types[key], DateTime), key
        chars = {
            (versions, "public_id"): 36,
            (versions, "base_currency"): 3,
            (versions, "quote_currency"): 3,
            (attempts, "base_currency"): 3,
        }
        for key, width in chars.items():
            assert isinstance(types[key], CHAR), key
            assert types[key].length == width, key
        widths = {
            (versions, "source"): 16,
            (versions, "source_reference"): 255,
            (versions, "status"): 16,
            (attempts, "source"): 16,
            # v3: `NO_QUOTE_FOR_DATE` is 17 characters; VARCHAR(16) would not hold it.
            (attempts, "outcome"): 32,
            (attempts, "error_code"): 64,
        }
        for key, width in widths.items():
            assert isinstance(types[key], String), key
            assert types[key].length == width, key
        rate = types[(versions, "rate")]
        assert isinstance(rate, Numeric)
        assert (rate.precision, rate.scale) == (24, 10)

        # Exactly one generated column, STORED, an integer.
        computed = {
            (table, c["name"]): c.get("computed")
            for table, found in columns.items()
            for c in found
            if c.get("computed")
        }
        assert set(computed) == {(versions, "open_slot")}
        assert computed[(versions, "open_slot")]["persisted"] is True
        assert "PUBLISHED" in str(computed[(versions, "open_slot")]["sqltext"])
        assert isinstance(types[(versions, "open_slot")], Integer)
        assert not isinstance(types[(versions, "open_slot")], BigInteger)
    finally:
        engine.dispose()


@needs_mysql
def test_0015_keys_foreign_keys_checks_and_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        for table, expected in _EXPECTED_UNIQUE_0015.items():
            assert _unique_sets(table) == expected, table
            foreign_keys = {
                str(fk["name"]): (
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table)
            }
            assert foreign_keys == _EXPECTED_FOREIGN_KEYS_0015[table], table

        indexes = {i["name"]: i for i in inspector.get_indexes("fx_rate_versions")}
        open_slot = indexes["ux_fx_rate_versions_open_slot"]
        assert open_slot["column_names"] == ["base_currency", "quote_currency", "open_slot"]
        assert open_slot["unique"]
        by_time = {i["name"]: i for i in inspector.get_indexes("fx_fetch_attempts")}
        assert by_time["ix_fx_fetch_attempts_currency_time"]["column_names"] == [
            "base_currency",
            "attempted_at",
        ]

        with engine.connect() as connection:
            delete_rules = dict(connection.execute(_FX_REFERENTIAL_RULES_QUERY).all())
            checks = set(connection.execute(_FX_CHECKS_QUERY).scalars())
        # ⚠️ RESTRICT: versions and attempts are never deleted, nor the users they name (INV-6).
        names = {name for keys in _EXPECTED_FOREIGN_KEYS_0015.values() for name in keys}
        assert delete_rules == dict.fromkeys(names, "RESTRICT")
        assert checks == set(_fx_checks())
        ours = {name: row for name, row in _all_triggers().items() if row[0] in _TABLES_0015}
        # At head the lock table also has 0016's BEFORE UPDATE.
        assert ours == _EXPECTED_TRIGGERS_0015 | _triggers_0016_on(_TABLES_0015)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0016_lock_tables_no_update（AIH-TASK-045，设计闸门 #200 v1）
#
# The first cases need no database: the revision chain; the two triggers (table, timing,
# event) written like the lock tables' BEFORE DELETE with the same message; the 0006 precheck
# before any DDL and skipped in `--sql` mode; downgrade drops exactly these two. On a real
# MySQL: the two triggers at head, and that downgrade to 0015 drops only them (tables, lock
# rows and every other trigger unchanged). What they refuse or let through is in
# test_lock_tables_db.py.
# ---------------------------------------------------------------------------

_REVISION_0016 = "0016_lock_tables_no_update"
_MIGRATION_0016 = pathlib.Path("alembic/versions/20260930_0016_lock_tables_no_update.py")

# Trigger → (table, timing, event). The two of design §2, no more, no fewer.
_EXPECTED_TRIGGERS_0016 = {
    "trg_pricing_rule_locks_before_update": ("pricing_rule_locks", "BEFORE", "UPDATE"),
    "trg_fx_rate_locks_before_update": ("fx_rate_locks", "BEFORE", "UPDATE"),
}


def _triggers_0016_on(tables: set[str]) -> dict[str, tuple[str, str, str]]:
    return {name: row for name, row in _EXPECTED_TRIGGERS_0016.items() if row[0] in tables}


def _load_0016() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0016", _MIGRATION_0016)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0016_follows_0015() -> None:
    migration = _load_0016()

    assert migration.revision == _REVISION_0016
    assert migration.down_revision == _REVISION_0015


def test_0016_triggers_are_the_two_of_the_design_written_like_the_lock_delete_ones() -> None:
    """Unconditional refusal, written like the same table's BEFORE DELETE (only the event
    differs), with the message of the same table's INSERT / DELETE triggers."""
    triggers = _load_0016()._TRIGGERS
    earlier = {**_load_0014()._TRIGGERS, **_load_0015()._TRIGGERS}

    assert set(triggers) == set(_EXPECTED_TRIGGERS_0016)
    for name, (table, timing, event) in _EXPECTED_TRIGGERS_0016.items():
        statement = _normalised(triggers[name])
        assert statement.startswith(f"CREATE TRIGGER {name} {timing} {event} ON {table}"), name
        assert "FOR EACH ROW" in statement, name
        message = f"'{table} holds exactly one row'"
        assert statement.endswith(f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = {message}"), name
        # No condition: not even a `SET id = 1` that changes nothing passes.
        assert "IF" not in statement.split(), name
        delete = _normalised(earlier[f"trg_{table}_before_delete"])
        as_update = delete.replace("before_delete", "before_update").replace("DELETE", "UPDATE")
        assert statement == as_update, name
        assert message in _normalised(earlier[f"trg_{table}_before_insert"]), name


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0016_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    migration = _load_0016()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0016_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL DDL is not transactional: a failed precheck must not leave a trigger behind."""
    migration = _load_0016()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    # The other way round: with the switch on, the same upgrade does create the two triggers.
    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    assert allowed.calls == ["execute", "execute"]


def test_0016_offline_mode_skips_the_precheck_and_downgrade_drops_only_the_two(
    monkeypatch,
) -> None:
    migration = _load_0016()
    upgrade = _OfflineArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    # `--sql`: no bind asked for, only the two CREATE TRIGGER statements, in design §2 order.
    assert [name for name, _ in upgrade.calls] == ["execute", "execute"]
    assert upgrade.first_args("execute") == list(migration._TRIGGERS.values())

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    assert downgrade.calls == [
        ("execute", ("DROP TRIGGER trg_fx_rate_locks_before_update",)),
        ("execute", ("DROP TRIGGER trg_pricing_rule_locks_before_update",)),
    ]


def _lock_rows() -> dict[str, list[int]]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            return {
                table: list(connection.execute(text(f"SELECT id FROM {table}")).scalars())
                for table in ("pricing_rule_locks", "fx_rate_locks")
            }
    finally:
        engine.dispose()


@needs_mysql
def test_0016_triggers_exist_at_head(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")

    triggers = _all_triggers()

    found = {name: triggers.get(name) for name in _EXPECTED_TRIGGERS_0016}
    assert found == _EXPECTED_TRIGGERS_0016


@needs_mysql
def test_0016_downgrade_drops_only_the_two_triggers(alembic_config: Config) -> None:
    # Measured at 0016 itself: from head the downgrade would also drop 0017's tables.
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION_0016)
    tables = _table_names()
    triggers = _all_triggers()
    assert _EXPECTED_TRIGGERS_0016.items() <= triggers.items()
    assert _lock_rows() == {"pricing_rule_locks": [1], "fx_rate_locks": [1]}

    try:
        command.downgrade(alembic_config, _REVISION_0015)
        without = {n: r for n, r in triggers.items() if n not in _EXPECTED_TRIGGERS_0016}
        assert _all_triggers() == without
        # Only the triggers: tables and the lock rows stay.
        assert _table_names() == tables
        assert _lock_rows() == {"pricing_rule_locks": [1], "fx_rate_locks": [1]}

        command.upgrade(alembic_config, _REVISION_0016)
        assert _all_triggers() == triggers
        assert _table_names() == tables
        assert _lock_rows() == {"pricing_rule_locks": [1], "fx_rate_locks": [1]}
    finally:
        command.upgrade(alembic_config, "head")


# ---------------------------------------------------------------------------
# 0017_usage_events（AIH-TASK-029，设计闸门 #176 v8）
#
# The first cases need no database: the revision chain; the migration's checks and the two
# parent unique constraints equal the models'; the two triggers written like 0010's; the 0006
# precheck before any DDL; downgrade drops the tables, then the two unique constraints. On a
# real MySQL: the shapes (NOT NULL column by column), keys, foreign keys and their rules, checks,
# indexes and triggers, and that upgrade / downgrade add and drop only these. What the
# constraints and triggers refuse or let through is in test_usage_ingest_service.py.
# ---------------------------------------------------------------------------

_REVISION_0017 = "0017_usage_events"
_MIGRATION_0017 = pathlib.Path("alembic/versions/20260929_0017_usage_events.py")
_USAGE_MODELS = (UsageEvent, UsageEventConflict)
_TABLES_0017 = {model.__tablename__ for model in _USAGE_MODELS}
# Creation order; downgrade must be exactly the reverse (foreign keys).
_CREATE_ORDER_0017 = ["usage_events", "usage_event_conflicts"]

# Trigger → (table, timing, event). The two of design §2, no more, no fewer.
_EXPECTED_TRIGGERS_0017 = {
    "trg_usage_event_conflicts_before_update": ("usage_event_conflicts", "BEFORE", "UPDATE"),
    "trg_usage_event_conflicts_before_delete": ("usage_event_conflicts", "BEFORE", "DELETE"),
}

# Column → nullable. Design §2 lists the nullable ones; every other column is NOT NULL.
_EXPECTED_0017_COLUMNS = {
    "usage_events": {
        "id": False,
        "public_id": False,
        "event_id": False,
        "schema_version": False,
        "tenant_id": False,
        "project_id": False,
        "integration_credential_id": False,
        "request_id": False,
        "conversation_id": True,
        "provider_code_raw": False,
        "model_code_raw": False,
        "provider_id": True,
        "model_id": True,
        "usage_meter_type_id": False,
        "payload_shape": False,
        "quantity_kind": False,
        "input_tokens": True,
        "output_tokens": True,
        "cache_creation_input_tokens": True,
        "cache_read_input_tokens": True,
        "quantity": True,
        "unit": False,
        "payload_fingerprint": False,
        "status": False,
        "error_code": True,
        "error_message": True,
        "occurred_at": False,
        "received_at": False,
        "processed_at": True,
        "created_at": False,
    },
    "usage_event_conflicts": {
        "id": False,
        "usage_event_id": False,
        "event_id": False,
        "integration_credential_id": False,
        "api_key": False,
        "payload_fingerprint": False,
        "mismatch": False,
        "received_at": False,
    },
}

# (table, column) → width. Compared byte for byte (utf8mb4_0900_bin).
_BINARY_COLUMNS_0017 = {
    ("usage_events", "event_id"): 64,
    ("usage_events", "request_id"): 128,
    ("usage_events", "conversation_id"): 128,
    ("usage_events", "provider_code_raw"): 64,
    ("usage_events", "model_code_raw"): 128,
    ("usage_events", "unit"): 16,
    ("usage_event_conflicts", "event_id"): 64,
    ("usage_event_conflicts", "api_key"): 64,
}

_EXPECTED_UNIQUE_0017 = {
    "usage_events": {("public_id",), ("event_id",)},
    "usage_event_conflicts": {("event_id", "api_key", "payload_fingerprint")},
}

_EXPECTED_FOREIGN_KEYS_0017 = {
    "usage_events": {
        "fk_usage_events_project": (
            ["project_id", "tenant_id"],
            "projects",
            ["id", "tenant_id"],
        ),
        "fk_usage_events_credential": (
            ["integration_credential_id", "project_id", "tenant_id"],
            "integration_credentials",
            ["id", "project_id", "tenant_id"],
        ),
        "fk_usage_events_meter_type": (
            ["usage_meter_type_id", "payload_shape", "unit", "quantity_kind"],
            "usage_meter_types",
            ["id", "payload_shape", "unit", "quantity_kind"],
        ),
        "fk_usage_events_model": (["model_id", "provider_id"], "ai_models", ["id", "provider_id"]),
        "fk_usage_events_provider": (["provider_id"], "ai_providers", ["id"]),
    },
    "usage_event_conflicts": {
        "fk_usage_event_conflicts_event": (["usage_event_id"], "usage_events", ["id"]),
        "fk_usage_event_conflicts_credential": (
            ["integration_credential_id"],
            "integration_credentials",
            ["id"],
        ),
    },
}

# Index → columns, on `usage_events` (design §2 plus the two composite foreign keys').
_EXPECTED_INDEXES_0017 = {
    "ix_usage_events_status_occurred": ["status", "occurred_at"],
    "ix_usage_events_tenant_id": ["tenant_id"],
    "ix_usage_events_project_id": ["project_id", "tenant_id"],
    "ix_usage_events_model_id": ["model_id", "provider_id"],
    "ix_usage_events_provider_id": ["provider_id"],
    "ix_usage_events_conversation_id": ["conversation_id"],
    "ix_usage_events_request_id": ["request_id"],
    "ix_usage_events_occurred_at": ["occurred_at"],
    "ix_usage_events_credential": ["integration_credential_id", "project_id", "tenant_id"],
    "ix_usage_events_meter_type": [
        "usage_meter_type_id",
        "payload_shape",
        "unit",
        "quantity_kind",
    ],
}

# (table, column) of every BIGINT: each id and reference, and the four token counts.
_BIGINT_COLUMNS_0017 = [
    ("usage_events", "id"),
    ("usage_events", "tenant_id"),
    ("usage_events", "project_id"),
    ("usage_events", "integration_credential_id"),
    ("usage_events", "provider_id"),
    ("usage_events", "model_id"),
    ("usage_events", "usage_meter_type_id"),
    ("usage_events", "input_tokens"),
    ("usage_events", "output_tokens"),
    ("usage_events", "cache_creation_input_tokens"),
    ("usage_events", "cache_read_input_tokens"),
    ("usage_event_conflicts", "id"),
    ("usage_event_conflicts", "usage_event_id"),
    ("usage_event_conflicts", "integration_credential_id"),
]

_USAGE_REFERENTIAL_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE"
    " FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE()"
    " AND TABLE_NAME IN ('usage_events', 'usage_event_conflicts')"
)
_USAGE_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
    " AND TABLE_NAME IN ('usage_events', 'usage_event_conflicts')"
)

# The two parent unique constraints 0017 adds: name → (table, columns).
_PARENT_UNIQUES_0017 = {
    "uq_integration_credentials_id_scope": (
        "integration_credentials",
        ("id", "project_id", "tenant_id"),
    ),
    "uq_usage_meter_types_id_shape_unit_kind": (
        "usage_meter_types",
        ("id", "payload_shape", "unit", "quantity_kind"),
    ),
}


def _load_0017() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0017", _MIGRATION_0017)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _usage_checks() -> dict[str, tuple[str, str]]:
    return {
        str(constraint.name): (model.__tablename__, _normalised(str(constraint.sqltext)))
        for model in _USAGE_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_0017_follows_0016() -> None:
    migration = _load_0017()

    assert migration.revision == _REVISION_0017
    assert migration.down_revision == _REVISION_0016


def test_0017_checks_are_the_ones_the_models_declare() -> None:
    migration = _load_0017()
    from_migration = {
        name: (table, _normalised(condition))
        for name, (table, condition) in migration._CHECKS.items()
    }
    # 0018 加在 usage_events 上的七条由 test_0018_checks_are_the_ones_the_model_declares 比对。
    later = set(_load_0018()._CHECKS)
    declared = {name: check for name, check in _usage_checks().items() if name not in later}

    assert from_migration == declared
    # Events 9 (shape, integer quantity, four tokens, quantity, status, catalog), conflicts 1.
    assert len(from_migration) == 10
    # Explicit groups, not a chained `=` (design v2); the integer CHECK uses FLOOR (v3).
    shape = from_migration["ck_usage_events_shape_fields"][1]
    assert "=" not in shape.replace("payload_shape = ", "")
    integer = from_migration["ck_usage_events_integer_quantity"][1]
    assert "quantity = FLOOR(quantity)" in integer
    assert migration._TEXT_COLLATION == TEXT_COLLATION == CODE_COLLATION


def test_0017_parent_unique_constraints_are_the_ones_the_models_declare() -> None:
    """Only new indexes on the two parent tables, declared on the models too (design §2)."""
    migration = _load_0017()
    declared = {
        str(constraint.name): (model.__tablename__, tuple(constraint.columns.keys()))
        for model in (IntegrationCredential, UsageMeterType)
        for constraint in model.__table__.constraints
        if str(constraint.name) in _PARENT_UNIQUES_0017
    }
    from_migration = {
        name: (table, tuple(columns))
        for name, (table, columns) in migration._PARENT_UNIQUES.items()
    }

    assert from_migration == _PARENT_UNIQUES_0017
    assert declared == _PARENT_UNIQUES_0017


def test_0017_triggers_are_the_two_of_the_design_written_like_0010() -> None:
    """Unconditional refusals, written like `audit_logs`' (0010) with the table's own message."""
    triggers = _load_0017()._TRIGGERS
    audit = _load_0010()._TRIGGERS

    assert set(triggers) == set(_EXPECTED_TRIGGERS_0017)
    for name, (table, timing, event) in _EXPECTED_TRIGGERS_0017.items():
        statement = _normalised(triggers[name])
        assert statement.startswith(f"CREATE TRIGGER {name} {timing} {event} ON {table}"), name
        assert "FOR EACH ROW" in statement, name
        assert "IF" not in statement.split(), name
        like_audit = _normalised(audit[name.replace(table, "audit_logs")])
        assert statement == like_audit.replace("audit_logs", table), name


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0017_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    migration = _load_0017()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0017_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL DDL is not transactional: a failed precheck must not leave an index behind."""
    migration = _load_0017()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    # Two parent unique constraints, the two tables, then the two triggers.
    expected = ["create_unique_constraint"] * 2 + ["create_table"] * 2 + ["execute"] * 2
    assert allowed.calls == expected


def test_0017_downgrade_drops_the_tables_then_the_two_unique_constraints(monkeypatch) -> None:
    migration = _load_0017()
    upgrade = _OfflineArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert upgrade.first_args("create_unique_constraint") == list(_PARENT_UNIQUES_0017)
    assert upgrade.first_args("create_table") == _CREATE_ORDER_0017
    assert upgrade.first_args("execute") == list(migration._TRIGGERS.values())

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    # The foreign keys go with the tables; only then MySQL lets the referenced indexes go.
    assert [name for name, _ in downgrade.calls] == ["drop_table"] * 2 + ["drop_constraint"] * 2
    assert downgrade.first_args("drop_table") == _CREATE_ORDER_0017[::-1]
    assert downgrade.first_args("drop_constraint") == list(_PARENT_UNIQUES_0017)[::-1]


@needs_mysql
def test_0017_only_adds_and_drops_its_two_tables_and_two_unique_constraints(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION_0016)
        without = _table_names()
        assert "fx_rate_versions" in without, "0016's tables must be there"
        assert not _TABLES_0017 & without
        parents = {table: _unique_sets(table) for table, _ in _PARENT_UNIQUES_0017.values()}
        for table, columns in _PARENT_UNIQUES_0017.values():
            assert columns not in parents[table], table

        command.upgrade(alembic_config, _REVISION_0017)
        assert _table_names() == without | _TABLES_0017
        for table, columns in _PARENT_UNIQUES_0017.values():
            assert _unique_sets(table) == parents[table] | {columns}, table

        command.downgrade(alembic_config, _REVISION_0016)
        assert _table_names() == without
        assert {table: _unique_sets(table) for table in parents} == parents
        # Triggers go with their table.
        assert not set(_EXPECTED_TRIGGERS_0017) & set(_all_triggers())
    finally:
        command.upgrade(alembic_config, "head")


@needs_mysql
def test_0017_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        columns = {table: inspector.get_columns(table) for table in _EXPECTED_0017_COLUMNS}
        # NOT NULL column by column (design v6): composite keys and CHECKs hold only then.
        # At head `usage_events` also has 0018's columns (test_0018_column_shape).
        for table, expected in _EXPECTED_0017_COLUMNS.items():
            at_head = expected | _EXPECTED_0018_COLUMNS if table == "usage_events" else expected
            assert {c["name"]: c["nullable"] for c in columns[table]} == at_head, table
        types = {(table, c["name"]): c["type"] for table, found in columns.items() for c in found}

        events, conflicts = _CREATE_ORDER_0017
        for key in _BIGINT_COLUMNS_0017:
            assert isinstance(types[key], BigInteger), key
        for key, width in _BINARY_COLUMNS_0017.items():
            assert isinstance(types[key], String), key
            assert types[key].length == width, key
            assert types[key].collation == TEXT_COLLATION, key
        # The meter type's shape and kind: same type and collation as in `usage_meter_types`.
        for name, width in (("payload_shape", 32), ("quantity_kind", 16)):
            assert isinstance(types[(events, name)], String), name
            assert types[(events, name)].length == width, name
            assert getattr(types[(events, name)], "collation", None) != TEXT_COLLATION, name
        chars = {
            (events, "public_id"): 36,
            (events, "payload_fingerprint"): 64,
            (conflicts, "payload_fingerprint"): 64,
        }
        for key, width in chars.items():
            assert isinstance(types[key], CHAR), key
            assert types[key].length == width, key
        widths = {
            (events, "schema_version"): 8,
            (events, "status"): 32,
            (events, "error_code"): 64,
            (events, "error_message"): 255,
            (conflicts, "mismatch"): 32,
        }
        for key, width in widths.items():
            assert isinstance(types[key], String), key
            assert types[key].length == width, key
        quantity = types[(events, "quantity")]
        assert isinstance(quantity, Numeric)
        assert (quantity.precision, quantity.scale) == (20, 8)
        # Microseconds kept on `occurred_at` only; the server times are whole seconds.
        assert isinstance(types[(events, "occurred_at")], mysql.DATETIME)
        assert types[(events, "occurred_at")].fsp == 6
        for key in [(events, "received_at"), (events, "processed_at"), (events, "created_at")]:
            assert isinstance(types[key], DateTime), key
            assert not getattr(types[key], "fsp", None), key
        [status] = [c for c in columns[events] if c["name"] == "status"]
        assert "RECEIVED" in str(status["default"])
    finally:
        engine.dispose()


@needs_mysql
def test_0017_keys_foreign_keys_indexes_checks_and_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        # At head `usage_events` also has 0018's unique key and foreign keys.
        for table, expected in _EXPECTED_UNIQUE_0017.items():
            later_unique = _EXPECTED_UNIQUE_0018 if table == "usage_events" else set()
            assert _unique_sets(table) == expected | later_unique, table
            foreign_keys = {
                str(fk["name"]): (
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table)
            }
            later_keys = _EXPECTED_FOREIGN_KEYS_0018 if table == "usage_events" else {}
            assert foreign_keys == _EXPECTED_FOREIGN_KEYS_0017[table] | later_keys, table

        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("usage_events")}
        for name, columns in _EXPECTED_INDEXES_0017.items():
            assert indexes.get(name) == columns, name

        with engine.connect() as connection:
            delete_rules = dict(connection.execute(_USAGE_REFERENTIAL_RULES_QUERY).all())
            checks = set(connection.execute(_USAGE_CHECKS_QUERY).scalars())
        # ⚠️ RESTRICT: events and conflicts are never deleted, nor what they point at.
        names = {name for keys in _EXPECTED_FOREIGN_KEYS_0017.values() for name in keys}
        names |= set(_EXPECTED_FOREIGN_KEYS_0018)
        assert delete_rules == dict.fromkeys(names, "RESTRICT")
        # The models declare 0017's and 0018's checks; both are at head.
        assert checks == set(_usage_checks())
        ours = {name: row for name, row in _all_triggers().items() if row[0] in _TABLES_0017}
        assert ours == _EXPECTED_TRIGGERS_0017 | _EXPECTED_TRIGGERS_0018
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0018_usage_billing（AIH-TASK-032，设计闸门 #181 v2）
#
# The first cases need no database: the revision chain; the migration's columns and seven checks
# equal the model's, written column by column (no row constructor, no boolean equality); the three
# triggers written like 0006's; the 0006 precheck before any DDL; downgrade undoes upgrade in
# reverse. On a real MySQL: upgrade / downgrade add and drop only these columns and triggers; the
# shapes, keys, foreign keys and their rules, indexes, checks and triggers at head. What the
# checks and triggers refuse or let through is in test_usage_billing_concurrency.py.
# ---------------------------------------------------------------------------

_REVISION_0018 = "0018_usage_billing"
_MIGRATION_0018 = pathlib.Path("alembic/versions/20260929_0018_usage_billing.py")

# Column → nullable (design §2): the snapshot and claim columns are nullable, the count is not.
_EXPECTED_0018_COLUMNS = {
    "provider_price_version_id": True,
    "pricing_rule_id": True,
    "fx_rate_version_id": True,
    "provider_source_currency": True,
    "provider_source_cost": True,
    "fx_rate_applied": True,
    "estimated_provider_cost_myr": True,
    "billable_cost": True,
    "wallet_transaction_id": True,
    "attempt_count": False,
    "next_attempt_at": True,
    "claim_token": True,
    "claimed_at": True,
    "lease_expires_at": True,
}
_EXPECTED_UNIQUE_0018 = {("wallet_transaction_id",)}
_EXPECTED_FOREIGN_KEYS_0018 = {
    "fk_usage_events_price_version": (
        ["provider_price_version_id"],
        "provider_price_versions",
        ["id"],
    ),
    "fk_usage_events_pricing_rule": (["pricing_rule_id"], "pricing_rules", ["id"]),
    "fk_usage_events_fx_rate_version": (["fx_rate_version_id"], "fx_rate_versions", ["id"]),
    "fk_usage_events_wallet_transaction": (
        ["wallet_transaction_id"],
        "wallet_transactions",
        ["id"],
    ),
}
_EXPECTED_INDEXES_0018 = {
    "ix_usage_events_claim": ["status", "next_attempt_at", "id"],
    "ix_usage_events_lease": ["status", "lease_expires_at"],
    "ix_usage_events_price_version_id": ["provider_price_version_id"],
    "ix_usage_events_pricing_rule_id": ["pricing_rule_id"],
    "ix_usage_events_fx_rate_version_id": ["fx_rate_version_id"],
}
_EXPECTED_CHECKS_0018 = {
    "ck_usage_events_processed_snapshot",
    "ck_usage_events_processed_ledger",
    "ck_usage_events_unprocessed_ledger",
    "ck_usage_events_billable_cost",
    "ck_usage_events_provider_source_cost",
    "ck_usage_events_estimated_cost",
    "ck_usage_events_claim_fields",
}
# Trigger → (table, timing, event). The three of design §2, in creation order.
_EXPECTED_TRIGGERS_0018 = {
    "trg_usage_events_processed_immutable": ("usage_events", "BEFORE", "UPDATE"),
    "trg_usage_events_ledger_link": ("usage_events", "BEFORE", "UPDATE"),
    "trg_usage_events_requeue_unbilled": ("usage_events", "BEFORE", "UPDATE"),
}


def _load_0018() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0018", _MIGRATION_0018)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0018_follows_0017() -> None:
    migration = _load_0018()

    assert migration.revision == _REVISION_0018
    assert migration.down_revision == _REVISION_0017


def test_0018_checks_are_the_ones_the_model_declares() -> None:
    migration = _load_0018()
    from_migration = {name: _normalised(condition) for name, condition in migration._CHECKS.items()}
    declared = {
        name: condition
        for name, (table, condition) in _usage_checks().items()
        if name in _EXPECTED_CHECKS_0018 and table == "usage_events"
    }

    assert set(from_migration) == _EXPECTED_CHECKS_0018
    assert from_migration == declared
    for name, condition in from_migration.items():
        # Column by column: no row constructor, no `(a) = (b)` between two conditions (design §2).
        assert "(provider_id, model_id" not in condition, name
        assert ") = (" not in condition, name
    claim = from_migration["ck_usage_events_claim_fields"]
    for column in ("claim_token", "claimed_at", "lease_expires_at"):
        assert f"{column} IS NOT NULL" in claim and f"{column} IS NULL" in claim
    assert "claimed_at < lease_expires_at" in claim
    snapshot = from_migration["ck_usage_events_processed_snapshot"]
    for column in (
        "provider_id",
        "model_id",
        "provider_price_version_id",
        "pricing_rule_id",
        "provider_source_currency",
        "provider_source_cost",
        "estimated_provider_cost_myr",
        "billable_cost",
        "processed_at",
    ):
        assert f"{column} IS NOT NULL" in snapshot, column


def test_0018_columns_are_the_ones_the_model_declares() -> None:
    migration = _load_0018()
    model = UsageEvent.__table__.c

    columns = migration._columns()

    assert {column.name: column.nullable for column in columns} == _EXPECTED_0018_COLUMNS
    for column in columns:
        declared = model[column.name]
        assert declared.nullable == column.nullable, column.name
        if isinstance(column.type, Numeric):
            assert isinstance(declared.type, Numeric), column.name
            pair = (column.type.precision, column.type.scale)
            assert pair == (declared.type.precision, declared.type.scale), column.name
        if isinstance(column.type, CHAR):
            assert declared.type.length == column.type.length, column.name
    assert str(model["attempt_count"].server_default.arg) == "0"


def test_0018_triggers_are_the_three_of_the_design_written_like_0006() -> None:
    triggers = _load_0018()._TRIGGERS

    assert list(triggers) == list(_EXPECTED_TRIGGERS_0018)
    for name, (table, timing, event) in _EXPECTED_TRIGGERS_0018.items():
        statement = _normalised(triggers[name])
        prefix = f"CREATE TRIGGER {name} {timing} {event} ON {table} FOR EACH ROW BEGIN IF "
        assert statement.startswith(prefix), name
        assert statement.endswith("END IF; END"), name
        assert "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = " in statement, name
    immutable = _normalised(triggers["trg_usage_events_processed_immutable"])
    assert "IF OLD.status = 'PROCESSED' THEN SIGNAL" in immutable
    link = _normalised(triggers["trg_usage_events_ledger_link"])
    for condition in (
        "NEW.status = 'PROCESSED' AND NEW.billable_cost > 0",
        "id = NEW.wallet_transaction_id",
        "reference_type = 'USAGE_EVENT'",
        "reference_id = NEW.event_id",
        "amount = -NEW.billable_cost",
    ):
        assert condition in link, condition
    requeue = _normalised(triggers["trg_usage_events_requeue_unbilled"])
    errors = "'MODEL_UNKNOWN', 'PRICING_ERROR', 'FX_RATE_ERROR', 'FAILED_FINAL'"
    assert f"OLD.status IN ({errors})" in requeue
    assert "NEW.status = 'RECEIVED' AND EXISTS" in requeue


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0018_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    migration = _load_0018()
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_FakeBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_FakeBind(log_bin, trusted))


def test_0018_precheck_runs_before_any_ddl(monkeypatch) -> None:
    """⚠️ MySQL DDL is not transactional: a failed precheck must not leave a column behind."""
    migration = _load_0018()
    refused = _RecordingOp(_FakeBind(log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", refused)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert refused.calls == []

    allowed = _RecordingOp(_FakeBind(log_bin=1, trusted=1))
    monkeypatch.setattr(migration, "op", allowed)
    migration.upgrade()

    # Columns, indexes, the unique key, foreign keys, checks, then the three triggers.
    expected = (
        ["add_column"] * 14
        + ["create_index"] * 5
        + ["create_unique_constraint"]
        + ["create_foreign_key"] * 4
        + ["create_check_constraint"] * 7
        + ["execute"] * 3
    )
    assert allowed.calls == expected


def test_0018_downgrade_undoes_upgrade_in_reverse(monkeypatch) -> None:
    migration = _load_0018()
    upgrade = _OfflineArgsRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    # `--sql`: no bind asked for; the triggers are the last statements, in design §2 order.
    assert upgrade.first_args("execute") == list(migration._TRIGGERS.values())
    added = [args[1].name for name, args in upgrade.calls if name == "add_column"]
    assert added == list(_EXPECTED_0018_COLUMNS)
    assert upgrade.first_args("create_index") == list(_EXPECTED_INDEXES_0018)
    assert upgrade.first_args("create_foreign_key") == list(_EXPECTED_FOREIGN_KEYS_0018)
    assert upgrade.first_args("create_check_constraint") == list(migration._CHECKS)

    downgrade = _ArgsRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    drops = [f"DROP TRIGGER {name}" for name in reversed(_EXPECTED_TRIGGERS_0018)]
    checks = reversed(migration._CHECKS)
    drops += [f"ALTER TABLE usage_events DROP CHECK {name}" for name in checks]
    assert downgrade.first_args("execute") == drops
    assert downgrade.first_args("drop_constraint") == [
        *reversed(_EXPECTED_FOREIGN_KEYS_0018),
        "uq_usage_events_wallet_transaction_id",
    ]
    assert downgrade.first_args("drop_index") == list(reversed(_EXPECTED_INDEXES_0018))
    dropped = [args[1] for name, args in downgrade.calls if name == "drop_column"]
    assert dropped == list(reversed(_EXPECTED_0018_COLUMNS))
    # Triggers and checks before what they refer to; foreign keys before their indexes.
    order = [name for name, _ in downgrade.calls]
    expected = ["execute"] * 10 + ["drop_constraint"] * 5 + ["drop_index"] * 5
    assert order == expected + ["drop_column"] * 14


@needs_mysql
def test_0018_only_adds_and_drops_its_columns_keys_checks_and_triggers(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION_0017)
        tables = _table_names()
        columns = _column_names("usage_events")
        triggers = _all_triggers()
        assert not set(_EXPECTED_0018_COLUMNS) & columns
        assert not set(_EXPECTED_TRIGGERS_0018) & set(triggers)
        assert _unique_sets("usage_events") == _EXPECTED_UNIQUE_0017["usage_events"]

        command.upgrade(alembic_config, _REVISION_0018)
        assert _table_names() == tables
        assert _column_names("usage_events") == columns | set(_EXPECTED_0018_COLUMNS)
        assert _all_triggers() == triggers | _EXPECTED_TRIGGERS_0018

        command.downgrade(alembic_config, _REVISION_0017)
        assert _table_names() == tables
        assert _column_names("usage_events") == columns
        assert _all_triggers() == triggers
        assert _unique_sets("usage_events") == _EXPECTED_UNIQUE_0017["usage_events"]
    finally:
        command.upgrade(alembic_config, "head")


@needs_mysql
def test_0018_column_shape(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("usage_events")}
        nullable = {name: found[name]["nullable"] for name in _EXPECTED_0018_COLUMNS}
        assert nullable == _EXPECTED_0018_COLUMNS
        types = {name: found[name]["type"] for name in _EXPECTED_0018_COLUMNS}
        for name in (
            "provider_price_version_id",
            "pricing_rule_id",
            "fx_rate_version_id",
            "wallet_transaction_id",
        ):
            assert isinstance(types[name], BigInteger), name
        for name in ("provider_source_cost", "estimated_provider_cost_myr", "billable_cost"):
            assert isinstance(types[name], Numeric), name
            assert (types[name].precision, types[name].scale) == (20, 8), name
        assert isinstance(types["fx_rate_applied"], Numeric)
        assert (types["fx_rate_applied"].precision, types["fx_rate_applied"].scale) == (24, 10)
        for name, width in (("provider_source_currency", 3), ("claim_token", 36)):
            assert isinstance(types[name], CHAR), name
            assert types[name].length == width, name
        # Whole seconds, as every other server time.
        for name in ("next_attempt_at", "claimed_at", "lease_expires_at"):
            assert isinstance(types[name], DateTime), name
            assert not getattr(types[name], "fsp", None), name
        assert isinstance(types["attempt_count"], Integer)
        assert "0" in str(found["attempt_count"]["default"])
    finally:
        engine.dispose()


@needs_mysql
def test_0018_keys_indexes_checks_and_triggers(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        assert _EXPECTED_UNIQUE_0018 <= _unique_sets("usage_events")
        foreign_keys = {
            str(fk["name"]): (
                fk["constrained_columns"],
                fk["referred_table"],
                fk["referred_columns"],
            )
            for fk in inspector.get_foreign_keys("usage_events")
        }
        for name, expected in _EXPECTED_FOREIGN_KEYS_0018.items():
            assert foreign_keys.get(name) == expected, name
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("usage_events")}
        for name, columns in _EXPECTED_INDEXES_0018.items():
            assert indexes.get(name) == columns, name

        with engine.connect() as connection:
            delete_rules = dict(connection.execute(_USAGE_REFERENTIAL_RULES_QUERY).all())
            checks = set(connection.execute(_USAGE_CHECKS_QUERY).scalars())
        # ⚠️ RESTRICT: price versions, rules, FX versions and ledger rows are never deleted.
        for name in _EXPECTED_FOREIGN_KEYS_0018:
            assert delete_rules.get(name) == "RESTRICT", name
        assert _EXPECTED_CHECKS_0018 <= checks
        triggers = _all_triggers()
        ours = {name: triggers.get(name) for name in _EXPECTED_TRIGGERS_0018}
        assert ours == _EXPECTED_TRIGGERS_0018
    finally:
        engine.dispose()
