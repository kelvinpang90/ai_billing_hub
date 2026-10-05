"""Usage event ingestion: signature, replay protection, durable 202 and global idempotency.

设计闸门 #176 v8（docs/design/AIH-TASK-029-usage-ingest.md）。三个入口，端点按顺序调用：

1. `authenticate`：解析五个 `X-Acuven-*` 请求头 → 每个请求都读一次凭据行（吊销即时生效）
   → 解密 secret（进程内缓存）→ 用**原始请求体**验签 → Redis nonce（不可用放行）。
   任何一步不过都是同一个 401 `INTEGRATION_AUTH_FAILED`，具体原因只进服务端日志。
2. `ingest_one`：一个事件的校验、归属比对、指纹与落库。批量端点（AIH-TASK-030）逐个复用它。
3. `record_use`：响应之后、独立短事务里节流地写 `last_used_at`，失败只记日志。

批量端点（设计闸门 #180 v3）在 `authenticate` 之后调用 `parse_batch`（顶层结构与条数上限）与
`ingest_batch`：按数组顺序逐条 `ingest_item` —— 即 `ingest_one` 加上「错误变成这一条的结果、
不抛出」—— 每条各自一个事务，整批共用请求层取的那一个 `now`。

**校验顺序**（决定一个请求报哪个码，设计 §2）：① 请求体大小（端点）→ ② 签名与凭据 →
③ nonce → ④ JSON 顶层结构与各字段的类型、格式 → ⑤ `schema_version` → ⑥ 查 `usage_type`
（不在表里即 409 `UNKNOWN_USAGE_TYPE`，不再往下）→ ⑦ 按该类型的形态校验字段组、`unit`、
`quantity_kind` → ⑧ `occurred_at` 的未来上界 → ⑨ 归属比对。

**事务边界**（设计 §2）：

- 新事件：一个事务 `INSERT usage_events` → commit，之后调用方才回 202（REQ-INGEST-001）；
- 撞 `event_id` 唯一约束：回滚，新事务读已有行。归属（租户、项目、`api_key`）与指纹都一致
  → 合法重复（200）；否则 → 冲突行（去重）+ 审计同一事务（INV-13）→ 409，原事件不变；
- 归属不符：不插入，独立事务写审计 → 403。

⚠️ **归属只来自凭据行**：载荷里的 `tenant_id` / `project_id` 只作比对。按 `api_key` 而不是
凭据行比对归属：轮换重叠期内用新 `key_version` 重试同一事件是合法重复（设计 §2、§9）。

⚠️ 不计费、不入队、不发 Celery 任务：T-H 从 `(status, occurred_at)` 扫描 `RECEIVED`。

⚠️ **日志里没有请求体、签名、secret、解密结果**；认证失败的原因码只进日志、不进响应。
日志绑定 `tenant_id`、`project_id`、`event_id`；请求头的 Request-Id 记作
`integration_request_id`。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NoReturn

from pydantic import ValidationError
from sqlalchemy import exc as sa_exc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import EncryptionNotConfigured, load_keyring
from app.core.database import session_scope
from app.core.errors import AppError
from app.core.logging import bind_log_context, clear_log_context, get_log_context
from app.core.nonce import NonceStore, NonceStoreUnavailable
from app.models.ai_catalog import TOKEN_FIELDS, PayloadShape, QuantityKind, UsageMeterType
from app.models.auth import AuditAction
from app.models.usage import ConflictMismatch, UsageEventStatus
from app.repositories import integration_access as credentials
from app.repositories import usage_events
from app.schemas.usage_ingest import (
    REQUEST_ID_PATTERN,
    SCHEMA_VERSION,
    UsageEventBatch,
    UsageEventBatchItem,
    UsageEventBatchReceipt,
    UsageEventPayload,
    UsageEventReceipt,
)
from app.services.auth import RequestContext, record_audit
from app.services.integration_auth import (
    SecretCache,
    SignatureRejected,
    find_verifiable_credential,
    verify_signature,
)

logger = logging.getLogger(__name__)

# 审计的 entity_type。归属不符记在凭据上（entity_id 是 api_key），冲突记在原事件上
# （entity_id 是原事件的 public_id）。
ENTITY_INTEGRATION_CREDENTIAL: Final = "integration_credential"
ENTITY_USAGE_EVENT: Final = "usage_event"

# 应用层单条上限（设计 §2）。nginx 对集成前缀另有 1 MiB 的上限与同一个 JSON 413。
# 批量端点对每个元素用同一个上限（按元素重新序列化后的长度）；整批的上限是配置项
# `ingest_batch_max_bytes`。
MAX_BODY_BYTES: Final = 16 * 1024
# `occurred_at` 最多晚于服务端当前时刻 300 秒；没有过去的下限（晚到事件是合法的）。
FUTURE_TOLERANCE: Final = dt.timedelta(seconds=300)
# `last_used_at` 每个凭据行每 60 秒至多写一次。
LAST_USED_INTERVAL: Final = dt.timedelta(seconds=60)

# 请求头（docs/api.md「集成请求签名」）。
HEADER_API_KEY: Final = "X-Acuven-Api-Key"
HEADER_KEY_VERSION: Final = "X-Acuven-Key-Version"
HEADER_TIMESTAMP: Final = "X-Acuven-Timestamp"
HEADER_REQUEST_ID: Final = "X-Acuven-Request-Id"
HEADER_SIGNATURE: Final = "X-Acuven-Signature"

# 响应 `data.status`。
STATUS_ACCEPTED: Final = "accepted"
STATUS_ALREADY_RECEIVED: Final = "already_received"
STATUS_ALREADY_PROCESSED: Final = "already_processed"
# 只出现在批量响应的逐条结果里：错误表里的任何一个码（含可重试的）。
STATUS_REJECTED: Final = "rejected"

# 认证失败的原因码：只进服务端日志（签名库自己的三个码之外的部分）。
_MISSING_HEADER: Final = "MISSING_HEADER"
_MALFORMED_HEADER: Final = "MALFORMED_HEADER"
_CREDENTIAL_UNUSABLE: Final = "CREDENTIAL_UNUSABLE"
_REPLAYED_REQUEST_ID: Final = "REPLAYED_REQUEST_ID"

# ⚠️ `fullmatch` 而不是 `$`：Python 的 `$` 在末尾换行之前也算匹配。
_REQUEST_ID_RE: Final = re.compile(REQUEST_ID_PATTERN.removeprefix("^").removesuffix("$"))
# `api_key` 的列宽是 64（nonce 键长的上界也按它算）。
_API_KEY_RE: Final = re.compile(r"[A-Za-z0-9_]{1,64}")
# 十进制正整数，不补零。上界由签名库判（INT 列放不下的版本号不拿去查库）。
_KEY_VERSION_RE: Final = re.compile(r"[1-9][0-9]{0,18}")

_QUANTUM: Final = Decimal("0.00000001")


# --- 错误（设计 §2 错误表：每个码的 HTTP 状态与 retryable 固定） ---------------------


class IntegrationAuthFailed(AppError):
    """Missing / malformed headers, unusable credential, bad signature, skew, replay — one 401.

    ⚠️ 不可重试：每一个原因都是集成方的配置或实现问题，原样重试永远不会成功（设计 §2）。
    """

    retryable = False

    def __init__(self) -> None:
        super().__init__(
            "The request could not be authenticated.",
            code="INTEGRATION_AUTH_FAILED",
            http_status=401,
        )


class CredentialScopeMismatch(AppError):
    retryable = False

    def __init__(self) -> None:
        super().__init__(
            "tenant_id or project_id does not match the credential.",
            code="CREDENTIAL_SCOPE_MISMATCH",
            http_status=403,
        )


class IdempotencyConflict(AppError):
    """Same `event_id`, different owner or fingerprint. Says nothing about the existing event."""

    retryable = False

    def __init__(self) -> None:
        super().__init__(
            "This event_id was already used for a different event.",
            code="IDEMPOTENCY_CONFLICT",
            http_status=409,
        )


class UnknownUsageType(AppError):
    """`usage_type` is not a meter type *yet*: retryable, the admin may create it (design §2)."""

    retryable = True

    def __init__(self) -> None:
        super().__init__(
            "usage_type is not a known meter type.",
            code="UNKNOWN_USAGE_TYPE",
            http_status=409,
        )


class PayloadTooLarge(AppError):
    retryable = False

    def __init__(self) -> None:
        # 文案与 nginx 的 JSON 413 相同（deploy/nginx/billing.conf）。
        super().__init__("request body too large", code="PAYLOAD_TOO_LARGE", http_status=413)


class UsageValidationError(AppError):
    """422 that names the offending fields and never echoes their values."""

    retryable = False

    def __init__(self, *fields: str) -> None:
        names = ", ".join(sorted(set(fields)))
        message = f"Invalid request fields: {names}" if names else "Invalid request"
        super().__init__(message, code="VALIDATION_ERROR", http_status=422)


class UnsupportedSchemaVersion(AppError):
    retryable = False

    def __init__(self) -> None:
        super().__init__(
            f"schema_version must be {SCHEMA_VERSION}.",
            code="UNSUPPORTED_SCHEMA_VERSION",
            http_status=422,
        )


class OccurredAtInFuture(AppError):
    retryable = False

    def __init__(self) -> None:
        super().__init__(
            "occurred_at is more than 300 seconds in the future.",
            code="OCCURRED_AT_IN_FUTURE",
            http_status=422,
        )


class BatchTooLarge(AppError):
    """More events than `BILLING_INGEST_BATCH_MAX` (design gate #180 v3 §2 ④). Nothing written."""

    retryable = False

    def __init__(self, max_events: int) -> None:
        super().__init__(
            f"A batch holds at most {max_events} events.",
            code="BATCH_TOO_LARGE",
            http_status=422,
        )


class ServiceUnavailable(AppError):
    """The database (or the master key file) is not available. Retryable."""

    retryable = True

    def __init__(self) -> None:
        super().__init__(
            "The service is temporarily unavailable.",
            code="SERVICE_UNAVAILABLE",
            http_status=503,
        )


# --- 数据 ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedRequest:
    """What the signature covers, plus the five headers as received."""

    method: str
    path_and_query: str
    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class AuthenticatedCredential:
    """The credential row a request was verified with. Ownership comes only from here."""

    id: int
    tenant_id: int
    project_id: int
    api_key: str
    key_version: int


@dataclass(frozen=True)
class IngestOutcome:
    event_id: str
    status: str
    processing_status: str

    @property
    def http_status(self) -> int:
        return 202 if self.status == STATUS_ACCEPTED else 200

    def receipt(self) -> UsageEventReceipt:
        return UsageEventReceipt(
            event_id=self.event_id,
            status=self.status,
            processing_status=self.processing_status,
        )


@dataclass(frozen=True)
class ItemOutcome:
    """One batch element's result (design gate #180 v3 §2). Never an exception."""

    event_id: str | None
    status: str
    processing_status: str | None
    error_code: str | None
    retryable: bool

    def item(self, index: int) -> UsageEventBatchItem:
        return UsageEventBatchItem(
            index=index,
            event_id=self.event_id,
            status=self.status,
            processing_status=self.processing_status,
            error_code=self.error_code,
            retryable=self.retryable,
        )


@dataclass(frozen=True)
class BatchOutcome:
    """Every element's result, in request order, and the three counts."""

    items: tuple[ItemOutcome, ...]

    @property
    def accepted(self) -> int:
        return sum(1 for item in self.items if item.status == STATUS_ACCEPTED)

    @property
    def duplicates(self) -> int:
        duplicate = {STATUS_ALREADY_RECEIVED, STATUS_ALREADY_PROCESSED}
        return sum(1 for item in self.items if item.status in duplicate)

    @property
    def rejected(self) -> int:
        return sum(1 for item in self.items if item.status == STATUS_REJECTED)

    def receipt(self) -> UsageEventBatchReceipt:
        return UsageEventBatchReceipt(
            accepted=self.accepted,
            duplicates=self.duplicates,
            rejected=self.rejected,
            results=[item.item(index) for index, item in enumerate(self.items)],
        )


@dataclass(frozen=True)
class _Event:
    """A payload past steps ④ and ⑤: types and formats checked, times and decimals parsed."""

    payload: UsageEventPayload
    occurred_at: dt.datetime
    quantity: Decimal | None

    @property
    def event_id(self) -> str:
        return self.payload.event_id


@dataclass(frozen=True)
class _Measures:
    """Step ⑦'s result: what the row stores for the shape of its meter type."""

    tokens: dict[str, int] | None
    quantity: Decimal | None
    unit: str


class _ScopeMismatch(Exception):
    def __init__(self, tenant_public_id: str, project_public_id: str) -> None:
        super().__init__("scope mismatch")
        self.tenant_public_id = tenant_public_id
        self.project_public_id = project_public_id


# --- 数据库不可用 → 503 -------------------------------------------------------------


@contextmanager
def _database_errors() -> Iterator[None]:
    """Connection-level database failures become 503 `SERVICE_UNAVAILABLE`.

    其余异常（包括提交时的其他错误）原样上抛，由全局处理器回 500 `INTERNAL_ERROR`；
    两者都可重试。异常原文只进日志（引擎开了 `hide_parameters`）。
    """
    try:
        yield
    except (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError):
        logger.exception("Database unavailable while ingesting a usage event")
        raise ServiceUnavailable from None


# --- ② ③ 认证 -----------------------------------------------------------------------


def _reject(reason: str) -> NoReturn:
    logger.warning("Integration request rejected", extra={"reason": reason})
    raise IntegrationAuthFailed


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None or value == "":
        _reject(_MISSING_HEADER)
    return value


def authenticate(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    secret_cache: SecretCache,
    nonce_store: NonceStore,
    request: SignedRequest,
    now: dt.datetime,
) -> AuthenticatedCredential:
    """Steps ② and ③. Returns the verified credential or raises the one 401.

    顺序照设计 §2：请求头格式 → 凭据行（每个请求都读，不加锁）→ 解密（缓存）→ 验签（时间窗
    在里面，出窗的不查 Redis）→ nonce。`now` 是不带时区的 UTC。
    """
    headers = request.headers
    api_key = _header(headers, HEADER_API_KEY)
    key_version_text = _header(headers, HEADER_KEY_VERSION)
    timestamp = _header(headers, HEADER_TIMESTAMP)
    request_id = _header(headers, HEADER_REQUEST_ID)
    signature = _header(headers, HEADER_SIGNATURE)
    if _REQUEST_ID_RE.fullmatch(request_id) is None:
        _reject(_MALFORMED_HEADER)
    # 只有格式合法的 Request-Id 才进日志（挡住换行注入）。
    bind_log_context(integration_request_id=request_id)
    if _API_KEY_RE.fullmatch(api_key) is None:
        _reject(_MALFORMED_HEADER)
    if _KEY_VERSION_RE.fullmatch(key_version_text) is None:
        _reject(_MALFORMED_HEADER)
    key_version = int(key_version_text)

    with _database_errors(), session_factory() as session:
        row = find_verifiable_credential(session, api_key, key_version, now)
        if row is None:
            _reject(_CREDENTIAL_UNUSABLE)
        try:
            secret = secret_cache.secret_for(row, lambda: load_keyring(settings))
        except EncryptionNotConfigured:
            logger.error("No master key: integration requests cannot be verified")
            raise ServiceUnavailable from None
        credential = AuthenticatedCredential(
            id=row.id,
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            api_key=row.public_api_key,
            key_version=row.key_version,
        )

    try:
        verify_signature(
            secret,
            method=request.method,
            path_and_query=request.path_and_query,
            timestamp=timestamp,
            request_id=request_id,
            body=request.body,
            signature=signature,
            now=now,
        )
    except SignatureRejected as rejected:
        _reject(rejected.code)
    bind_log_context(tenant_id=credential.tenant_id, project_id=credential.project_id)

    try:
        first_use = nonce_store.claim(credential.api_key, request_id, int(timestamp))
    except NonceStoreUnavailable as unavailable:
        # 放行（设计 §2「防重放」）：财务安全网是 event_id 唯一约束。
        logger.warning("nonce_store_unavailable", extra={"reason": str(unavailable)})
        return credential
    if not first_use:
        _reject(_REPLAYED_REQUEST_ID)
    return credential


# --- ④ ⑤ 结构、类型与版本 -----------------------------------------------------------


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"{value} is not a JSON number this endpoint accepts")


def parse_json_object(body: bytes) -> object:
    """Step ④'s syntax half: strict UTF-8 JSON. `NaN` / `Infinity` are refused."""
    try:
        text = body.decode("utf-8")
        return json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        # json.JSONDecodeError 是 ValueError 的子类。不回显内容。
        raise UsageValidationError("body") from None


def _validation_fields(error: ValidationError) -> list[str]:
    fields = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        fields.append(f"body.{location}" if location else "body")
    return fields


def _utc(occurred_at: str) -> dt.datetime:
    """RFC 3339 with an offset → naive UTC. The pattern already held; the calendar may not."""
    try:
        moment = dt.datetime.fromisoformat(occurred_at)
    except ValueError:
        raise UsageValidationError("body.occurred_at") from None
    if moment.tzinfo is None:
        raise UsageValidationError("body.occurred_at")
    return moment.astimezone(dt.UTC).replace(tzinfo=None)


def _validate(payload: object) -> _Event:
    try:
        parsed = UsageEventPayload.model_validate(payload, strict=True)
    except ValidationError as error:
        raise UsageValidationError(*_validation_fields(error)) from None
    occurred_at = _utc(parsed.occurred_at)
    # 正则已保证是 ≤ 12 位整数、≤ 8 位小数的十进制串：Decimal 精确解析，不经过 float。
    quantity = Decimal(parsed.quantity) if parsed.quantity is not None else None
    if parsed.schema_version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion
    return _Event(payload=parsed, occurred_at=occurred_at, quantity=quantity)


# --- ⑦ ⑧ 形态与时间 -----------------------------------------------------------------


def _measures(event: _Event, meter: UsageMeterType) -> _Measures:
    """Step ⑦: the field group of the meter type's shape, its `unit` and `quantity_kind`."""
    payload = event.payload
    present = payload.model_fields_set
    bad: list[str] = []
    if meter.payload_shape is PayloadShape.LLM_TOKEN_FIELDS:
        tokens: dict[str, int] = {}
        for name in TOKEN_FIELDS:
            value = getattr(payload, name)
            if value is None:
                bad.append(f"body.{name}")
            else:
                tokens[name] = value
        bad.extend(f"body.{name}" for name in ("quantity", "unit") if name in present)
        if bad:
            raise UsageValidationError(*bad)
        # 这类类型的单位必然是 TOKEN（025 的 CHECK）；存类型的单位，外键兜底。
        return _Measures(tokens=tokens, quantity=None, unit=meter.unit)

    bad.extend(f"body.{name}" for name in TOKEN_FIELDS if name in present)
    fractional = payload.quantity is not None and "." in payload.quantity
    if event.quantity is None:
        bad.append("body.quantity")
    elif meter.quantity_kind is QuantityKind.INTEGER and fractional:
        # 整数类型不许有小数部分，`"3.0"` 也拒绝。
        bad.append("body.quantity")
    if payload.unit != meter.unit:
        # 逐字相等（区分大小写）。缺失同样落在这里。
        bad.append("body.unit")
    if bad:
        raise UsageValidationError(*bad)
    return _Measures(tokens=None, quantity=event.quantity, unit=meter.unit)


def _check_not_in_future(event: _Event, now: dt.datetime) -> None:
    """Step ⑧: at most 300 seconds after the server's clock; no lower bound."""
    if event.occurred_at > now + FUTURE_TOLERANCE:
        raise OccurredAtInFuture


# --- 指纹 ---------------------------------------------------------------------------


def payload_fingerprint(event: _Event, measures: _Measures) -> str:
    """SHA-256 hex of the canonical object (design §2「规范化与指纹」).

    键排序、无空白、UTF-8。不含诊断用的 `tenant_id` / `project_id`。语义相同、写法不同的
    两次请求（`"37.42"` 与 `"37.420"`、`…Z` 与等价的 `+08:00`）得到同一个指纹。
    """
    payload = event.payload
    canonical: dict[str, Any] = {
        "schema_version": payload.schema_version,
        "event_id": payload.event_id,
        "request_id": payload.request_id,
        "conversation_id": payload.conversation_id,
        "provider": payload.provider,
        "model": payload.model,
        "usage_type": payload.usage_type,
        "occurred_at": event.occurred_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    if measures.tokens is not None:
        canonical.update(measures.tokens)
    else:
        assert measures.quantity is not None
        canonical["quantity"] = format(measures.quantity.quantize(_QUANTUM), "f")
        canonical["unit"] = measures.unit
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- ⑨ 归属 -------------------------------------------------------------------------


def _check_scope(session: Session, credential: AuthenticatedCredential, event: _Event) -> None:
    """Step ⑨: the diagnostic ids, when given, must be the credential's own public ids."""
    given_tenant, given_project = event.payload.tenant_id, event.payload.project_id
    if given_tenant is None and given_project is None:
        return
    tenant_public_id, project_public_id = usage_events.scope_public_ids(
        session, tenant_id=credential.tenant_id, project_id=credential.project_id
    )
    tenant_differs = given_tenant is not None and given_tenant != tenant_public_id
    project_differs = given_project is not None and given_project != project_public_id
    if tenant_differs or project_differs:
        raise _ScopeMismatch(tenant_public_id, project_public_id)


def _audit_scope_mismatch(
    session_factory: sessionmaker[Session],
    credential: AuthenticatedCredential,
    event: _Event,
    mismatch: _ScopeMismatch,
    *,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    """The independent transaction of a 403 (先例：登录失败的审计单独提交)."""
    with session_scope(session_factory) as session:
        record_audit(
            session,
            action=AuditAction.USAGE_EVENT_SCOPE_MISMATCH,
            context=context,
            now=now,
            entity_type=ENTITY_INTEGRATION_CREDENTIAL,
            entity_id=credential.api_key,
            # ⚠️ 字段白名单（设计 §2「审计」）：不含载荷的其余内容。
            after_state={
                "key_version": credential.key_version,
                "event_id": event.event_id,
                "payload_tenant_id": event.payload.tenant_id,
                "payload_project_id": event.payload.project_id,
                "tenant_public_id": mismatch.tenant_public_id,
                "project_public_id": mismatch.project_public_id,
            },
        )


# --- 撞唯一约束之后 -----------------------------------------------------------------


def _mismatch(
    credential: AuthenticatedCredential,
    *,
    tenant_id: int,
    project_id: int,
    api_key: str,
    same_fingerprint: bool,
) -> ConflictMismatch | None:
    same_owner = (
        tenant_id == credential.tenant_id
        and project_id == credential.project_id
        and api_key == credential.api_key
    )
    if same_owner and same_fingerprint:
        return None
    if same_owner:
        return ConflictMismatch.FINGERPRINT
    if same_fingerprint:
        return ConflictMismatch.OWNERSHIP
    return ConflictMismatch.BOTH


def _resolve_existing(
    session_factory: sessionmaker[Session],
    credential: AuthenticatedCredential,
    event: _Event,
    fingerprint: str,
    *,
    context: RequestContext,
    now: dt.datetime,
) -> IngestOutcome | None:
    """After the insert lost to `uq_usage_events_event_id`: a duplicate, or a conflict (raises).

    返回 `None` = 库里没有这个 event_id，说明撞的不是它（调用方把原异常抛出去，500）。
    """
    try:
        with session_scope(session_factory) as session:
            found = usage_events.find_event(session, event.event_id)
            if found is None:
                return None
            existing, existing_api_key = found
            mismatch = _mismatch(
                credential,
                tenant_id=existing.tenant_id,
                project_id=existing.project_id,
                api_key=existing_api_key,
                same_fingerprint=existing.payload_fingerprint == fingerprint,
            )
            if mismatch is None:
                # 合法重复：这个事务只读，提交是空操作。
                status = UsageEventStatus(existing.status)
                processed = status is UsageEventStatus.PROCESSED
                return IngestOutcome(
                    event_id=event.event_id,
                    status=STATUS_ALREADY_PROCESSED if processed else STATUS_ALREADY_RECEIVED,
                    processing_status=status.value,
                )
            dedupe = {
                "event_id": event.event_id,
                "api_key": credential.api_key,
                "payload_fingerprint": fingerprint,
            }
            if not usage_events.conflict_recorded(session, **dedupe):
                usage_events.insert_conflict(
                    session,
                    usage_event_id=existing.id,
                    integration_credential_id=credential.id,
                    mismatch=mismatch,
                    now=now,
                    **dedupe,
                )
                # 冲突行与审计同一事务（INV-13）：审计写失败，冲突行一起回滚。
                record_audit(
                    session,
                    action=AuditAction.USAGE_EVENT_IDEMPOTENCY_CONFLICT,
                    context=context,
                    now=now,
                    entity_type=ENTITY_USAGE_EVENT,
                    entity_id=existing.public_id,
                    # ⚠️ 不含载荷内容与指纹以外的数据（设计 §2「审计」）。
                    after_state={
                        "event_id": event.event_id,
                        "api_key": credential.api_key,
                        "key_version": credential.key_version,
                        "mismatch": mismatch.value,
                    },
                )
    except IntegrityError:
        # 并发的同一个冲突请求先提交了：去重约束兜底，这一次不写第二条审计。
        # 确认那一行真的在；不在说明撞的是别的约束，照常 500。
        with session_factory() as session:
            if not usage_events.conflict_recorded(
                session,
                event_id=event.event_id,
                api_key=credential.api_key,
                payload_fingerprint=fingerprint,
            ):
                raise
    raise IdempotencyConflict


# --- 一个事件 -----------------------------------------------------------------------


def ingest_one(
    session_factory: sessionmaker[Session],
    credential: AuthenticatedCredential,
    payload: object,
    *,
    context: RequestContext,
    now: dt.datetime,
) -> IngestOutcome:
    """Steps ④ to ⑨ and the insert for one parsed JSON value. Reused by the batch endpoint.

    返回时事件行（新事件）已经提交；合法重复返回已有行的状态。其余情形抛对应的 `AppError`。
    `now` 是不带时区的 UTC（`received_at` / `created_at` 取它的整秒）。
    """
    event = _validate(payload)
    bind_log_context(event_id=event.event_id)
    moment = now.replace(microsecond=0)
    fingerprint = ""
    try:
        with _database_errors(), session_scope(session_factory) as session:
            meter = usage_events.find_meter_type(session, event.payload.usage_type)
            if meter is None:
                raise UnknownUsageType
            measures = _measures(event, meter)
            _check_not_in_future(event, now)
            _check_scope(session, credential, event)
            fingerprint = payload_fingerprint(event, measures)
            usage_events.insert_event(
                session,
                event_id=event.event_id,
                schema_version=event.payload.schema_version,
                tenant_id=credential.tenant_id,
                project_id=credential.project_id,
                integration_credential_id=credential.id,
                request_id=event.payload.request_id,
                conversation_id=event.payload.conversation_id,
                provider_code_raw=event.payload.provider,
                model_code_raw=event.payload.model,
                usage_meter_type_id=meter.id,
                payload_shape=meter.payload_shape,
                quantity_kind=meter.quantity_kind,
                unit=measures.unit,
                tokens=measures.tokens,
                quantity=measures.quantity,
                payload_fingerprint=fingerprint,
                occurred_at=event.occurred_at,
                now=moment,
            )
    except _ScopeMismatch as mismatch:
        with _database_errors():
            _audit_scope_mismatch(
                session_factory, credential, event, mismatch, context=context, now=moment
            )
        raise CredentialScopeMismatch from None
    except IntegrityError:
        if not fingerprint:
            raise
        with _database_errors():
            outcome = _resolve_existing(
                session_factory, credential, event, fingerprint, context=context, now=moment
            )
        if outcome is None:
            raise
        return outcome
    return IngestOutcome(
        event_id=event.event_id,
        status=STATUS_ACCEPTED,
        processing_status=UsageEventStatus.RECEIVED.value,
    )


# --- 批量（设计闸门 #180 v3，AIH-TASK-030） ---------------------------------------------


def parse_batch(body: bytes, *, max_events: int) -> list[object]:
    """Step ④ of a batch: `{"events": [ … ]}`, non-empty, at most `max_events` elements.

    结构不合法 422 `VALIDATION_ERROR`，条数超限 422 `BATCH_TOO_LARGE`，都在写任何事件之前。
    元素原样返回：每一个都交给 `ingest_item`，按单条端点的同一套规则校验。
    """
    try:
        batch = UsageEventBatch.model_validate(parse_json_object(body), strict=True)
    except ValidationError as error:
        raise UsageValidationError(*_validation_fields(error)) from None
    if len(batch.events) > max_events:
        raise BatchTooLarge(max_events)
    return batch.events


def _event_id_of(raw_event: object) -> str | None:
    """The element's `event_id` string, or `None` when there is none to give back."""
    if isinstance(raw_event, dict):
        event_id = raw_event.get("event_id")
        if isinstance(event_id, str):
            return event_id
    return None


def _encoded_size(raw_event: object) -> int:
    """The element re-serialised compactly (design §2「请求体上限」): its bytes in UTF-8."""
    text = json.dumps(raw_event, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8"))


def _retryable_of(error: AppError) -> bool:
    # 单条端点的错误类都显式写了 retryable；没写的按全局规则：5xx 可重试
    # （app/core/errors.py）。
    if error.retryable is not None:
        return error.retryable
    return error.http_status >= 500


def ingest_item(
    session_factory: sessionmaker[Session],
    credential: AuthenticatedCredential,
    raw_event: object,
    *,
    context: RequestContext,
    now: dt.datetime,
) -> ItemOutcome:
    """One element of a batch: `ingest_one` with its error turned into a result, never raised.

    设计 §2「单条处理的复用」：批里的一条与单独发给单条端点结果相同 —— 同一个 `ingest_one`、
    同一张错误表的码与 `retryable`；数据库不可用（`SERVICE_UNAVAILABLE`）同样只是这一条的
    结果。错误表以外的异常（未预期的 500）照常上抛：整批 500、可重试，
    已提交的条保留（设计 §5）。
    """
    event_id = _event_id_of(raw_event)
    try:
        if _encoded_size(raw_event) > MAX_BODY_BYTES:
            # 单条端点的第 ① 步，对批里的每个元素同样适用。
            raise PayloadTooLarge
        outcome = ingest_one(session_factory, credential, raw_event, context=context, now=now)
    except AppError as error:
        return ItemOutcome(
            event_id=event_id,
            status=STATUS_REJECTED,
            processing_status=None,
            error_code=error.code,
            retryable=_retryable_of(error),
        )
    return ItemOutcome(
        event_id=outcome.event_id,
        status=outcome.status,
        processing_status=outcome.processing_status,
        error_code=None,
        retryable=False,
    )


def ingest_batch(
    session_factory: sessionmaker[Session],
    credential: AuthenticatedCredential,
    raw_events: list[object],
    *,
    context: RequestContext,
    now: dt.datetime,
) -> BatchOutcome:
    """Every element in request order, each in its own transaction; one `now` for all of them.

    ⚠️ 没有外层事务、不预先去重（设计 §2）：批内重复的第二条走单条的冲突路径，与单独发一致；
    某条失败（包括数据库不可用）不影响已提交的条，后面的条照常尝试。
    ⚠️ 日志只记条数与汇总，不记请求体（设计 §6）。
    """
    base_context = get_log_context()
    items: list[ItemOutcome] = []
    try:
        for raw_event in raw_events:
            # `ingest_one` 把 event_id 绑进日志上下文：每条从请求层的上下文重新开始，
            # 取不到 event_id 的那条不会带着上一条的。
            clear_log_context()
            bind_log_context(**base_context)
            items.append(
                ingest_item(session_factory, credential, raw_event, context=context, now=now)
            )
    finally:
        clear_log_context()
        bind_log_context(**base_context)
    outcome = BatchOutcome(items=tuple(items))
    logger.info(
        "Usage event batch processed",
        extra={
            "events": len(items),
            "accepted": outcome.accepted,
            "duplicates": outcome.duplicates,
            "rejected": outcome.rejected,
        },
    )
    return outcome


# --- ⑩ last_used_at -----------------------------------------------------------------


def record_use(
    session_factory: sessionmaker[Session],
    credential_id: int,
    now: dt.datetime,
    *,
    min_interval: dt.timedelta = LAST_USED_INTERVAL,
) -> None:
    """Best effort, after the response: a short transaction of its own; failures only logged."""
    try:
        with session_scope(session_factory) as session:
            credentials.touch_last_used(
                session,
                credential_id=credential_id,
                now=now.replace(microsecond=0),
                min_interval=min_interval,
            )
    except Exception:
        logger.warning("Updating last_used_at failed", exc_info=True)


__all__ = [
    "ENTITY_INTEGRATION_CREDENTIAL",
    "ENTITY_USAGE_EVENT",
    "FUTURE_TOLERANCE",
    "HEADER_API_KEY",
    "HEADER_KEY_VERSION",
    "HEADER_REQUEST_ID",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "LAST_USED_INTERVAL",
    "MAX_BODY_BYTES",
    "STATUS_ACCEPTED",
    "STATUS_ALREADY_PROCESSED",
    "STATUS_ALREADY_RECEIVED",
    "STATUS_REJECTED",
    "AuthenticatedCredential",
    "BatchOutcome",
    "BatchTooLarge",
    "CredentialScopeMismatch",
    "IdempotencyConflict",
    "IngestOutcome",
    "IntegrationAuthFailed",
    "ItemOutcome",
    "OccurredAtInFuture",
    "PayloadTooLarge",
    "ServiceUnavailable",
    "SignedRequest",
    "UnknownUsageType",
    "UnsupportedSchemaVersion",
    "UsageValidationError",
    "authenticate",
    "ingest_batch",
    "ingest_item",
    "ingest_one",
    "parse_batch",
    "parse_json_object",
    "payload_fingerprint",
    "record_use",
]
