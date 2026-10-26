"""CSV format, export lease, lease middleware and chunked sending (design gate #435 v10 §2, §7).

单元层：不起服务、不碰数据库。接口（经真实中间件栈、真实 uvicorn 的用例）属于 AIH-TASK-115。
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import logging
import tracemalloc
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core import csv_export
from app.core.config import Settings
from app.core.csv_export import (
    EXPORT_LEASE_SCOPE_KEY,
    EXPORT_LEASE_WARN_SECONDS,
    EXPORT_MAX_CONCURRENT,
    EXPORT_MAX_ROWS,
    EXPORT_SEND_CHUNK_BYTES,
    CustomerExportFailed,
    ExportBusy,
    ExportCsvResponse,
    ExportLeaseMiddleware,
    ExportLeaseSlot,
    ExportTooLarge,
    csv_response,
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
    write_csv,
)
from app.core.middleware import RequestContextMiddleware
from app.main import create_app
from app.models.ai_catalog import (
    METER_TYPE_CODE_LENGTH,
    MODEL_CODE_LENGTH,
    PROVIDER_CODE_LENGTH,
    UNIT_LENGTH,
)
from app.models.usage import EVENT_ID_LENGTH, FEATURE_LENGTH, REQUEST_ID_LENGTH
from app.repositories.usage_events import CustomerUsageRow
from app.schemas.customer_usage import customer_usage_event_view

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "billing.conf"

BOM = b"\xef\xbb\xbf"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
MIB = 1024 * 1024

# 设计 §2「接口一」的 18 列与文本列。
USAGE_COLUMNS = (
    "id",
    "event_id",
    "request_id",
    "conversation_id",
    "feature",
    "project_id",
    "project_name",
    "provider",
    "model",
    "usage_type",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "quantity",
    "unit",
    "charged_amount",
    "occurred_at_utc",
)
USAGE_TEXT_COLUMNS = frozenset(
    {
        "id",
        "event_id",
        "request_id",
        "conversation_id",
        "feature",
        "project_id",
        "project_name",
        "provider",
        "model",
        "usage_type",
        "unit",
    }
)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲，用例之间不互相挡住。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


def read_back(body: bytes) -> list[list[str]]:
    assert body.startswith(BOM)
    return list(csv.reader(io.StringIO(body[len(BOM) :].decode("utf-8"), newline="")))


def run_asgi(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def http_scope() -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": "/", "headers": []}


# --- CSV 格式 ---------------------------------------------------------------------------


def test_header_rows_bom_and_crlf() -> None:
    body = write_csv(("a", "b"), [("1", "2"), ("3", None)], text_columns=())
    assert body == BOM + b"a,b\r\n1,2\r\n3,\r\n"


def test_an_empty_export_is_bom_and_header_only() -> None:
    assert write_csv(("id", "event_id"), [], text_columns={"id"}) == BOM + b"id,event_id\r\n"


def test_rfc_4180_quoting_reads_back_value_for_value() -> None:
    values = ("a,b", 'say "hi"', "line\nbreak", "cr\rlf\r\n", "", "plain")
    body = write_csv(("c1", "c2", "c3", "c4", "c5", "c6"), [values], text_columns=())
    assert b'"a,b"' in body
    assert b'"say ""hi"""' in body
    assert b'"line\nbreak"' in body
    assert read_back(body) == [["c1", "c2", "c3", "c4", "c5", "c6"], list(values)]


def test_none_is_an_empty_field() -> None:
    body = write_csv(("a", "b", "c"), [(None, "x", None)], text_columns={"a", "c"})
    assert body == BOM + b"a,b,c\r\n,x,\r\n"
    assert read_back(body)[1] == ["", "x", ""]


def test_non_ascii_text_is_utf_8() -> None:
    body = write_csv(("project_name",), [("项目 Ā",)], text_columns={"project_name"})
    assert body == BOM + "project_name\r\n项目 Ā\r\n".encode()


def test_columns_are_written_in_the_given_order() -> None:
    body = write_csv(("z", "a", "m"), [("1", "2", "3")], text_columns=())
    assert read_back(body) == [["z", "a", "m"], ["1", "2", "3"]]


@pytest.mark.parametrize("value", [1, 0, Decimal("1.5"), 1.5, dt.datetime(2026, 1, 1), b"x", True])
def test_cells_must_be_str_or_none(value: object) -> None:
    with pytest.raises(TypeError):
        write_csv(("a",), [(value,)], text_columns=())


def test_a_row_must_match_the_columns() -> None:
    with pytest.raises(ValueError):
        write_csv(("a", "b"), [("1",)], text_columns=())


# --- 公式注入 ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_text_columns_starting_with_a_formula_prefix_get_a_quote(prefix: str) -> None:
    value = f"{prefix}1+1"
    body = write_csv(("text", "amount"), [(value, value)], text_columns={"text"})
    text, amount = read_back(body)[1]
    assert text == "'" + value
    # 数值列原样：负金额必须保持是数。
    assert amount == value


def test_a_negative_amount_in_a_numeric_column_stays_a_number() -> None:
    body = write_csv(("id", "amount"), [("-x", "-0.04120000")], text_columns={"id"})
    assert read_back(body)[1] == ["'-x", "-0.04120000"]


@pytest.mark.parametrize("value", ["a=1", "a-b", "x+y@z", " =1", "1\t2", "'=already"])
def test_values_not_starting_with_a_prefix_are_untouched(value: str) -> None:
    body = write_csv(("text",), [(value,)], text_columns={"text"})
    assert read_back(body)[1] == [value]


# --- 上限常量与错误 ---------------------------------------------------------------------


def test_export_limits() -> None:
    assert EXPORT_MAX_ROWS == 20_000
    assert EXPORT_MAX_CONCURRENT == 1
    assert EXPORT_LEASE_WARN_SECONDS == 120
    assert EXPORT_SEND_CHUNK_BYTES == 65_536


def test_export_errors_have_fixed_codes_status_and_text() -> None:
    too_large = ExportTooLarge()
    assert (too_large.code, too_large.http_status) == ("EXPORT_TOO_LARGE", 422)
    assert "20,000" in too_large.message
    busy = ExportBusy()
    assert (busy.code, busy.http_status) == ("EXPORT_BUSY", 503)
    assert busy.headers == {"Retry-After": "5"}
    failed = CustomerExportFailed()
    assert (failed.code, failed.http_status) == ("INTERNAL_ERROR", 500)
    # 与 handle_unexpected_error 的通用 500 逐字相同。
    assert failed.message == "An internal error occurred."


# --- 导出租约 ---------------------------------------------------------------------------


def test_acquire_busy_release_reacquire() -> None:
    first = try_acquire_export_lease()
    assert first is not None
    try:
        assert export_lease_held()
        assert try_acquire_export_lease() is None
    finally:
        release_export_lease(first)
    assert not export_lease_held()
    second = try_acquire_export_lease()
    assert second is not None
    assert second is not first
    release_export_lease(second)


def test_releasing_a_stale_token_does_not_free_another_holder() -> None:
    first = try_acquire_export_lease()
    assert first is not None
    release_export_lease(first)
    second = try_acquire_export_lease()
    assert second is not None
    try:
        release_export_lease(first)
        assert export_lease_held()
        assert try_acquire_export_lease() is None
    finally:
        release_export_lease(second)


def held_long_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage() == "customer_export_lease_held_long"
    ]


def test_a_long_held_lease_is_never_taken_over_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(csv_export, "_clock", lambda: now[0])
    holder = try_acquire_export_lease()
    assert holder is not None
    try:
        with caplog.at_level(logging.WARNING, logger=csv_export.__name__):
            now[0] += EXPORT_LEASE_WARN_SECONDS
            assert try_acquire_export_lease() is None
            assert not held_long_warnings(caplog)
            now[0] += 1
            assert try_acquire_export_lease() is None
            now[0] += 3_600
            assert try_acquire_export_lease() is None
        warnings = held_long_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert hasattr(warnings[0], "request_id")
    finally:
        release_export_lease(holder)
    caplog.clear()
    # 下一个持有者重新计时，也重新有一次告警的机会。
    after = try_acquire_export_lease()
    assert after is not None
    try:
        with caplog.at_level(logging.WARNING, logger=csv_export.__name__):
            now[0] += EXPORT_LEASE_WARN_SECONDS + 1
            assert try_acquire_export_lease() is None
        assert len(held_long_warnings(caplog)) == 1
    finally:
        release_export_lease(after)


# --- 租约槽与中间件 ---------------------------------------------------------------------


def test_releasing_an_empty_slot_is_a_no_op() -> None:
    other = try_acquire_export_lease()
    assert other is not None
    try:
        ExportLeaseSlot().release()
        assert export_lease_held()
    finally:
        release_export_lease(other)


def test_a_slot_releases_only_its_own_token_once() -> None:
    slot = ExportLeaseSlot()
    token = try_acquire_export_lease()
    assert token is not None
    slot.hold(token)
    slot.release()
    assert not export_lease_held()
    later = try_acquire_export_lease()
    assert later is not None
    try:
        slot.release()
        assert export_lease_held()
    finally:
        release_export_lease(later)


def test_the_lease_middleware_is_the_outermost_user_middleware() -> None:
    app = create_app(Settings(database_url="", jwt_secret_file=""))
    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes[0] is ExportLeaseMiddleware
    assert classes.count(ExportLeaseMiddleware) == 1
    assert classes.index(RequestContextMiddleware) > 0


def test_the_middleware_does_nothing_without_a_token() -> None:
    other = try_acquire_export_lease()
    assert other is not None
    seen: list[object] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope[EXPORT_LEASE_SCOPE_KEY])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    try:
        sent = run_asgi(ExportLeaseMiddleware(inner), http_scope())
        assert isinstance(seen[0], ExportLeaseSlot)
        assert [message["type"] for message in sent] == [
            "http.response.start",
            "http.response.body",
        ]
        # 别人的租约不受影响。
        assert export_lease_held()
    finally:
        release_export_lease(other)


def test_the_middleware_passes_non_http_scopes_through() -> None:
    seen: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope)

    run_asgi(ExportLeaseMiddleware(inner), {"type": "lifespan"})
    assert EXPORT_LEASE_SCOPE_KEY not in seen[0]


def holding_app(*, fail: BaseException | None = None) -> Any:
    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        token = try_acquire_export_lease()
        assert token is not None
        scope[EXPORT_LEASE_SCOPE_KEY].hold(token)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # 发送期间租约仍被持有。
        assert export_lease_held()
        if fail is not None:
            raise fail
        await send({"type": "http.response.body", "body": b"x", "more_body": False})

    return ExportLeaseMiddleware(inner)


def test_the_middleware_releases_after_the_response_is_sent() -> None:
    run_asgi(holding_app(), http_scope())
    assert not export_lease_held()


def test_the_middleware_releases_when_the_app_raises() -> None:
    with pytest.raises(RuntimeError):
        run_asgi(holding_app(fail=RuntimeError("boom")), http_scope())
    assert not export_lease_held()


def test_the_middleware_releases_when_the_request_is_cancelled() -> None:
    async def scenario() -> None:
        paused = asyncio.Event()

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                paused.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(holding_app()(http_scope(), receive, send))
        await paused.wait()
        assert export_lease_held()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not export_lease_held()


# --- 响应与分段发送 ---------------------------------------------------------------------


def test_csv_response_headers() -> None:
    body = write_csv(("id",), [(ZERO_UUID,)], text_columns={"id"})
    response = csv_response("usage-events_2026-10-01_2026-10-31.csv", body)
    assert isinstance(response, ExportCsvResponse)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="usage-events_2026-10-01_2026-10-31.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-length"] == str(len(body))


@pytest.mark.parametrize("name", ["", 'a"b.csv', "a b.csv", "../x.csv", "项目.csv", "a;b.csv"])
def test_csv_response_rejects_unsafe_filenames(name: str) -> None:
    with pytest.raises(ValueError):
        csv_response(name, b"")


@pytest.mark.parametrize(
    "size",
    [
        len(BOM + b"id\r\n"),
        EXPORT_SEND_CHUNK_BYTES,
        EXPORT_SEND_CHUNK_BYTES + 1,
        8 * MIB + 123,
    ],
)
def test_the_body_is_sent_in_chunks(size: int) -> None:
    body = bytes(index % 251 for index in range(size))
    sent = run_asgi(csv_response("x.csv", body), http_scope())
    start, *chunks = sent
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-length"] == str(size).encode()
    assert headers[b"x-accel-buffering"] == b"no"
    assert all(message["type"] == "http.response.body" for message in chunks)
    assert all(isinstance(message["body"], memoryview) for message in chunks)
    assert all(len(message["body"]) <= EXPORT_SEND_CHUNK_BYTES for message in chunks)
    assert [message["more_body"] for message in chunks] == [True] * (len(chunks) - 1) + [False]
    assert len(chunks) == max(1, -(-size // EXPORT_SEND_CHUNK_BYTES))
    assert b"".join(bytes(message["body"]) for message in chunks) == body


def test_an_empty_export_file_takes_the_same_path() -> None:
    body = write_csv(USAGE_COLUMNS, [], text_columns=USAGE_TEXT_COLUMNS)
    sent = run_asgi(csv_response("usage-events_2026-10-01_2026-10-31.csv", body), http_scope())
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["more_body"] is False
    assert bytes(sent[1]["body"]) == body


# --- 代理配置 ---------------------------------------------------------------------------


def nginx_instructions() -> str:
    # 照 tests/backend/test_compose.py：先剥注释，被注释掉的指令不算数。
    kept = [
        line
        for line in NGINX_CONF.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return "\n".join(kept)


def nginx_location_block(name: str) -> str:
    text = nginx_instructions()
    start = text.index(f"location {name} {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces around `location {name}` in {NGINX_CONF}")


def test_the_api_location_passes_x_accel_buffering_through() -> None:
    assert "proxy_pass_header X-Accel-Buffering;" in nginx_location_block("/api/")
    text = nginx_instructions()
    assert "proxy_ignore_headers" not in text
    # 其他接口照旧缓冲：只由导出响应自己的头关闭。
    assert "proxy_buffering off" not in text


# --- 内存上界 ---------------------------------------------------------------------------


def longest_row(index: int) -> CustomerUsageRow:
    """Every string at its column width, each cell its own object (design §2 memory bound)."""

    def text(prefix: str, width: int) -> str:
        return f"{prefix}{index:0{width - len(prefix)}d}"

    def uuid(prefix: str) -> str:
        return f"{prefix}{index:07d}{ZERO_UUID[8:]}"

    return CustomerUsageRow(
        public_id=uuid("0"),
        event_id=text("evt", EVENT_ID_LENGTH),
        request_id=text("req", REQUEST_ID_LENGTH),
        conversation_id=text("cnv", REQUEST_ID_LENGTH),
        feature=text("ftr", FEATURE_LENGTH),
        project_public_id=uuid("1"),
        project_name=text("prj", 255),
        provider=text("prv", PROVIDER_CODE_LENGTH),
        model=text("mdl", MODEL_CODE_LENGTH),
        usage_type=text("typ", METER_TYPE_CODE_LENGTH),
        input_tokens=int("9223372036854775807"),
        output_tokens=int("9223372036854775807"),
        cache_creation_input_tokens=int("9223372036854775807"),
        cache_read_input_tokens=int("9223372036854775807"),
        quantity=Decimal("999999999999.99999999"),
        unit=text("u", UNIT_LENGTH),
        billable_cost=Decimal("999999999999.99999999"),
        occurred_at=dt.datetime(2026, 10, 26, 12, 34, 56, index % 1_000_000),
    )


def usage_cells(row: CustomerUsageRow) -> list[str | None]:
    # AIH-TASK-115 调用方的写法：18 键白名单视图的 JSON 值，时间列改名，整数变字符串。
    view = customer_usage_event_view(row).model_dump(mode="json")
    view["occurred_at_utc"] = view.pop("occurred_at")
    return [None if view[name] is None else str(view[name]) for name in USAGE_COLUMNS]


def test_the_peak_memory_of_one_export_is_bounded() -> None:
    tracemalloc.start()
    try:
        rows = [longest_row(index) for index in range(EXPORT_MAX_ROWS)]
        body = write_csv(
            USAGE_COLUMNS, (usage_cells(row) for row in rows), text_columns=USAGE_TEXT_COLUMNS
        )
        del rows
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert body.startswith(BOM)
    assert body.count(b"\r\n") == EXPORT_MAX_ROWS + 1
    assert peak <= 128 * MIB, f"export peak {peak / MIB:.1f} MiB exceeds 128 MiB"
