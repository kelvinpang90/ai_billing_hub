"""Searching an uncompressed fpdf2 PDF (gate #750 v1 §7; used by the receipt and pdf_text tests).

Helvetica 的文字是 Latin-1 的 `(…)` 字面量，fpdf2 会把其中的 `\\`、`(`、`)` 转义，所以检索前先用
`literal` 转义同样的字符。Noto Sans SC 的文字是子集里的两字节编号，经 ToUnicode CMap 还原
（`unicode_texts`）。页数数 `/Type /Page` 对象（不含 `/Type /Pages`）。对账单测试保留自己的那份。
"""

from __future__ import annotations

import re

_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_OBJECT = re.compile(rb"(?<![0-9])([0-9]+)\s+0\s+obj\b(.*?)\bendobj", re.S)
_CONTENTS = re.compile(rb"/Contents\s*(\[[^\]]*\]|[0-9]+\s+0\s+R)")
_REFERENCE = re.compile(rb"([0-9]+)\s+0\s+R")
_LITERAL_TJ = re.compile(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", re.S)
_HEX_TJ = re.compile(rb"<([0-9A-Fa-f]*)>\s*Tj")
_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFCHAR_PAIR = re.compile(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]+)>")
_BASE_FONT_NOTO = re.compile(rb"/BaseFont\s*/\S*NotoSansSC")
# fpdf2 把每段文字写成 `BT x y Td … ET`（设计 #750 §10 假设 3）。
_TEXT_POSITION = re.compile(rb"\bBT\s+(-?[0-9]+(?:\.[0-9]+)?)\s+(-?[0-9]+(?:\.[0-9]+)?)\s+Td\b")


def literal(text: str) -> bytes:
    """`text` as fpdf2 writes a Helvetica string into the content stream."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("latin-1") + b")"


def _unescape(raw: bytes) -> bytes:
    return re.sub(rb"\\(.)", lambda m: b"\r" if m.group(1) == b"r" else m.group(1), raw, flags=re.S)


def pages(pdf: bytes) -> int:
    return len(_PAGE_OBJECT.findall(pdf))


def page_streams(pdf: bytes) -> list[bytes]:
    """The content of every page, found through the `/Contents` of each `/Type /Page` object.

    一页的 `/Contents` 是数组时，几段内容流按顺序接起来。
    """
    objects = {int(number): body for number, body in _OBJECT.findall(pdf)}
    streams = []
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
        streams.append(b"\n".join(parts))
    return streams


def texts(pdf: bytes) -> list[str]:
    """Every Helvetica `(…) Tj` string of the page content, in drawing order."""
    return [
        _unescape(raw).decode("latin-1")
        for stream in page_streams(pdf)
        for raw in _LITERAL_TJ.findall(stream)
    ]


def to_unicode(pdf: bytes) -> dict[bytes, str]:
    mapping: dict[bytes, str] = {}
    for section in _BFCHAR.findall(pdf):
        for code, target in _BFCHAR_PAIR.findall(section):
            mapping[bytes.fromhex(code.decode("ascii"))] = bytes.fromhex(
                target.decode("ascii")
            ).decode("utf-16-be")
    return mapping


def unicode_texts(pdf: bytes) -> list[str]:
    """Every Noto Sans SC string of the page content, decoded through the ToUnicode CMap.

    只收每两个字节都在 CMap 里的字符串：Helvetica 的 ASCII 两两拼起来是 0x2020 以上的编号，
    子集的编号远小于它。
    """
    mapping = to_unicode(pdf)
    raws: list[bytes] = []
    for stream in page_streams(pdf):
        raws.extend(_unescape(raw) for raw in _LITERAL_TJ.findall(stream))
        raws.extend(bytes.fromhex(raw.decode("ascii")) for raw in _HEX_TJ.findall(stream))
    decoded = []
    for raw in raws:
        codes = [raw[index : index + 2] for index in range(0, len(raw), 2)]
        if raw and len(raw) % 2 == 0 and all(code in mapping for code in codes):
            decoded.append("".join(mapping[code] for code in codes))
    return decoded


def bfchar_hex(pdf: bytes) -> str:
    return b"".join(_BFCHAR.findall(pdf)).decode("latin-1").upper()


def embeds_noto(pdf: bytes) -> bool:
    """A Noto Sans SC subset is embedded (`/FontFile2` and a `/BaseFont` naming it)."""
    return b"/FontFile2" in pdf and _BASE_FONT_NOTO.search(pdf) is not None


def text_positions(pdf: bytes) -> list[tuple[float, float]]:
    """`(x, y)` of every `BT x y Td` in the page content, in points from the bottom-left corner."""
    return [
        (float(x), float(y))
        for stream in page_streams(pdf)
        for x, y in _TEXT_POSITION.findall(stream)
    ]
