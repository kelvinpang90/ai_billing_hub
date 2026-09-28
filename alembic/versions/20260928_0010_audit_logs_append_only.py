"""audit_logs append-only: BEFORE UPDATE / BEFORE DELETE triggers

照 AIH-TASK-021：审计记录在数据库层只能插入（spec §66「Audit records must not be
editable」）。写法照搬 0006 给账本建的 `trg_wallet_transactions_before_update` /
`trg_wallet_transactions_before_delete`。应用本来就只有插入路径（`record_audit`），
本版不改任何应用代码。

步骤
----

0. **预检**：与 0006 相同，查 `@@log_bin` 与 `@@log_bin_trust_function_creators`。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL
   之前**抛出明确的错误，`alembic_version` 仍是 0009。
1. `CREATE TRIGGER trg_audit_logs_before_update`：一律 `SIGNAL SQLSTATE '45000'`
2. `CREATE TRIGGER trg_audit_logs_before_delete`：同上

§132 第 13 条分析
-----------------

**已有数据**：生产上已有的审计行**不受影响**。触发器只在之后的 UPDATE / DELETE 上
触发，建触发器既不读也不改任何一行；不回填、不改列。

**锁表与停机**：不改表结构、不重建表、不拷数据。`CREATE TRIGGER` 只在 `audit_logs`
上短暂拿一次元数据锁（等正在进行的、碰过这张表的事务结束），不锁行、不锁表内容，
瞬时完成。不需要停机。

**备份**：要求前置备份（与 0005–0009 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。新旧代码对 `audit_logs` 都只 INSERT，
先迁移后换镜像、或反过来，都不会撞上触发器。⚠️ 仍会撞上的只有**人工或脚本**对
审计行的 UPDATE / DELETE：`scripts/perf_baseline.py` 的收尾已在同一任务里去掉了
删审计行那一步；测试清场改用 `TRUNCATE`。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 预检失败：什么都没改，`alembic_version` 仍是 `0009_tenant_account_status`。打开
  `log_bin_trust_function_creators` 后重新部署。
- 第 1 步失败：什么都没建，修好后重新部署。
- 第 2 步失败，或进程在两步之间被杀：`trg_audit_logs_before_update` 已存在而
  `alembic_version` 没有前进，重跑会撞 "Trigger already exists"。确认
  `alembic_version` 仍是 `0009_tenant_account_status` 后
  `DROP TRIGGER trg_audit_logs_before_update`，再重新部署。
- 本版不写数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 删两个触发器，表与数据不动。回滚之后审计表重新可改可删 ——
只在需要撤回本版时这么做，平时应前滚。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 的账本同一处缺口）；
拥有 `TRIGGER` / `DROP` 权限的账号也能先删触发器再改数据。这两条挡不住，归「迁移
账号与运行账号拆分」那项运维任务：运行账号不给 DDL 权限之后，应用这一侧就只剩
INSERT。

Revision ID: 0010_audit_logs_append_only
Revises: 0009_tenant_account_status
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_audit_logs_append_only"
down_revision: str | None = "0009_tenant_account_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 0006 的预检同一条查询、同一个判据。
_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the audit_logs triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 触发器名 → 建触发器的语句。写法照 0006 的账本触发器。
_TRIGGERS: dict[str, str] = {
    # spec §66：审计只能插入。
    "trg_audit_logs_before_update": """
CREATE TRIGGER trg_audit_logs_before_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'
""",
    "trg_audit_logs_before_delete": """
CREATE TRIGGER trg_audit_logs_before_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'
""",
}


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前（理由见 0006 的同一段）。`--sql` 离线模式
    # 不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 反向删。表与数据不动。
    for name in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
