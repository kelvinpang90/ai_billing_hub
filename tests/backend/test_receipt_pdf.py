"""Receipt PDF rendering (gate #589 v2 §2「PDF」, §7「渲染」「渲染只读快照」; AIH-TASK-199).

中文字体（设计闸门 #750 v1 §2「收据」、§7；AIH-TASK-228）：每个印出的值按 `app/services/pdf_text.py`
的规则选 Helvetica 或嵌入 Noto Sans SC 子集。

内容流不压缩，文字直接在字节里检索（标准字体的字符串是 Latin-1 编码的 `(…)` 字面量）；页数数
`/Type /Page` 对象（不含 `/Type /Pages`）。Noto Sans SC 的文字经 ToUnicode 还原（`pdf_content`）。
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fpdf import FPDF
from pdf_content import (
    bfchar_hex,
    embeds_noto,
    literal,
    text_positions,
    to_unicode,
    unicode_texts,
)

from app.services import pdf_text, receipt_pdf
from app.services.receipt_pdf import (
    ReceiptFontUnavailable,
    ReceiptSnapshotInvalid,
    render_receipt_pdf,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_LABELS = (
    "Receipt No.",
    "Issued",
    "Received from",
    "Payment date",
    "Amount",
    "Currency",
    "Payment method",
    "Gateway reference",
    "Payment ID",
    "Status",
)
# A4 底边往上 20 mm，单位 pt（`BT x y Td` 的 y 从页面左下角量起）。
_BOTTOM_LIMIT = 56.69


def _snapshot(**payment: Any) -> dict[str, Any]:
    base = {
        "payment_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "paid_at": "2026-12-03T06:05:09",
        "amount": "1000.00",
        "currency": "MYR",
        "status": "PAID",
        "gateway": "STRIPE",
        "gateway_reference": "pi_3Testreference",
        "payment_method": "fpx",
    }
    base.update(payment)
    return {
        "schema_version": 1,
        "receipt_number": "ACV-AIR-2026-000001",
        "issued_at": "2026-12-31T16:05:09",
        "issuer": {"name": "Acuven Technology Sdn Bhd"},
        "customer": {"company_name": "Example Sdn Bhd"},
        "payment": base,
        "tax_notice": {
            "zh": "本收据不是电子发票。",
            "en": "This receipt is not an LHDN e-Invoice. No SST is charged on prepaid top-ups.",
        },
    }


def _pages(pdf: bytes) -> int:
    return len(_PAGE_OBJECT.findall(pdf))


def _company(name: str) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        snapshot = _snapshot()
        snapshot["customer"]["company_name"] = name
        return snapshot

    return build


def _chinese_snapshot() -> dict[str, Any]:
    return _company("示例科技 Sdn Bhd")()


def test_renders_one_page_with_every_value() -> None:
    pdf = render_receipt_pdf(_snapshot())

    assert pdf.startswith(b"%PDF-")
    assert _pages(pdf) == 1
    for text in (
        b"(PAYMENT RECEIPT)",
        b"(Acuven Technology Sdn Bhd)",
        b"(ACV-AIR-2026-000001)",
        b"(Example Sdn Bhd)",
        # issued_at 16:05:09 UTC 是马来西亚的次日 00:05。
        b"(2027-01-01 00:05 MYT)",
        b"(2026-12-03 14:05 MYT)",
        b"(RM 1,000.00)",
        b"(MYR)",
        b"(FPX)",
        b"(pi_3Testreference)",
        b"(0f8fad5b-d9cb-469f-a165-70867728950e)",
        b"(PAID)",
        b"No SST is charged on prepaid top-ups.",
    ):
        assert text in pdf, text
    for label in (
        b"Receipt No.",
        b"Issued",
        b"Received from",
        b"Payment date",
        b"Amount",
        b"Currency",
        b"Payment method",
        b"Gateway reference",
        b"Payment ID",
        b"Status",
    ):
        assert b"(" + label + b")" in pdf, label


@pytest.mark.parametrize(
    ("amount", "shown"),
    [("1000.00", b"(RM 1,000.00)"), ("0.01", b"(RM 0.01)"), ("999999.99", b"(RM 999,999.99)")],
)
def test_amount_is_shown_with_two_decimals_and_thousands(amount: str, shown: bytes) -> None:
    assert shown in render_receipt_pdf(_snapshot(amount=amount))


@pytest.mark.parametrize(
    ("method", "shown"),
    [("card", b"(Card)"), ("fpx", b"(FPX)"), ("grabpay", b"(grabpay)"), (None, b"(Stripe)")],
)
def test_payment_method_display(method: str | None, shown: bytes) -> None:
    pdf = render_receipt_pdf(_snapshot(payment_method=method))

    assert shown in pdf
    assert _pages(pdf) == 1


def test_characters_outside_latin1_are_printed_with_noto_sans_sc() -> None:
    """设计闸门 #750 §7「旧的印成 ? 用例」：本批之前这里印成 `??`。"""
    snapshot = _snapshot()
    snapshot["customer"]["company_name"] = "Café 中文 Sdn Bhd"

    pdf = render_receipt_pdf(snapshot)

    assert embeds_noto(pdf)
    texts = unicode_texts(pdf)
    assert any("Café 中文 Sdn Bhd" in text for text in texts), texts
    assert not any("??" in text for text in texts), texts
    assert b" ?? Sdn Bhd)" not in pdf
    assert _pages(pdf) == 1


# --- 中文字体（设计闸门 #750 §7）--------------------------------------------------------


class _LegacyFonts:
    """The rule before AIH-TASK-228: always Helvetica, `ord ≥ 256` → `?`, never `add_font`."""

    def __init__(self, pdf: FPDF, stack: contextlib.ExitStack) -> None:
        del pdf, stack

    def prepare(self, text: str) -> tuple[str, str]:
        return "Helvetica", "".join(char if ord(char) < 256 else "?" for char in text)


# 快照 → 收据上印出的公司名。
_LATIN1_RESULTS = {
    "ascii": (_snapshot, "Example Sdn Bhd"),
    "latin1": (_company("Café Sdn Bhd"), "Café Sdn Bhd"),
    "missing_glyph": (_company("Café த Sdn Bhd"), "Café ? Sdn Bhd"),
}


@pytest.mark.parametrize(
    ("snapshot", "shown"), list(_LATIN1_RESULTS.values()), ids=list(_LATIN1_RESULTS)
)
def test_latin1_receipts_render_the_same_bytes_as_before(
    monkeypatch: pytest.MonkeyPatch, snapshot: Callable[[], dict[str, Any]], shown: str
) -> None:
    current = render_receipt_pdf(snapshot())

    monkeypatch.setattr(receipt_pdf, "PdfFonts", _LegacyFonts)
    legacy = render_receipt_pdf(snapshot())

    assert current == legacy
    assert b"/FontFile2" not in current
    assert literal(shown) in current


def test_character_missing_from_the_font_stays_helvetica() -> None:
    assert 0x0BA4 not in pdf_text.font_cmap()

    pdf = render_receipt_pdf(_company("Café த Sdn Bhd")())

    assert literal("Café ? Sdn Bhd") in pdf
    assert b"/FontFile2" not in pdf


def test_chinese_company_name_embeds_a_noto_sans_sc_subset() -> None:
    pdf = render_receipt_pdf(_chinese_snapshot())

    assert b"/FontFile2" in pdf
    assert re.search(rb"/BaseFont\s*/\S*NotoSansSC", pdf)
    hexadecimal = bfchar_hex(pdf)
    for char in "示例科技":
        assert f"{ord(char):04X}" in hexadecimal, char
    texts = unicode_texts(pdf)
    assert any("示例科技 Sdn Bhd" in text for text in texts), texts
    # 标题、标签与 ASCII 的值仍是 Helvetica 字面量。
    for text in ("PAYMENT RECEIPT", "RM 1,000.00", "ACV-AIR-2026-000001", *_LABELS):
        assert literal(text) in pdf, text
    assert _pages(pdf) == 1


def test_character_missing_from_the_font_becomes_question_mark_with_noto() -> None:
    assert 0x0BA4 not in pdf_text.font_cmap()

    pdf = render_receipt_pdf(_company("Café 中 த")())

    assert embeds_noto(pdf)
    assert any("Café 中 ?" in text for text in unicode_texts(pdf))
    assert "த" not in to_unicode(pdf).values()


def test_other_free_text_uses_the_same_rule() -> None:
    cmap = pdf_text.font_cmap()
    assert ord("’") in cmap
    assert ord("\t") not in cmap
    snapshot = _snapshot()
    snapshot["issuer"]["name"] = "示例开具方 Sdn Bhd"
    snapshot["tax_notice"]["en"] = "Customer’s copy\tA\nSecond line B"

    pdf = render_receipt_pdf(snapshot)
    texts = unicode_texts(pdf)

    assert embeds_noto(pdf)
    assert any("示例开具方 Sdn Bhd" in text for text in texts), texts
    assert any("Customer’s copy?A" in text for text in texts), texts
    assert any("Second line B" in text for text in texts), texts
    # 在换行符处断开：两段不在同一个字符串里。
    assert not any("A" in text and "Second" in text for text in texts), texts
    assert all("\t" not in text and "\n" not in text for text in texts), texts
    assert "\t" not in to_unicode(pdf).values()
    assert _pages(pdf) == 1


def test_long_chinese_company_name_fits_on_one_page() -> None:
    pdf = render_receipt_pdf(_company("中" * 255)())

    assert _pages(pdf) == 1
    # fpdf2 把每段文字写成 `BT x y Td … ET`（设计闸门 #750 §10 假设 3）。
    positions = text_positions(pdf)
    assert len(positions) > len(_LABELS)
    assert all(y >= _BOTTOM_LIMIT for _, y in positions), min(y for _, y in positions)
    assert sum(text.count("中") for text in unicode_texts(pdf)) == 255


@pytest.fixture
def fresh_font_cache() -> Iterator[None]:
    pdf_text.font_cmap.cache_clear()
    yield
    pdf_text.font_cmap.cache_clear()


@pytest.mark.usefixtures("fresh_font_cache")
def test_missing_font_fails_only_receipts_that_need_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pdf_text, "_font_resource", lambda: tmp_path / "missing.ttf")

    with pytest.raises(ReceiptFontUnavailable) as raised:
        render_receipt_pdf(_chinese_snapshot())
    assert not isinstance(raised.value, ReceiptSnapshotInvalid)
    # 全 Latin-1 的收据不读字体文件，照常渲染。
    assert render_receipt_pdf(_snapshot()).startswith(b"%PDF-")
    # 先校验、后读字体：不合规的中文快照仍是 ReceiptSnapshotInvalid。
    invalid = _chinese_snapshot()
    invalid["schema_version"] = 2
    with pytest.raises(ReceiptSnapshotInvalid):
        render_receipt_pdf(invalid)


def test_chinese_receipt_renders_identical_bytes_in_one_process() -> None:
    assert render_receipt_pdf(_chinese_snapshot()) == render_receipt_pdf(_chinese_snapshot())


_RENDER_SCRIPT = textwrap.dedent(
    """
    import json
    import sys

    sys.path.insert(0, sys.argv[1])

    from app.services.receipt_pdf import render_receipt_pdf

    sys.stdout.buffer.write(render_receipt_pdf(json.loads(sys.stdin.read())))
    """
)


def _render_in_subprocess(snapshot: dict[str, Any], **environment: str) -> bytes:
    done = subprocess.run(
        [sys.executable, "-c", _RENDER_SCRIPT, str(_REPO_ROOT)],
        input=json.dumps(snapshot).encode("utf-8"),
        capture_output=True,
        cwd=_REPO_ROOT,
        env={**os.environ, **environment},
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout


def test_chinese_receipt_renders_identical_bytes_across_processes() -> None:
    """哈希随机化与 fontTools 的 `head.modified`（当前时间或 SOURCE_DATE_EPOCH）都不影响字节。"""
    snapshot = _chinese_snapshot()

    first = _render_in_subprocess(snapshot, PYTHONHASHSEED="1", SOURCE_DATE_EPOCH="0")
    second = _render_in_subprocess(snapshot, PYTHONHASHSEED="2", SOURCE_DATE_EPOCH="2000000000")

    assert b"/FontFile2" in first
    assert first == second
    assert first == render_receipt_pdf(snapshot)


def test_long_company_name_wraps_on_one_page() -> None:
    words = [f"Nm{index:05d}" for index in range(32)]
    name = " ".join(words)
    assert len(name) == 255
    snapshot = _snapshot()
    snapshot["customer"]["company_name"] = name

    pdf = render_receipt_pdf(snapshot)

    assert _pages(pdf) == 1
    for word in words:
        assert word.encode("ascii") in pdf
    # 换了行：整串不在同一个字符串字面量里。
    assert name.encode("ascii") not in pdf


def test_same_snapshot_renders_identical_bytes() -> None:
    assert render_receipt_pdf(_snapshot()) == render_receipt_pdf(_snapshot())


def test_metadata_comes_from_the_snapshot() -> None:
    pdf = render_receipt_pdf(_snapshot())

    # 信息字典里的字符串可能是 `(…)` 字面量，也可能是 UTF-16BE 的十六进制串。
    assert _info_text(pdf, "/CreationDate", "D:20261231160509")
    assert _info_text(pdf, "/Title", "ACV-AIR-2026-000001")


def _info_text(pdf: bytes, key: str, text: str) -> bool:
    literal = f"{key} ({text}".encode("ascii")
    hexadecimal = f"{key} <FEFF{text.encode('utf-16-be').hex()}".upper().encode("ascii")
    return literal in pdf or hexadecimal in pdf.upper()


def test_render_does_not_modify_the_snapshot() -> None:
    snapshot = _snapshot()
    before = copy.deepcopy(snapshot)

    render_receipt_pdf(snapshot)

    assert snapshot == before


def _set(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(snapshot: dict[str, Any]) -> None:
        target = snapshot
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(path: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(snapshot: dict[str, Any]) -> None:
        target = snapshot
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


_INVALID: dict[str, Callable[[dict[str, Any]], None]] = {
    # 各层的键集合
    "top_extra_key": _set(("fee",), "1.00"),
    "top_missing_key": _drop(("tax_notice",)),
    "issuer_extra_key": _set(("issuer", "address"), "Kuala Lumpur"),
    "issuer_missing_key": _drop(("issuer", "name")),
    "issuer_not_object": _set(("issuer",), "Acuven Technology Sdn Bhd"),
    "customer_extra_key": _set(("customer", "email"), "someone"),
    "customer_missing_key": _drop(("customer", "company_name")),
    "payment_extra_key": _set(("payment", "fee"), "1.00"),
    "payment_missing_key": _drop(("payment", "payment_method")),
    "payment_not_object": _set(("payment",), None),
    "tax_notice_extra_key": _set(("tax_notice", "ms"), "Resit"),
    "tax_notice_missing_key": _drop(("tax_notice", "zh")),
    # schema_version
    "schema_version_2": _set(("schema_version",), 2),
    "schema_version_bool": _set(("schema_version",), True),
    "schema_version_string": _set(("schema_version",), "1"),
    "schema_version_float": _set(("schema_version",), 1.0),
    # 收据号
    "receipt_number_format": _set(("receipt_number",), "ACV-AIR-2026-1"),
    "receipt_number_trailing_newline": _set(("receipt_number",), "ACV-AIR-2026-000001\n"),
    "receipt_number_not_string": _set(("receipt_number",), 1),
    # 两个时刻
    "issued_at_unreadable": _set(("issued_at",), "yesterday"),
    "issued_at_with_timezone": _set(("issued_at",), "2026-12-03T06:05:09+00:00"),
    "issued_at_year_2025": _set(("issued_at",), "2025-12-31T23:59:59"),
    "issued_at_year_2100": _set(("issued_at",), "2100-01-01T00:00:00"),
    "issued_at_not_string": _set(("issued_at",), 1764741909),
    "paid_at_unreadable": _set(("payment", "paid_at"), "2026-13-01T00:00:00"),
    "paid_at_with_timezone": _set(("payment", "paid_at"), "2026-12-03T06:05:09Z"),
    "paid_at_year_2025": _set(("payment", "paid_at"), "2025-06-01T00:00:00"),
    "paid_at_year_2100": _set(("payment", "paid_at"), "2100-06-01T00:00:00"),
    "paid_at_empty": _set(("payment", "paid_at"), ""),
    # 金额
    "amount_no_decimals": _set(("payment", "amount"), "100"),
    "amount_one_decimal": _set(("payment", "amount"), "100.0"),
    "amount_three_decimals": _set(("payment", "amount"), "100.000"),
    "amount_seven_digits": _set(("payment", "amount"), "1000000.00"),
    "amount_zero": _set(("payment", "amount"), "0.00"),
    "amount_negative": _set(("payment", "amount"), "-1.00"),
    "amount_thousands": _set(("payment", "amount"), "1,000.00"),
    "amount_not_string": _set(("payment", "amount"), 100),
    # 三个固定值
    "currency_usd": _set(("payment", "currency"), "USD"),
    "status_pending": _set(("payment", "status"), "PENDING"),
    "gateway_other": _set(("payment", "gateway"), "PAYPAL"),
    # 付款方式
    "payment_method_upper": _set(("payment", "payment_method"), "Card"),
    "payment_method_empty": _set(("payment", "payment_method"), ""),
    "payment_method_too_long": _set(("payment", "payment_method"), "a" * 33),
    "payment_method_not_string": _set(("payment", "payment_method"), 1),
    # 其余为非空字符串
    "issuer_name_empty": _set(("issuer", "name"), ""),
    "company_name_empty": _set(("customer", "company_name"), ""),
    "company_name_not_string": _set(("customer", "company_name"), None),
    "payment_id_empty": _set(("payment", "payment_id"), ""),
    "gateway_reference_null": _set(("payment", "gateway_reference"), None),
    "tax_notice_zh_empty": _set(("tax_notice", "zh"), ""),
    "tax_notice_en_not_string": _set(("tax_notice", "en"), ["notice"]),
}


@pytest.mark.parametrize("mutate", list(_INVALID.values()), ids=list(_INVALID))
def test_invalid_snapshot_is_rejected(mutate: Callable[[dict[str, Any]], None]) -> None:
    snapshot = _snapshot()
    mutate(snapshot)

    # 只能是 ReceiptSnapshotInvalid：别的异常会让这条用例出错，而不是通过。
    with pytest.raises(ReceiptSnapshotInvalid):
        render_receipt_pdf(snapshot)


@pytest.mark.parametrize("snapshot", [None, [], "snapshot"], ids=["none", "list", "string"])
def test_snapshot_that_is_not_an_object_is_rejected(snapshot: Any) -> None:
    with pytest.raises(ReceiptSnapshotInvalid):
        render_receipt_pdf(snapshot)


@pytest.mark.parametrize("make", [_snapshot, _chinese_snapshot], ids=["ascii", "chinese"])
def test_renders_without_database_or_configuration(make: Callable[[], dict[str, Any]]) -> None:
    """设计 §7「渲染只读快照」：会话、模型、仓储与配置模块都导入不了时照常渲染，字节相同。

    设计闸门 #750 §7：含中文字体时也一样，快照不被改动。
    """
    script = textwrap.dedent(
        """
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
        ):
            sys.modules[name] = None
        sys.path.insert(0, sys.argv[1])

        from app.services.receipt_pdf import render_receipt_pdf

        sys.stdout.buffer.write(render_receipt_pdf(json.loads(sys.stdin.read())))
        """
    )
    snapshot = make()
    before = copy.deepcopy(snapshot)

    done = subprocess.run(
        [sys.executable, "-c", script, str(_REPO_ROOT)],
        input=json.dumps(snapshot).encode("utf-8"),
        capture_output=True,
        cwd=_REPO_ROOT,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout == render_receipt_pdf(snapshot)
    assert snapshot == before
