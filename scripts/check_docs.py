#!/usr/bin/env python3
"""Validate the docs navigation layer and cross-file conventions.

本仓库的 docs/ 是一层「导航层」：PROJECT / ARCHITECTURE / REQUIREMENTS / TODO /
HANDOFF 互相交叉链接，并大量以 §N 指回 spec 正文。这两样东西烂掉是静默的
——链接断了、spec 重新编号了，读的人不会立刻发现。所以放进 CI 挡住合并。

检查三件事：
1. Markdown 相对链接指向的文件真实存在
2. 文档里引用的 §N / §N.M 在 spec 里有对应标题
3. 跨文件重复的「约定串」逐字一致
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "Acuven_Central_AI_Billing_Platform_Spec.md"
# 历史评审记录针对的是当时的 spec 版本，里面的 §N 不再对现行 spec 校验；链接与约定串照查。
ARCHIVE = ROOT / "docs" / "archive"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
SECTION_REF_RE = re.compile(r"§(\d+(?:\.\d+)?)")
SPEC_HEADING_RE = re.compile(r"^#{1,2}\s+(\d+(?:\.\d+)?)[.\s]")
FENCE_RE = re.compile(r"^\s*```")

EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel"}

# 审查脚本产出的临时材料，不是仓库内容
TRANSIENT_PREFIX = ".codex-"

# 跨文件重复的「约定串」。同一个约定散在多个文件里，改一处忘另一处是本仓库
# 反复出现的缺陷 —— REVIEW-LOG 里已经记过三次，说明光靠自觉不管用。
#
# loose 用来匹配「疑似在说同一件事」的写法；一旦命中，就必须与 canonical
# 逐字相同，否则报错。
#
# files  —— 只在这些文件里检查。用于本身有歧义的串（例如 §1–§4 在需求索引里
#           是合法的章节区间，不是设计闸门档位）。
# 行内加 check-docs:allow 可放行有意为之的反例（例如故意写错大小写的测试用例）。
ALLOW_MARKER = "check-docs:allow"

CONSISTENCY_RULES = [
    {
        "name": "碰钱清单",
        "canonical": "钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机",
        "loose": re.compile(r"钱包\s*/\s*账本[^\n]{0,80}?状态机"),
    },
    {
        "name": "设计闸门档位",
        "canonical": "§1–§7",
        "loose": re.compile(r"§1\s*[–—-]\s*§\d(?:\s*\+\s*§\d)?"),
        "files": {
            ".github/ISSUE_TEMPLATE/design-gate.md",
            "docs/WORKFLOW.md",
            "scripts/review_checklist.md",
        },
    },
    {
        # 「不走闸门」的清单出现在 WORKFLOW、design-gate 模板、PR 模板、CLAUDE.md 四处，
        # 各自服务不同读者，删不掉，所以钉住写法。
        "name": "不走闸门清单",
        "canonical": "前端 / 文档 / CI / 脚本",
        "loose": re.compile(r"前端\s*/\s*文档\s*/\s*CI\s*/\s*脚本", re.IGNORECASE),
    },
    {
        # 署名前缀是「这条评论是独立审查」的唯一凭据（两边共用同一个 GitHub
        # 账号）。它同时出现在 lib 的常量、prompt 模板、WORKFLOW 的约定表里 ——
        # 任意一处漂移，脚本发出去的批准就会被读取方判为「不是审查」。
        "name": "设计审查署名前缀",
        "canonical": "## 🔍 CODEX REVIEW — 设计闸门",
        "loose": re.compile(r"##\s*🔍\s*CODEX REVIEW\s*[—–-]\s*设计闸门", re.IGNORECASE),
    },
    {
        "name": "实现审查署名前缀",
        "canonical": "## 🔍 CODEX REVIEW",
        # 负向前瞻把设计闸门那条排除掉，否则它会被当成实现前缀的不一致写法
        "loose": re.compile(r"##\s*🔍\s*CODEX REVIEW(?!\s*[—–-])", re.IGNORECASE),
    },
    {
        "name": "实现闸门通过判定",
        "canonical": "VERDICT: APPROVE",
        "loose": re.compile(r"VERDICT:\s*APPROVE(?!_)", re.IGNORECASE),
    },
    {
        "name": "实现闸门拒绝判定",
        "canonical": "VERDICT: REQUEST_CHANGES",
        "loose": re.compile(r"VERDICT:\s*REQUEST_CHANGES", re.IGNORECASE),
    },
]

CONSISTENCY_SUFFIXES = {".md", ".ps1", ".py"}


def is_transient(path: Path) -> bool:
    return path.name.startswith(TRANSIENT_PREFIX)


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def strip_code_fences(lines: list[str]) -> list[tuple[int, str]]:
    """Return (line_number, text) pairs outside fenced code blocks."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append((line_no, line))
    return out


def spec_section_numbers() -> set[str]:
    numbers = set()
    for line in read_lines(SPEC):
        match = SPEC_HEADING_RE.match(line)
        if match:
            numbers.add(match.group(1))
    return numbers


def check_links(md_path: Path) -> list[str]:
    errors = []
    rel_name = md_path.relative_to(ROOT).as_posix()
    for line_no, line in strip_code_fences(read_lines(md_path)):
        for target in LINK_RE.findall(line):
            if urlparse(target).scheme in EXTERNAL_SCHEMES or target.startswith("#"):
                continue
            path_part = unquote(target.split("#", 1)[0])
            if not path_part:
                continue
            if not (md_path.parent / path_part).exists():
                errors.append(f"{rel_name}:{line_no}  dead link -> {target}")
    return errors


def check_section_refs(md_path: Path, known: set[str]) -> list[str]:
    errors = []
    rel_name = md_path.relative_to(ROOT).as_posix()
    for line_no, line in strip_code_fences(read_lines(md_path)):
        for ref in SECTION_REF_RE.findall(line):
            # §7.10 这类指的是 §7 里的编号列表项，不是独立标题；
            # 精确匹配不上就退回检查顶层节号是否存在。
            if ref in known or ref.split(".", 1)[0] in known:
                continue
            errors.append(f"{rel_name}:{line_no}  unknown spec section -> §{ref}")
    return errors


def check_conventions(path: Path) -> list[str]:
    """约定串一旦出现，必须与 canonical 逐字相同。"""
    errors = []
    rel_name = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for rule in CONSISTENCY_RULES:
        allowed_files = rule.get("files")
        if allowed_files is not None and rel_name not in allowed_files:
            continue
        for match in rule["loose"].finditer(text):
            found = match.group(0)
            if found == rule["canonical"]:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            if ALLOW_MARKER in lines[line_no - 1]:
                continue
            errors.append(
                f"{rel_name}:{line_no}  约定串「{rule['name']}」写法不一致："
                f"实得 [{found}]，应为 [{rule['canonical']}]"
            )
    return errors


def main() -> int:
    # Windows 控制台默认不是 UTF-8，§ 会打成乱码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not SPEC.exists():
        print(f"spec not found: {SPEC.relative_to(ROOT).as_posix()}", file=sys.stderr)
        return 1

    known = spec_section_numbers()
    errors: list[str] = []

    for md_path in sorted(ROOT.rglob("*.md")):
        if ".git" in md_path.parts or is_transient(md_path):
            continue
        errors.extend(check_links(md_path))
        if md_path != SPEC and ARCHIVE not in md_path.parents:
            errors.extend(check_section_refs(md_path, known))

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or is_transient(path):
            continue
        if path.suffix not in CONSISTENCY_SUFFIXES or path == SPEC:
            continue
        errors.extend(check_conventions(path))

    if errors:
        print(f"docs check failed ({len(errors)} problem(s)):\n")
        for error in errors:
            print(f"  {error}")
        return 1

    print(
        f"docs check passed ({len(known)} spec sections indexed, "
        f"{len(CONSISTENCY_RULES)} convention rules)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
