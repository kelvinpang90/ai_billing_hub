"""Integration coverage for the public AI messages route lives in CI's database shards."""

from app.ai_api.routes import _write_limit
from app.core.config import Settings


def test_ai_write_limit_is_derived_from_database_timeouts() -> None:
    settings = Settings(
        database_url="sqlite://",
        ai_db_pool_timeout_seconds=1.0,
        ai_db_connect_timeout_seconds=2.0,
        ai_db_rw_timeout_seconds=3.0,
    )
    assert _write_limit(settings) == 15.0
