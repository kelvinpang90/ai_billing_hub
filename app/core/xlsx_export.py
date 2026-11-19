"""Excel (.xlsx) versions of the exports: one worksheet, same columns and rows as the CSV.

设计闸门 #753 v1 §2「新模块 app/core/xlsx_export.py」。输入与 `write_csv` 相同（单元格只收
`str` 或 `None`），只换编码；查询、上限、租约与分段发送都在调用方与 `csv_export`。

⚠️ **单元格类型**（§2「单元格类型」，INV-10）：先公式防护、再类型、再字符清洗。
- 所有字符串单元格显式标为字符串类型：openpyxl 的推断会把 `=` 开头的值写成公式、
  把 `#N/A` 等错误码写成错误值，文件里绝不能出现 `<f>` 与 `t="e"`。
- 文本列经 `formula_guarded`，与 CSV 逐字相同。
- 整数列只有 `_NUMERIC_INTEGER.fullmatch` 成立时写数字（数字格式 `0`），此时
  `str(int(value)) == value`；其余任何值原样写文本、不抛错。只用 `fullmatch`：
  `$` 在末尾换行之前也匹配，`"5\\n"` 会被当成整数。
- 金额、比率、时间等其余列原样写文本：Excel 的数字是双精度，只保 15 位有效数字。
  全程不经 `float`。
- 文本里 XML 1.0 不允许的字符各换成一个 U+FFFD；清洗后超过 `XLSX_MAX_TEXT_LENGTH`
  抛 `ValueError`（openpyxl 会静默截断）。

⚠️ **临时文件**（§2「临时文件」）：openpyxl 第一次 `append` 时把工作表写进
`tempfile` 目录下的临时文件，保存成功时自己删除；中途出错不删。`write_xlsx` 在出错时
按固定四步删掉自己工作表的那一个文件，不扫描目录（会删到并发导出正在写的文件）。
这些步骤用到 openpyxl 3.1 的私有属性（`_rows`、`_writer`、`xf`、`out`、`ALL_TEMP_FILES`），
依赖上界 `<3.2` 就是为此；属性名变了，`tests/backend/test_xlsx_export.py` 的清理用例会红。

ZIP 只在内存里拼。模块不碰数据库、不打日志。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from collections.abc import Collection, Iterable, Sequence
from typing import Any, Final

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.worksheet import _writer as openpyxl_worksheet_writer

from app.core.csv_export import ExportCsvResponse, export_attachment_headers, formula_guarded

XLSX_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Excel 单元格的字符上限。
XLSX_MAX_TEXT_LENGTH: Final = 32_767

# 只用 fullmatch；15 位 = Excel 的有效数字上限（0 到 999 999 999 999 999）。
_NUMERIC_INTEGER: Final = re.compile(r"0|[1-9][0-9]{0,14}")
# XML 1.0 不允许的字符：C0 控制字符（制表符、换行、回车除外）、U+FFFE、U+FFFF、代理码点。
_XML_ILLEGAL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_REPLACEMENT: Final = "�"
_INTEGER_NUMBER_FORMAT: Final = "0"

_TEXT: Final = 0
_INTEGER: Final = 1
_PLAIN: Final = 2


def _text_cell(worksheet: Any, value: str) -> Any:
    text = _XML_ILLEGAL.sub(_REPLACEMENT, value)
    if len(text) > XLSX_MAX_TEXT_LENGTH:
        raise ValueError("xlsx text cells hold at most 32,767 characters")
    cell = WriteOnlyCell(worksheet, text)
    # 推断可能已把它标成公式（`f`）或错误值（`e`）；值不变，类型改回字符串。
    cell.data_type = "s"
    return cell


def _integer_cell(worksheet: Any, value: str) -> Any:
    cell = WriteOnlyCell(worksheet, int(value))
    cell.number_format = _INTEGER_NUMBER_FORMAT
    return cell


def _row_cells(worksheet: Any, row: Sequence[str | None], kinds: Sequence[int]) -> list[Any]:
    cells: list[Any] = []
    for value, kind in zip(row, kinds, strict=True):
        if value is None:
            cells.append(None)
        elif type(value) is not str:
            raise TypeError("xlsx cells must be str or None")
        elif kind == _TEXT:
            cells.append(_text_cell(worksheet, formula_guarded(value)))
        elif kind == _INTEGER and _NUMERIC_INTEGER.fullmatch(value) is not None:
            cells.append(_integer_cell(worksheet, value))
        else:
            cells.append(_text_cell(worksheet, value))
    return cells


def _discard_temporary_file(worksheet: Any) -> None:
    """Delete the worksheet's temporary file after a failure (§2「临时文件」, four steps in order).

    每一步各自吞掉清理异常：清理失败不掩盖原异常。
    """
    # ① 结束 `_write_rows` 生成器，退出它持有的 `sheetData` 元素。
    with contextlib.suppress(Exception):
        rows = getattr(worksheet, "_rows", None)
        if rows is not None:
            rows.close()
    # ② 写出器在第一次 `append` 时才建；没有它就没有可达的临时文件。
    writer = None
    with contextlib.suppress(Exception):
        writer = getattr(worksheet, "_writer", None)
    if writer is None:
        return
    # ③ 关闭写 XML 的生成器，`with xmlfile(out)` 随之退出、句柄关闭。
    # 先关再删：Windows 上才删得掉，Linux 上也不删仍在写的文件。
    with contextlib.suppress(Exception):
        writer.xf.close()
    # ④ `out` 只是路径字符串。保存时复制完之后才出错的，openpyxl 已删，这里什么也不做。
    with contextlib.suppress(Exception):
        path = writer.out
        if isinstance(path, str) and os.path.exists(path):
            os.remove(path)
        if path in openpyxl_worksheet_writer.ALL_TEMP_FILES:
            openpyxl_worksheet_writer.ALL_TEMP_FILES.remove(path)


def write_xlsx(
    columns: Sequence[str],
    rows: Iterable[Sequence[str | None]],
    text_columns: Collection[str],
    integer_columns: Collection[str],
    *,
    sheet_title: str,
) -> bytes:
    """One worksheet `sheet_title`: the header, then one row per item of `rows`, as xlsx bytes."""
    if any(name not in columns for name in integer_columns):
        raise ValueError("integer_columns must be a subset of columns")
    if any(name in text_columns for name in integer_columns):
        raise ValueError("a column cannot be both a text column and an integer column")
    kinds = [
        _TEXT if name in text_columns else _INTEGER if name in integer_columns else _PLAIN
        for name in columns
    ]
    workbook = None
    try:
        workbook = Workbook(write_only=True)
        worksheet = workbook.create_sheet(title=sheet_title)
        worksheet.append([_text_cell(worksheet, name) for name in columns])
        for row in rows:
            worksheet.append(_row_cells(worksheet, row, kinds))
        buffer = io.BytesIO()
        workbook.save(buffer)
    except BaseException:
        if workbook is not None:
            for sheet in workbook.worksheets:
                _discard_temporary_file(sheet)
        raise
    return buffer.getvalue()


class ExportXlsxResponse(ExportCsvResponse):
    """The CSV attachment response with the xlsx media type; same 64 KiB chunked sending."""

    # 不是 `text/*`，Starlette 不追加 charset。
    media_type = XLSX_MEDIA_TYPE


def xlsx_response(filename: str, body: bytes) -> ExportXlsxResponse:
    """200 xlsx attachment. `filename` is built from validated dates by the caller."""
    return ExportXlsxResponse(content=body, headers=export_attachment_headers(filename))


__all__ = [
    "XLSX_MAX_TEXT_LENGTH",
    "XLSX_MEDIA_TYPE",
    "ExportXlsxResponse",
    "write_xlsx",
    "xlsx_response",
]
