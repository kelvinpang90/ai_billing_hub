"""Status webhook delivery: claim, signed send, settle, retry (design gate #241 v6, AIH-TASK-051).

spec §25、§28、§29、§119；设计 §2「事务边界」「外部系统与异步边界」「配置」、§4「投递行」。
「要推送什么」由版本差推导，不消费 outbox：每个项目一行投递行，`delivered_version` 落后于租户的
`status_version` 就该推送。Beat 每 15 秒跑一次 `dispatch`，逐行处理：

- **领取**（`claim_next`）：一个短事务。`SELECT … FOR UPDATE OF project_status_deliveries
  SKIP LOCKED LIMIT 1`，条件是项目有地址、`integration_status = ENABLED`、
  `COALESCE(delivered_version, -1) < tenant.status_version`、`next_attempt_at ≤ now`、租约空或已
  过期。只锁投递行，JOIN 进来的租户与项目是一致性读。同一事务里用读到的快照组装 payload，
  写 `lease_token`（uuid4）、`lease_expires_at = now + 60s`、
  `attempted_version` = 快照里的版本，提交。快照与版本出自同一次读取，二者一定对应；
- **发送**（`attempt`）：在任何事务之外。签名密钥的判定顺序固定：先查有没有 `ACTIVE` 行（只读、
  不解密），没有就 `NO_ACTIVE_SECRET`；有才加载主密钥（`ENCRYPTION_NOT_CONFIGURED`），再解密
  （`SECRET_DECRYPT_FAILED`）。三种都不发送。之后 `sign_status_webhook` → POST（超时 5 秒，不跟随
  重定向）。响应体不读，拿到状态码就关连接；只有 2xx 算送达，3xx、4xx、5xx、超时、连接错误都是失败；
- **结算**（`settle`）：一个事务，顺序固定。
  ① `SELECT status_version FROM tenants … FOR SHARE` 读当前已提交的版本；
  ② 带令牌的条件 UPDATE（`WHERE project_id = ? AND lease_token = ?`）；③ 提交。
  令牌不匹配（改了地址、启用了新密钥、租约被别人接手）就什么都不写。

⚠️ **加锁顺序**（设计 §4）：领取只锁投递行；结算是租户（共享）→ 投递行，与
`bump_status_version`（租户排他 → 投递行）同序且互斥 —— 跃迁先拿到锁，结算读到新版本、
`next_attempt_at = now`；结算先拿到锁，跃迁随后把退避提前到 now。没有任何路径先锁投递行、再锁
租户或项目，所以不成环。

⚠️ **失败不改任何状态**（INV-1）：投递只写投递行，不碰租户、项目与钱包。重试**不设上限**，
退避 `min(base × 2^(n−1), 上限)`，`n` 是加一之后的连续失败次数。

⚠️ **日志只记项目 public_id、版本、HTTP 状态、错误码**（设计 §6）：
不记地址（路径里可能带集成方的令牌）、签名、密钥、响应体；异常只记类型。
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import DecryptionFailed, EncryptionNotConfigured, load_keyring
from app.core.database import session_scope
from app.core.fx_source import urllib_transport
from app.models.integration import ProjectStatusDelivery
from app.models.tenancy import (
    AccountStatus,
    BillingMode,
    BillingStatus,
    IntegrationStatus,
    Project,
    Tenant,
    compose_effective_status,
)
from app.repositories import webhook_signing as webhook_secrets
from app.services.auth import utc_now
from app.services.webhook_signing import SigningMaterial, sign_status_webhook, signing_material

logger = logging.getLogger(__name__)

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]
# 单调时钟（秒），只用来量一次 dispatch 的预算。
type Ticker = Callable[[], float]

# `last_error_code`（设计 §2「数据库」）。地址在写入时已校验，投递时没有「地址被拒」。
TIMEOUT: Final = "TIMEOUT"
CONNECTION_ERROR: Final = "CONNECTION_ERROR"
HTTP_STATUS: Final = "HTTP_STATUS"
NO_ACTIVE_SECRET: Final = "NO_ACTIVE_SECRET"
SECRET_DECRYPT_FAILED: Final = "SECRET_DECRYPT_FAILED"
ENCRYPTION_NOT_CONFIGURED: Final = "ENCRYPTION_NOT_CONFIGURED"
# `DispatchResult.outcomes` 里送达的那一类。
DELIVERED: Final = "DELIVERED"
# 结算时令牌对不上：什么都没写。
SUPERSEDED: Final = "SUPERSEDED"
# 处理一行时意外出错：租约留着，过期后重新领取。
PROCESSING_FAILED: Final = "PROCESSING_FAILED"

EVENT_ID_PREFIX: Final = "sevt_"
CONTENT_TYPE: Final = "application/json"
# 退避的指数先夹住：重试不设上限，计数会一直涨，不让它算出巨大的整数。
# 2^32 早已超过任何上限。
_MAX_BACKOFF_EXPONENT: Final = 32
# 「从未送达」在比较里当作 -1（设计 §2：`COALESCE(delivered_version, -1)`）。
_NEVER_DELIVERED: Final = -1


class _ResponseLike(Protocol):
    status: int

    def close(self) -> None: ...


# (请求, 超时秒数) → 响应。可注入：测试据此不联网。
# 真实实现是 BNM 适配器那个不跟随重定向的 opener（app/core/fx_source.py）。
type Transport = Callable[[urllib.request.Request, float], _ResponseLike]


@dataclass(frozen=True)
class Claim:
    """One claimed delivery row and the payload assembled from the snapshot read with it.

    `url` 与 `payload` 不进 `repr`：地址的路径里可能带集成方的令牌（设计 §6）。
    `attempt_count` 是领取时的值；令牌还匹配就说明没有别人改过它。
    """

    project_id: int
    tenant_id: int
    project_public_id: str
    status_version: int
    attempt_count: int
    token: str
    url: str = field(repr=False)
    payload: dict[str, object] = field(repr=False)


@dataclass(frozen=True)
class Attempt:
    """What one attempt ended in. `error_code is None` means a 2xx: delivered."""

    error_code: str | None
    http_status: int | None

    @property
    def delivered(self) -> bool:
        return self.error_code is None


@dataclass(frozen=True)
class DispatchResult:
    claimed: int
    outcomes: dict[str, int] = field(default_factory=dict)


class SigningUnavailable(Exception):
    """The project cannot be signed for now; `code` goes into `last_error_code`."""

    def __init__(self, code: str, *, key_version: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.key_version = key_version


def _seconds(moment: dt.datetime) -> dt.datetime:
    # 投递表各时间列截到整秒（设计 §2「时间」）。
    return moment.replace(microsecond=0)


def backoff_seconds(failures: int, *, base_seconds: int, cap_seconds: int) -> int:
    """`min(base × 2^(n−1), cap)`, `n` the consecutive failures including this one."""
    exponent = min(max(failures - 1, 0), _MAX_BACKOFF_EXPONENT)
    return min(base_seconds * 2**exponent, cap_seconds)


def event_id(project_public_id: str, status_version: int) -> str:
    """`sevt_<project_public_id>_<status_version>`: the same version always has the same id."""
    return f"{EVENT_ID_PREFIX}{project_public_id}_{status_version}"


def encode_payload(payload: dict[str, object]) -> bytes:
    """The request body: keys sorted, no extra whitespace (设计 §2「Webhook 请求」)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


# --- 领取 -------------------------------------------------------------------------------


def _due(now: dt.datetime):
    delivery = ProjectStatusDelivery
    return (
        Project.status_webhook_url.is_not(None),
        Project.integration_status == IntegrationStatus.ENABLED,
        func.coalesce(delivery.delivered_version, _NEVER_DELIVERED) < Tenant.status_version,
        delivery.next_attempt_at <= now,
        or_(delivery.lease_expires_at.is_(None), delivery.lease_expires_at <= now),
    )


def claim_next(
    factory: sessionmaker[Session],
    *,
    lease_seconds: int,
    clock: Clock = utc_now,
) -> Claim | None:
    """Claim one due delivery row in one short transaction, or `None` when nothing is due.

    `FOR UPDATE OF project_status_deliveries SKIP LOCKED`：
    被别的事务锁着的投递行（改地址、启用密钥、另一个 dispatch 的领取）直接跳过。
    租户与项目只做一致性读，与投递行出自同一次读取。
    """
    delivery = ProjectStatusDelivery
    with session_scope(factory) as session:
        now = _seconds(clock())
        statement = (
            select(
                delivery.project_id,
                delivery.tenant_id,
                delivery.attempt_count,
                Project.public_id.label("project_public_id"),
                Project.status_webhook_url,
                Project.integration_status,
                Tenant.public_id.label("tenant_public_id"),
                Tenant.account_status,
                Tenant.billing_status,
                Tenant.billing_mode,
                Tenant.status_version,
                Tenant.status_changed_at,
                Tenant.created_at,
            )
            .select_from(delivery)
            .join(
                Project,
                (Project.id == delivery.project_id) & (Project.tenant_id == delivery.tenant_id),
            )
            .join(Tenant, Tenant.id == delivery.tenant_id)
            .where(*_due(now))
            .order_by(delivery.next_attempt_at, delivery.project_id)
            .limit(1)
            .with_for_update(of=delivery, skip_locked=True)
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            return None
        version = int(row.status_version)
        payload = _payload(row, version)
        token = str(uuid.uuid4())
        session.execute(
            update(delivery)
            .where(delivery.project_id == row.project_id)
            .values(
                lease_token=token,
                lease_expires_at=now + dt.timedelta(seconds=lease_seconds),
                attempted_version=version,
            )
            .execution_options(synchronize_session=False)
        )
        return Claim(
            project_id=int(row.project_id),
            tenant_id=int(row.tenant_id),
            project_public_id=row.project_public_id,
            status_version=version,
            attempt_count=int(row.attempt_count),
            token=token,
            url=row.status_webhook_url,
            payload=payload,
        )


def _payload(row, version: int) -> dict[str, object]:
    """The eleven fields (设计 §2「Webhook 请求」), names exactly as in `effective-status`.

    ⚠️ 白名单（INV-7）：不带余额、阈值、成本、管理员原因、内部 id。有效状态只来自
    `compose_effective_status`。
    """
    account_status = AccountStatus(row.account_status)
    billing_status = BillingStatus(row.billing_status)
    billing_mode = BillingMode(row.billing_mode)
    integration_status = IntegrationStatus(row.integration_status)
    effective, reason_code = compose_effective_status(
        account_status, billing_status, billing_mode, integration_status
    )
    effective_at = row.status_changed_at or row.created_at
    return {
        "event_id": event_id(row.project_public_id, version),
        "tenant_id": row.tenant_public_id,
        "project_id": row.project_public_id,
        "account_status": account_status.value,
        "billing_status": billing_status.value,
        "billing_mode": billing_mode.value,
        "project_integration_status": integration_status.value,
        "effective_status": effective.value,
        "reason_code": reason_code.value if reason_code is not None else None,
        "status_version": version,
        "effective_at": effective_at.isoformat(),
    }


# --- 签名与发送 ---------------------------------------------------------------------------


def resolve_signing(
    factory: sessionmaker[Session], settings: Settings, project_id: int
) -> SigningMaterial:
    """The project's ACTIVE secret, or `SigningUnavailable` with the error code.

    ⚠️ 判定顺序固定（设计 §4「投递行」）：
    先查有没有 `ACTIVE` 行（只读、不解密）→ 再加载主密钥 → 再解密。
    只读会话，不加锁；明文不缓存，每次发送重新解密（ADR-0004）。
    """
    with factory() as session:
        found = webhook_secrets.get_active(session, project_id)
        if found is None:
            raise SigningUnavailable(NO_ACTIVE_SECRET)
        row, _project_public_id = found
        key_version = row.key_version
        try:
            keyring = load_keyring(settings)
        except EncryptionNotConfigured:
            raise SigningUnavailable(ENCRYPTION_NOT_CONFIGURED, key_version=key_version) from None
        try:
            material = signing_material(session, keyring, project_id)
        except DecryptionFailed:
            raise SigningUnavailable(SECRET_DECRYPT_FAILED, key_version=key_version) from None
    if material is None:
        # 两次读之间 ACTIVE 被退役了：与没有密钥一样。
        raise SigningUnavailable(NO_ACTIVE_SECRET)
    return material


def build_request(
    claim: Claim, material: SigningMaterial, now: dt.datetime
) -> urllib.request.Request:
    """The signed POST: the claim's payload to the claim's URL, four §28 headers."""
    body = encode_payload(claim.payload)
    headers = sign_status_webhook(
        material.secret,
        material.key_version,
        url_path_and_query=_path_and_query(claim.url),
        event_id=str(claim.payload["event_id"]),
        body=body,
        now=now,
    )
    return urllib.request.Request(
        claim.url,
        data=body,
        headers={"Content-Type": CONTENT_TYPE, **headers},
        method="POST",
    )


def _discard_body(response) -> None:
    # 状态码已经拿到，响应体一个字节都不读，直接关（设计 §2「最多读 1 KiB」，不读也满足）。
    # ⚠️ 不要改回 `read(n)`：超时是每次 recv 各 5 秒，对端慢慢吐响应体时总时长没有上限，
    #    会拖过 60 秒租约、占住 worker（Codex 对 #277 的阻断项）。
    try:
        response.close()
    except (OSError, http.client.HTTPException):
        pass


def send(
    request: urllib.request.Request, *, transport: Transport, timeout_seconds: float
) -> Attempt:
    """POST once. Only 2xx is delivered; every other ending is a failure with its error code."""
    try:
        response = transport(request, timeout_seconds)
    except urllib.error.HTTPError as error:
        # 非 2xx（含不跟随的 3xx）在 urllib 里是异常，带着状态码。
        _discard_body(error)
        return Attempt(error_code=HTTP_STATUS, http_status=error.code)
    except TimeoutError:
        return Attempt(error_code=TIMEOUT, http_status=None)
    except urllib.error.URLError as error:
        code = TIMEOUT if isinstance(error.reason, TimeoutError) else CONNECTION_ERROR
        return Attempt(error_code=code, http_status=None)
    except (OSError, http.client.HTTPException):
        return Attempt(error_code=CONNECTION_ERROR, http_status=None)
    status = response.status
    _discard_body(response)
    if 200 <= status < 300:
        return Attempt(error_code=None, http_status=status)
    return Attempt(error_code=HTTP_STATUS, http_status=status)


def attempt(
    factory: sessionmaker[Session],
    settings: Settings,
    claim: Claim,
    *,
    transport: Transport,
    clock: Clock = utc_now,
) -> Attempt:
    """Sign and send the claimed payload, outside any transaction.

    密钥缺失或解不开时不发送，返回对应的错误码；异常在这里接住，由结算写进投递行，不让租约
    反复过期（设计 §4）。签名时间戳是发送这一刻（每次重发都重新生成）。
    """
    context = {"project_id": claim.project_public_id, "status_version": claim.status_version}
    try:
        material = resolve_signing(factory, settings, claim.project_id)
    except SigningUnavailable as unavailable:
        if unavailable.code == NO_ACTIVE_SECRET:
            logger.warning(
                "A status webhook was not sent: the project has no active signing secret",
                extra={**context, "error_code": unavailable.code},
            )
        else:
            # 加密配置坏了：只记项目 id 与 key_version（设计 §5）。
            logger.error(
                "A status webhook was not sent: the signing secret could not be opened",
                extra={
                    **context,
                    "error_code": unavailable.code,
                    "key_version": unavailable.key_version,
                },
            )
        return Attempt(error_code=unavailable.code, http_status=None)
    request = build_request(claim, material, clock())
    result = send(
        request, transport=transport, timeout_seconds=settings.status_webhook_timeout_seconds
    )
    if not result.delivered:
        logger.warning(
            "A status webhook was not delivered; it is retried with backoff",
            extra={**context, "http_status": result.http_status, "error_code": result.error_code},
        )
    return result


# --- 结算 -------------------------------------------------------------------------------


def settle(
    factory: sessionmaker[Session],
    claim: Claim,
    result: Attempt,
    *,
    retry_base_seconds: int,
    retry_cap_seconds: int,
    clock: Clock = utc_now,
) -> bool:
    """Write the attempt into the delivery row under the claim's token. `False`: nothing written.

    ① 对租户行加共享锁，读当前已提交的版本；② 带令牌的条件 UPDATE（设计 §4「投递行」）：

    - 2xx：
      `delivered_version = GREATEST(COALESCE(delivered_version, -1), attempted_version)`，
      `attempt_count = 0`，清租约与 `attempted_version`，
      `delivered_at` 与 `last_attempt_at`，`last_error_code = NULL`，
      `last_http_status` = 本次状态码，`next_attempt_at = now`；
    - 其余（含三种密钥错误）：`attempt_count + 1`，清租约与 `attempted_version`，
      记错误码与收到的状态码（没有响应时为空），`last_attempt_at = now`；
      `next_attempt_at = now + 退避`，但①读到的版本大于 `attempted_version` 时取 `now`
      （有更新的版本待发，不继承旧版本的退避）。
    """
    delivery = ProjectStatusDelivery
    with session_scope(factory) as session:
        current_version = session.execute(
            select(Tenant.status_version)
            .where(Tenant.id == claim.tenant_id)
            .with_for_update(read=True)
        ).scalar_one()
        now = _seconds(clock())
        cleared = {"lease_token": None, "lease_expires_at": None, "attempted_version": None}
        if result.delivered:
            # GREATEST 的可移植写法（SQLite 没有 GREATEST）。令牌匹配时 `attempted_version`
            # 就是领取时写下的 `claim.status_version`，这里用后者：MySQL 的单表 UPDATE 从左到右
            # 求值，同一条语句里清空 `attempted_version` 之后再读它就是 NULL。
            previous = func.coalesce(delivery.delivered_version, _NEVER_DELIVERED)
            delivered_version = case(
                (previous > claim.status_version, delivery.delivered_version),
                else_=claim.status_version,
            )
            values: dict[str, object] = {
                "delivered_version": delivered_version,
                "attempt_count": 0,
                **cleared,
                "delivered_at": now,
                "last_attempt_at": now,
                "last_error_code": None,
                "last_http_status": result.http_status,
                "next_attempt_at": now,
            }
        else:
            next_attempt_at = now
            if current_version <= claim.status_version:
                wait = backoff_seconds(
                    claim.attempt_count + 1,
                    base_seconds=retry_base_seconds,
                    cap_seconds=retry_cap_seconds,
                )
                next_attempt_at = now + dt.timedelta(seconds=wait)
            values = {
                "attempt_count": delivery.attempt_count + 1,
                **cleared,
                "last_attempt_at": now,
                "last_error_code": result.error_code,
                "last_http_status": result.http_status,
                "next_attempt_at": next_attempt_at,
            }
        written = session.execute(
            update(delivery)
            .where(delivery.project_id == claim.project_id, delivery.lease_token == claim.token)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return written.rowcount == 1


# --- dispatch ---------------------------------------------------------------------------


def process(
    factory: sessionmaker[Session],
    settings: Settings,
    claim: Claim,
    *,
    transport: Transport,
    clock: Clock = utc_now,
) -> str:
    """Send and settle one claim. Returns `DELIVERED`, the error code, or `SUPERSEDED`."""
    result = attempt(factory, settings, claim, transport=transport, clock=clock)
    written = settle(
        factory,
        claim,
        result,
        retry_base_seconds=settings.status_webhook_retry_base_seconds,
        retry_cap_seconds=settings.status_webhook_retry_cap_seconds,
        clock=clock,
    )
    if not written:
        logger.info(
            "A status webhook attempt was superseded; its result was not written",
            extra={"project_id": claim.project_public_id, "status_version": claim.status_version},
        )
        return SUPERSEDED
    return DELIVERED if result.delivered else str(result.error_code)


def dispatch(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    transport: Transport | None = None,
    clock: Clock = utc_now,
    ticker: Ticker = time.monotonic,
) -> DispatchResult:
    """One Beat run: claim → send → settle, one row at a time, until nothing is due or the budget
    (`status_webhook_dispatch_budget_seconds`, 12) is spent.

    预算只在领取前检查。结算后又到期的行（例如有了更新的版本）在同一次 dispatch 里可以再次领取。
    每一行的租约从它被领取时算起。一行处理中意外出错只记日志、接着处理下一行：租约留着，
    过期后重新领取（设计 §5「结算时 DB 失败」）。领取本身出错（例如数据库不可用）直接抛出。
    """
    send_with = transport if transport is not None else urllib_transport
    budget = settings.status_webhook_dispatch_budget_seconds
    started = ticker()
    claimed = 0
    outcomes: Counter[str] = Counter()
    while ticker() - started < budget:
        claim = claim_next(
            factory, lease_seconds=settings.status_webhook_lease_seconds, clock=clock
        )
        if claim is None:
            break
        claimed += 1
        try:
            outcome = process(factory, settings, claim, transport=send_with, clock=clock)
        except Exception as error:
            logger.error(
                "Processing a claimed status webhook failed; the lease expires and it is retried",
                extra={
                    "project_id": claim.project_public_id,
                    "status_version": claim.status_version,
                    "error_type": type(error).__name__,
                },
            )
            outcome = PROCESSING_FAILED
        outcomes[outcome] += 1
    return DispatchResult(claimed=claimed, outcomes=dict(outcomes))


__all__ = [
    "CONNECTION_ERROR",
    "CONTENT_TYPE",
    "DELIVERED",
    "ENCRYPTION_NOT_CONFIGURED",
    "EVENT_ID_PREFIX",
    "HTTP_STATUS",
    "NO_ACTIVE_SECRET",
    "PROCESSING_FAILED",
    "SECRET_DECRYPT_FAILED",
    "SUPERSEDED",
    "TIMEOUT",
    "Attempt",
    "Claim",
    "DispatchResult",
    "SigningUnavailable",
    "Transport",
    "attempt",
    "backoff_seconds",
    "build_request",
    "claim_next",
    "dispatch",
    "encode_payload",
    "event_id",
    "process",
    "resolve_signing",
    "send",
    "settle",
]
