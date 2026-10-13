"""ULID event IDs accepted by ai_billing_hub."""

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    value = ((time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))
