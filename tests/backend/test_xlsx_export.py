"""xlsx writer, attachment response and the shared CSV helpers (design gate #753 v1 §2, §7).

单元层：不起服务、不碰数据库，覆盖 §7 标 AIH-TASK-235 的各行。
接口用例属于 AIH-TASK-236、237。

⚠️ **回车的比较口径**（§7 开头）：值里含回车时，期望值与读回值两边
都先把回车换成换行再逐字比较；其余字符（含开头的 `'`、换行、制表符）
逐字比较。见 `assert_same_text`。

⚠️ 只从 test_csv_export 导入普通函数与常量，不转引用夹具（CLAUDE.md「写后端测试的约定」）。
"""

from __future__ import annotations

import io
import re
import tempfile
import time
import tracemalloc
import zipfile
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from openpyxl.worksheet import _writer as openpyxl_worksheet_writer
from test_csv_export import (
    MIB,
    ZERO_UUID,
    http_scope,
    longest_row,
    read_back,
    run_asgi,
    usage_cells,
)

from app.core.csv_export import (
    EXPORT_MAX_ROWS,
    EXPORT_SEND_CHUNK_BYTES,
    ExportCsvResponse,
    csv_response,
    export_attachment_headers,
    formula_guarded,
    write_csv,
)
from app.core.xlsx_export import (
    XLSX_MAX_TEXT_LENGTH,
    XLSX_MEDIA_TYPE,
    ExportXlsxResponse,
    write_xlsx,
    xlsx_response,
)
from app.schemas.customer_usage import USAGE_EXPORT_COLUMNS, USAGE_EXPORT_TEXT_COLUMNS

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
SHEET = "usage-events"
SHEET_XML = "xl/worksheets/sheet1.xml"
CORE_XML = "docProps/core.xml"
ZIP_MEMBERS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/theme/theme1.xml",
        "xl/worksheets/sheet1.xml",
    }
)
# 设计 §2「各导出的整数列」的用量一行；schema 常量由 AIH-TASK-236 加。
USAGE_INTEGER_COLUMNS = frozenset(
    {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}
)

# 三列：文本、整数、其余（金额）。
COLUMNS = ("project_name", "request_count", "charged_amount")
TEXT_COLUMNS = frozenset({"project_name"})
INTEGER_COLUMNS = frozenset({"request_count"})

_FORMULA_ELEMENT = re.compile(rb"<(?:\w+:)?f[\s>/]")


def write(
    rows: Sequence[Sequence[str | None]],
    columns: Sequence[str] = COLUMNS,
    text_columns: frozenset[str] = TEXT_COLUMNS,
    integer_columns: frozenset[str] = INTEGER_COLUMNS,
) -> bytes:
    return write_xlsx(columns, rows, text_columns, integer_columns, sheet_title=SHEET)


def zip_member(body: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return archive.read(name)


def read_cells(body: bytes, width: int) -> list[tuple[Any, ...]]:
    """Every row of the only worksheet as read-only cells, padded to `width` columns."""
    workbook = openpyxl.load_workbook(io.BytesIO(body), read_only=True)
    try:
        (worksheet,) = workbook.worksheets
        # 不信文件里的 dimension：按给定宽度补齐末尾的空单元格。
        worksheet.reset_dimensions()
        return [tuple(row) for row in worksheet.iter_rows(max_col=width)]
    finally:
        workbook.close()


def read_values(body: bytes, width: int) -> list[tuple[Any, ...]]:
    return [tuple(cell.value for cell in row) for row in read_cells(body, width)]


def assert_same_text(actual: object, expected: str) -> None:
    """§7「回车的比较口径」：只放宽回车本身。"""
    assert isinstance(actual, str), (actual, expected)
    if "\r" in expected:
        assert actual.replace("\r", "\n") == expected.replace("\r", "\n")
    else:
        assert actual == expected


def assert_matches_csv(xlsx_value: object, csv_field: str) -> None:
    if xlsx_value is None:
        assert csv_field == ""
    elif isinstance(xlsx_value, int):
        assert str(xlsx_value) == csv_field
    else:
        assert_same_text(xlsx_value, csv_field)


# --- CSV 的配合改动 ---------------------------------------------------------------------


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_formula_guarded_quotes_each_prefix(prefix: str) -> None:
    assert formula_guarded(prefix + "1") == "'" + prefix + "1"
    assert formula_guarded(prefix) == "'" + prefix


@pytest.mark.parametrize("value", ["", "a=1", " =1", "0.5", "项目", "'=1", "\n=1", "#N/A"])
def test_formula_guarded_leaves_other_values_untouched(value: str) -> None:
    assert formula_guarded(value) == value


def test_export_attachment_headers_are_the_csv_response_headers() -> None:
    name = "usage-events_2026-10-01_2026-10-31.csv"
    headers = export_attachment_headers(name)
    assert headers == {
        "Content-Disposition": f'attachment; filename="{name}"',
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    response = csv_response(name, b"")
    for key, value in headers.items():
        assert response.headers[key] == value


@pytest.mark.parametrize("name", ["", 'a"b.xlsx', "a b.xlsx", "../x.xlsx", "项目.xlsx", "a;b"])
def test_export_attachment_headers_reject_unsafe_filenames(name: str) -> None:
    with pytest.raises(ValueError):
        export_attachment_headers(name)


# --- 基本形状 ---------------------------------------------------------------------------


def test_one_worksheet_header_first_and_typed_cells() -> None:
    body = write([("alpha", "12", "0.50000000"), ("beta", None, None), (None, "0", "1.00000000")])
    assert body.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert set(archive.namelist()) == ZIP_MEMBERS
        assert len(archive.namelist()) == len(ZIP_MEMBERS)
    workbook = openpyxl.load_workbook(io.BytesIO(body), read_only=True)
    try:
        assert workbook.sheetnames == [SHEET]
    finally:
        workbook.close()
    assert read_values(body, len(COLUMNS)) == [
        COLUMNS,
        ("alpha", 12, "0.50000000"),
        ("beta", None, None),
        (None, 0, "1.00000000"),
    ]
    rows = read_values(body, len(COLUMNS))
    assert all(type(value) is str for value in rows[0])
    assert type(rows[1][0]) is str
    assert type(rows[1][1]) is int
    assert type(rows[1][2]) is str


def test_no_column_widths_panes_or_filters() -> None:
    sheet = zip_member(write([("alpha", "12", "0.50000000")]), SHEET_XML)
    for marker in (b"<cols", b"<pane", b"<autoFilter", b"<mergeCell"):
        assert marker not in sheet


def test_an_empty_export_is_the_header_only() -> None:
    body = write([])
    assert read_values(body, len(COLUMNS)) == [COLUMNS]


# --- 与 CSV 逐格一致 --------------------------------------------------------------------


def test_every_cell_matches_the_csv_field() -> None:
    columns = ("name", "project", "count", "amount", "note")
    text_columns = frozenset({"name", "project"})
    integer_columns = frozenset({"count"})
    rows: list[tuple[str | None, ...]] = [
        ("=SUM(A1)", '项目,"引号"', "0", "-0.04120000", "a,b"),
        ("+1", "line\nbreak", "123456789012345", "999999999999.99999999", 'say "hi"'),
        ("\rcr", "a\rb", "1000000000000000", "0.00000001", "x\ry"),
        ("中文 مرحبا", None, None, None, None),
        ("@A1", "\t tab", "007", "1.5", "-1"),
        ("-2", "a\rb\nc", str(2**63 - 1), "0E-8", "line\n"),
    ]
    csv_rows = read_back(write_csv(columns, rows, text_columns))
    xlsx_rows = read_values(write(rows, columns, text_columns, integer_columns), len(columns))
    assert len(xlsx_rows) == len(csv_rows) == len(rows) + 1
    for xlsx_row, csv_row in zip(xlsx_rows, csv_rows, strict=True):
        assert len(xlsx_row) == len(csv_row) == len(columns)
        for xlsx_value, csv_field in zip(xlsx_row, csv_row, strict=True):
            assert_matches_csv(xlsx_value, csv_field)


# --- 公式与错误值 -----------------------------------------------------------------------


def test_formulas_and_error_values_never_appear() -> None:
    columns = ("name", "amount")
    text_values = ["=1+1", "+1", "-1", "@A1", "\t=1", "\r=1", "#N/A", "#REF!"]
    rows = [(value, "#N/A") for value in text_values] + [("x", "#REF!"), ("y", "=1+1")]
    body = write(rows, columns, frozenset({"name"}), frozenset())
    csv_rows = read_back(write_csv(columns, rows, frozenset({"name"})))
    xlsx_rows = read_values(body, len(columns))
    for xlsx_row, csv_row in zip(xlsx_rows[1:], csv_rows[1:], strict=True):
        assert_same_text(xlsx_row[0], csv_row[0])
        assert_same_text(xlsx_row[1], csv_row[1])
    for value, xlsx_row in zip(text_values, xlsx_rows[1:9], strict=True):
        assert_same_text(xlsx_row[0], formula_guarded(value))
    assert [row[0] for row in xlsx_rows[1:6]] == ["'=1+1", "'+1", "'-1", "'@A1", "'\t=1"]
    # 回车开头的值按「回车的比较口径」：关键是开头的单引号。
    assert xlsx_rows[6][0] in ("'\r=1", "'\n=1")
    assert [row[0] for row in xlsx_rows[7:9]] == ["#N/A", "#REF!"]
    assert [row[1] for row in xlsx_rows[1:]] == ["#N/A"] * len(text_values) + ["#REF!", "=1+1"]
    sheet = zip_member(body, SHEET_XML)
    assert _FORMULA_ELEMENT.search(sheet) is None
    assert b't="e"' not in sheet
    assert b"t='e'" not in sheet


# --- 金额与整数精度 ---------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["999999999999.99999999", "0.00000001", "-0.04120000"])
def test_amounts_read_back_as_the_same_text(amount: str) -> None:
    (cell,) = read_cells(write([("p", None, amount)]), len(COLUMNS))[1][2:]
    assert cell.data_type == "s"
    assert cell.value == amount


@pytest.mark.parametrize("value", ["0", "999999999999999"])
def test_canonical_integers_up_to_15_digits_are_numbers(value: str) -> None:
    cell = read_cells(write([("p", value, None)]), len(COLUMNS))[1][1]
    assert type(cell.value) is int
    assert cell.data_type == "n"
    assert cell.number_format == "0"
    assert str(cell.value) == value


@pytest.mark.parametrize(
    "value",
    [
        "1000000000000000",
        str(2**63 - 1),
        "007",
        "5\n",
        " 5",
        "1.5",
        "-1",
        chr(0xFF10),
    ],
)
def test_other_integer_column_values_stay_the_same_text(value: str) -> None:
    cell = read_cells(write([("p", value, None)]), len(COLUMNS))[1][1]
    assert cell.data_type == "s"
    # 都不含回车：逐字比较。
    assert cell.value == value


# --- 字符清洗与长度 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\x01a", "�a"),
        ("a\x0bb", "a�b"),
        ("a\x0cb\x1f", "a�b�"),
        ("\x00", "�"),
        (chr(0xFFFE), "�"),
        (chr(0xFFFF) + "z", "�z"),
        ("a\ud800b", "a�b"),
        ("a\tb\nc", "a\tb\nc"),
        ("a\rb", "a\rb"),
    ],
)
def test_xml_illegal_characters_become_the_replacement_character(value: str, expected: str) -> None:
    rows = read_values(write([(value, None, value)]), len(COLUMNS))
    # 文本列（经公式防护，这些值都不以前缀开头）与其余列一样清洗。
    assert_same_text(rows[1][0], expected)
    assert_same_text(rows[1][2], expected)


def test_text_longer_than_the_excel_limit_is_rejected() -> None:
    assert XLSX_MAX_TEXT_LENGTH == 32_767
    longest = "x" * XLSX_MAX_TEXT_LENGTH
    assert read_values(write([(longest, None, None)]), len(COLUMNS))[1][0] == longest
    with pytest.raises(ValueError):
        write([("x" * (XLSX_MAX_TEXT_LENGTH + 1), None, None)])
    with pytest.raises(ValueError):
        write([(None, None, "y" * (XLSX_MAX_TEXT_LENGTH + 1))])
    # 公式前缀加的单引号也算长度。
    with pytest.raises(ValueError):
        write([("=" + "x" * (XLSX_MAX_TEXT_LENGTH - 1), None, None)])


# --- 输入约定 ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, Decimal("1.00000000"), 1.5, b"x"])
def test_cells_must_be_str_or_none(value: object) -> None:
    with pytest.raises(TypeError):
        write([("p", "1", value)])


@pytest.mark.parametrize("row", [("p", "1"), ("p", "1", "2", "3")])
def test_a_row_must_match_the_columns(row: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        write([row])


def test_integer_columns_must_not_overlap_text_columns() -> None:
    with pytest.raises(ValueError):
        write([], integer_columns=frozenset({"request_count", "project_name"}))


def test_integer_columns_must_be_known_columns() -> None:
    with pytest.raises(ValueError):
        write([], integer_columns=frozenset({"request_count", "unknown"}))


# --- 临时文件清理 -----------------------------------------------------------------------


@pytest.fixture
def temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    yield tmp_path
    assert list(tmp_path.iterdir()) == []
    assert not [
        path
        for path in openpyxl_worksheet_writer.ALL_TEMP_FILES
        if Path(path).is_relative_to(tmp_path)
    ]


ROW = ("p", "1", "0.10000000")


def test_a_successful_export_leaves_no_temporary_file(temp_dir: Path) -> None:
    body = write([ROW] * 3)
    assert len(read_values(body, len(COLUMNS))) == 4


@pytest.mark.parametrize("good_rows", [2, 0])
def test_a_failing_row_iterator_leaves_no_temporary_file(temp_dir: Path, good_rows: int) -> None:
    error = RuntimeError("row source failed")
    seen: list[list[Path]] = []

    def rows() -> Iterator[tuple[str, ...]]:
        yield from [ROW] * good_rows
        # 写出器已建（表头已 append）；good_rows == 0 时还没有数据行。
        seen.append(list(temp_dir.iterdir()))
        raise error

    with pytest.raises(RuntimeError) as caught:
        write_xlsx(COLUMNS, rows(), TEXT_COLUMNS, INTEGER_COLUMNS, sheet_title=SHEET)
    assert caught.value is error
    assert len(seen) == 1 and len(seen[0]) == 1


def test_a_failing_write_tail_leaves_no_temporary_file(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = RuntimeError("write_tail failed")
    seen: list[list[Path]] = []

    def write_tail(self: Any) -> None:
        seen.append(list(temp_dir.iterdir()))
        raise error

    monkeypatch.setattr(openpyxl_worksheet_writer.WorksheetWriter, "write_tail", write_tail)
    with pytest.raises(RuntimeError) as caught:
        write([ROW] * 3)
    assert caught.value is error
    assert len(seen) == 1 and len(seen[0]) == 1


def test_a_failing_copy_into_the_zip_leaves_no_temporary_file(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = OSError("No space left on device")
    seen: list[list[Path]] = []

    def failing_write(self: Any, filename: Any, *args: Any, **kwargs: Any) -> None:
        # 工作表已关闭，文件仍在。
        seen.append(list(temp_dir.iterdir()))
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "write", failing_write)
    with pytest.raises(OSError) as caught:
        write([ROW] * 3)
    assert caught.value is error
    assert len(seen) == 1 and len(seen[0]) == 1


# --- 可重现 -----------------------------------------------------------------------------


def test_the_same_rows_give_the_same_members_except_core_properties() -> None:
    rows = [
        ("qzproject-alpha", "987654321098", "123.45678901"),
        ("=qzformula", None, "-0.04120000"),
        (None, "86420", None),
    ]
    first = write(rows)
    second = write(rows)
    with zipfile.ZipFile(io.BytesIO(first)) as one, zipfile.ZipFile(io.BytesIO(second)) as two:
        assert set(one.namelist()) == set(two.namelist()) == ZIP_MEMBERS
        for name in ZIP_MEMBERS - {CORE_XML}:
            assert one.read(name) == two.read(name), name
        core = one.read(CORE_XML).decode("utf-8")
    values = [*COLUMNS, *(value for row in rows for value in row if value is not None)]
    for value in values:
        assert value not in core
    assert formula_guarded("=qzformula") not in core


# --- 资源边界 ---------------------------------------------------------------------------


def longest_usage_xlsx() -> bytes:
    rows = [longest_row(index) for index in range(EXPORT_MAX_ROWS)]
    return write_xlsx(
        USAGE_EXPORT_COLUMNS,
        (usage_cells(row) for row in rows),
        USAGE_EXPORT_TEXT_COLUMNS,
        USAGE_INTEGER_COLUMNS,
        sheet_title=SHEET,
    )


def test_the_peak_memory_of_one_xlsx_export_is_bounded() -> None:
    tracemalloc.start()
    try:
        body = longest_usage_xlsx()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak <= 128 * MIB, f"xlsx export peak {peak / MIB:.1f} MiB exceeds 128 MiB"
    cells = read_cells(body, len(USAGE_EXPORT_COLUMNS))
    assert len(cells) == EXPORT_MAX_ROWS + 1
    assert tuple(cell.value for cell in cells[0]) == USAGE_EXPORT_COLUMNS
    for row in (cells[1], cells[-1]):
        for name in USAGE_INTEGER_COLUMNS:
            cell = row[USAGE_EXPORT_COLUMNS.index(name)]
            assert cell.data_type == "s"
            assert cell.value == str(2**63 - 1)
        for name in ("quantity", "charged_amount"):
            cell = row[USAGE_EXPORT_COLUMNS.index(name)]
            assert cell.data_type == "s"
            assert cell.value == "999999999999.99999999"


def test_the_temporary_worksheet_file_is_bounded(capsys: pytest.CaptureFixture[str]) -> None:
    started = time.perf_counter()
    body = longest_usage_xlsx()
    seconds = time.perf_counter() - started
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        size = archive.getinfo(SHEET_XML).file_size
    # 设计 §2「资源边界」：CI 上的生成秒数写进 PR，所以不让 pytest 吞掉这一行。
    with capsys.disabled():
        print(
            f"\nxlsx longest shape, {EXPORT_MAX_ROWS} rows: generated in {seconds:.2f} s, "
            f"{SHEET_XML} {size / MIB:.1f} MiB, file {len(body) / MIB:.1f} MiB"
        )
    assert size <= 96 * MIB, f"{SHEET_XML} is {size / MIB:.1f} MiB, over 96 MiB"


# --- 响应与分段发送 ---------------------------------------------------------------------


def test_xlsx_response_headers() -> None:
    body = write([(ZERO_UUID, "1", "0.10000000")])
    response = xlsx_response("usage-events_2026-10-01_2026-10-31.xlsx", body)
    assert isinstance(response, ExportXlsxResponse)
    assert isinstance(response, ExportCsvResponse)
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MEDIA_TYPE
    assert "charset" not in response.headers["content-type"]
    assert XLSX_MEDIA_TYPE == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="usage-events_2026-10-01_2026-10-31.xlsx"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-length"] == str(len(body))


@pytest.mark.parametrize("name", ["", 'a"b.xlsx', "a b.xlsx", "../x.xlsx", "项目.xlsx", "a;b.xlsx"])
def test_xlsx_response_rejects_unsafe_filenames(name: str) -> None:
    with pytest.raises(ValueError):
        xlsx_response(name, b"")


@pytest.mark.parametrize(
    "size",
    [4, EXPORT_SEND_CHUNK_BYTES, EXPORT_SEND_CHUNK_BYTES + 1, 8 * MIB + 123],
)
def test_the_xlsx_body_is_sent_in_chunks_like_csv(size: int) -> None:
    body = bytes(index % 251 for index in range(size))
    sent = run_asgi(xlsx_response("x.xlsx", body), http_scope())
    start, *chunks = sent
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-type"] == XLSX_MEDIA_TYPE.encode()
    assert headers[b"content-length"] == str(size).encode()
    assert headers[b"x-accel-buffering"] == b"no"
    assert all(message["type"] == "http.response.body" for message in chunks)
    assert all(isinstance(message["body"], memoryview) for message in chunks)
    assert all(len(message["body"]) <= EXPORT_SEND_CHUNK_BYTES for message in chunks)
    assert [message["more_body"] for message in chunks] == [True] * (len(chunks) - 1) + [False]
    assert len(chunks) == max(1, -(-size // EXPORT_SEND_CHUNK_BYTES))
    assert b"".join(bytes(message["body"]) for message in chunks) == body
    csv_sent = run_asgi(csv_response("x.csv", body), http_scope())
    assert [bytes(message["body"]) for message in csv_sent[1:]] == [
        bytes(message["body"]) for message in chunks
    ]
