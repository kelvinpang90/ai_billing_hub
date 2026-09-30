"""Admin audit log query (spec §89; AIH-TASK-022).

只读：一个普通会话，不提交、不写任何行 —— 查看审计本身不写审计（spec §66 没有要求）。

⚠️ **排序按审计行 `id` 倒序，不按 `created_at`。**登录路径与业务路径的时间取整不一致
（docs/TODO.md 的 AIH-TASK-006 记录段）：登录审计可能比它之后写入的建客户审计还晚一秒。
自增 id 才是真实的写入顺序。

⚠️ **响应里不出现内部自增 id。**审计行 `id` 与 `actor_user_id` 不返回，操作者换成
`users.email`。`entity_id` 在大多数类型上是对外 id（`public_id`、`api_key`、令牌家族 id），
原样返回；但下面 `USER_ENTITY_TYPES` 里的三类存的是**内部用户 id** —— 那三类的
`entity_id` 置 `null`，改填 `entity_user_email`，按 `entity_id` 筛选也不对它们生效。

新增一种写审计的 `entity_type`，必须归进下面两类之一：
`tests/backend/test_admin_audit_api.py` 扫描 `app/` 下所有写审计的地方，漏了就红。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog, User
from app.schemas.audit_logs import AuditLogView
from app.schemas.customers import Page

# `entity_id` 存的是内部用户 id（`str(user.id)`）的类型。写入方：`users` —— 登录、
# 密码重置、`app/cli.py` 建管理员；`two_factor_settings` / `recovery_codes` ——
# app/services/two_factor.py。
USER_ENTITY_TYPES: Final = frozenset({"users", "two_factor_settings", "recovery_codes"})

# `entity_id` 本身就是对外 id 的类型，原样返回。
PUBLIC_ENTITY_TYPES: Final = frozenset(
    {
        # 客户的 public_id：app/services/customers.py、account_status.py，
        # app/repositories/wallet.py 的计费状态跃迁。
        "tenant",
        # 项目的 public_id：app/services/customers.py。
        "project",
        # api_key：app/services/integration_access.py。
        "integration_credential",
        # 项目的 public_id：app/services/webhook_signing.py。
        "project_webhook_secret",
        # 账本行的 public_id：app/repositories/wallet.py 的调账审计。
        "wallet_transaction",
        # 令牌家族 id（随机 uuid 的十六进制，不是自增 id）：app/services/auth.py 的登出与重放。
        "refresh_tokens",
        # 目录行的 public_id：app/services/ai_catalog.py（AIH-TASK-025）。计量类型、供应商、
        # 模型各是自己那一行的；别名是新段（映射）或被截断那一段（撤销）的。
        "usage_meter_type",
        "ai_provider",
        "ai_model",
        "ai_model_alias",
        # 价格版本的 public_id：app/services/provider_prices.py（AIH-TASK-026）。
        "provider_price_version",
        # 定价规则的 public_id：app/services/pricing_rules.py（AIH-TASK-027）。
        "pricing_rule",
    }
)

# 与 docs/api.md「时间」一致：不带时区的 UTC，精确到秒。不收日期、时区、小数秒、空格分隔。
_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


class InvalidAuditQuery(AppError):
    """A query parameter that only the service can judge: time values and combinations.

    与 FastAPI 的 422 同一个码、同一种文案：只列字段名，不回显值（app/core/errors.py）。
    """

    def __init__(self, *fields: str) -> None:
        names = ", ".join(f"query.{name}" for name in sorted(fields))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


def _parse_time(field: str, value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    # 先卡形状再解析：`strptime` 自己会收 `2026-9-1T1:2:3` 这种不补零的写法。
    if not _TIME_PATTERN.fullmatch(value):
        raise InvalidAuditQuery(field)
    try:
        return dt.datetime.strptime(value, _TIME_FORMAT)
    except ValueError:
        # 形状对、日历上不存在（`2026-02-30T00:00:00`）。
        raise InvalidAuditQuery(field) from None


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _state(text: str | None) -> dict[str, Any] | None:
    # 写入方都是 `json.dumps(dict)`（`record_audit`、账本的审计），空状态存成 NULL。
    return json.loads(text) if text else None


def _user_id(row: AuditLog) -> int | None:
    """The internal user id a user-typed row points at, if it is a well-formed one."""
    if row.entity_type not in USER_ENTITY_TYPES or not row.entity_id:
        return None
    if not (row.entity_id.isascii() and row.entity_id.isdigit()):
        return None
    return int(row.entity_id)


def _user_emails(session: Session, rows: list[AuditLog]) -> dict[str, str]:
    """`entity_id` → email for the rows whose entity is a user."""
    ids = {user_id for user_id in map(_user_id, rows) if user_id is not None}
    if not ids:
        return {}
    found = session.execute(select(User.id, User.email).where(User.id.in_(ids))).all()
    return {str(user_id): email for user_id, email in found}


def _view(row: AuditLog, actor_email: str | None, user_emails: dict[str, str]) -> AuditLogView:
    about_user = row.entity_type in USER_ENTITY_TYPES
    return AuditLogView(
        # 库里（MySQL DATETIME）本来就是整秒；截掉是为了契约不随存储后端变。
        created_at=row.created_at.replace(microsecond=0),
        action=AuditAction(row.action).value,
        actor_role=row.actor_role,
        actor_email=actor_email,
        entity_type=row.entity_type,
        entity_id=None if about_user else row.entity_id,
        entity_user_email=user_emails.get(row.entity_id or "") if about_user else None,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        reason=row.reason,
        before_state=_state(row.before_state),
        after_state=_state(row.after_state),
    )


def list_audit_logs(
    session_factory: sessionmaker[Session],
    *,
    page: int,
    page_size: int,
    action: AuditAction | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_email: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> Page[AuditLogView]:
    """Newest first (by row id). Every filter is optional; they combine with AND.

    时间区间含 `created_from`、不含 `created_to`。
    """
    start = _parse_time("created_from", created_from)
    end = _parse_time("created_to", created_to)
    if start is not None and end is not None and start >= end:
        raise InvalidAuditQuery("created_from", "created_to")
    # 这三类的 entity_id 是内部用户 id：不让它成为可以探测的筛选条件。
    if entity_id is not None and entity_type in USER_ENTITY_TYPES:
        raise InvalidAuditQuery("entity_id")

    actor = aliased(User)
    conditions = []
    if action is not None:
        conditions.append(AuditLog.action == action)
    if entity_type is not None:
        conditions.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        conditions.append(AuditLog.entity_id == entity_id)
        if entity_type is None:
            # 没指定类型时同样只在非用户类型里找：`entity_id=1` 不能命中「用户 1」的审计。
            conditions.append(AuditLog.entity_type.not_in(sorted(USER_ENTITY_TYPES)))
    if actor_email is not None:
        # `users.email` 存的是小写（登录时同样先小写再查）。
        conditions.append(actor.email == actor_email.strip().lower())
    if start is not None:
        conditions.append(AuditLog.created_at >= start)
    if end is not None:
        conditions.append(AuditLog.created_at < end)

    # 左连接：没有操作者、或用户已不存在的审计照样列出，`actor_email` 为 null。
    on_actor = actor.id == AuditLog.actor_user_id
    listing = (
        select(AuditLog, actor.email)
        .outerjoin(actor, on_actor)
        .where(*conditions)
        .order_by(AuditLog.id.desc())
        .offset(_offset(page, page_size))
        .limit(page_size)
    )
    counting = (
        select(func.count()).select_from(AuditLog).outerjoin(actor, on_actor).where(*conditions)
    )

    with session_factory() as session:
        found = session.execute(listing).all()
        total = session.execute(counting).scalar_one()
        rows = [row for row, _ in found]
        user_emails = _user_emails(session, rows)
        return Page[AuditLogView](
            items=[_view(row, email, user_emails) for row, email in found],
            page=page,
            page_size=page_size,
            total=total,
        )


__all__ = [
    "PUBLIC_ENTITY_TYPES",
    "USER_ENTITY_TYPES",
    "InvalidAuditQuery",
    "list_audit_logs",
]
