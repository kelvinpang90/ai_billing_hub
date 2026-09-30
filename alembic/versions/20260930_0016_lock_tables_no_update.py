"""lock tables reject UPDATE: BEFORE UPDATE triggers on pricing_rule_locks and fx_rate_locks

照设计闸门 #200 v1（docs/design/AIH-TASK-045-lock-tables-no-update.md）§2 与 §8。
`pricing_rule_locks`（0014）与 `fx_rate_locks`（0015）都是「恰好一行 `id = 1`」的锁表，
此前只拒绝 INSERT（已有一行时）与 DELETE；`UPDATE … SET id = 2` 能把锁行移走，之后版本表
BEFORE UPDATE 触发器里的 `SELECT … WHERE id = 1 FOR UPDATE` 静默锁不到行。本版给两张锁表
各加一个无条件拒绝的 BEFORE UPDATE，DML 范围内锁表恒为 `{1}`。不改 0014 / 0015 的任何对象，
不改模型与服务。

步骤
----

0. **预检**：与 0006 相同，查 `@@log_bin` 与 `@@log_bin_trust_function_creators`。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL
   之前**抛出明确的错误，`alembic_version` 仍是 0015。`--sql` 离线模式跳过（与 0015 相同）。
1. `CREATE TRIGGER trg_pricing_rule_locks_before_update`：一律 `SIGNAL SQLSTATE '45000'`
2. `CREATE TRIGGER trg_fx_rate_locks_before_update`：同上

§132 第 13 条分析
-----------------

**已有数据**：不 ALTER、不读写任何行。两张锁表各自那一行 `id = 1` 原样保留；触发器只在
之后的 UPDATE 上触发。锁定读（`SELECT … FOR UPDATE` / `FOR SHARE`）不是 UPDATE 语句，不触发。

**锁表与停机**：`CREATE TRIGGER` 对目标表取短暂的元数据锁，要等进行中的、碰过这张表的事务
结束；两张锁表上最长的事务是发布 / 退役（毫秒级），计费事务尚未接上。不重建表、不拷数据，
不需要停机。

**备份**：要求前置备份（与 0005–0015 同一口径），虽然本版不改任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。新旧代码都从不 UPDATE 锁表（只有锁定读与建表时
写入那一行），先后顺序无关。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，两条 `CREATE TRIGGER` 各自隐式提交。
- 预检失败：什么都没建，`alembic_version` 仍是 `0015_fx_rates`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 第 1 步失败：什么都没建，修好后重新部署。
- 第 2 步失败，或进程在两步之间被杀（两个触发器只建成一个）：
  `trg_pricing_rule_locks_before_update` 已存在（`pricing_rule_locks` 已拒绝 UPDATE，
  `fx_rate_locks` 还没有）而 `alembic_version` 没有前进，重跑会撞 "Trigger already exists"。
  清场步骤：确认 `alembic_version` 仍是 `0015_fx_rates` 后执行
  `DROP TRIGGER IF EXISTS trg_pricing_rule_locks_before_update`，再重新部署。
- 本版不写数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 只删这两个触发器，表、行与 0014 / 0015 的触发器都不动。回滚后回到 0015
的状态（锁行又能被 UPDATE 移走，但不丢数据）—— 只在需要撤回本版时这么做，平时应前滚。

**残余风险**：`TRUNCATE` / `DROP TRIGGER` / `ALTER TABLE` 是 DDL，不经触发器（与 0006 /
0014 / 0015 相同）；能执行它们的账号同样能删掉本版的触发器。归「迁移账号与运行账号拆分」。

Revision ID: 0016_lock_tables_no_update
Revises: 0015_fx_rates
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_lock_tables_no_update"
down_revision: str | None = "0015_fx_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 0006 的预检同一条查询、同一个判据。
_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the lock table triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 触发器名 → 建触发器的语句。写法照 0014 / 0015 的锁表 BEFORE DELETE（单条 SIGNAL，整条
# CREATE TRIGGER 是一条语句）；错误文本与同表 INSERT / DELETE 触发器相同。无条件拒绝，不判断
# `id` 是否变化：这一行只该被锁、永远不该被写（设计 §2）。
_TRIGGERS: dict[str, str] = {
    "trg_pricing_rule_locks_before_update": """
CREATE TRIGGER trg_pricing_rule_locks_before_update
BEFORE UPDATE ON pricing_rule_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'pricing_rule_locks holds exactly one row'
""",
    "trg_fx_rate_locks_before_update": """
CREATE TRIGGER trg_fx_rate_locks_before_update
BEFORE UPDATE ON fx_rate_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'fx_rate_locks holds exactly one row'
""",
}


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，建了一半要人工清场。
    # `--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 反向删，只删这两个。表、行与 0014 / 0015 的触发器都不动。
    for name in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
