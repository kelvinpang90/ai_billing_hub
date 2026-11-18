"""Rendering a monthly statement PDF from its frozen snapshot (gate #708 v3 §2「PDF」).

AIH-TASK-216。下载接口属于 AIH-TASK-217。

⚠️ **只读三个输入**：快照、`statement_number` 与 `finalized_at`（后两者是定稿行上不可变的列）。
不读任何表与当前配置（spec §106、§65，INV-6）——改公司名、改项目名、改定价、改税务政策都不改变
重渲染的结果。所以本模块不 import 会话、模型、配置、`app/services/statements.py` 与
`app/services/receipts.py`（后者带着模型与 SQLAlchemy，设计 §9 备选 G），调用方在会话关闭
之后再渲染。

⚠️ **先校验、再渲染**：输入任何一处不合规都抛 `StatementSnapshotInvalid`，不抛别的异常；键集合
恰好相等，多一个键也拒绝（INV-7）；不复核算术（设计 §9 备选 D）。

⚠️ **可重现**：创建时刻取 `finalized_at`、不压缩内容流、月份缩写不经 locale，同一份输入在任何
进程里渲染的字节都相同（含中文字体子集）。

字体照 `app/services/pdf_text.py`（与收据共用，设计 #750 §2「共用模块」）：全 Latin-1 的字符串用
Helvetica（标准字体，不嵌入）；其他的用 Noto Sans SC，fpdf2 只把用到的字形子集嵌进 PDF。两种字体都
印不出的字符换成 `?`。金额只经 `Decimal`，不经 float（INV-10）。
"""

from __future__ import annotations

import calendar
import contextlib
import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from fpdf import FPDF, FontFace, XPos, YPos

from app.services.pdf_text import HELVETICA, NOTO_FONT_SHA256, PdfFonts, PdfFontUnavailable

# 与 `app.services.receipts.RECEIPT_ISSUER_NAME` 相同（测试断言）；不 import 它，见文档串。
# ⚠️ 不在快照里：改它会改变历史对账单重渲染的结果，要过设计闸门（设计 §10 已知限制 1）。
STATEMENT_ISSUER_NAME: Final = "Acuven Technology Sdn Bhd"
# 公共名字保留为共用模块的别名（设计 #750 §2「对账单」）：
# `app/data/NotoSansSC-Regular.ttf` 的 SHA-256。
# ⚠️ 换字体会改变历史对账单与收据 PDF 的字形，改这个值要过设计闸门。
STATEMENT_FONT_SHA256: Final = NOTO_FONT_SHA256

_SCHEMA_VERSION: Final = 1
_TOP_KEYS: Final = frozenset(
    {
        "schema_version",
        "period",
        "currency",
        "company_name",
        "tax_notice",
        "opening_balance",
        "total_topup",
        "total_ai_usage",
        "total_adjustments",
        "prior_period",
        "closing_balance",
        "request_count",
        "conversation_count",
        "input_tokens",
        "output_tokens",
        "total_customer_charges",
        "by_provider",
        "by_model",
        "by_project",
    }
)
_TAX_NOTICE_KEYS: Final = frozenset({"zh", "en"})
_PRIOR_PERIOD_KEYS: Final = frozenset({"usage", "rebill", "total"})
_PRIOR_USAGE_KEYS: Final = frozenset({"usage_period", "request_count", "amount"})
_LINE_COUNTS: Final = ("request_count", "input_tokens", "output_tokens")
_PROVIDER_KEYS: Final = frozenset({"provider_code", *_LINE_COUNTS, "charges"})
_MODEL_KEYS: Final = frozenset({"provider_code", "model_code", *_LINE_COUNTS, "charges"})
_PROJECT_KEYS: Final = frozenset({"project_public_id", "project_name", *_LINE_COUNTS, "charges"})
# 明细列表 → (键集合, 其中的字符串字段)。
_LINE_SECTIONS: Final = {
    "by_provider": (_PROVIDER_KEYS, ("provider_code",)),
    "by_model": (_MODEL_KEYS, ("provider_code", "model_code")),
    "by_project": (_PROJECT_KEYS, ("project_public_id", "project_name")),
}
_TOP_AMOUNTS: Final = (
    "opening_balance",
    "total_topup",
    "total_ai_usage",
    "total_adjustments",
    "closing_balance",
    "total_customer_charges",
)
_TOP_COUNTS: Final = ("request_count", "conversation_count", "input_tokens", "output_tokens")

_STATEMENT_NUMBER: Final = re.compile(r"ST-([0-9]{6})-[0-9A-HJKMNP-TV-Z]{8}")
_PERIOD: Final = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_AMOUNT: Final = re.compile(r"-?[0-9]{1,12}\.[0-9]{8}")
_FIRST_YEAR: Final = 2026
_LAST_YEAR: Final = 2999
_CURRENCY: Final = "MYR"

_MALAYSIA: Final = dt.timezone(dt.timedelta(hours=8))
_CENT: Final = Decimal("0.01")
# ⚠️ 不用 `strftime("%b")`：它随进程 locale 变化，破坏可重现。
_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_HELVETICA: Final = HELVETICA

_MARGIN: Final = 15
_PAGE_BREAK_MARGIN: Final = 20
_CONTENT_WIDTH: Final = 180
_LABEL_WIDTH: Final = 40
_LINE_HEIGHT: Final = 6
_ROW_HEIGHT: Final = 5
_SUMMARY_WIDTHS: Final = (120, 60)
_SUMMARY_ALIGNS: Final = ("LEFT", "RIGHT")
# 明细表里文字列（供应商 / 模型 / 项目）合占的宽度；数值列合占其余 110。
_TEXT_WIDTH: Final = 70
_HEADINGS: Final = FontFace(family=_HELVETICA, emphasis="BOLD")
_NO_USAGE: Final = "No usage in this period."
_PRIOR_PERIOD_NOTE: Final = (
    "Usage from an earlier month that was processed after that month's cut-off "
    "(00:00 MYT on the 2nd day of the following month) is shown here under its original "
    "usage period."
)
_ROUNDING_NOTE: Final = (
    "Amounts are shown in MYR rounded to 2 decimal places. Each total is rounded from its exact "
    "value, so it may differ slightly from the sum of the rounded lines."
)
_ATTRIBUTION_NOTE: Final = (
    "The closing balance is calculated by period attribution (T+1 cut-off) and may differ from "
    "the wallet balance at the end of the month."
)
_COUNT_HEADINGS: Final = ("Requests", "Input tokens", "Output tokens", "Charges")


class StatementSnapshotInvalid(Exception):
    """The inputs do not have the shape of gate #708 v3 §2「校验」."""


# 同一个类（`app/api/statement_download.py` 的 `except` 照常命中）。
StatementFontUnavailable = PdfFontUnavailable


# --- 校验 -------------------------------------------------------------------------------


def _keys(value: object, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StatementSnapshotInvalid(f"snapshot section {name} has unexpected keys")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise StatementSnapshotInvalid(f"snapshot field {name} is not a non-empty string")
    return value


def _matching(value: object, pattern: re.Pattern[str], name: str) -> str:
    # `fullmatch`：`$` 会放过结尾的换行。
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StatementSnapshotInvalid(f"snapshot field {name} has an unexpected format")
    return value


def _amount(value: object, name: str) -> None:
    _matching(value, _AMOUNT, name)


def _count(value: object, name: str) -> None:
    # bool 是 int 的子类：`True == 1`，要单独挡掉。
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise StatementSnapshotInvalid(f"snapshot field {name} is not a non-negative integer")


def _period(value: object, name: str) -> str:
    text = _matching(value, _PERIOD, name)
    if not _FIRST_YEAR <= int(text[:4]) <= _LAST_YEAR:
        raise StatementSnapshotInvalid(f"snapshot field {name} is out of range")
    return text


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise StatementSnapshotInvalid(f"snapshot field {name} is not a list")
    return value


def _validate(snapshot: object, statement_number: object, finalized_at: object) -> None:
    """Check every rule of 设计 §2「校验」; nothing is modified and no arithmetic is re-checked."""
    top = _keys(snapshot, _TOP_KEYS, "snapshot")
    version = top["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != _SCHEMA_VERSION:
        raise StatementSnapshotInvalid("snapshot schema_version is not supported")
    period = _period(top["period"], "period")

    if not isinstance(statement_number, str):
        raise StatementSnapshotInvalid("statement_number is not a string")
    number = _STATEMENT_NUMBER.fullmatch(statement_number)
    if number is None or number.group(1) != period.replace("-", ""):
        raise StatementSnapshotInvalid("statement_number has an unexpected format")
    if not isinstance(finalized_at, dt.datetime) or finalized_at.tzinfo is not None:
        raise StatementSnapshotInvalid("finalized_at is not a naive datetime")

    if top["currency"] != _CURRENCY:
        raise StatementSnapshotInvalid("snapshot field currency has an unexpected value")
    _text(top["company_name"], "company_name")
    notice = _keys(top["tax_notice"], _TAX_NOTICE_KEYS, "tax_notice")
    _text(notice["zh"], "tax_notice.zh")
    _text(notice["en"], "tax_notice.en")
    for key in _TOP_AMOUNTS:
        _amount(top[key], key)
    for key in _TOP_COUNTS:
        _count(top[key], key)

    prior = _keys(top["prior_period"], _PRIOR_PERIOD_KEYS, "prior_period")
    _amount(prior["rebill"], "prior_period.rebill")
    _amount(prior["total"], "prior_period.total")
    for item in _list(prior["usage"], "prior_period.usage"):
        usage = _keys(item, _PRIOR_USAGE_KEYS, "prior_period.usage[]")
        usage_period = _period(usage["usage_period"], "prior_period.usage[].usage_period")
        if not usage_period < period:
            raise StatementSnapshotInvalid("prior_period usage_period is not before period")
        _count(usage["request_count"], "prior_period.usage[].request_count")
        _amount(usage["amount"], "prior_period.usage[].amount")

    for section, (keys, texts) in _LINE_SECTIONS.items():
        for item in _list(top[section], section):
            line = _keys(item, keys, f"{section}[]")
            for key in texts:
                _text(line[key], f"{section}[].{key}")
            for key in _LINE_COUNTS:
                _count(line[key], f"{section}[].{key}")
            _amount(line["charges"], f"{section}[].charges")


# --- 显示 -------------------------------------------------------------------------------


def _display_money(raw: str) -> str:
    """`MYR` plus 2 decimals, HALF_UP, thousands separators; the sign is kept as stored.

    ⚠️ 先 `quantize(ROUND_HALF_UP)` 再格式化：`Decimal.__format__` 按上下文舍入（默认
    `ROUND_HALF_EVEN`）。量化结果为 0 时去掉符号，不显示 `-0.00`。
    """
    rounded = Decimal(raw).quantize(_CENT, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return f"MYR {rounded:,.2f}"


def _display_count(value: int) -> str:
    return f"{value:,}"


def _billing_period(period: str) -> str:
    """`2026-12 (1 Dec 2026 - 31 Dec 2026, MYT)`."""
    year, month = int(period[:4]), int(period[5:])
    last_day = calendar.monthrange(year, month)[1]
    name = _MONTHS[month - 1]
    return f"{period} (1 {name} {year} - {last_day} {name} {year}, MYT)"


def _malaysia_time(moment: dt.datetime) -> str:
    """A naive UTC datetime as `YYYY-MM-DD HH:MM MYT`."""
    local = moment.replace(tzinfo=dt.UTC).astimezone(_MALAYSIA)
    return f"{local:%Y-%m-%d %H:%M} MYT"


# --- 版面 -------------------------------------------------------------------------------


class _StatementPdf(FPDF):
    def __init__(self, statement_number: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self._statement_number = statement_number

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(_HELVETICA, "", 9)
        self.cell(
            0,
            _ROW_HEIGHT,
            f"Monthly statement {self._statement_number} (continued)",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
        self.ln(3)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(_HELVETICA, "", 8)
        # `{nb}` 是 `alias_nb_pages()` 的别名，输出时换成总页数。
        self.cell(0, 10, f"{self._statement_number} | Page {self.page_no()} of {{nb}}", align="C")


class _Layout:
    def __init__(self, pdf: _StatementPdf, fonts: PdfFonts) -> None:
        self._pdf = pdf
        self._fonts = fonts

    def paragraph(self, text: str, size: int, height: float) -> None:
        family, text = self._fonts.prepare(text)
        self._pdf.set_font(family, "", size)
        self._pdf.multi_cell(0, height, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def labelled(self, label: str, value: str) -> None:
        self._pdf.set_font(_HELVETICA, "B", 10)
        self._pdf.cell(_LABEL_WIDTH, _LINE_HEIGHT, label)
        family, value = self._fonts.prepare(value)
        self._pdf.set_font(family, "", 10)
        self._pdf.multi_cell(0, _LINE_HEIGHT, value, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def section(self, title: str) -> None:
        self._pdf.ln(5)
        self._pdf.set_font(_HELVETICA, "B", 12)
        self._pdf.cell(0, 8, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def table(
        self,
        widths: tuple[int, ...],
        aligns: tuple[str, ...],
        headings: tuple[str, ...] | None,
        rows: list[tuple[str, ...]],
    ) -> None:
        """One `FPDF.table()`; every cell gets the font its own text needs.

        fpdf2 在放不下一整行时先翻页再画这一行（一行不跨页拆开），续页重复表头。
        """
        self._pdf.set_font(_HELVETICA, "", 9)
        with self._pdf.table(
            width=_CONTENT_WIDTH,
            col_widths=widths,
            text_align=aligns,
            first_row_as_headings=headings is not None,
            headings_style=_HEADINGS,
            repeat_headings=1,
            line_height=_ROW_HEIGHT,
        ) as table:
            if headings is not None:
                heading = table.row()
                for text in headings:
                    heading.cell(text, style=_HEADINGS)
            for values in rows:
                row = table.row()
                for value in values:
                    family, text = self._fonts.prepare(value)
                    row.cell(text, style=FontFace(family=family))
        self._pdf.ln(2)

    def lines(
        self,
        leading: tuple[str, ...],
        lines: list[dict[str, Any]],
        texts: tuple[str, ...],
    ) -> None:
        """A usage breakdown (设计 §2「版面」第 4–6 节), in snapshot order."""
        if not lines:
            self.paragraph(_NO_USAGE, 9, _ROW_HEIGHT)
            return
        widths = (*(_TEXT_WIDTH // len(texts) for _ in texts), 22, 28, 28, 32)
        aligns = (*("LEFT" for _ in texts), "RIGHT", "RIGHT", "RIGHT", "RIGHT")
        rows = [
            (
                *(line[key] for key in texts),
                _display_count(line["request_count"]),
                _display_count(line["input_tokens"]),
                _display_count(line["output_tokens"]),
                _display_money(line["charges"]),
            )
            for line in lines
        ]
        self.table(widths, aligns, (*leading, *_COUNT_HEADINGS), rows)


def render_statement_pdf(
    snapshot: dict[str, Any], *, statement_number: str, finalized_at: dt.datetime
) -> bytes:
    """An A4 portrait statement in English labels, rendered only from the three inputs.

    不合规的输入抛 `StatementSnapshotInvalid`；需要中文字体而字体读不了时抛
    `StatementFontUnavailable`。同一份输入任何时候渲染的字节都相同。
    """
    _validate(snapshot, statement_number, finalized_at)
    prior = snapshot["prior_period"]

    pdf = _StatementPdf(statement_number)
    # 不压缩：便于测试检索文字，也不让压缩库的版本影响字节。
    pdf.set_compression(False)
    # 可重现：创建时刻取定稿时刻（不带时区的 UTC），fpdf2 的文件标识也由它与内容算出。
    pdf.set_creation_date(finalized_at.replace(tzinfo=dt.UTC))
    pdf.set_title(statement_number)
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
    pdf.set_auto_page_break(auto=True, margin=_PAGE_BREAK_MARGIN)
    pdf.alias_nb_pages()

    with contextlib.ExitStack() as stack:
        layout = _Layout(pdf, PdfFonts(pdf, stack))
        pdf.add_page()

        pdf.set_font(_HELVETICA, "B", 18)
        pdf.cell(0, 10, "MONTHLY STATEMENT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        layout.paragraph(STATEMENT_ISSUER_NAME, 12, 7)
        pdf.ln(4)
        layout.labelled("Statement No.", statement_number)
        layout.labelled("Billing period", _billing_period(snapshot["period"]))
        layout.labelled("Customer", snapshot["company_name"])
        layout.labelled("Currency", snapshot["currency"])
        layout.labelled("Finalized", _malaysia_time(finalized_at))

        layout.section("Balance summary")
        layout.table(
            _SUMMARY_WIDTHS,
            _SUMMARY_ALIGNS,
            None,
            [
                ("Opening balance", _display_money(snapshot["opening_balance"])),
                ("Top-ups", _display_money(snapshot["total_topup"])),
                ("AI usage this period", _display_money(snapshot["total_ai_usage"])),
                ("Adjustments", _display_money(snapshot["total_adjustments"])),
                ("Prior-period adjustments", _display_money(prior["total"])),
                ("Closing balance", _display_money(snapshot["closing_balance"])),
            ],
        )

        layout.section("Prior-period adjustments")
        layout.paragraph(_PRIOR_PERIOD_NOTE, 9, _ROW_HEIGHT)
        pdf.ln(2)
        layout.table(
            (80, 40, 60),
            ("LEFT", "RIGHT", "RIGHT"),
            ("Usage period", "Requests", "Amount"),
            [
                *(
                    (
                        item["usage_period"],
                        _display_count(item["request_count"]),
                        _display_money(item["amount"]),
                    )
                    for item in prior["usage"]
                ),
                ("Rebill", "-", _display_money(prior["rebill"])),
                ("Total", "-", _display_money(prior["total"])),
            ],
        )

        layout.section("Usage summary")
        layout.table(
            _SUMMARY_WIDTHS,
            _SUMMARY_ALIGNS,
            None,
            [
                ("Requests", _display_count(snapshot["request_count"])),
                ("Conversations", _display_count(snapshot["conversation_count"])),
                ("Input tokens", _display_count(snapshot["input_tokens"])),
                ("Output tokens", _display_count(snapshot["output_tokens"])),
                ("Total customer charges", _display_money(snapshot["total_customer_charges"])),
            ],
        )

        layout.section("Usage by provider")
        layout.lines(("Provider",), snapshot["by_provider"], ("provider_code",))
        layout.section("Usage by model")
        layout.lines(("Provider", "Model"), snapshot["by_model"], ("provider_code", "model_code"))
        layout.section("Usage by project")
        # 只印项目名，不印 `project_public_id`（内部 id，设计 §6）。
        layout.lines(("Project",), snapshot["by_project"], ("project_name",))

        layout.section("Notes")
        layout.paragraph(_ROUNDING_NOTE, 9, _ROW_HEIGHT)
        pdf.ln(1)
        layout.paragraph(_ATTRIBUTION_NOTE, 9, _ROW_HEIGHT)
        pdf.ln(1)
        # 原文：不改写、不拼接，保留其中的换行。
        layout.paragraph(snapshot["tax_notice"]["en"], 9, _ROW_HEIGHT)

        # 字体文件在输出（子集化）之后才释放。
        return bytes(pdf.output())


__all__ = [
    "STATEMENT_FONT_SHA256",
    "STATEMENT_ISSUER_NAME",
    "StatementFontUnavailable",
    "StatementSnapshotInvalid",
    "render_statement_pdf",
]
