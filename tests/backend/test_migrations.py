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
import os
import pathlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic.config import Config
from sqlalchemy import CHAR, BigInteger, create_engine, delete, func, insert, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.base import Base
from app.models.tenancy import Project, Tenant

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

# 列名 → 是否可空。⚠️ 比的是**完整集合**：状态列、金额列、webhook 列谁被顺手加进来，
# 这里都会红 —— 它们各归一个后续任务（docs/database-schema.md「尚未建的列」）。
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

            # public_id 是唯一的那一个；tenants.email 刻意**不**唯一。
            unique = [u["column_names"] for u in inspector.get_unique_constraints(table)]
            assert unique == [["public_id"]], table

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
