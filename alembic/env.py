"""Alembic environment.

连接串从 `app.core.config` 读（`BILLING_DATABASE_URL`），**不从 `alembic.ini`
读** —— 仓库是公开的，配置只许有一个入口，而且那个入口不落盘。
"""

from __future__ import annotations

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings

# 必须 import 所有模型模块，`Base.metadata` 才是完整的；否则 autogenerate 会把
# 没被 import 的表当成「已删除」，生成一个 drop table 的迁移。
from app.models.base import Base  # noqa: F401  (imported for metadata side effects)

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "BILLING_DATABASE_URL is not set; alembic cannot run without a target database."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 列类型变更默认不被 autogenerate 检测到 —— 对一个金额必须是
            # DECIMAL(20,8) 的系统来说，那正是最不能漏的一类变更。
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
