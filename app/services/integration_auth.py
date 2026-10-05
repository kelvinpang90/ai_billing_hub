"""Request signing for integrated application backends (spec §37; design gate #118 v1 §2).

本任务**不接任何端点**（摄取端点属于 Phase 2 / 3），只提供三样东西：

- `canonical_request`：§37 的五行规范化请求串；
- `verify_signature`：HMAC-SHA256 校验，外加 ±5 分钟时间窗；
- `find_verifiable_credential`：按 `api_key` + `key_version` 找出此刻可用于校验的版本。

解密交给调用方（`decrypt_secret`，带 `credential_aad` 算出的同一份 AAD）。这里不做
防重放 nonce、不写 `last_used_at`（设计 §1 的后移项，由摄取端点做：app/core/nonce.py、
app/services/usage_ingest.py）。

AIH-TASK-029（设计闸门 #176 v8 §2「凭据与缓存」）加了 `SecretCache`：只缓存**解密结果**，
键 = `(credential.id, sha256(encrypted_secret), encryption_key_version)`，进程内 LRU 1024、
TTL 300 秒。凭据行本身每个请求照读（`find_verifiable_credential` 不缓存），吊销与过期即时
生效；行的密文或主密钥版本一变，键就变，旧条目自然失效。

spec §37 没写死、由设计补的两条定义（Billing Client 与服务端必须一致，唯一出处是
docs/api.md 的「集成请求签名」）：

- `X-Acuven-Timestamp` 是 Unix 纪元秒的十进制整数：不带小数、不带正负号、不补零；
- `NORMALIZED_PATH_AND_QUERY`：路径原样；查询串按 `&` 拆开，按键、再按值的字节序排序后
  用 `&` 重新连接，各段原样保留（百分号编码不动、空值不丢）；没有查询串时不带 `?`。

⚠️ 这一层不写日志；`secret` 不进任何异常消息。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from sqlalchemy.orm import Session

from app.core.crypto import MasterKeyring, decrypt_secret
from app.models.integration import CredentialStatus, IntegrationCredential
from app.repositories import integration_access as credentials

MAX_SKEW_SECONDS: Final = 300

# 解密缓存（设计 §2「凭据与缓存」）。
SECRET_CACHE_SIZE: Final = 1024
SECRET_CACHE_TTL_SECONDS: Final = 300

# 拒绝原因。以后的端点对它们给同一个 401，这几个码只进服务端的判断。
MALFORMED_TIMESTAMP: Final = "MALFORMED_TIMESTAMP"
TIMESTAMP_OUT_OF_WINDOW: Final = "TIMESTAMP_OUT_OF_WINDOW"
BAD_SIGNATURE: Final = "BAD_SIGNATURE"

# ⚠️ 用 [0-9] 而不是 \d：\d 还认全角与其他文字的数字。最多 18 位，int() 不会溢出成
# 别的错误。
_TIMESTAMP_PATTERN: Final = re.compile(r"0|[1-9][0-9]{0,17}")
# HMAC-SHA256 输出的小写十六进制。
_SIGNATURE_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
# `key_version` 列是 INT；更大的数不可能存在，也不拿去查库。
_MAX_KEY_VERSION: Final = 2**31 - 1


class SignatureRejected(Exception):
    """The request signature did not verify. `code` is one of the three reasons above.

    消息就是问题码本身：不含 secret、签名、请求体或任何请求头的值。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def credential_aad(api_key: str, key_version: int) -> bytes:
    """The AES-GCM associated data that binds a ciphertext to its row (design §2).

    把一行的密文拷到另一行（换 key 或换版本）就解不开，`DecryptionFailed`。
    """
    return b"integration_credentials|" + api_key.encode("utf-8") + b"|" + str(key_version).encode()


def _normalized_path_and_query(path_and_query: str) -> str:
    path, _, query = path_and_query.partition("?")
    if not query:
        return path
    segments = query.split("&")

    def order(segment: str) -> tuple[bytes, bytes, bytes]:
        key, _, value = segment.partition("=")
        # 最后一项只为让「a」与「a=」这类键值都相同的段也有确定的先后。
        return key.encode("utf-8"), value.encode("utf-8"), segment.encode("utf-8")

    return path + "?" + "&".join(sorted(segments, key=order))


def canonical_request(
    method: str, path_and_query: str, timestamp: str, request_id: str, body: bytes
) -> str:
    """The five §37 lines joined by `\\n`. `timestamp` is the header value as received."""
    return "\n".join(
        (
            method.upper(),
            _normalized_path_and_query(path_and_query),
            timestamp,
            request_id,
            # 空请求体也照算：SHA256("")。
            hashlib.sha256(body).hexdigest(),
        )
    )


def sign(secret: str, canonical: str) -> str:
    """HMAC-SHA256 of the canonical string, lowercase hex. Key: the whole `sk_…` string."""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _epoch_seconds(moment: dt.datetime) -> float:
    # 本仓库的时刻是不带时区的 UTC（spec §109）。
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.timestamp()


def verify_signature(
    secret: str,
    *,
    method: str,
    path_and_query: str,
    timestamp: str,
    request_id: str,
    body: bytes,
    signature: str,
    now: dt.datetime,
    max_skew_seconds: int = MAX_SKEW_SECONDS,
) -> None:
    """Return quietly when the signature is good; otherwise raise `SignatureRejected`.

    先查时间戳格式与时间窗，再查签名。时间窗是 `|now - timestamp| <= max_skew_seconds`。
    签名不是 64 位小写十六进制时按 `BAD_SIGNATURE` 处理；比较用 `hmac.compare_digest`
    （常量时间）。
    """
    if not isinstance(timestamp, str) or _TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        raise SignatureRejected(MALFORMED_TIMESTAMP)
    if abs(_epoch_seconds(now) - int(timestamp)) > max_skew_seconds:
        raise SignatureRejected(TIMESTAMP_OUT_OF_WINDOW)
    if not isinstance(signature, str) or _SIGNATURE_PATTERN.fullmatch(signature) is None:
        raise SignatureRejected(BAD_SIGNATURE)
    expected = sign(secret, canonical_request(method, path_and_query, timestamp, request_id, body))
    if not hmac.compare_digest(expected, signature):
        raise SignatureRejected(BAD_SIGNATURE)


def is_verifiable(row: IntegrationCredential, now: dt.datetime) -> bool:
    """ACTIVE, already valid, and not yet past `valid_until` (design §4). The one definition."""
    return (
        row.status is CredentialStatus.ACTIVE
        and row.valid_from <= now
        and (row.valid_until is None or now < row.valid_until)
    )


def find_verifiable_credential(
    session: Session, api_key: str, key_version: int, now: dt.datetime
) -> IntegrationCredential | None:
    """The version usable for verification at `now`, or `None`.

    不存在、已吊销、已过期、版本号不是正整数，全部返回 `None`：以后的端点对这些情况给
    同一个 401，不让调用方区分。
    """
    if not isinstance(api_key, str) or not api_key:
        return None
    if isinstance(key_version, bool) or not isinstance(key_version, int):
        return None
    if not 1 <= key_version <= _MAX_KEY_VERSION:
        return None
    row = credentials.get_credential(session, api_key=api_key, key_version=key_version)
    if row is None or not is_verifiable(row, now):
        return None
    return row


type _CacheKey = tuple[int, str, int]


class SecretCache:
    """Decrypted secrets of credential rows, in process memory only (ADR-0004 场景 A).

    ⚠️ 值从不序列化、不进日志、不进异常；键里没有 secret。线程安全：同步端点在线程池里
    并发调用它。只缓存成功的解密，`DecryptionFailed` 照常抛出、不留条目。
    """

    def __init__(
        self,
        *,
        max_entries: int = SECRET_CACHE_SIZE,
        ttl_seconds: float = SECRET_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[_CacheKey, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key_of(row: IntegrationCredential) -> _CacheKey:
        digest = hashlib.sha256(row.encrypted_secret.encode("utf-8")).hexdigest()
        return (row.id, digest, row.encryption_key_version)

    def secret_for(self, row: IntegrationCredential, keyring: Callable[[], MasterKeyring]) -> str:
        """The plaintext secret of `row`; decrypts (loading the keyring) only on a miss."""
        key = self.key_of(row)
        now = self._clock()
        with self._lock:
            found = self._entries.get(key)
            if found is not None and found[0] > now:
                self._entries.move_to_end(key)
                return found[1]
            if found is not None:
                del self._entries[key]
        # 解密不在锁里：主密钥文件的读取与 AES 不该让别的请求排队。同一个键并发未命中时
        # 各解密一次，结果相同，后写的覆盖先写的。
        secret = decrypt_secret(
            keyring(),
            row.encrypted_secret,
            associated_data=credential_aad(row.public_api_key, row.key_version),
        )
        with self._lock:
            self._entries[key] = (now + self._ttl, secret)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return secret

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "BAD_SIGNATURE",
    "MALFORMED_TIMESTAMP",
    "MAX_SKEW_SECONDS",
    "SECRET_CACHE_SIZE",
    "SECRET_CACHE_TTL_SECONDS",
    "TIMESTAMP_OUT_OF_WINDOW",
    "SecretCache",
    "SignatureRejected",
    "canonical_request",
    "credential_aad",
    "find_verifiable_credential",
    "is_verifiable",
    "sign",
    "verify_signature",
]
