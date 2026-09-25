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
from types import ModuleType, SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
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
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.base import Base
from app.models.integration import API_KEY_COLLATION, IntegrationCredential
from app.models.tenancy import BillingStatus, Project, Tenant
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

# 列名 → 是否可空（head 上）。⚠️ 比的是**完整集合**：`account_status`、webhook 列谁被
# 顺手加进来，这里都会红 —— 它们各归一个后续任务（docs/database-schema.md「尚未建的列」）。
# 最后三列是 0006 加的（AIH-TASK-005）。
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

    assert from_migration == _model_checks()
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
        assert triggers == _EXPECTED_TRIGGERS
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
        assert _unique_sets(_CREDENTIALS_TABLE) == {("public_api_key", "key_version")}
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
