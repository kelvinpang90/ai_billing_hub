"""把 docs/REQUIREMENTS.md 与 spec 之间的三件事机械锁死（AIH-TASK-007 / R5）。

1. spec 的每个一级编号章节（`# N.`）在第二节索引里恰好一行；反过来，索引里出现的
   每个编号要么在 spec 里有对应标题，要么被就地标成已退役。
2. spec 与第 1.2 节追溯表里的 `REQ-*` 双向一致 —— 一边有一边没有都算破。
3. 每条硬性要求在第 1.3 节覆盖表里恰好一行；覆盖列要么是 spec 原文里真实存在的
   `REQ-*` ID，要么是固定串「缺口」并在同一行给出理由。

「硬性要求」按 REQUIREMENTS.md 第 1.1 节的定义，**从 spec 原文按结构枚举**：§133 的
每个 `## Invariant N` 标题 + §132 有序列表里的每一项 DoD + 该有序列表之后那串上线前
闸门。条数与条目正文都不写进本文件 —— spec 增删一条，枚举结果自动跟着变，脚本不用改。
这正是不能用关键词判定的原因：spec 不按 RFC 2119 写，大写 MUST / SHALL / NEVER 全文
只有 14 次，而不分大小写的 must 有 173 次，两头都够不着。

失败信息一律指名道姓（缺哪一节、多哪个编号、哪个 REQ 只在一边、哪条硬性要求没有行、
哪个「缺口」没写理由）。只报「断言失败」不给具体项的检查拦不住任何真实回退。

只读 docs/ 下那两个文件：不调 Git、不联网、不写任何文件、只用标准库。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "Acuven_Central_AI_Billing_Platform_Spec.md"
DOC_PATH = ROOT / "docs" / "REQUIREMENTS.md"

# ---- spec 侧的解析规则（结构，不是关键词）----
TOP_SECTION_RE = re.compile(r"^# (\d+)\. +(\S.*?)\s*$")
ANY_SECTION_RE = re.compile(r"^#{1,6} (\d+(?:\.\d+)*)[.\s]\s*\S")
INVARIANT_RE = re.compile(r"^## Invariant (\d+)\s*$")
ORDERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(\S.*?)\s*$")
BULLET_ITEM_RE = re.compile(r"^-\s+(\S.*?)\s*$")
REQ_ID_RE = re.compile(r"REQ-[A-Z0-9]+-\d+")

# ---- 文档侧的解析规则 ----
DOC_HEADING_RE = re.compile(r"^(#{2,6})\s+(\S.*?)\s*$")
INDEX_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*$")
REQ_CELL_RE = re.compile(r"^`(REQ-[A-Z0-9]+-\d+)`$")
HARD_REQ_CELL_RE = re.compile(r"^(?:Invariant|DoD|Gate) \d+$")
SECTION_REF_RE = re.compile(r"§(\d+(?:\.\d+)*)")
RETIRED_VERSION_RE = re.compile(r"v\d+\.\d+")

RETIRED_MARKER = "已退役"
GAP_MARKER = "缺口"
# 占位符不算「填了」。em dash 是本仓库表格里表示「不适用」的写法。
EMPTY_CELLS = frozenset({"", "—", "–", "-", "N/A", "n/a", "TBD", "待定"})

INDEX_HEADING = "按主题的章节索引"
DEFINITION_HEADING = "什么算一条「硬性要求」"
TRACE_HEADING = "关键需求追溯表"
COVERAGE_HEADING = "硬性要求 → REQ 覆盖表"


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


SPEC_LINES = read_lines(SPEC_PATH)
SPEC_TEXT = "\n".join(SPEC_LINES)
DOC_LINES = read_lines(DOC_PATH)


def spec_top_sections() -> dict[str, str]:
    """`# N. Title` 形式的一级编号章节 → 标题，按 spec 里的出现顺序。"""
    sections: dict[str, str] = {}
    for line in SPEC_LINES:
        match = TOP_SECTION_RE.match(line)
        if match:
            sections[match.group(1)] = match.group(2)
    return sections


def spec_all_section_numbers() -> set[str]:
    """spec 里所有带编号的标题（含 §N.M 这类子节）。"""
    numbers = set()
    for line in SPEC_LINES:
        match = ANY_SECTION_RE.match(line)
        if match:
            numbers.add(match.group(1))
    return numbers


def spec_section_body(number: str) -> list[str]:
    """一级章节 `# <number>.` 的正文，到下一个一级标题为止。"""
    start = None
    for offset, line in enumerate(SPEC_LINES):
        match = TOP_SECTION_RE.match(line)
        if match and match.group(1) == number:
            start = offset + 1
            break
    if start is None:
        return []
    for offset in range(start, len(SPEC_LINES)):
        if SPEC_LINES[offset].startswith("# "):
            return SPEC_LINES[start:offset]
    return SPEC_LINES[start:]


def spec_req_ids() -> set[str]:
    return set(REQ_ID_RE.findall(SPEC_TEXT))


def hard_requirements() -> list[tuple[str, str]]:
    """按 REQUIREMENTS.md 第 1.1 节的规则从 spec 原文枚举硬性要求。

    返回 [(稳定标识, spec 原文)]。标识由解析结果派生（Invariant 的编号取自标题、
    DoD 的编号取自有序列表自己的序号、闸门按出现次序），不是写死的清单。
    """
    items: list[tuple[str, str]] = []

    invariants = spec_section_body("133")
    for offset, line in enumerate(invariants):
        match = INVARIANT_RE.match(line)
        if not match:
            continue
        text = ""
        for follow in invariants[offset + 1 :]:
            if INVARIANT_RE.match(follow):
                break
            if follow.strip():
                text = follow.strip()
                break
        items.append((f"Invariant {match.group(1)}", text))

    definition_of_done = spec_section_body("132")
    seen_ordered = False
    gates = 0
    for line in definition_of_done:
        ordered = ORDERED_ITEM_RE.match(line)
        if ordered:
            seen_ordered = True
            items.append((f"DoD {ordered.group(1)}", ordered.group(2)))
            continue
        bullet = BULLET_ITEM_RE.match(line)
        if bullet and seen_ordered:
            gates += 1
            items.append((f"Gate {gates}", bullet.group(1)))

    return items


def doc_section(keyword: str) -> list[str]:
    """标题里含 keyword 的那一小节的正文行；keyword 必须唯一命中一个标题。"""
    hits = []
    for offset, line in enumerate(DOC_LINES):
        match = DOC_HEADING_RE.match(line)
        if match and keyword in match.group(2):
            hits.append((offset, len(match.group(1))))
    if len(hits) != 1:
        raise AssertionError(
            f"docs/REQUIREMENTS.md 里标题含「{keyword}」的小节应恰好有 1 个，实得 {len(hits)} 个"
        )
    start, level = hits[0]
    for offset in range(start + 1, len(DOC_LINES)):
        match = DOC_HEADING_RE.match(DOC_LINES[offset])
        if match and len(match.group(1)) <= level:
            return DOC_LINES[start + 1 : offset]
    return DOC_LINES[start + 1 :]


def table_rows(lines: list[str]) -> list[list[str]]:
    """Markdown 表格行 → 去掉首尾竖线并逐格 strip 的单元格列表。"""
    rows = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


class DefinitionTests(unittest.TestCase):
    """第 1.1 节：定义必须写在正文里，而且逐类点名三个来源。"""

    def test_definition_is_prose_and_names_all_three_sources(self) -> None:
        body = "\n".join(doc_section(DEFINITION_HEADING))
        visible = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
        self.assertTrue(visible, "「硬性要求」的定义一节是空的，或整段被写进了 HTML 注释")
        for needle in ("## Invariant", "Definition of Done", "闸门"):
            self.assertIn(
                needle,
                visible,
                f"定义正文里没有出现「{needle}」；定义必须逐类点名 §133 的 Invariant、"
                "§132 的 Definition of Done 与 §132 末尾的上线前跨功能闸门",
            )


class SectionIndexTests(unittest.TestCase):
    """第二节章节索引：与 spec 的一级编号章节一一对应。"""

    def setUp(self) -> None:
        self.rows = [
            row for row in table_rows(doc_section(INDEX_HEADING)) if INDEX_NUMBER_RE.match(row[0])
        ]

    def _retired(self, row: list[str]) -> bool:
        return RETIRED_MARKER in " ".join(row)

    def test_every_top_level_spec_section_has_exactly_one_index_row(self) -> None:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row[0]] = counts.get(row[0], 0) + 1

        problems = []
        for number, title in spec_top_sections().items():
            seen = counts.get(number, 0)
            if seen == 0:
                problems.append(f"索引里缺一级章节 {number}（spec 标题：{title}）")
            elif seen > 1:
                problems.append(f"索引里一级章节 {number} 有 {seen} 行，应恰好 1 行")
        if problems:
            self.fail("\n".join(problems))

    def test_index_numbers_are_known_sections_or_marked_retired(self) -> None:
        known = spec_all_section_numbers()
        problems = []
        for row in self.rows:
            number = row[0]
            retired = self._retired(row)
            if retired and number in known:
                problems.append(
                    f"索引把 {number} 标成{RETIRED_MARKER}，但 spec 里仍有编号为 {number} 的标题"
                )
            elif not retired and number not in known:
                problems.append(
                    f"索引里的 {number} 在 spec 里没有对应标题，也没有标成{RETIRED_MARKER}"
                )
        if problems:
            self.fail("\n".join(problems))

    def test_retired_rows_name_the_retiring_version(self) -> None:
        problems = []
        for row in self.rows:
            text = " ".join(row)
            if self._retired(row) and not RETIRED_VERSION_RE.search(text):
                problems.append(
                    f"索引里 {row[0]} 标成{RETIRED_MARKER}但没写退役版本（形如 v1.4）"
                )
        if problems:
            self.fail("\n".join(problems))

    def test_retired_numbers_are_never_written_as_section_refs(self) -> None:
        """已退役的编号写成 §N 会让 scripts/check_docs.py 报 unknown spec section。"""
        retired = {row[0] for row in self.rows if self._retired(row)}
        problems = []
        for line_no, line in enumerate(DOC_LINES, 1):
            for ref in SECTION_REF_RE.findall(line):
                if ref in retired:
                    problems.append(
                        f"docs/REQUIREMENTS.md:{line_no} 把已退役的 {ref} 写成了带 § 的形式；"
                        "请沿用索引的纯数字列写法"
                    )
        if problems:
            self.fail("\n".join(problems))


class TraceabilityTableTests(unittest.TestCase):
    """第 1.2 节追溯表：与 spec 的 REQ-* 双向闭合。"""

    def setUp(self) -> None:
        self.rows = [
            row for row in table_rows(doc_section(TRACE_HEADING)) if REQ_CELL_RE.match(row[0])
        ]

    def test_req_ids_close_both_ways(self) -> None:
        in_spec = spec_req_ids()
        counts: dict[str, int] = {}
        for row in self.rows:
            req = REQ_CELL_RE.match(row[0]).group(1)
            counts[req] = counts.get(req, 0) + 1

        problems = []
        for req in sorted(in_spec - set(counts)):
            problems.append(f"{req} 出现在 spec 原文里，但追溯表里没有对应行")
        for req in sorted(set(counts) - in_spec):
            problems.append(
                f"{req} 出现在追溯表里，但 spec 原文里一次都没有 —— REQ ID 的唯一来源是 spec"
            )
        for req in sorted(counts):
            if counts[req] > 1:
                problems.append(f"{req} 在追溯表里有 {counts[req]} 行，应恰好 1 行")
        if problems:
            self.fail("\n".join(problems))

    def test_every_row_fills_sections_and_evidence(self) -> None:
        problems = []
        for row in self.rows:
            req = REQ_CELL_RE.match(row[0]).group(1)
            if len(row) < 4:
                problems.append(f"{req} 那一行只有 {len(row)} 列，追溯表应为 4 列")
                continue
            if row[1] in EMPTY_CELLS:
                problems.append(f"{req} 的「规范性规则」列是空的")
            if row[2] in EMPTY_CELLS:
                problems.append(f"{req} 的「主要章节」列是空的")
            if row[3] in EMPTY_CELLS:
                problems.append(f"{req} 的「必需测试证据」列是空的")
        if problems:
            self.fail("\n".join(problems))


class CoverageTableTests(unittest.TestCase):
    """第 1.3 节覆盖表：每条硬性要求一行，覆盖状态明确。"""

    def setUp(self) -> None:
        self.required = hard_requirements()
        self.by_id: dict[str, list[list[str]]] = {}
        for row in table_rows(doc_section(COVERAGE_HEADING)):
            if HARD_REQ_CELL_RE.match(row[0]):
                self.by_id.setdefault(row[0], []).append(row)

    def test_spec_enumeration_produces_all_three_kinds(self) -> None:
        """三类里任何一类解析不出来，下面的检查都会变成空转。"""
        kinds = {item[0].split(" ")[0] for item in self.required}
        self.assertEqual(
            kinds,
            {"Invariant", "DoD", "Gate"},
            f"从 spec §132 / §133 枚举硬性要求时有一类一条都没解析出来：实得 {sorted(kinds)}。"
            "检查 §133 的 `## Invariant N` 标题、§132 的有序列表与其后的闸门列表是否被改过",
        )

    def test_every_hard_requirement_has_exactly_one_row(self) -> None:
        problems = []
        for req_id, text in self.required:
            found = self.by_id.get(req_id, [])
            if not found:
                problems.append(f"覆盖表里缺「{req_id}」（spec 原文：{text}）")
            elif len(found) > 1:
                problems.append(f"覆盖表里「{req_id}」有 {len(found)} 行，应恰好 1 行")
        enumerated = {req_id for req_id, _ in self.required}
        for row_id in sorted(self.by_id):
            if row_id not in enumerated:
                problems.append(f"覆盖表里的「{row_id}」在 spec 里枚举不出来，是多出来的行")
        if problems:
            self.fail("\n".join(problems))

    def test_every_row_quotes_the_spec_text_verbatim(self) -> None:
        problems = []
        for req_id, text in self.required:
            found = self.by_id.get(req_id)
            if not found or len(found[0]) < 2:
                continue
            quoted = " ".join(found[0][1].split())
            expected = " ".join(text.split())
            if quoted != expected:
                problems.append(
                    f"「{req_id}」引用的原文与 spec 不一致：\n"
                    f"      表里：{quoted}\n"
                    f"      spec：{expected}"
                )
        if problems:
            self.fail("\n".join(problems))

    def test_coverage_cell_is_real_req_ids_or_the_gap_marker(self) -> None:
        in_spec = spec_req_ids()
        problems = []
        for req_id, _ in self.required:
            found = self.by_id.get(req_id)
            if not found:
                continue
            row = found[0]
            if len(row) < 4:
                problems.append(f"「{req_id}」那一行只有 {len(row)} 列，覆盖表应为 4 列")
                continue
            cell = row[2]
            if cell == GAP_MARKER:
                continue
            ids = REQ_ID_RE.findall(cell)
            if not ids:
                problems.append(
                    f"「{req_id}」的覆盖列既不是固定串「{GAP_MARKER}」，也没有任何 "
                    f"REQ-* ID：[{cell}]"
                )
                continue
            unknown = [req for req in ids if req not in in_spec]
            if unknown:
                problems.append(
                    f"「{req_id}」的覆盖列引用了 spec 原文里不存在的 {'、'.join(unknown)} "
                    "—— 不得发明 REQ ID"
                )
            leftover = REQ_ID_RE.sub("", cell).replace("`", "").replace("、", "")
            leftover = leftover.replace(",", "").strip()
            if leftover:
                problems.append(
                    f"「{req_id}」的覆盖列除 REQ-* ID 外还混了别的内容：[{leftover}]；"
                    f"该列只允许写 REQ-* ID 或固定串「{GAP_MARKER}」"
                )
        if problems:
            self.fail("\n".join(problems))

    def test_gap_rows_give_a_reason(self) -> None:
        problems = []
        for req_id, _ in self.required:
            found = self.by_id.get(req_id)
            if not found or len(found[0]) < 4:
                continue
            row = found[0]
            if row[2] == GAP_MARKER and row[3] in EMPTY_CELLS:
                problems.append(
                    f"「{req_id}」写了「{GAP_MARKER}」却没有在同一行给理由 —— "
                    "必须说明现有 REQ 为什么都不覆盖它"
                )
        if problems:
            self.fail("\n".join(problems))


if __name__ == "__main__":
    unittest.main()
