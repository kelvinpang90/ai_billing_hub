"""Choosing Helvetica or Noto Sans SC per printed string (gate #750 v1 §2「共用模块」).

AIH-TASK-228。对账单（`app/services/statement_pdf.py`）与收据（`app/services/receipt_pdf.py`）共用
这一套规则；代码由对账单原样搬来，规则见设计 #708 v3 §2「文本与字体」第 1–5 步。

⚠️ 本模块只 import 标准库、`fontTools` 与 `fpdf`：不 import 会话、模型、配置与 `app.services`
下任何别的模块（INV-6）。

⚠️ **换字体会同时改变历史对账单与历史收据重新渲染出的字形**：家族名、字体文件与
`NOTO_FONT_SHA256` 改一处都要过设计闸门（设计 #750 §10 已知限制 2）。

规则：全 Latin-1 的字符串用 Helvetica（标准字体，不嵌入）；其他的用 Noto Sans SC，fpdf2 只把用到的
字形子集嵌进 PDF。两种字体都印不出的字符换成 `?`。
"""

from __future__ import annotations

import contextlib
import functools
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final

from fontTools.ttLib import TTFont
from fpdf import FPDF

# 家族名进 PDF 的 `/BaseFont`（子集名形如 `MPDFAA+NotoSansSC`）：⚠️ 不得改。
HELVETICA: Final = "Helvetica"
NOTO: Final = "NotoSansSC"
NOTO_FONT_FILE: Final = "NotoSansSC-Regular.ttf"
# `app/data/NotoSansSC-Regular.ttf` 的 SHA-256（PR #706，`app/data/__init__.py` 文档串）。
# ⚠️ 换字体会改变历史对账单与收据 PDF 的字形，改这个值要过设计闸门。
NOTO_FONT_SHA256: Final = "d45f67f0a7c0ca3f256950777ce6a61cc7ce5f9696d02900cbbaac25f8aa7d16"

_LATIN1_END: Final = 256
# multi_cell 的换行指令，不是要印的字形（设计 #708 v3 §2「文本与字体」第 3 步）。
_NEWLINE: Final = "\n"


class PdfFontUnavailable(Exception):
    """The bundled Noto Sans SC font is missing, unreadable or not TrueType."""


def _font_resource() -> Traversable:
    return resources.files("app.data").joinpath(NOTO_FONT_FILE)


@functools.lru_cache(maxsize=1)
def font_cmap() -> frozenset[int]:
    """Code points of the bundled Noto Sans SC, read once per process.

    并发下可能重复计算，结果相同，无害。读不了时抛 `PdfFontUnavailable`（不缓存异常）。
    """
    try:
        with resources.as_file(_font_resource()) as path:
            font = TTFont(path, lazy=True)
            try:
                # 只认 TrueType（`glyf` 轮廓）：CFF 轮廓与可变字体 fpdf2 的支持未验证
                # （设计 #708 §2「P0」）。
                if "glyf" not in font:
                    raise PdfFontUnavailable("PDF font is not TrueType")
                cmap = font.getBestCmap()
            finally:
                font.close()
    except PdfFontUnavailable:
        raise
    except Exception as error:  # noqa: BLE001 - 读不出的原因各式各样，都算「字体不可用」
        raise PdfFontUnavailable("PDF font cannot be read") from error
    if not cmap:
        raise PdfFontUnavailable("PDF font has no character map")
    return frozenset(cmap)


def is_latin1(text: str) -> bool:
    return all(ord(char) < _LATIN1_END for char in text)


class PdfFonts:
    """Choose Helvetica or Noto Sans SC per string (设计 #708 v3 §2「文本与字体」).

    Noto Sans SC 在本次渲染第一次需要时 `add_font` 一次；字体文件在 `stack` 关闭之前一直可读
    （fpdf2 懒加载字体表，输出时才子集化）。
    """

    def __init__(self, pdf: FPDF, stack: contextlib.ExitStack) -> None:
        self._pdf = pdf
        self._stack = stack
        self._added = False

    def prepare(self, text: str) -> tuple[str, str]:
        """`(family, text to print)`."""
        if is_latin1(text):
            return HELVETICA, text
        cmap = font_cmap()
        text = "".join(
            char if ord(char) < _LATIN1_END or ord(char) in cmap else "?" for char in text
        )
        if is_latin1(text):
            return HELVETICA, text
        # 字体里没有的字符（含 Latin-1 的控制字符）不印缺字框；换行符是版面指令，保留。
        text = "".join(char if char == _NEWLINE or ord(char) in cmap else "?" for char in text)
        self._add_noto()
        return NOTO, text

    def _add_noto(self) -> None:
        if self._added:
            return
        try:
            path = self._stack.enter_context(resources.as_file(_font_resource()))
            self._pdf.add_font(NOTO, "", str(path))
        except Exception as error:  # noqa: BLE001 - 同 `font_cmap`
            raise PdfFontUnavailable("PDF font cannot be loaded") from error
        self._added = True


__all__ = [
    "HELVETICA",
    "NOTO",
    "NOTO_FONT_FILE",
    "NOTO_FONT_SHA256",
    "PdfFontUnavailable",
    "PdfFonts",
    "font_cmap",
    "is_latin1",
]
