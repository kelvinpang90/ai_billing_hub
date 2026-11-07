"""ULIDs for event ids and request ids (design gate #571 v24 §2「模块与公开接口」).

与 `app/ai_api/client_rules.py` 的 `new_ulid` 同算法：48 位毫秒时间加 80 位随机，
26 个大写 Crockford base32 字符。客户端不 import 服务端代码，所以在这里另写一份。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Final

_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_ulid(
    *,
    now_ms: Callable[[], int] | None = None,
    randbits: Callable[[int], int] | None = None,
) -> str:
    """`now_ms` returns integer milliseconds; `randbits` has the signature of `secrets.randbits`."""
    value = ((now_ms or _now_ms)() << 80) | (randbits or secrets.randbits)(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


__all__ = ["new_ulid"]
