"""The BNM adapter against the recorded responses (design gate #183 v3 §7, AIH-TASK-040).

Three rows of design §7:

- **BNM 解析** — the recorded USD and JPY noon sessions, the recorded `1130` session, the
  recorded 404「No records found.」, and the cases derived from the 2026-09-29 USD recording
  (one field changed each; every derived body says so where it is built), a non-JSON body, a
  404 of another shape, HTTP 500, a redirect, a timeout and a connection failure;
- **双精度还原** — the six `middle_rate` literals of design §10;
- **请求** — the URL and the `Accept` header the adapter sends, across Kuala Lumpur's midnight
  (UTC 16:00 vs 15:59), and that a redirect is not followed.

The recordings below are copied **verbatim** from design §10 (BNM's public API,
2026-09-30); they are public exchange-rate data. Nothing here touches the network: the
adapter's transport is a fake, and the redirect case talks to a server on the loopback
interface. The base URL in these tests is a placeholder.
"""

from __future__ import annotations

import datetime as dt
import email.message
import io
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core.config import Settings
from app.core.fx_source import (
    ACCEPT_HEADER,
    BAD_PAYLOAD,
    NETWORK,
    TIMEOUT,
    UNIT_NOT_EXACT,
    BnmFxSource,
    FxSourceError,
    build_fx_source,
    divide_exactly,
    kuala_lumpur_date,
    parse_quote,
    restore_middle_rate,
    urllib_transport,
)

# --- design §10: the responses recorded on 2026-09-30, verbatim ------------------------------

# USD，2026-09-29，session=1200（HTTP 200）
USD_1200 = '{"data":{"currency_code":"USD","unit":1,"rate":{"date":"2026-09-29","buying_rate":4.0789999999999997,"selling_rate":4.0869999999999997,"middle_rate":4.0830000000000002}},"meta":{"quote":"rm","session":"1200","last_updated":"2026-09-30 11:56:16","total_result":1}}'  # noqa: E501
# JPY，2026-09-29，session=1200（HTTP 200，unit = 100）
JPY_1200 = '{"data":{"currency_code":"JPY","unit":100,"rate":{"date":"2026-09-29","buying_rate":2.5908000000000002,"selling_rate":2.5964,"middle_rate":2.5935999999999999}},"meta":{"quote":"rm","session":"1200","last_updated":"2026-09-29 23:01:20","total_result":1}}'  # noqa: E501
# USD，2026-09-29，未传 session（HTTP 200，BNM 给的是 1130 场）
USD_1130 = '{"data":{"currency_code":"USD","unit":1,"rate":{"date":"2026-09-29","buying_rate":4.0650000000000004,"selling_rate":4.0899999999999999,"middle_rate":null}},"meta":{"quote":"rm","session":"1130","last_updated":"2026-09-30 11:56:16","total_result":1}}'  # noqa: E501
# USD，2026-09-26（周六），session=1200（HTTP 404）
NO_RECORDS = '{"message":"No records found.","code":404}'

# The other four `middle_rate` literals of design §10 (same day, session=1200), with the decimal
# BNM published. USD and JPY come from the recordings above.
MIDDLE_RATE_LITERALS = [
    ("USD", "4.0830000000000002", "4.083"),
    ("JPY", "2.5935999999999999", "2.5936"),
    ("IDR", "0.022700000000000001", "0.0227"),
    ("SGD", "3.1934999999999998", "3.1935"),
    ("EUR", "4.6387", "4.6387"),
    ("GBP", "5.4057000000000004", "5.4057"),
]

QUOTE_DATE = dt.date(2026, 9, 29)
# A placeholder: these tests never reach any host.
BASE_URL = "https://bnm.example.com"
USD_MIDDLE_RATE = '"middle_rate":4.0830000000000002'


def derived_from_usd(old: str, new: str) -> str:
    """派生自 2026-09-29 USD 录制：`USD_1200` with exactly one fragment replaced."""
    assert USD_1200.count(old) == 1, old
    return USD_1200.replace(old, new)


# --- a fake transport ---------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes, *, read_error: BaseException | None = None):
        self.status = status
        self.body = body
        self.read_error = read_error
        self.closed = False

    def read(self) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    """Answers like `urllib`: a 2xx is a response, anything else is an `HTTPError`."""

    def __init__(
        self,
        status: int = 200,
        body: str = "",
        *,
        error: BaseException | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.body = body.encode("utf-8")
        self.error = error
        self.read_error = read_error
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self.responses: list[FakeResponse] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        if not 200 <= self.status < 300:
            headers = email.message.Message()
            raise urllib.error.HTTPError(
                request.full_url, self.status, "fake", headers, io.BytesIO(self.body)
            )
        response = FakeResponse(self.status, self.body, read_error=self.read_error)
        self.responses.append(response)
        return response


def fetch_usd(transport: FakeTransport, quote_date: dt.date = QUOTE_DATE):
    return BnmFxSource(BASE_URL, 10, transport=transport).fetch("USD", quote_date)


def failure(transport: FakeTransport, currency: str = "USD") -> str:
    """The error code of a fetch that must fail."""
    with pytest.raises(FxSourceError) as raised:
        BnmFxSource(BASE_URL, 10, transport=transport).fetch(currency, QUOTE_DATE)
    return raised.value.code


# --- BNM 解析 ------------------------------------------------------------------------------


def test_the_recorded_usd_noon_session_is_a_quote_of_4_083() -> None:
    transport = FakeTransport(200, USD_1200)

    quote = fetch_usd(transport)

    assert quote is not None
    assert quote.quote_date == QUOTE_DATE
    assert quote.unit == 1
    assert quote.middle_rate == Decimal("4.083")
    assert quote.rate == Decimal("4.083")
    assert quote.reference == "bnm:exchange-rate:USD:2026-09-29:session=1200:middle_rate:unit=1"
    assert transport.responses[0].closed


def test_the_recorded_jpy_quote_is_divided_by_its_unit_of_100() -> None:
    source = BnmFxSource(BASE_URL, 10, transport=FakeTransport(200, JPY_1200))

    quote = source.fetch("JPY", QUOTE_DATE)

    assert quote is not None
    assert quote.unit == 100
    assert quote.middle_rate == Decimal("2.5936")
    assert quote.rate == Decimal("0.025936")
    assert quote.reference == "bnm:exchange-rate:JPY:2026-09-29:session=1200:middle_rate:unit=100"


def test_the_recorded_1130_session_is_a_bad_payload() -> None:
    """Without `session` BNM answers the 1130 session, whose `middle_rate` is `null`."""
    assert failure(FakeTransport(200, USD_1130)) == BAD_PAYLOAD


def test_the_1130_session_is_refused_even_with_a_number() -> None:
    # 派生自 2026-09-29 USD 录制（1130 场那一份）：`middle_rate` 换成一个数，场次仍不符。
    body = USD_1130.replace('"middle_rate":null', USD_MIDDLE_RATE)

    assert failure(FakeTransport(200, body)) == BAD_PAYLOAD


def test_the_recorded_no_records_404_means_no_quote_for_that_date() -> None:
    """Saturdays, public holidays, before the noon session is out, unknown codes: all this."""
    transport = FakeTransport(404, NO_RECORDS)

    assert fetch_usd(transport, dt.date(2026, 9, 26)) is None


def test_the_no_records_404_is_compared_as_json_not_as_text() -> None:
    # 派生自 2026-09-26 USD 的「无记录」录制：同一个 JSON，键序与空白不同。
    body = '{"code": 404, "message": "No records found."}'

    assert fetch_usd(FakeTransport(404, body)) is None


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>Not Found</body></html>",
        '{"message":"Not Found","code":404}',
        '{"message":"No records found.","code":"404"}',
        '{"message":"No records found."}',
        "",
    ],
    ids=["html", "other-message", "code-as-string", "no-code", "empty"],
)
def test_a_404_of_another_shape_is_http_404(body: str) -> None:
    assert failure(FakeTransport(404, body)) == "HTTP_404"


# Every body below: 派生自 2026-09-29 USD 录制, one fragment changed.
DERIVED_FAILURES = [
    ("date-differs", '"date":"2026-09-29"', '"date":"2026-09-28"', BAD_PAYLOAD),
    ("currency-differs", '"currency_code":"USD"', '"currency_code":"EUR"', BAD_PAYLOAD),
    ("session-differs", '"session":"1200"', '"session":"1130"', BAD_PAYLOAD),
    ("quote-differs", '"quote":"rm"', '"quote":"usd"', BAD_PAYLOAD),
    ("middle-rate-zero", USD_MIDDLE_RATE, '"middle_rate":0', BAD_PAYLOAD),
    ("middle-rate-negative", USD_MIDDLE_RATE, '"middle_rate":-4.083', BAD_PAYLOAD),
    ("middle-rate-string", USD_MIDDLE_RATE, '"middle_rate":"4.083"', BAD_PAYLOAD),
    ("middle-rate-null", USD_MIDDLE_RATE, '"middle_rate":null', BAD_PAYLOAD),
    ("middle-rate-missing", "," + USD_MIDDLE_RATE, "", BAD_PAYLOAD),
    ("middle-rate-boolean", USD_MIDDLE_RATE, '"middle_rate":true', BAD_PAYLOAD),
    ("middle-rate-nan", USD_MIDDLE_RATE, '"middle_rate":NaN', BAD_PAYLOAD),
    ("middle-rate-overflow", USD_MIDDLE_RATE, '"middle_rate":1e999', BAD_PAYLOAD),
    ("middle-rate-seven-places", USD_MIDDLE_RATE, '"middle_rate":4.0830001', BAD_PAYLOAD),
    ("unit-zero", '"unit":1,', '"unit":0,', BAD_PAYLOAD),
    ("unit-negative", '"unit":1,', '"unit":-1,', BAD_PAYLOAD),
    ("unit-string", '"unit":1,', '"unit":"1",', BAD_PAYLOAD),
    ("unit-fraction", '"unit":1,', '"unit":1.0,', BAD_PAYLOAD),
    ("unit-boolean", '"unit":1,', '"unit":true,', BAD_PAYLOAD),
    ("unit-missing", '"unit":1,', "", BAD_PAYLOAD),
    # v3: 4.083 / 7 does not end within 10 places (v2's 3 did: 4.083 / 3 = 1.361).
    ("unit-seven", '"unit":1,', '"unit":7,', UNIT_NOT_EXACT),
]


@pytest.mark.parametrize(
    "old, new, code",
    [case[1:] for case in DERIVED_FAILURES],
    ids=[case[0] for case in DERIVED_FAILURES],
)
def test_a_body_derived_from_the_usd_recording_fails_with_its_code(
    old: str, new: str, code: str
) -> None:
    body = derived_from_usd(old, new)

    assert failure(FakeTransport(200, body)) == code


def test_a_body_that_is_not_json_is_a_bad_payload() -> None:
    """Without the `Accept` header BNM answers an HTML page with 200."""
    for body in ("<!DOCTYPE html><html><body>BNM</body></html>", "[]", '"USD"', ""):
        assert failure(FakeTransport(200, body)) == BAD_PAYLOAD


@pytest.mark.parametrize("status", [500, 503, 429, 401, 400])
def test_other_http_statuses_are_recorded_as_http_status(status: int) -> None:
    assert failure(FakeTransport(status, "upstream trouble")) == f"HTTP_{status}"


def test_a_2xx_other_than_200_is_its_status() -> None:
    assert failure(FakeTransport(204, "")) == "HTTP_204"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_its_status_not_a_quote(status: int) -> None:
    """With redirects off, urllib surfaces the 3xx as an `HTTPError` (see the loopback case)."""
    assert failure(FakeTransport(status, "")) == f"HTTP_{status}"


@pytest.mark.parametrize(
    "error, code",
    [
        (TimeoutError("timed out"), TIMEOUT),
        (urllib.error.URLError(TimeoutError("timed out")), TIMEOUT),
        (urllib.error.URLError(ConnectionRefusedError("refused")), NETWORK),
        (urllib.error.URLError("name resolution failed"), NETWORK),
        (ConnectionResetError("reset"), NETWORK),
    ],
    ids=["timeout", "connect-timeout", "refused", "dns", "reset"],
)
def test_transport_failures_have_their_codes(error: BaseException, code: str) -> None:
    assert failure(FakeTransport(error=error)) == code


def test_a_timeout_while_reading_the_body_is_a_timeout() -> None:
    transport = FakeTransport(200, USD_1200, read_error=TimeoutError("timed out"))

    assert failure(transport) == TIMEOUT
    assert transport.responses[0].closed


def test_the_error_carries_only_the_code() -> None:
    """设计 §6：拉取失败只记错误码，不记 BNM 响应体。"""
    body = "<html>a body that must not be kept</html>"
    with pytest.raises(FxSourceError) as raised:
        fetch_usd(FakeTransport(500, body))

    assert str(raised.value) == "HTTP_500"
    assert raised.value.args == ("HTTP_500",)


# --- 双精度还原 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "literal, published",
    [case[1:] for case in MIDDLE_RATE_LITERALS],
    ids=[case[0] for case in MIDDLE_RATE_LITERALS],
)
def test_a_binary64_literal_is_restored_to_the_published_decimal(
    literal: str, published: str
) -> None:
    restored = restore_middle_rate(json.loads(literal))

    assert restored == Decimal(published)
    assert str(restored) == published


@pytest.mark.parametrize(
    "literal, published",
    [case[1:] for case in MIDDLE_RATE_LITERALS if case[1] != case[2]],
    ids=[case[0] for case in MIDDLE_RATE_LITERALS if case[1] != case[2]],
)
def test_parsing_the_literal_exactly_would_not_be_the_published_rate(
    literal: str, published: str
) -> None:
    """防回退：`json.loads(..., parse_float=Decimal)` 读出的是 16 位小数，不是公布值。

    按字面精确解析时，除以 `unit` 必然超过 10 位小数 —— 每一次拉取都会失败。
    """
    exact = json.loads(literal, parse_float=Decimal)

    assert exact != Decimal(published)
    assert exact == Decimal(literal)


def test_the_recorded_usd_body_parses_to_the_published_decimal() -> None:
    quote = parse_quote(USD_1200.encode("utf-8"), "USD", QUOTE_DATE)
    exact = json.loads(USD_1200, parse_float=Decimal)["data"]["rate"]["middle_rate"]

    assert str(quote.middle_rate) == "4.083"
    assert quote.middle_rate != exact


def test_a_restored_rate_has_at_most_six_places_and_is_positive() -> None:
    for value in (4.0830001, 0.0000001, 0.0, -4.083, float("inf"), float("nan"), 1e14):
        with pytest.raises(FxSourceError) as raised:
            restore_middle_rate(value)
        assert raised.value.code == BAD_PAYLOAD

    assert restore_middle_rate(4.083001) == Decimal("4.083001")


def test_dividing_by_the_unit_is_exact_or_refused() -> None:
    assert divide_exactly(Decimal("4.083"), 1) == Decimal("4.083")
    assert divide_exactly(Decimal("2.5936"), 100) == Decimal("0.025936")
    assert divide_exactly(Decimal("0.0227"), 100) == Decimal("0.000227")
    assert divide_exactly(Decimal("4.083"), 3) == Decimal("1.361")
    for middle_rate, unit in ((Decimal("4.083"), 7), (Decimal("0.000001"), 10_000_000)):
        with pytest.raises(FxSourceError) as raised:
            divide_exactly(middle_rate, unit)
        assert raised.value.code == UNIT_NOT_EXACT


# --- 请求 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment, kuala_lumpur_today",
    [
        (dt.datetime(2026, 9, 29, 16, 0, 0), dt.date(2026, 9, 30)),
        (dt.datetime(2026, 9, 29, 15, 59, 59), dt.date(2026, 9, 29)),
    ],
    ids=["utc-16-00", "utc-15-59"],
)
def test_the_request_asks_for_kuala_lumpur_today_by_date(
    moment: dt.datetime, kuala_lumpur_today: dt.date
) -> None:
    """吉隆坡今天 = UTC + 8 小时后的日期；按日期取，不用「最新」端点。"""
    assert kuala_lumpur_date(moment) == kuala_lumpur_today
    transport = FakeTransport(404, NO_RECORDS)

    assert fetch_usd(transport, kuala_lumpur_date(moment)) is None

    (request,) = transport.requests
    path = f"/public/exchange-rate/USD/date/{kuala_lumpur_today.isoformat()}"
    assert request.full_url == f"{BASE_URL}{path}?session=1200&quote=rm"
    assert request.get_method() == "GET"
    assert request.get_header("Accept") == "application/vnd.BNM.API.v1+json"
    assert ACCEPT_HEADER == "application/vnd.BNM.API.v1+json"


def test_the_request_uses_the_configured_timeout_and_base_url() -> None:
    transport = FakeTransport(200, USD_1200)
    BnmFxSource(f"{BASE_URL}/", 7, transport=transport).fetch("USD", QUOTE_DATE)

    assert transport.timeouts == [7]
    assert transport.requests[0].full_url.startswith(f"{BASE_URL}/public/exchange-rate/USD/")


def test_the_factory_builds_the_bnm_source_from_settings() -> None:
    source = build_fx_source(Settings(fx_bnm_base_url=BASE_URL, fx_fetch_timeout_seconds=7))

    assert isinstance(source, BnmFxSource)
    url = source.request_for("USD", QUOTE_DATE).full_url
    assert url == f"{BASE_URL}/public/exchange-rate/USD/date/2026-09-29?session=1200&quote=rm"


def test_only_an_https_base_url_is_accepted() -> None:
    for bad in ("http://bnm.example.com", "bnm.example.com", "https://"):
        with pytest.raises(ValueError):
            BnmFxSource(bad, 10, transport=FakeTransport())


def test_a_currency_that_is_not_a_code_is_never_put_into_the_path() -> None:
    source = BnmFxSource(BASE_URL, 10, transport=FakeTransport())
    for bad in ("usd", "US", "USD/../X", ""):
        with pytest.raises(ValueError):
            source.request_for(bad, QUOTE_DATE)


# --- redirects are not followed (loopback only) ---------------------------------------------


class _RedirectingHandler(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        self.send_response(302)
        self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def redirecting_server() -> Iterator[str]:
    _RedirectingHandler.paths = []
    server = HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_real_transport_does_not_follow_a_redirect(redirecting_server: str) -> None:
    """urllib 默认跟随 3xx（甚至跟到明文 http）；这里的 opener 不跟随，3xx 原样成为 HTTPError。

    Loopback only: the server runs in this process; the default transport is called directly
    because the adapter itself only takes an https:// base URL.
    """
    request = urllib.request.Request(f"{redirecting_server}/start", method="GET")

    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib_transport(request, 5)

    assert raised.value.code == 302
    raised.value.close()
    assert _RedirectingHandler.paths == ["/start"]
