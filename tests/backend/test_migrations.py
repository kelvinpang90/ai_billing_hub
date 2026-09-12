"""`alembic upgrade head` must actually execute (spec §123 Phase 0 acceptance).

**这条必须对着真 MySQL 跑。**SQLite 的 DDL 与 MySQL 差得远，在 SQLite 上跑通
的迁移证明不了生产上跑得通 —— 而迁移跑不通是部署时才发现的那种故障。

需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**数据库。CI 的
`backend` job 起了一个 MySQL service 并设了它，所以 CI 里这些用例**从不跳过**。
本地没设时会 skip，`pytest` 的输出里会明写 skipped —— 不要把 skipped 读成
passed。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.base import Base

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
