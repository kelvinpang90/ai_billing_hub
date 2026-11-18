"""The shared font choice of the statement and receipt PDFs (gate #750 v1 §2「共用模块」, §7).

AIH-TASK-228。纯单元测试：对一个新 `FPDF` 与 `ExitStack` 直接调用 `PdfFonts.prepare`。
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fpdf import FPDF, XPos, YPos
from pdf_content import embeds_noto, unicode_texts

from app.services import pdf_text
from app.services.pdf_text import (
    HELVETICA,
    NOTO,
    NOTO_FONT_FILE,
    NOTO_FONT_SHA256,
    PdfFonts,
    PdfFontUnavailable,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _counting(pdf: FPDF) -> list[Any]:
    """Wrap `pdf.add_font` so that the family of every call is recorded in the returned list."""
    added: list[Any] = []
    original = pdf.add_font

    def add_font(family: Any = None, *args: Any, **kwargs: Any) -> Any:
        added.append(family)
        return original(family, *args, **kwargs)

    setattr(pdf, "add_font", add_font)  # noqa: B010 - 包一层计数，不改 fpdf2 的行为
    return added


@pytest.fixture
def fresh_font_cache() -> Iterator[None]:
    pdf_text.font_cmap.cache_clear()
    yield
    pdf_text.font_cmap.cache_clear()


def test_names_and_values() -> None:
    assert HELVETICA == "Helvetica"
    assert NOTO == "NotoSansSC"
    assert NOTO_FONT_FILE == "NotoSansSC-Regular.ttf"
    assert NOTO_FONT_SHA256 == "d45f67f0a7c0ca3f256950777ce6a61cc7ce5f9696d02900cbbaac25f8aa7d16"
    assert issubclass(PdfFontUnavailable, Exception)


# --- 规则矩阵 ---------------------------------------------------------------------------


def test_rule_matrix() -> None:
    cmap = pdf_text.font_cmap()
    # 前提：த 不在字体里；’ 在、\t 不在。
    assert 0x0BA4 not in cmap
    assert ord("’") in cmap
    assert ord("\t") not in cmap
    expected = [
        ("Example Sdn Bhd", (HELVETICA, "Example Sdn Bhd")),
        ("Café\tA", (HELVETICA, "Café\tA")),
        ("Café த Sdn Bhd", (HELVETICA, "Café ? Sdn Bhd")),
        ("示例科技 Sdn Bhd", (NOTO, "示例科技 Sdn Bhd")),
        ("Café 中 த", (NOTO, "Café 中 ?")),
        ("Customer’s copy\tA\nSecond line B", (NOTO, "Customer’s copy?A\nSecond line B")),
    ]

    with contextlib.ExitStack() as stack:
        fonts = PdfFonts(FPDF(), stack)
        for text, result in expected:
            assert fonts.prepare(text) == result, text


def test_is_latin1() -> None:
    assert pdf_text.is_latin1("Café\tA\n\x9f\xff")
    assert pdf_text.is_latin1("")
    assert not pdf_text.is_latin1("Café 中")
    assert not pdf_text.is_latin1("’")


# --- add_font 次数 ----------------------------------------------------------------------


def test_noto_is_added_once_per_instance_when_first_needed() -> None:
    pdf = FPDF()
    pdf.set_compression(False)
    added = _counting(pdf)
    with contextlib.ExitStack() as stack:
        fonts = PdfFonts(pdf, stack)
        pdf.add_page()
        for text in ("示例科技 Sdn Bhd", "Example Sdn Bhd", "客服项目"):
            family, shown = fonts.prepare(text)
            pdf.set_font(family, "", 11)
            pdf.multi_cell(0, 7, shown, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        assert added == [NOTO]
        output = bytes(pdf.output())

    # 一次 `add_font` 足够印出两段中文。
    assert embeds_noto(output)
    decoded = unicode_texts(output)
    assert any("示例科技 Sdn Bhd" in text for text in decoded), decoded
    assert any("客服项目" in text for text in decoded), decoded


def test_noto_is_not_added_for_latin1_results() -> None:
    pdf = FPDF()
    added = _counting(pdf)
    with contextlib.ExitStack() as stack:
        fonts = PdfFonts(pdf, stack)
        assert fonts.prepare("Example Sdn Bhd") == (HELVETICA, "Example Sdn Bhd")
        assert fonts.prepare("Café த") == (HELVETICA, "Café ?")

    assert added == []


def test_each_instance_adds_noto_for_its_own_pdf() -> None:
    first, second = FPDF(), FPDF()
    first_added, second_added = _counting(first), _counting(second)
    with contextlib.ExitStack() as stack:
        PdfFonts(first, stack).prepare("中")
        PdfFonts(second, stack).prepare("中")

    assert first_added == [NOTO]
    assert second_added == [NOTO]


# --- 字体不可用不缓存 -------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_font_cache")
def test_missing_font_is_unavailable_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = pdf_text._font_resource
    monkeypatch.setattr(pdf_text, "_font_resource", lambda: tmp_path / "missing.ttf")

    with contextlib.ExitStack() as stack:
        fonts = PdfFonts(FPDF(), stack)
        with pytest.raises(PdfFontUnavailable):
            fonts.prepare("中")
        # 全 Latin-1 的字符串不读字体文件，照常。
        assert fonts.prepare("Example") == (HELVETICA, "Example")

    # 恢复后成功：异常没有被缓存。
    monkeypatch.setattr(pdf_text, "_font_resource", original)
    with contextlib.ExitStack() as stack:
        assert PdfFonts(FPDF(), stack).prepare("中") == (NOTO, "中")


@pytest.mark.usefixtures("fresh_font_cache")
def test_file_that_is_not_a_font_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.ttf"
    broken.write_bytes(b"not a font at all")
    monkeypatch.setattr(pdf_text, "_font_resource", lambda: broken)

    with contextlib.ExitStack() as stack:
        fonts = PdfFonts(FPDF(), stack)
        with pytest.raises(PdfFontUnavailable):
            fonts.prepare("中")
        assert fonts.prepare("Example") == (HELVETICA, "Example")


# --- 模块隔离 ---------------------------------------------------------------------------

_FORBIDDEN_MODULES = (
    "sqlalchemy",
    "pydantic_settings",
    "app.models",
    "app.repositories",
    "app.core.config",
    "app.core.database",
    "app.services.statements",
    "app.services.receipts",
)


def _loaded_modules(module: str) -> list[str]:
    script = textwrap.dedent(
        """
        import importlib
        import json
        import sys

        sys.path.insert(0, sys.argv[1])

        importlib.import_module(sys.argv[2])

        print(json.dumps(sorted(sys.modules)))
        """
    )

    done = subprocess.run(
        [sys.executable, "-c", script, str(_REPO_ROOT), module],
        capture_output=True,
        cwd=_REPO_ROOT,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    loaded: list[str] = json.loads(done.stdout)
    assert module in loaded
    return loaded


def _assert_not_loaded(loaded: list[str], forbidden: tuple[str, ...]) -> None:
    for module in loaded:
        for name in forbidden:
            assert module != name and not module.startswith(name + "."), module


@pytest.mark.parametrize(
    ("module", "also_forbidden"),
    [
        ("app.services.pdf_text", ("app.services.statement_pdf", "app.services.receipt_pdf")),
        ("app.services.receipt_pdf", ("app.services.statement_pdf",)),
    ],
    ids=["pdf_text", "receipt_pdf"],
)
def test_module_imports_no_session_model_or_configuration(
    module: str, also_forbidden: tuple[str, ...]
) -> None:
    _assert_not_loaded(_loaded_modules(module), (*_FORBIDDEN_MODULES, *also_forbidden))
