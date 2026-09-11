"""baseline: establish the migration chain

这一版**故意不建任何表**。它的作用是让 `alembic upgrade head` 在一个空库上
真的跑起来、建出 `alembic_version` 表，从而把「迁移链能不能执行」这件事在
Phase 0 就验证掉（§123 的验收里有「DB migrations execute」一条）。

业务表从 Phase 1 的 tenant / project / wallet 开始加，各自一版迁移。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
