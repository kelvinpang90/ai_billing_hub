"""Rendering a payment receipt PDF from its frozen snapshot (gate #589 v2 §2「PDF」).

AIH-TASK-199。下载接口属于 AIH-TASK-202（客户）与 203（管理端）。

⚠️ **只读快照**：不读任何表与当前配置（spec §106、INV-6）——改公司名、改定价、改税务政策都不改变
重现的收据。所以本模块不 import 任何会话、模型与配置模块，调用方在会话关闭之后再渲染。

⚠️ **先校验、再渲染**：快照任何一处不合规都抛 `ReceiptSnapshotInvalid`，不抛别的异常（下载接口
把它转成 500）。

⚠️ **可重现**：创建时刻取快照的 `issued_at`、不压缩内容流，同一份快照两次渲染的字节完全相同。

只用 Helvetica 标准字体（不嵌入字体文件）：它只支持 Latin-1，每个印出的字符串先把编不了的字符
换成 `?`（设计 §10 已知限制 1）。金额只经 `Decimal`，不经 float（INV-10）。
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from fpdf import FPDF, XPos, YPos

_SCHEMA_VERSION: Final = 1
_TOP_KEYS: Final = frozenset(
    {"schema_version", "receipt_number", "issued_at", "issuer", "customer", "payment", "tax_notice"}
)
_ISSUER_KEYS: Final = frozenset({"name"})
_CUSTOMER_KEYS: Final = frozenset({"company_name"})
_PAYMENT_KEYS: Final = frozenset(
    {
        "payment_id",
        "paid_at",
        "amount",
        "currency",
        "status",
        "gateway",
        "gateway_reference",
        "payment_method",
    }
)
_TAX_NOTICE_KEYS: Final = frozenset({"zh", "en"})

_RECEIPT_NUMBER: Final = re.compile(r"ACV-AIR-[0-9]{4}-[0-9]{6}")
_AMOUNT: Final = re.compile(r"[0-9]{1,6}\.[0-9]{2}")
_PAYMENT_METHOD: Final = re.compile(r"[a-z0-9_]{1,32}")
_FIRST_YEAR: Final = 2026
_LAST_YEAR: Final = 2099

_MALAYSIA: Final = dt.timezone(dt.timedelta(hours=8))
_CENT: Final = Decimal("0.01")
_METHOD_LABELS: Final = {"card": "Card", "fpx": "FPX"}
# 补发的收据与 Stripe 没给付款方式时快照里是 null（设计 §2「补发扫描」）。
_METHOD_UNKNOWN: Final = "Stripe"

_FONT: Final = "Helvetica"
_LABEL_WIDTH: Final = 45
_LINE_HEIGHT: Final = 7


class ReceiptSnapshotInvalid(Exception):
    """The snapshot does not have the shape of gate #589 v2 §2「快照」."""


def _section(container: dict[str, Any], key: str, keys: frozenset[str]) -> dict[str, Any]:
    value = container[key]
    if not isinstance(value, dict) or set(value) != keys:
        raise ReceiptSnapshotInvalid(f"snapshot section {key} has unexpected keys")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptSnapshotInvalid(f"snapshot field {name} is not a non-empty string")
    return value


def _moment(value: object, name: str) -> dt.datetime:
    text = _text(value, name)
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ReceiptSnapshotInvalid(f"snapshot field {name} is not a timestamp") from None
    if moment.tzinfo is not None or not _FIRST_YEAR <= moment.year <= _LAST_YEAR:
        raise ReceiptSnapshotInvalid(f"snapshot field {name} is out of range")
    return moment


def _matching(value: object, pattern: re.Pattern[str], name: str) -> str:
    # `fullmatch`：`$` 会放过结尾的换行。
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReceiptSnapshotInvalid(f"snapshot field {name} has an unexpected format")
    return value


def _constant(value: object, expected: str, name: str) -> str:
    if value != expected:
        raise ReceiptSnapshotInvalid(f"snapshot field {name} has an unexpected value")
    return expected


def _validate(snapshot: object) -> dict[str, Any]:
    """Check every rule of 设计 §2「PDF」; the snapshot itself is never modified."""
    if not isinstance(snapshot, dict) or set(snapshot) != _TOP_KEYS:
        raise ReceiptSnapshotInvalid("snapshot has unexpected keys")
    issuer = _section(snapshot, "issuer", _ISSUER_KEYS)
    customer = _section(snapshot, "customer", _CUSTOMER_KEYS)
    payment = _section(snapshot, "payment", _PAYMENT_KEYS)
    notice = _section(snapshot, "tax_notice", _TAX_NOTICE_KEYS)

    version = snapshot["schema_version"]
    # bool 是 int 的子类：`True == 1`，要单独挡掉。
    if not isinstance(version, int) or isinstance(version, bool) or version != _SCHEMA_VERSION:
        raise ReceiptSnapshotInvalid("snapshot schema_version is not supported")
    _matching(snapshot["receipt_number"], _RECEIPT_NUMBER, "receipt_number")
    _moment(snapshot["issued_at"], "issued_at")
    _moment(payment["paid_at"], "payment.paid_at")
    amount = _matching(payment["amount"], _AMOUNT, "payment.amount")
    if Decimal(amount) <= 0:
        raise ReceiptSnapshotInvalid("snapshot field payment.amount is not positive")
    _constant(payment["currency"], "MYR", "payment.currency")
    _constant(payment["status"], "PAID", "payment.status")
    _constant(payment["gateway"], "STRIPE", "payment.gateway")
    if payment["payment_method"] is not None:
        _matching(payment["payment_method"], _PAYMENT_METHOD, "payment.payment_method")
    _text(issuer["name"], "issuer.name")
    _text(customer["company_name"], "customer.company_name")
    _text(payment["payment_id"], "payment.payment_id")
    _text(payment["gateway_reference"], "payment.gateway_reference")
    _text(notice["zh"], "tax_notice.zh")
    _text(notice["en"], "tax_notice.en")
    return snapshot


def _latin1(text: str) -> str:
    """Replace what Helvetica cannot encode with `?` (fpdf2 raises on it otherwise)."""
    return "".join(char if ord(char) < 256 else "?" for char in text)


def _malaysia_time(raw: str) -> str:
    """A validated naive UTC timestamp as `YYYY-MM-DD HH:MM MYT`."""
    moment = dt.datetime.fromisoformat(raw).replace(tzinfo=dt.UTC).astimezone(_MALAYSIA)
    return f"{moment:%Y-%m-%d %H:%M} MYT"


def _display_money(raw: str) -> str:
    """`RM` plus 2 decimals, HALF_UP, thousands separators — the rule of `_display_money`.

    ⚠️ 先 `quantize(ROUND_HALF_UP)` 再格式化：`Decimal.__format__` 按上下文舍入（默认
    `ROUND_HALF_EVEN`）。量化结果为 0 时去掉符号，不显示 `-0.00`。
    """
    rounded = Decimal(raw).quantize(_CENT, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return f"RM {rounded:,.2f}"


def _payment_method(raw: str | None) -> str:
    if raw is None:
        return _METHOD_UNKNOWN
    return _METHOD_LABELS.get(raw, raw)


def render_receipt_pdf(snapshot: dict[str, Any]) -> bytes:
    """One A4 portrait page in English, rendered only from the snapshot.

    不合规的快照抛 `ReceiptSnapshotInvalid`。同一份快照两次渲染的字节相同。
    """
    _validate(snapshot)
    payment = snapshot["payment"]
    rows = (
        ("Receipt No.", snapshot["receipt_number"]),
        ("Issued", _malaysia_time(snapshot["issued_at"])),
        ("Received from", snapshot["customer"]["company_name"]),
        ("Payment date", _malaysia_time(payment["paid_at"])),
        ("Amount", _display_money(payment["amount"])),
        ("Currency", payment["currency"]),
        ("Payment method", _payment_method(payment["payment_method"])),
        ("Gateway reference", payment["gateway_reference"]),
        ("Payment ID", payment["payment_id"]),
        ("Status", payment["status"]),
    )
    issued_at = dt.datetime.fromisoformat(snapshot["issued_at"])

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    # 不压缩：一页的文件很小，也便于测试检索文字。
    pdf.set_compression(False)
    # 恰好一页：长公司名在单元格里换行，不让 fpdf2 自动翻页。
    pdf.set_auto_page_break(auto=False)
    # 可重现：创建时刻取快照（不带时区的 UTC），fpdf2 的文件标识也由它与内容算出。
    pdf.set_creation_date(issued_at.replace(tzinfo=dt.UTC))
    pdf.set_title(_latin1(snapshot["receipt_number"]))
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    pdf.set_font(_FONT, "B", 18)
    pdf.cell(0, 10, "PAYMENT RECEIPT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(_FONT, "", 12)
    pdf.multi_cell(
        0, 8, _latin1(snapshot["issuer"]["name"]), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT
    )
    pdf.ln(8)

    for label, value in rows:
        pdf.set_font(_FONT, "B", 11)
        pdf.cell(_LABEL_WIDTH, _LINE_HEIGHT, label)
        pdf.set_font(_FONT, "", 11)
        pdf.multi_cell(
            0, _LINE_HEIGHT, _latin1(value), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

    pdf.ln(10)
    pdf.set_font(_FONT, "", 9)
    pdf.multi_cell(
        0, 5, _latin1(snapshot["tax_notice"]["en"]), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT
    )
    return bytes(pdf.output())


__all__ = ["ReceiptSnapshotInvalid", "render_receipt_pdf"]
