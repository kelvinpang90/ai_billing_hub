"""FX quote sources: the BNM exchange-rate adapter (design gate #183 v3, AIH-TASK-040).

设计 §2「BNM 适配器」；ADR-0005 §1；spec §17.1「pluggable」。
**这一层只负责「向来源要某一天的报价」**，不碰数据库：写草稿与拉取记录在
`app/tasks/fx_fetch.py`。业务代码只依赖 `FxRateSource` 协议，实现由 `build_fx_source`
一处决定（照 `app/core/mailer.py` 的 `build_transport`）；换来源不影响已发布的版本与
历史计算。

BNM 实现按 2026-09-30 的实测写（设计 §10）：

- **按日期取，不用「最新」端点**：
  `GET {base_url}/public/exchange-rate/{ccy}/date/{YYYY-MM-DD}?session=1200&quote=rm`，
  请求头 `Accept: application/vnd.BNM.API.v1+json`（不带这个头返回的是 HTML）。
  「最新」端点在当天中午场公布之前 404、不回退上一交易日；
- **200 的响应体逐项核对**，任何一项不符就是 `BAD_PAYLOAD`：`data.currency_code`、
  `data.rate.date` 是所请求的币种与日期，`meta.session = "1200"`（漏传 `session` 时
  BNM 给的是 `1130` 场，`middle_rate` 为 `null`），`meta.quote = "rm"`，`data.unit`
  是正整数，`data.rate.middle_rate` 是 JSON 数字；
- ⚠️ **`middle_rate` 是双精度数的 17 位字面量**（公布值 4.0830 输出为
  `4.0830000000000002`）。按字面精确解析（`parse_float=Decimal`）得到 16 位小数，
  除以 `unit` 必然超精度 —— 每一次拉取都会失败。所以先按双精度解析，再取
  **最短往返十进制表示**（`Decimal(repr(x))`），正是 BNM 公布的那个数。这是全系统
  唯一经过 `float` 的地方（INV-10 的例外：来源本身就是双精度数）；之后全程 `Decimal`；
- `rate = middle_rate / unit`，必须在 10 位小数内精确，否则 `UNIT_NOT_EXACT`（不舍入）；
- **404 且响应体是 `{"message":"No records found.","code":404}`** → `None`：
  周末、公众假期、当天中午场尚未公布，以及**不存在的币种代码**都是这个响应。
  其他形状的 404 → `HTTP_404`；
- 其余 HTTP 状态 → `HTTP_<status>`；超时 → `TIMEOUT`；连接失败 → `NETWORK`；不是 JSON →
  `BAD_PAYLOAD`。错误只带错误码，**不带响应体**（设计 §6）。

⚠️ **不跟随重定向**：urllib 默认跟随 3xx，而且会跟到明文 http —— 那样「只接受 HTTPS 的
`base_url`」就形同虚设。这里把重定向处理器换成不跟随的那个，3xx 按「其余 HTTP 状态」记
`HTTP_<status>`。

只用标准库 `urllib`（设计 §9：不为一个每日调用加运行时依赖）。
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

from app.core.config import Settings, require_https_base_url

# 马来西亚固定 UTC+8、没有夏令时：不依赖镜像里的 tzdata（设计 §2「时间语义」）。
KUALA_LUMPUR_OFFSET: Final = dt.timedelta(hours=8)
# BNM 中午场：报价日 12:00 吉隆坡 = 当日 04:00 UTC，即 BNM 草稿的 `observed_at`。
NOON_SESSION: Final = "1200"
NOON_SESSION_UTC: Final = dt.time(4, 0, 0)
QUOTE_RM: Final = "rm"
ACCEPT_HEADER: Final = "application/vnd.BNM.API.v1+json"

# 错误码（`fx_fetch_attempts.error_code`）。
TIMEOUT: Final = "TIMEOUT"
NETWORK: Final = "NETWORK"
BAD_PAYLOAD: Final = "BAD_PAYLOAD"
UNIT_NOT_EXACT: Final = "UNIT_NOT_EXACT"

# 还原后的 `middle_rate` 最多 6 位小数（BNM 公布 4 位，留 2 位余量）。
MIDDLE_RATE_MAX_PLACES: Final = 6
# `rate` 存进 DECIMAL(24,10)：10 位小数、整数部分最多 14 位。`rate ≤ middle_rate`，所以只限
# `middle_rate` 的整数部分；超出就是格式不对（BNM 的任何真实报价都远小于它）。
RATE_PLACES: Final = 10
_MIDDLE_RATE_LIMIT: Final = Decimal(10) ** 14

_CURRENCY_PATTERN: Final = re.compile(r"[A-Z]{3}")
_NO_RECORDS_MESSAGE: Final = "No records found."
_OK: Final = 200
_NOT_FOUND: Final = 404


def kuala_lumpur_date(moment: dt.datetime) -> dt.date:
    """The Kuala Lumpur calendar date at `moment` (naive UTC): UTC + 8 hours, then the date."""
    return (moment + KUALA_LUMPUR_OFFSET).date()


def noon_session_observed_at(quote_date: dt.date) -> dt.datetime:
    """12:00 Kuala Lumpur on the quote date, as naive UTC (04:00)."""
    return dt.datetime.combine(quote_date, NOON_SESSION_UTC)


@dataclass(frozen=True)
class FxQuote:
    """One quote for one currency and one quote date.

    `middle_rate` 是来源公布的中间价（每 `unit` 单位多少 MYR）；`rate` 是 1 单位多少 MYR
    （= `middle_rate / unit`，精确）；`reference` 写进草稿的 `source_reference`。
    """

    quote_date: dt.date
    middle_rate: Decimal
    unit: int
    rate: Decimal
    reference: str


class FxSourceError(Exception):
    """The fetch failed; `code` goes into the fetch attempt. Never carries the response body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FxRateSource(Protocol):
    """Where FX quotes come from (spec §17.1「pluggable」)."""

    def fetch(self, currency: str, quote_date: dt.date) -> FxQuote | None:
        """The quote for `quote_date`, or `None` when the source says it has none for that date.

        失败抛 `FxSourceError`（带错误码）。
        """
        ...


class _ResponseLike(Protocol):
    status: int

    def read(self) -> bytes: ...

    def close(self) -> None: ...


# (请求, 超时秒数) → 响应。可注入：测试据此不联网。
type Transport = Callable[[urllib.request.Request, float], _ResponseLike]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx surfaces as an `HTTPError` with its own status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 传入 `HTTPRedirectHandler` 的子类会替换掉默认的那个（`build_opener` 的约定）。
_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def urllib_transport(request: urllib.request.Request, timeout: float) -> _ResponseLike:
    """The real transport: the module's opener, which does not follow redirects."""
    return _OPENER.open(request, timeout=timeout)


def restore_middle_rate(value: float) -> Decimal:
    """The decimal BNM published, from the binary64 number it printed (设计 §2).

    最短往返十进制表示：`Decimal(repr(x))`。必须有限、> 0、最多 6 位小数、整数部分不超过
    `rate` 列能放下的 14 位，否则 `BAD_PAYLOAD`。
    """
    if not math.isfinite(value):
        raise FxSourceError(BAD_PAYLOAD)
    restored = Decimal(repr(value))
    exponent = restored.as_tuple().exponent
    if (
        restored <= 0
        or restored >= _MIDDLE_RATE_LIMIT
        or not isinstance(exponent, int)
        or exponent < -MIDDLE_RATE_MAX_PLACES
    ):
        raise FxSourceError(BAD_PAYLOAD)
    return restored


def divide_exactly(middle_rate: Decimal, unit: int) -> Decimal:
    """`middle_rate / unit` with at most 10 decimal places, exactly; else `UNIT_NOT_EXACT`.

    整数运算判断整除（`middle_rate` 最多 6 位小数，乘以 10^10 必是整数），不经过任何舍入。
    """
    scaled = middle_rate.scaleb(RATE_PLACES)
    numerator = int(scaled)
    if numerator != scaled or numerator % unit != 0:
        raise FxSourceError(UNIT_NOT_EXACT)
    return Decimal(numerator // unit).scaleb(-RATE_PLACES)


def bnm_reference(currency: str, quote_date: dt.date, unit: int) -> str:
    """`source_reference` of a BNM draft (设计 §2 `fx_rate_versions`)."""
    return (
        f"bnm:exchange-rate:{currency}:{quote_date.isoformat()}"
        f":session={NOON_SESSION}:middle_rate:unit={unit}"
    )


def _is_int(value: object) -> bool:
    # JSON 的 true / false 解析成 bool，而 bool 是 int 的子类：不算数字。
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_constant(_name: str) -> object:
    # `NaN`、`Infinity` 不是合法 JSON，json 模块默认却收下它们。
    raise ValueError("not a JSON number")


def _load_json(body: bytes) -> object:
    try:
        return json.loads(body, parse_constant=_reject_constant)
    except ValueError:
        # JSONDecodeError 与 UnicodeDecodeError 都是 ValueError。
        raise FxSourceError(BAD_PAYLOAD) from None


def _is_no_records(body: bytes) -> bool:
    """`{"message":"No records found.","code":404}`, compared after parsing as JSON."""
    try:
        payload = _load_json(body)
    except FxSourceError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("message") == _NO_RECORDS_MESSAGE
        and _is_int(payload.get("code"))
        and payload.get("code") == _NOT_FOUND
    )


def parse_quote(body: bytes, currency: str, quote_date: dt.date) -> FxQuote:
    """Check a 200 body field by field and turn it into a quote (设计 §2「200 的响应体」)."""
    payload = _load_json(body)
    if not isinstance(payload, dict):
        raise FxSourceError(BAD_PAYLOAD)
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, dict) or not isinstance(meta, dict):
        raise FxSourceError(BAD_PAYLOAD)
    rate = data.get("rate")
    if not isinstance(rate, dict):
        raise FxSourceError(BAD_PAYLOAD)
    if (
        data.get("currency_code") != currency
        or rate.get("date") != quote_date.isoformat()
        or meta.get("session") != NOON_SESSION
        or meta.get("quote") != QUOTE_RM
    ):
        raise FxSourceError(BAD_PAYLOAD)
    unit = data.get("unit")
    if not _is_int(unit) or unit <= 0:
        raise FxSourceError(BAD_PAYLOAD)
    literal = rate.get("middle_rate")
    if not (_is_int(literal) or isinstance(literal, float)):
        # `null`、字符串、缺失都不行。
        raise FxSourceError(BAD_PAYLOAD)
    try:
        number = float(literal)
    except OverflowError:
        raise FxSourceError(BAD_PAYLOAD) from None
    middle_rate = restore_middle_rate(number)
    return FxQuote(
        quote_date=quote_date,
        middle_rate=middle_rate,
        unit=unit,
        rate=divide_exactly(middle_rate, unit),
        reference=bnm_reference(currency, quote_date, unit),
    )


class BnmFxSource:
    """BNM's public exchange-rate API, noon session, middle rate in RM."""

    def __init__(
        self, base_url: str, timeout_seconds: float, *, transport: Transport = urllib_transport
    ) -> None:
        # 配置已校验过；这里再挡一次，直接构造的实例同样只能走 HTTPS。
        self._base_url = require_https_base_url(base_url)
        self._timeout = timeout_seconds
        self._transport = transport

    def request_for(self, currency: str, quote_date: dt.date) -> urllib.request.Request:
        if not _CURRENCY_PATTERN.fullmatch(currency):
            raise ValueError("currency must be three upper-case letters")
        url = (
            f"{self._base_url}/public/exchange-rate/{currency}/date/{quote_date.isoformat()}"
            f"?session={NOON_SESSION}&quote={QUOTE_RM}"
        )
        return urllib.request.Request(url, headers={"Accept": ACCEPT_HEADER}, method="GET")

    def fetch(self, currency: str, quote_date: dt.date) -> FxQuote | None:
        status, body = self._send(self.request_for(currency, quote_date))
        if status == _NOT_FOUND and _is_no_records(body):
            return None
        if status != _OK:
            raise FxSourceError(f"HTTP_{status}")
        return parse_quote(body, currency, quote_date)

    def _send(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """(status, body). Non-2xx statuses come back as a status, not as an exception."""
        try:
            response = self._transport(request, self._timeout)
            try:
                return response.status, response.read()
            finally:
                response.close()
        except urllib.error.HTTPError as error:
            return error.code, _error_body(error)
        except TimeoutError:
            raise FxSourceError(TIMEOUT) from None
        except urllib.error.URLError as error:
            code = TIMEOUT if isinstance(error.reason, TimeoutError) else NETWORK
            raise FxSourceError(code) from None
        except (OSError, http.client.HTTPException):
            raise FxSourceError(NETWORK) from None


def _error_body(error: urllib.error.HTTPError) -> bytes:
    # 只用来判断是不是「无记录」；读不出来就当不是。
    try:
        return error.read()
    except (OSError, http.client.HTTPException):
        return b""
    finally:
        error.close()


def build_fx_source(settings: Settings) -> FxRateSource:
    """The one place that decides which FX source implementation is in use."""
    if settings.fx_source == "bnm":
        return BnmFxSource(settings.fx_bnm_base_url, settings.fx_fetch_timeout_seconds)
    raise ValueError(f"Unknown FX source: {settings.fx_source}")


__all__ = [
    "ACCEPT_HEADER",
    "BAD_PAYLOAD",
    "NETWORK",
    "TIMEOUT",
    "UNIT_NOT_EXACT",
    "BnmFxSource",
    "FxQuote",
    "FxRateSource",
    "FxSourceError",
    "Transport",
    "bnm_reference",
    "build_fx_source",
    "divide_exactly",
    "kuala_lumpur_date",
    "noon_session_observed_at",
    "parse_quote",
    "restore_middle_rate",
    "urllib_transport",
]
