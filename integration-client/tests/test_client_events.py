"""Event ids, validation and serialization (design gate #571 v24 §2「事件」, §7).

规则照服务端 `app/schemas/usage_ingest.py`；这里不 import 服务端，期望值是写死的字面值。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from acuven_billing_client import build_llm_event, build_quantity_event, new_ulid

NOW = datetime(2026, 11, 7, 8, 30, 0, tzinfo=UTC)
ULID = "01JBZX6Q0000000000000000AB"
UUID7 = "01890a5d-ac96-774b-bcce-b302099a8057"
ULID_PATTERN = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")


def fixed_id() -> str:
    return ULID


LLM = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "usage_type": "LLM_TOKEN",
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 5,
    "request_id": "req_1",
    "now": NOW,
    "new_id": fixed_id,
}

QUANTITY = {
    "provider": "openai",
    "model": "whisper-1",
    "usage_type": "AUDIO_SECONDS",
    "quantity": Decimal("12.5"),
    "unit": "second",
    "request_id": "req_2",
    "now": NOW,
    "new_id": fixed_id,
}

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def llm(**changes: object) -> tuple[str, bytes]:
    return build_llm_event(**{**LLM, **changes})  # type: ignore[arg-type]


def quantity(**changes: object) -> tuple[str, bytes]:
    return build_quantity_event(**{**QUANTITY, **changes})  # type: ignore[arg-type]


def llm_payload(**changes: object) -> dict:
    return json.loads(llm(**changes)[1])


def quantity_payload(**changes: object) -> dict:
    return json.loads(quantity(**changes)[1])


# --- new_ulid --------------------------------------------------------------------


def test_new_ulid_shape() -> None:
    for _ in range(100):
        assert ULID_PATTERN.fullmatch(new_ulid())


def test_new_ulid_with_injected_sources() -> None:
    assert new_ulid(now_ms=lambda: 0, randbits=lambda bits: 0) == "0" * 26
    assert new_ulid(now_ms=lambda: 0, randbits=lambda bits: 2**bits - 1) == "0" * 10 + "Z" * 16
    assert new_ulid(now_ms=lambda: 2**48 - 1, randbits=lambda bits: 0) == "7" + "Z" * 9 + "0" * 16


def test_new_ulid_layout_is_48_bit_time_then_80_bit_random() -> None:
    requested: list[int] = []

    def randbits(bits: int) -> int:
        requested.append(bits)
        return 1

    value = new_ulid(now_ms=lambda: 1, randbits=randbits)
    assert requested == [80]
    # 时间 1 落在第 80 位：第 10 个字符（每个字符 5 位，前 10 个字符共 50 位，含 2 位填充）。
    assert value == "0000000001" + "0" * 15 + "1"


def test_new_ulid_orders_by_time() -> None:
    earlier = new_ulid(now_ms=lambda: 1_790_325_000_000, randbits=lambda bits: 2**bits - 1)
    later = new_ulid(now_ms=lambda: 1_790_325_000_001, randbits=lambda bits: 0)
    assert earlier < later


# --- 返回值与载荷形态 ---------------------------------------------------------------


def test_llm_event_payload() -> None:
    event_id, payload = llm()
    assert event_id == ULID
    assert payload == (
        b'{"cache_creation_input_tokens":0,"cache_read_input_tokens":5,'
        b'"event_id":"01JBZX6Q0000000000000000AB","input_tokens":10,"model":"gpt-4o-mini",'
        b'"occurred_at":"2026-11-07T08:30:00Z","output_tokens":20,"provider":"openai",'
        b'"request_id":"req_1","schema_version":"1.0","usage_type":"LLM_TOKEN"}'
    )


def test_quantity_event_payload() -> None:
    event_id, payload = quantity()
    assert event_id == ULID
    assert payload == (
        b'{"event_id":"01JBZX6Q0000000000000000AB","model":"whisper-1",'
        b'"occurred_at":"2026-11-07T08:30:00Z","provider":"openai","quantity":"12.5",'
        b'"request_id":"req_2","schema_version":"1.0","unit":"second",'
        b'"usage_type":"AUDIO_SECONDS"}'
    )


def test_optional_fields_are_absent_not_null() -> None:
    for payload in (llm_payload(), quantity_payload()):
        assert "conversation_id" not in payload
        assert "feature" not in payload
        assert None not in payload.values()


def test_optional_fields_when_given() -> None:
    payload = llm_payload(conversation_id="conv:1", feature="chat.reply")
    assert payload["conversation_id"] == "conv:1"
    assert payload["feature"] == "chat.reply"


def test_shapes_do_not_mix() -> None:
    llm_keys = set(llm_payload())
    quantity_keys = set(quantity_payload())
    assert not {"quantity", "unit"} & llm_keys
    assert not set(TOKEN_FIELDS) & quantity_keys
    assert set(TOKEN_FIELDS) <= llm_keys
    assert {"quantity", "unit"} <= quantity_keys


def test_payload_has_no_tenant_or_project() -> None:
    for payload in (llm_payload(), quantity_payload()):
        assert "tenant_id" not in payload
        assert "project_id" not in payload


def test_serialization_is_stable() -> None:
    assert llm() == llm()
    assert quantity() == quantity()
    assert llm(conversation_id="c", feature="a.b") == llm(conversation_id="c", feature="a.b")


def test_serialization_is_compact_and_sorted() -> None:
    _, payload = llm(feature="chat.reply")
    document = json.loads(payload)
    assert payload == json.dumps(
        document, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    assert b" " not in payload


def test_usage_type_non_ascii_is_kept_as_utf8() -> None:
    # usage_type 只校验非空字符串；ensure_ascii=False，所以原样以 UTF-8 写出。
    _, payload = llm(usage_type="计量")
    assert "计量".encode() in payload


def test_schema_version_is_fixed() -> None:
    assert llm_payload()["schema_version"] == "1.0"
    assert quantity_payload()["schema_version"] == "1.0"


def test_payload_size_limit() -> None:
    llm(usage_type="X" * 15_000)
    with pytest.raises(ValueError):
        llm(usage_type="X" * 16_384)


# --- event_id 与 occurred_at -------------------------------------------------------


def test_new_id_is_called_only_when_event_id_is_missing() -> None:
    calls: list[int] = []

    def new_id() -> str:
        calls.append(1)
        return ULID

    llm(new_id=new_id)
    assert calls == [1]
    event_id, payload = llm(new_id=new_id, event_id=UUID7, occurred_at=NOW)
    assert calls == [1]
    assert event_id == UUID7
    assert json.loads(payload)["event_id"] == UUID7


def test_default_new_ulid_can_be_used_as_new_id() -> None:
    event_id, payload = llm(new_id=new_ulid)
    assert ULID_PATTERN.fullmatch(event_id)
    assert json.loads(payload)["event_id"] == event_id


def test_event_id_without_occurred_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        llm(event_id=ULID)
    with pytest.raises(ValueError):
        quantity(event_id=ULID)


def test_same_event_id_and_occurred_at_give_same_bytes() -> None:
    first = llm(event_id=ULID, occurred_at=NOW, now=NOW)
    second = llm(event_id=ULID, occurred_at=NOW, now=NOW + timedelta(seconds=5))
    assert first == second


def test_occurred_at_defaults_to_now() -> None:
    assert llm_payload(now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))["occurred_at"] == (
        "2026-01-02T03:04:05Z"
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 11, 7, 8, 30, 0, tzinfo=UTC), "2026-11-07T08:30:00Z"),
        (datetime(2026, 11, 7, 8, 30, 0, 1, tzinfo=UTC), "2026-11-07T08:30:00.000001Z"),
        (datetime(2026, 11, 7, 8, 30, 0, 120000, tzinfo=UTC), "2026-11-07T08:30:00.120000Z"),
        (
            datetime(2026, 11, 7, 16, 30, 0, tzinfo=timezone(timedelta(hours=8))),
            "2026-11-07T08:30:00Z",
        ),
        (
            datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone(timedelta(hours=1))),
            "2025-12-31T23:30:00Z",
        ),
    ],
)
def test_occurred_at_is_rfc3339_utc(moment: datetime, expected: str) -> None:
    assert llm_payload(occurred_at=moment)["occurred_at"] == expected


@pytest.mark.parametrize("name", ["occurred_at", "now"])
@pytest.mark.parametrize("value", [datetime(2026, 11, 7, 8, 30), "2026-11-07T08:30:00Z", 0])
def test_naive_or_non_datetime_moments_are_rejected(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        llm(**{name: value})


@pytest.mark.parametrize(
    "event_id",
    [
        "",
        "01jbzx6q0000000000000000ab",
        "81JBZX6Q0000000000000000AB",
        "01JBZX6Q0000000000000000AI",
        "01JBZX6Q0000000000000000A",
        "01890A5D-AC96-774B-BCCE-B302099A8057",
        "01890a5d-ac96-474b-bcce-b302099a8057",
        "01890a5d-ac96-774b-7cce-b302099a8057",
        ULID + "\n",
        12,
    ],
)
def test_invalid_event_ids(event_id: object) -> None:
    with pytest.raises(ValueError):
        llm(event_id=event_id, occurred_at=NOW)


def test_generated_event_id_is_validated() -> None:
    with pytest.raises(ValueError):
        llm(new_id=lambda: "not-an-id")


# --- 字段校验（表驱动，照服务端 schema） -----------------------------------------------


VALID_FIELDS = [
    ("provider", "a"),
    ("provider", "0"),
    ("provider", "a" + "b" * 63),
    ("provider", "open-ai_2"),
    ("model", "M"),
    ("model", "a" + "b" * 127),
    ("model", "org/model:v1.2@latest-x_y"),
    ("request_id", "a"),
    ("request_id", "a" * 128),
    ("request_id", "A.b_c:d-e"),
    ("conversation_id", "a" * 128),
    ("feature", "a.b"),
    ("feature", "a.b.c.d"),
    ("feature", "a" + "b" * 31 + ".c"),
    ("feature", ("a" * 21 + ".") * 2 + "a" * 20),
    ("usage_type", "X"),
    ("usage_type", " "),
]

INVALID_FIELDS = [
    ("provider", ""),
    ("provider", "A"),
    ("provider", "_a"),
    ("provider", "a" * 65),
    ("provider", "a.b"),
    ("provider", "a\n"),
    ("provider", None),
    ("model", ""),
    ("model", ".a"),
    ("model", "a" * 129),
    ("model", "a b"),
    ("model", "ａ"),
    ("request_id", ""),
    ("request_id", "a" * 129),
    ("request_id", "a/b"),
    ("request_id", "a b"),
    ("request_id", None),
    ("conversation_id", ""),
    ("conversation_id", "a" * 129),
    ("conversation_id", "a#b"),
    ("feature", ""),
    ("feature", "a"),
    ("feature", "A.b"),
    ("feature", "a.1b"),
    ("feature", "a.b.c.d.e"),
    ("feature", "a" + "b" * 32 + ".c"),
    ("feature", ("a" * 21 + ".") * 2 + "a" * 21),
    ("usage_type", ""),
    ("usage_type", None),
    ("usage_type", 1),
]


@pytest.mark.parametrize(("name", "value"), VALID_FIELDS)
def test_valid_common_fields(name: str, value: str) -> None:
    assert llm_payload(**{name: value})[name] == value
    assert quantity_payload(**{name: value})[name] == value


@pytest.mark.parametrize(("name", "value"), INVALID_FIELDS)
def test_invalid_common_fields(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        llm(**{name: value})
    with pytest.raises(ValueError):
        quantity(**{name: value})


@pytest.mark.parametrize("name", TOKEN_FIELDS)
@pytest.mark.parametrize("value", [0, 1, 10**12])
def test_valid_tokens(name: str, value: int) -> None:
    assert llm_payload(**{name: value})[name] == value


@pytest.mark.parametrize("name", TOKEN_FIELDS)
@pytest.mark.parametrize("value", [-1, 10**12 + 1, True, False, 1.0, "1", None, Decimal(1)])
def test_invalid_tokens(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        llm(**{name: value})


@pytest.mark.parametrize("unit", ["s", "second", "a" * 64, "kilo byte", "秒"])
def test_valid_units(unit: str) -> None:
    assert quantity_payload(unit=unit)["unit"] == unit


@pytest.mark.parametrize("unit", ["", " ", " s", "s ", "s\n", "a" * 65, None, 1])
def test_invalid_units(unit: object) -> None:
    with pytest.raises(ValueError):
        quantity(unit=unit)


# --- 只收关键字参数、未知参数 ----------------------------------------------------------


def test_unknown_keyword_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        llm(prompt="hello")
    with pytest.raises(TypeError):
        quantity(tenant_id="t")
    with pytest.raises(TypeError):
        llm(quantity="1")
    with pytest.raises(TypeError):
        quantity(input_tokens=1)


def test_positional_arguments_are_a_type_error() -> None:
    with pytest.raises(TypeError):
        build_llm_event("openai")  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_quantity_event("openai")  # type: ignore[misc]


@pytest.mark.parametrize("name", ["now", "new_id", "request_id", "input_tokens"])
def test_missing_required_keyword_is_a_type_error(name: str) -> None:
    arguments = {key: value for key, value in LLM.items() if key != name}
    with pytest.raises(TypeError):
        build_llm_event(**arguments)  # type: ignore[arg-type]


# --- 数量精度 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("37.420"), "37.42"),
        ("0.00000001", "0.00000001"),
        ("0.000000010", "0.00000001"),
        (Decimal("1E+3"), "1000"),
        (Decimal("5.000"), "5"),
        ("100.0", "100"),
        ("100", "100"),
        ("007", "7"),
        ("999999999999", "999999999999"),
        ("999999999999.99999999", "999999999999.99999999"),
        (Decimal("1.5E-7"), "0.00000015"),
        ("0", "0"),
        ("0.000", "0"),
        (Decimal("-0"), "0"),
        (Decimal("-0.00"), "0"),
        (Decimal("0E+20"), "0"),
    ],
)
def test_quantity_normalization(value: object, expected: str) -> None:
    assert quantity_payload(quantity=value)["quantity"] == expected


@pytest.mark.parametrize(
    "value",
    [
        "1000000000000",
        Decimal("1000000000000"),
        Decimal("1E+12"),
        Decimal("1E+100000"),
        "0.000000001",
        Decimal("1E-9"),
        Decimal("1E-100000"),
        "1.123456789",
        Decimal("-1"),
        Decimal("-0.5"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "-1",
        "+1",
        " 1",
        "1 ",
        "1_000",
        "1e3",
        "1E+3",
        "Infinity",
        "NaN",
        ".5",
        "5.",
        "",
        "١",
        "1,5",
        1,
        1.5,
        True,
        None,
    ],
)
def test_invalid_quantities(value: object) -> None:
    with pytest.raises(ValueError):
        quantity(quantity=value)
