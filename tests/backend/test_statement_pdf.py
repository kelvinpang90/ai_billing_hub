"""Monthly statement PDF rendering (gate #708 v3 §2「PDF」, §7 rows for AIH-TASK-216).

内容流不压缩：Helvetica 的文字是 Latin-1 的 `(…)` 字面量，fpdf2 会把其中的 `\\`、`(`、`)` 转义，
所以检索前先用 `_literal` 转义同样的字符。Noto Sans SC 的文字是子集里的两字节编号，经 ToUnicode
CMap 还原（`_unicode_texts`）。页数数 `/Type /Page` 对象（不含 `/Type /Pages`）。
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import pytest
from fontTools.ttLib import TTFont
from fpdf import FPDF

from app.services import pdf_text, statement_pdf
from app.services.pdf_text import PdfFontUnavailable
from app.services.statement_pdf import (
    STATEMENT_FONT_SHA256,
    STATEMENT_ISSUER_NAME,
    StatementFontUnavailable,
    StatementSnapshotInvalid,
    render_statement_pdf,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_OBJECT = re.compile(rb"(?<![0-9])([0-9]+)\s+0\s+obj\b(.*?)\bendobj", re.S)
_CONTENTS = re.compile(rb"/Contents\s*(\[[^\]]*\]|[0-9]+\s+0\s+R)")
_REFERENCE = re.compile(rb"([0-9]+)\s+0\s+R")
_LITERAL_TJ = re.compile(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", re.S)
_HEX_TJ = re.compile(rb"<([0-9A-Fa-f]*)>\s*Tj")
_LITERAL_OPERAND = rb"\((?:\\.|[^\\)])*\)"
_HEX_OPERAND = rb"<[0-9A-Fa-f]*>"
# 一次文字输出：`(…) Tj`、`<…> Tj` 或 `[(…) -120 <…>] TJ`。
_SHOW_TEXT = re.compile(
    rb"("
    + _LITERAL_OPERAND
    + rb"|"
    + _HEX_OPERAND
    + rb")\s*Tj|\[((?:"
    + _LITERAL_OPERAND
    + rb"|"
    + _HEX_OPERAND
    + rb"|[^\]()<])*)\]\s*TJ",
    re.S,
)
_STRING_OPERAND = re.compile(rb"\(((?:\\.|[^\\)])*)\)|<([0-9A-Fa-f]*)>", re.S)
_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFCHAR_PAIR = re.compile(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]+)>")

_NUMBER = "ST-202612-0123ABCD"
# 17:05:09 UTC 是马来西亚的次日 01:05。
_FINALIZED = dt.datetime(2027, 1, 1, 17, 5, 9)
_PROJECT_ID = "00000000-0000-0000-0000-000000000000"


def _line(**values: Any) -> dict[str, Any]:
    return {"request_count": 0, "input_tokens": 0, "output_tokens": 0, **values}


def _snapshot() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "period": "2026-12",
        "currency": "MYR",
        "company_name": "Example Trading Sdn Bhd",
        "tax_notice": {
            "zh": "本对账单不是电子发票。",
            "en": "This statement is not an LHDN e-Invoice.",
        },
        "opening_balance": "1000.00000000",
        "total_topup": "500.00000000",
        "total_ai_usage": "-123.45678901",
        "total_adjustments": "10.00000000",
        "prior_period": {
            "usage": [
                {"usage_period": "2026-10", "request_count": 3, "amount": "-1.23456789"},
                {"usage_period": "2026-11", "request_count": 5, "amount": "-2.00000000"},
            ],
            "rebill": "0.50000000",
            "total": "-2.73456789",
        },
        "closing_balance": "1383.80864310",
        "request_count": 1234,
        "conversation_count": 56,
        "input_tokens": 1234567,
        "output_tokens": 89012,
        "total_customer_charges": "123.45678901",
        "by_provider": [
            _line(
                provider_code="anthropic",
                request_count=1000,
                input_tokens=1000000,
                output_tokens=80000,
                charges="100.00000000",
            ),
            _line(
                provider_code="openai",
                request_count=234,
                input_tokens=234567,
                output_tokens=9012,
                charges="23.45678901",
            ),
        ],
        "by_model": [
            _line(
                provider_code="anthropic",
                model_code="claude-sonnet-5-5",
                request_count=1000,
                input_tokens=1000000,
                output_tokens=80000,
                charges="100.00000000",
            ),
            _line(
                provider_code="openai",
                model_code="gpt-5",
                request_count=234,
                input_tokens=234567,
                output_tokens=9012,
                charges="23.45678901",
            ),
        ],
        "by_project": [
            _line(
                project_public_id=_PROJECT_ID,
                project_name="Support Bot",
                request_count=1200,
                input_tokens=1200000,
                output_tokens=88000,
                charges="120.00000000",
            ),
            _line(
                project_public_id="00000000-0000-0000-0000-000000000001",
                project_name="Sales Assistant",
                request_count=34,
                input_tokens=34567,
                output_tokens=1012,
                charges="3.45678901",
            ),
        ],
    }


def _chinese_snapshot() -> dict[str, Any]:
    snapshot = _snapshot()
    snapshot["company_name"] = "示例科技 Sdn Bhd"
    snapshot["by_project"][0]["project_name"] = "客服项目"
    return snapshot


def _render(snapshot: dict[str, Any] | None = None) -> bytes:
    return render_statement_pdf(
        _snapshot() if snapshot is None else snapshot,
        statement_number=_NUMBER,
        finalized_at=_FINALIZED,
    )


def _literal(text: str) -> bytes:
    """`text` as fpdf2 writes a Helvetica string into the content stream."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("latin-1") + b")"


def _unescape(raw: bytes) -> bytes:
    return re.sub(rb"\\(.)", lambda m: b"\r" if m.group(1) == b"r" else m.group(1), raw, flags=re.S)


def _page_streams(pdf: bytes) -> list[bytes]:
    """The content of every page, found through the `/Contents` of each `/Type /Page` object.

    一页的 `/Contents` 是数组时，几段内容流按顺序接起来。
    """
    objects = {int(number): body for number, body in _OBJECT.findall(pdf)}
    pages = []
    for body in objects.values():
        if not _PAGE_OBJECT.search(body):
            continue
        contents = _CONTENTS.search(body)
        assert contents is not None, body[:200]
        parts = []
        for reference in _REFERENCE.findall(contents.group(1)):
            stream = _STREAM.search(objects[int(reference)])
            assert stream is not None, reference
            parts.append(stream.group(1))
        pages.append(b"\n".join(parts))
    return pages


def _decode_operand(operand: re.Match[bytes]) -> str:
    if operand.group(1) is not None:
        return _unescape(operand.group(1)).decode("latin-1")
    return bytes.fromhex(operand.group(2).decode("ascii")).decode("latin-1")


def _fragments(stream: bytes) -> list[str]:
    """Every Helvetica text shown in `stream`, in drawing order; a `TJ` array is one fragment."""
    fragments = []
    for show in _SHOW_TEXT.finditer(stream):
        operands = show.group(1) if show.group(1) is not None else show.group(2)
        fragments.append("".join(_decode_operand(o) for o in _STRING_OPERAND.finditer(operands)))
    return fragments


def _texts(pdf: bytes) -> list[str]:
    """Every Helvetica `(…) Tj` string of the page content, in drawing order."""
    return [
        _unescape(raw).decode("latin-1")
        for stream in _page_streams(pdf)
        for raw in _LITERAL_TJ.findall(stream)
    ]


def _after(texts: list[str], label: str) -> str:
    """The cell drawn right after the first `label` cell (the value of a two-column row)."""
    return texts[texts.index(label) + 1]


def _pages(pdf: bytes) -> int:
    return len(_PAGE_OBJECT.findall(pdf))


def _to_unicode(pdf: bytes) -> dict[bytes, str]:
    mapping: dict[bytes, str] = {}
    for section in _BFCHAR.findall(pdf):
        for code, target in _BFCHAR_PAIR.findall(section):
            mapping[bytes.fromhex(code.decode("ascii"))] = bytes.fromhex(
                target.decode("ascii")
            ).decode("utf-16-be")
    return mapping


def _unicode_texts(pdf: bytes) -> list[str]:
    """Every Noto Sans SC string of the page content, decoded through the ToUnicode CMap.

    只收每两个字节都在 CMap 里的字符串：Helvetica 的 ASCII 两两拼起来是 0x2020 以上的编号，
    子集的编号远小于它。
    """
    mapping = _to_unicode(pdf)
    raws: list[bytes] = []
    for stream in _page_streams(pdf):
        raws.extend(_unescape(raw) for raw in _LITERAL_TJ.findall(stream))
        raws.extend(bytes.fromhex(raw.decode("ascii")) for raw in _HEX_TJ.findall(stream))
    texts = []
    for raw in raws:
        codes = [raw[index : index + 2] for index in range(0, len(raw), 2)]
        if raw and len(raw) % 2 == 0 and all(code in mapping for code in codes):
            texts.append("".join(mapping[code] for code in codes))
    return texts


def _bfchar_hex(pdf: bytes) -> str:
    return b"".join(_BFCHAR.findall(pdf)).decode("latin-1").upper()


# --- 正常路径 ---------------------------------------------------------------------------


def test_renders_every_field_of_an_ascii_statement() -> None:
    pdf = _render()

    assert pdf.startswith(b"%PDF-")
    for text in (
        "MONTHLY STATEMENT",
        "Acuven Technology Sdn Bhd",
        _NUMBER,
        "2026-12 (1 Dec 2026 - 31 Dec 2026, MYT)",
        "Example Trading Sdn Bhd",
        "2027-01-02 01:05 MYT",
        "MYR",
        # 标签与表头
        "Statement No.",
        "Billing period",
        "Customer",
        "Currency",
        "Finalized",
        "Balance summary",
        "Opening balance",
        "Top-ups",
        "AI usage this period",
        "Adjustments",
        "Prior-period adjustments",
        "Closing balance",
        "Usage period",
        "Requests",
        "Amount",
        "Rebill",
        "Total",
        "-",
        "Usage summary",
        "Conversations",
        "Input tokens",
        "Output tokens",
        "Total customer charges",
        "Usage by provider",
        "Provider",
        "Charges",
        "Usage by model",
        "Model",
        "Usage by project",
        "Project",
        "Notes",
        # 金额
        "MYR 1,000.00",
        "MYR 500.00",
        "MYR -123.46",
        "MYR 10.00",
        "MYR -2.73",
        "MYR 1,383.81",
        "MYR -1.23",
        "MYR -2.00",
        "MYR 0.50",
        "MYR 123.46",
        "MYR 100.00",
        "MYR 23.46",
        "MYR 120.00",
        "MYR 3.46",
        # 明细
        "2026-10",
        "2026-11",
        "anthropic",
        "openai",
        "claude-sonnet-5-5",
        "gpt-5",
        "Support Bot",
        "Sales Assistant",
        "1,234",
        "56",
        "1,234,567",
        "89,012",
        # 税务说明原文
        "This statement is not an LHDN e-Invoice.",
    ):
        assert _literal(text) in pdf, text
    for fragment in (
        b"Usage from an earlier month that was processed after that month's cut-off",
        b"Amounts are shown in MYR rounded to 2 decimal places.",
        b"The closing balance is calculated by period attribution",
    ):
        assert fragment in pdf, fragment
    # 没有嵌入字体；不印项目的内部 id。
    assert b"/FontFile2" not in pdf
    assert _PROJECT_ID.encode("ascii") not in pdf


def test_balance_summary_rows_pair_labels_with_snapshot_values() -> None:
    texts = _texts(_render())

    assert _after(texts, "Opening balance") == "MYR 1,000.00"
    assert _after(texts, "Top-ups") == "MYR 500.00"
    assert _after(texts, "AI usage this period") == "MYR -123.46"
    assert _after(texts, "Adjustments") == "MYR 10.00"
    assert _after(texts, "Prior-period adjustments") == "MYR -2.73"
    assert _after(texts, "Closing balance") == "MYR 1,383.81"
    assert _after(texts, "Rebill") == "-"
    assert _after(texts, "Conversations") == "56"
    assert _after(texts, "Total customer charges") == "MYR 123.46"


def test_pdf_has_no_cost_words() -> None:
    """INV-7 / ADR-0008 §5：只在内容流的文字里查，大小写不敏感。"""
    content = " ".join(_texts(_render())).lower()

    for word in ("cost", "markup", "margin", "profit", "sst", "tax amount"):
        assert word not in content, word


# --- 金额与数字 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("0.00500000", "MYR 0.01"),
        ("-0.00500000", "MYR -0.01"),
        ("-0.00400000", "MYR 0.00"),
        ("1234567.99500000", "MYR 1,234,568.00"),
        ("0.00000000", "MYR 0.00"),
        ("-0.00000000", "MYR 0.00"),
    ],
)
def test_amount_display_rounds_half_up_without_negative_zero(raw: str, shown: str) -> None:
    snapshot = _snapshot()
    snapshot["total_adjustments"] = raw

    pdf = _render(snapshot)

    assert _after(_texts(pdf), "Adjustments") == shown
    assert b"MYR -0.00" not in pdf


def test_totals_are_the_rounded_snapshot_totals_not_sums_of_lines() -> None:
    snapshot = _snapshot()
    snapshot.update(
        opening_balance="0.00500000",
        total_topup="0.00500000",
        total_ai_usage="0.00000000",
        total_adjustments="0.00000000",
        closing_balance="0.01000000",
    )
    snapshot["prior_period"] = {"usage": [], "rebill": "0.00000000", "total": "0.00000000"}

    pdf = _render(snapshot)
    texts = _texts(pdf)

    assert _after(texts, "Opening balance") == "MYR 0.01"
    assert _after(texts, "Top-ups") == "MYR 0.01"
    # 显示值相加是 0.02；期末印的是快照期末自己的舍入值。
    assert _after(texts, "Closing balance") == "MYR 0.01"
    assert _literal("MYR 0.02") not in pdf
    assert b"Each total is rounded from its exact" in pdf


def test_counts_use_thousands_separators() -> None:
    snapshot = _snapshot()
    snapshot["input_tokens"] = 1234567

    assert _after(_texts(_render(snapshot)), "Input tokens") == "1,234,567"


# --- 中文与字体 -------------------------------------------------------------------------


def test_chinese_text_embeds_a_noto_sans_sc_subset() -> None:
    pdf = _render(_chinese_snapshot())

    assert b"/FontFile2" in pdf
    assert re.search(rb"/BaseFont\s*/\S*NotoSansSC", pdf)
    hexadecimal = _bfchar_hex(pdf)
    for char in "示例科技客服项目":
        assert f"{ord(char):04X}" in hexadecimal, char
    texts = _unicode_texts(pdf)
    assert any("示例科技 Sdn Bhd" in text for text in texts), texts
    assert any("客服项目" in text for text in texts), texts
    # ASCII 的标签与其余项目名仍是 Helvetica 字面量。
    for text in ("MONTHLY STATEMENT", "Customer", "Usage by project", "Sales Assistant"):
        assert _literal(text) in pdf, text


def test_character_missing_from_the_font_becomes_question_mark_with_helvetica() -> None:
    assert 0x0BA4 not in pdf_text.font_cmap()
    snapshot = _snapshot()
    snapshot["company_name"] = "Café த Sdn Bhd"

    pdf = _render(snapshot)

    # 换掉 த 之后全是 Latin-1：仍用 Helvetica，不嵌入字体。
    assert _literal("Café ? Sdn Bhd") in pdf
    assert b"/FontFile2" not in pdf


def test_character_missing_from_the_font_becomes_question_mark_with_noto() -> None:
    assert 0x0BA4 not in pdf_text.font_cmap()
    snapshot = _snapshot()
    snapshot["company_name"] = "Café 中 த"

    pdf = _render(snapshot)

    assert b"/FontFile2" in pdf
    assert any("Café 中 ?" in text for text in _unicode_texts(pdf))
    assert "த" not in _to_unicode(pdf).values()


def test_tax_notice_keeps_newlines_and_replaces_other_control_characters() -> None:
    """设计 v3：Noto 字符串里换行符保留（multi_cell 换行），制表符等不在字体里的印成 `?`。"""
    cmap = pdf_text.font_cmap()
    assert ord("’") in cmap
    assert ord("\t") not in cmap
    snapshot = _snapshot()
    snapshot["tax_notice"]["en"] = "Customer’s copy\tA\nSecond line B"

    pdf = _render(snapshot)
    texts = _unicode_texts(pdf)

    assert any("Customer’s copy?A" in text for text in texts), texts
    assert any("Second line B" in text for text in texts), texts
    # 在换行符处断开：两段不在同一个字符串里。
    assert not any("A" in text and "Second" in text for text in texts), texts
    assert all("\t" not in text and "\n" not in text for text in texts), texts
    assert "\t" not in _to_unicode(pdf).values()


@pytest.fixture
def fresh_font_cache() -> Iterator[None]:
    pdf_text.font_cmap.cache_clear()
    yield
    pdf_text.font_cmap.cache_clear()


@pytest.mark.usefixtures("fresh_font_cache")
def test_missing_font_fails_only_statements_that_need_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pdf_text, "_font_resource", lambda: tmp_path / "missing.ttf")

    with pytest.raises(StatementFontUnavailable):
        _render(_chinese_snapshot())
    # 全 Latin-1 的对账单不读字体文件，照常渲染。
    assert _render(_snapshot()).startswith(b"%PDF-")


@pytest.mark.usefixtures("fresh_font_cache")
def test_font_that_is_not_truetype_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.ttf"
    broken.write_bytes(b"not a font at all")
    monkeypatch.setattr(pdf_text, "_font_resource", lambda: broken)

    with pytest.raises(StatementFontUnavailable):
        _render(_chinese_snapshot())


def test_font_file_is_pinned_and_licensed() -> None:
    data = _REPO_ROOT / "app" / "data"

    digest = hashlib.sha256((data / "NotoSansSC-Regular.ttf").read_bytes()).hexdigest()
    assert digest == STATEMENT_FONT_SHA256
    license_text = (data / "NotoSansSC-OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text


def test_public_font_names_are_the_shared_ones() -> None:
    """设计 #750 §2「对账单」：公共名字是共用模块的别名。"""
    assert STATEMENT_FONT_SHA256 == pdf_text.NOTO_FONT_SHA256
    assert StatementFontUnavailable is PdfFontUnavailable


# --- 对账单字节不变（设计 #750 §7，H-b）-----------------------------------------------
#
# 冻结参照：AIH-TASK-228 开工时 main 上 `app/services/statement_pdf.py` 第 254–323 行
# `_font_resource`、`_font_cmap`、`_is_latin1`、`_Fonts` 的逐字副本，只改成测试私有名；自带缓存，
# 读同一个字体文件。⚠️ 不要随共用模块一起改：它钉的正是搬移之前的行为。

_FROZEN_HELVETICA = "Helvetica"
_FROZEN_NOTO = "NotoSansSC"
_FROZEN_FONT_FILE = "NotoSansSC-Regular.ttf"
_FROZEN_LATIN1_END = 256
_FROZEN_NEWLINE = "\n"


def _frozen_font_resource() -> Traversable:
    return resources.files("app.data").joinpath(_FROZEN_FONT_FILE)


@functools.lru_cache(maxsize=1)
def _frozen_font_cmap() -> frozenset[int]:
    """Code points of the bundled Noto Sans SC, read once per process.

    并发下可能重复计算，结果相同，无害。读不了时抛 `StatementFontUnavailable`（不缓存异常）。
    """
    try:
        with resources.as_file(_frozen_font_resource()) as path:
            font = TTFont(path, lazy=True)
            try:
                # 只认 TrueType（`glyf` 轮廓）：CFF 轮廓与可变字体 fpdf2 的支持未验证
                # （设计 §2「P0」）。
                if "glyf" not in font:
                    raise StatementFontUnavailable("statement font is not TrueType")
                cmap = font.getBestCmap()
            finally:
                font.close()
    except StatementFontUnavailable:
        raise
    except Exception as error:  # noqa: BLE001 - 读不出的原因各式各样，都算「字体不可用」
        raise StatementFontUnavailable("statement font cannot be read") from error
    if not cmap:
        raise StatementFontUnavailable("statement font has no character map")
    return frozenset(cmap)


def _frozen_is_latin1(text: str) -> bool:
    return all(ord(char) < _FROZEN_LATIN1_END for char in text)


class _FrozenStatementFonts:
    """Choose Helvetica or Noto Sans SC per string (设计 §2「文本与字体」).

    Noto Sans SC 在本次渲染第一次需要时 `add_font` 一次；字体文件在 `stack` 关闭之前一直可读
    （fpdf2 懒加载字体表，输出时才子集化）。
    """

    def __init__(self, pdf: FPDF, stack: contextlib.ExitStack) -> None:
        self._pdf = pdf
        self._stack = stack
        self._added = False

    def prepare(self, text: str) -> tuple[str, str]:
        """`(family, text to print)`."""
        if _frozen_is_latin1(text):
            return _FROZEN_HELVETICA, text
        cmap = _frozen_font_cmap()
        text = "".join(
            char if ord(char) < _FROZEN_LATIN1_END or ord(char) in cmap else "?" for char in text
        )
        if _frozen_is_latin1(text):
            return _FROZEN_HELVETICA, text
        # 字体里没有的字符（含 Latin-1 的控制字符）不印缺字框；换行符是版面指令，保留。
        text = "".join(
            char if char == _FROZEN_NEWLINE or ord(char) in cmap else "?" for char in text
        )
        self._add_noto()
        return _FROZEN_NOTO, text

    def _add_noto(self) -> None:
        if self._added:
            return
        try:
            path = self._stack.enter_context(resources.as_file(_frozen_font_resource()))
            self._pdf.add_font(_FROZEN_NOTO, "", str(path))
        except Exception as error:  # noqa: BLE001 - 同 `_font_cmap`
            raise StatementFontUnavailable("statement font cannot be loaded") from error
        self._added = True


def _company(name: str) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        snapshot = _snapshot()
        snapshot["company_name"] = name
        return snapshot

    return build


def _tax_notice_with_controls() -> dict[str, Any]:
    snapshot = _snapshot()
    snapshot["tax_notice"]["en"] = "Customer’s copy\tA\nSecond line B"
    return snapshot


def _many_pages() -> dict[str, Any]:
    snapshot = _snapshot()
    snapshot["by_project"] = [
        _line(
            project_public_id=f"00000000-0000-0000-0000-{index:012d}",
            project_name=name,
            request_count=index,
            charges="1.00000000",
        )
        for index, name in enumerate(_project_names())
    ]
    return snapshot


_FROZEN_INPUTS: dict[str, Callable[[], dict[str, Any]]] = {
    "ascii": _snapshot,
    "chinese": _chinese_snapshot,
    "missing_glyph_helvetica": _company("Café த Sdn Bhd"),
    "missing_glyph_noto": _company("Café 中 த"),
    "tax_notice_controls": _tax_notice_with_controls,
    "three_hundred_lines": _many_pages,
}


@pytest.mark.parametrize("snapshot", list(_FROZEN_INPUTS.values()), ids=list(_FROZEN_INPUTS))
def test_statement_bytes_match_the_frozen_font_choice(
    monkeypatch: pytest.MonkeyPatch, snapshot: Callable[[], dict[str, Any]]
) -> None:
    current = _render(snapshot())

    monkeypatch.setattr(statement_pdf, "PdfFonts", _FrozenStatementFonts)
    frozen = _render(snapshot())

    assert current == frozen


def test_issuer_matches_the_receipt_issuer() -> None:
    from app.services.receipts import RECEIPT_ISSUER_NAME

    assert STATEMENT_ISSUER_NAME == RECEIPT_ISSUER_NAME


# --- 分页 -------------------------------------------------------------------------------


def _project_names() -> list[str]:
    names = []
    for index in range(1, 301):
        name = f"Prj{index:05d}"
        if index % 50 == 0:
            # 255 个字符：在单元格里换成好几行。
            name += "".join(f" Long{index:05d}{part:02d}" for part in range(20))
            name += " " + "Z" * (255 - len(name) - 1)
            assert len(name) == 255
        names.append(name)
    return names


def _has_project_row(fragments: list[str]) -> bool:
    return any(fragment.startswith("Prj") for fragment in fragments)


def test_long_breakdown_spans_pages_with_footer_header_and_repeated_headings() -> None:
    names = _project_names()
    snapshot = _snapshot()
    snapshot["by_project"] = [
        _line(
            project_public_id=f"00000000-0000-0000-0000-{index:012d}",
            project_name=name,
            request_count=index,
            charges="1.00000000",
        )
        for index, name in enumerate(names)
    ]

    pdf = _render(snapshot)

    total = _pages(pdf)
    assert total > 1
    streams = _page_streams(pdf)
    assert len(streams) == total
    by_page: dict[int, list[str]] = {}
    for stream in streams:
        fragments = _fragments(stream)
        # 页脚可能被拆成几段输出：把这一页的文字接起来再找。
        text = "".join(fragments)
        found = re.findall(r"\| Page ([0-9]+) of ([0-9]+)", text)
        assert len(found) == 1, (found, fragments[-5:], stream[-400:])
        page, of = (int(value) for value in found[0])
        assert of == total
        assert f"{_NUMBER} | Page {page} of {total}" in text, page
        continued = f"Monthly statement {_NUMBER} (continued)"
        assert (continued in text) == (page > 1), page
        assert page not in by_page, page
        by_page[page] = fragments
    assert sorted(by_page) == list(range(1, total + 1))
    continued_tables = 0
    for page, fragments in by_page.items():
        # 表格从上一页延续到这一页：表头重复。
        if page > 1 and _has_project_row(by_page[page - 1]) and _has_project_row(fragments):
            continued_tables += 1
            assert "Project" in fragments, page
    assert continued_tables >= 1
    for name in names:
        if len(name) < 255:
            assert _literal(name) in pdf, name
        else:
            for word in name.split():
                assert word.encode("ascii") in pdf, word


def test_empty_breakdowns_say_no_usage() -> None:
    snapshot = _snapshot()
    snapshot["by_provider"] = []
    snapshot["by_model"] = []
    snapshot["by_project"] = []
    snapshot["prior_period"]["usage"] = []

    pdf = _render(snapshot)

    assert pdf.count(_literal("No usage in this period.")) == 3
    assert _literal("Rebill") in pdf


# --- 可重现与元数据 ---------------------------------------------------------------------


@pytest.mark.parametrize("snapshot", [_snapshot, _chinese_snapshot], ids=["ascii", "chinese"])
def test_same_input_renders_identical_bytes(snapshot: Callable[[], dict[str, Any]]) -> None:
    assert _render(snapshot()) == _render(snapshot())


_RENDER_SCRIPT = textwrap.dedent(
    """
    import datetime as dt
    import json
    import sys

    sys.path.insert(0, sys.argv[1])

    from app.services.statement_pdf import render_statement_pdf

    payload = json.loads(sys.stdin.read())
    sys.stdout.buffer.write(
        render_statement_pdf(
            payload["snapshot"],
            statement_number=payload["statement_number"],
            finalized_at=dt.datetime.fromisoformat(payload["finalized_at"]),
        )
    )
    """
)


def _render_in_subprocess(snapshot: dict[str, Any], **environment: str) -> bytes:
    payload = {
        "snapshot": snapshot,
        "statement_number": _NUMBER,
        "finalized_at": _FINALIZED.isoformat(),
    }
    done = subprocess.run(
        [sys.executable, "-c", _RENDER_SCRIPT, str(_REPO_ROOT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=_REPO_ROOT,
        env={**os.environ, **environment},
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout


def test_chinese_statement_renders_identical_bytes_across_processes() -> None:
    """哈希随机化与 fontTools 的 `head.modified`（当前时间或 SOURCE_DATE_EPOCH）都不影响字节。"""
    snapshot = _chinese_snapshot()

    first = _render_in_subprocess(snapshot, PYTHONHASHSEED="1", SOURCE_DATE_EPOCH="0")
    second = _render_in_subprocess(snapshot, PYTHONHASHSEED="2", SOURCE_DATE_EPOCH="2000000000")

    assert b"/FontFile2" in first
    assert first == second
    assert first == _render(snapshot)


def test_metadata_comes_from_the_inputs() -> None:
    pdf = _render()

    # 信息字典里的字符串可能是 `(…)` 字面量，也可能是 UTF-16BE 的十六进制串。
    assert _info_text(pdf, "/CreationDate", "D:20270101170509")
    assert _info_text(pdf, "/Title", _NUMBER)


def _info_text(pdf: bytes, key: str, text: str) -> bool:
    literal = f"{key} ({text}".encode("ascii")
    hexadecimal = f"{key} <FEFF{text.encode('utf-16-be').hex()}".upper().encode("ascii")
    return literal in pdf or hexadecimal in pdf.upper()


# --- 只读输入与隔离 ---------------------------------------------------------------------


@pytest.mark.parametrize("snapshot", [_snapshot, _chinese_snapshot], ids=["ascii", "chinese"])
def test_render_does_not_modify_the_snapshot(snapshot: Callable[[], dict[str, Any]]) -> None:
    value = snapshot()
    before = copy.deepcopy(value)

    _render(value)

    assert value == before


_FORBIDDEN_MODULES = (
    "sqlalchemy",
    "pydantic_settings",
    "app.models",
    "app.repositories",
    "app.core.config",
    "app.core.database",
    "app.services.statements",
    "app.services.receipts",
    "app.services.receipt_pdf",
)


def test_module_imports_no_session_model_or_configuration() -> None:
    script = textwrap.dedent(
        """
        import json
        import sys

        sys.path.insert(0, sys.argv[1])

        import app.services.statement_pdf  # noqa: F401

        print(json.dumps(sorted(sys.modules)))
        """
    )

    done = subprocess.run(
        [sys.executable, "-c", script, str(_REPO_ROOT)],
        capture_output=True,
        cwd=_REPO_ROOT,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    loaded = json.loads(done.stdout)
    for module in loaded:
        for forbidden in _FORBIDDEN_MODULES:
            assert module != forbidden and not module.startswith(forbidden + "."), module


def test_renders_without_database_or_configuration() -> None:
    """会话、模型、仓储与配置模块都导入不了时照常渲染（含中文字体），字节相同。"""
    script = textwrap.dedent(
        """
        import datetime as dt
        import json
        import sys

        # 置为 None 的模块一 import 就抛 ImportError。
        for name in (
            "sqlalchemy",
            "pydantic_settings",
            "app.core",
            "app.models",
            "app.repositories",
            "app.api",
            "app.tasks",
            "app.services.statements",
            "app.services.receipts",
        ):
            sys.modules[name] = None
        sys.path.insert(0, sys.argv[1])

        from app.services.statement_pdf import render_statement_pdf

        payload = json.loads(sys.stdin.read())
        sys.stdout.buffer.write(
            render_statement_pdf(
                payload["snapshot"],
                statement_number=payload["statement_number"],
                finalized_at=dt.datetime.fromisoformat(payload["finalized_at"]),
            )
        )
        """
    )
    snapshot = _chinese_snapshot()
    payload = {
        "snapshot": snapshot,
        "statement_number": _NUMBER,
        "finalized_at": _FINALIZED.isoformat(),
    }

    done = subprocess.run(
        [sys.executable, "-c", script, str(_REPO_ROOT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=_REPO_ROOT,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout == _render(snapshot)


# --- 校验 -------------------------------------------------------------------------------


def _set(path: tuple[str | int, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(snapshot: dict[str, Any]) -> None:
        target: Any = snapshot
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(path: tuple[str | int, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(snapshot: dict[str, Any]) -> None:
        target: Any = snapshot
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


_USAGE = ("prior_period", "usage", 0)
_PROVIDER = ("by_provider", 0)
_MODEL = ("by_model", 0)
_PROJECT = ("by_project", 0)

_INVALID: dict[str, Callable[[dict[str, Any]], None]] = {
    # 各层的键集合（INV-7）
    "top_extra_key": _set(("estimated_cost",), "1.00000000"),
    "top_missing_key": _drop(("by_project",)),
    "tax_notice_extra_key": _set(("tax_notice", "ms"), "Penyata"),
    "tax_notice_missing_key": _drop(("tax_notice", "zh")),
    "tax_notice_not_object": _set(("tax_notice",), "notice"),
    "prior_period_extra_key": _set(("prior_period", "fx_rate"), "4.50000000"),
    "prior_period_missing_key": _drop(("prior_period", "rebill")),
    "prior_period_not_object": _set(("prior_period",), []),
    "usage_not_list": _set(("prior_period", "usage"), {}),
    "usage_item_extra_key": _set((*_USAGE, "cost"), "1.00000000"),
    "usage_item_missing_key": _drop((*_USAGE, "amount")),
    "usage_item_not_object": _set(_USAGE, "2026-10"),
    "provider_extra_key": _set((*_PROVIDER, "cost"), "1.00000000"),
    "provider_missing_key": _drop((*_PROVIDER, "charges")),
    "provider_not_object": _set(_PROVIDER, "anthropic"),
    "model_extra_key": _set((*_MODEL, "markup"), "1.00000000"),
    "model_missing_key": _drop((*_MODEL, "model_code")),
    "model_not_object": _set(_MODEL, None),
    "project_extra_key": _set((*_PROJECT, "margin"), "1.00000000"),
    "project_missing_key": _drop((*_PROJECT, "project_public_id")),
    "project_not_object": _set(_PROJECT, ["Support Bot"]),
    "by_project_not_list": _set(("by_project",), {}),
    # schema_version
    "schema_version_2": _set(("schema_version",), 2),
    "schema_version_0": _set(("schema_version",), 0),
    "schema_version_bool": _set(("schema_version",), True),
    "schema_version_string": _set(("schema_version",), "1"),
    "schema_version_float": _set(("schema_version",), 1.0),
    # 金额
    "amount_one_decimal": _set(("opening_balance",), "1.5"),
    "amount_nine_decimals": _set(("opening_balance",), "1.123456789"),
    "amount_number": _set(("opening_balance",), 1.5),
    "amount_thirteen_digits": _set(("closing_balance",), "1234567890123.00000000"),
    "amount_plus_sign": _set(("total_topup",), "+1.00000000"),
    "amount_thousands": _set(("total_ai_usage",), "-1,000.00000000"),
    "amount_trailing_newline": _set(("total_adjustments",), "1.00000000\n"),
    "amount_customer_charges": _set(("total_customer_charges",), None),
    "amount_rebill": _set(("prior_period", "rebill"), "1.5"),
    "amount_prior_total": _set(("prior_period", "total"), 0),
    "amount_usage": _set((*_USAGE, "amount"), 1.5),
    "amount_provider_charges": _set((*_PROVIDER, "charges"), "1"),
    "amount_model_charges": _set((*_MODEL, "charges"), "1.5"),
    "amount_project_charges": _set((*_PROJECT, "charges"), "1.000000000"),
    # 计数
    "count_negative": _set(("request_count",), -1),
    "count_bool": _set(("conversation_count",), True),
    "count_string": _set(("input_tokens",), "3"),
    "count_float": _set(("output_tokens",), 1.0),
    "count_usage_bool": _set((*_USAGE, "request_count"), True),
    "count_provider_negative": _set((*_PROVIDER, "request_count"), -1),
    "count_model_string": _set((*_MODEL, "input_tokens"), "3"),
    "count_project_negative": _set((*_PROJECT, "output_tokens"), -1),
    # 账期
    "period_month_13": _set(("period",), "2026-13"),
    "period_year_2025": _set(("period",), "2025-12"),
    "period_year_3000": _set(("period",), "3000-01"),
    "period_short_month": _set(("period",), "2026-1"),
    "period_trailing_newline": _set(("period",), "2026-12\n"),
    "period_not_string": _set(("period",), 202612),
    "usage_period_equal": _set((*_USAGE, "usage_period"), "2026-12"),
    "usage_period_later": _set((*_USAGE, "usage_period"), "2027-01"),
    "usage_period_year_2025": _set((*_USAGE, "usage_period"), "2025-11"),
    "usage_period_month_13": _set((*_USAGE, "usage_period"), "2026-13"),
    # 固定值与非空字符串
    "currency_usd": _set(("currency",), "USD"),
    "company_name_empty": _set(("company_name",), ""),
    "company_name_not_string": _set(("company_name",), None),
    "tax_notice_en_empty": _set(("tax_notice", "en"), ""),
    "tax_notice_zh_not_string": _set(("tax_notice", "zh"), None),
    "provider_code_empty": _set((*_PROVIDER, "provider_code"), ""),
    "model_code_not_string": _set((*_MODEL, "model_code"), None),
    "project_name_empty": _set((*_PROJECT, "project_name"), ""),
    "project_public_id_empty": _set((*_PROJECT, "project_public_id"), ""),
}


@pytest.mark.parametrize("mutate", list(_INVALID.values()), ids=list(_INVALID))
def test_invalid_snapshot_is_rejected(mutate: Callable[[dict[str, Any]], None]) -> None:
    snapshot = _snapshot()
    mutate(snapshot)
    before = copy.deepcopy(snapshot)

    # 只能是 StatementSnapshotInvalid：别的异常会让这条用例出错，而不是通过。
    with pytest.raises(StatementSnapshotInvalid):
        _render(snapshot)
    assert snapshot == before


@pytest.mark.parametrize("snapshot", [None, [], "snapshot"], ids=["none", "list", "string"])
def test_snapshot_that_is_not_an_object_is_rejected(snapshot: Any) -> None:
    with pytest.raises(StatementSnapshotInvalid):
        render_statement_pdf(snapshot, statement_number=_NUMBER, finalized_at=_FINALIZED)


@pytest.mark.parametrize(
    "number",
    [
        "ST-202612-0123ABC",
        "ST-202612-0123ABCDE",
        "ST-202612-0123ABCI",
        "ST-202612-0123ABCL",
        "ST-202612-0123ABCO",
        "ST-202612-0123ABCU",
        "st-202612-0123abcd",
        "ST-2026-12-0123ABCD",
        "ST-202611-0123ABCD",
        "ST-202612-0123ABCD\n",
        " ST-202612-0123ABCD",
        None,
        202612,
    ],
)
def test_invalid_statement_number_is_rejected(number: Any) -> None:
    with pytest.raises(StatementSnapshotInvalid):
        render_statement_pdf(_snapshot(), statement_number=number, finalized_at=_FINALIZED)


@pytest.mark.parametrize(
    "moment",
    [
        _FINALIZED.replace(tzinfo=dt.UTC),
        None,
        _FINALIZED.isoformat(),
        _FINALIZED.date(),
    ],
    ids=["aware", "none", "string", "date"],
)
def test_invalid_finalized_at_is_rejected(moment: Any) -> None:
    with pytest.raises(StatementSnapshotInvalid):
        render_statement_pdf(_snapshot(), statement_number=_NUMBER, finalized_at=moment)
