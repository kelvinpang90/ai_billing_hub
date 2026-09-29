"""Response shape for the admin audit log query (spec §89; AIH-TASK-022).

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**（与 app/schemas/customers.py 同一条）。
审计行的自增 `id` 与 `actor_user_id` 都是内部自增 id，不出现在这里：操作者换成
`actor_email`，「以用户为对象」的审计把 `entity_id` 置 `null`、改填 `entity_user_email`
（替换规则在 app/services/audit_query.py）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel


class AuditLogView(BaseModel):
    """One audit row as an admin sees it."""

    # 不带时区的 UTC，精确到秒（docs/api.md「时间」）。
    created_at: dt.datetime
    action: str
    actor_role: str | None
    # 操作者的登录邮箱；没有操作者（如 UNKNOWN_EMAIL 的登录失败）或用户已不存在时为 null。
    actor_email: str | None
    entity_type: str | None
    # 对外 id 原样返回；以用户为对象的审计上一律是 null。
    entity_id: str | None
    # 只在以用户为对象的审计上有值：那个用户的邮箱。
    entity_user_email: str | None
    ip_address: str | None
    user_agent: str | None
    reason: str | None
    # 库里是 JSON 文本，这里解析成对象；为空时 null。
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
