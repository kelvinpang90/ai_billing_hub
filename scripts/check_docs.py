#!/usr/bin/env python3
"""Validate the docs navigation layer.

本仓库的 docs/ 是一层「导航层」：PROJECT / ARCHITECTURE / REQUIREMENTS / TODO /
HANDOFF 互相交叉链接，并大量以 §N 指回 spec 正文。这两样东西烂掉是静默的
——链接断了、spec 重新编号了，读的人不会立刻发现。所以放进 CI 挡住合并。

检查两件事：
1. Markdown 相对链接指向的文件真实存在
2. 文档里引用的 §N / §N.M 在 spec 里有对应标题
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "Acuven_Central_AI_Billing_Platform_Spec_v1.2.md"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
SECTION_REF_RE = re.compile(r"§(\d+(?:\.\d+)?)")
SPEC_HEADING_RE = re.compile(r"^#{1,2}\s+(\d+(?:\.\d+)?)[.\s]")
FENCE_RE = re.compile(r"^\s*```")

EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel"}


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
        if ".git" in md_path.parts:
            continue
        errors.extend(check_links(md_path))
        if md_path != SPEC:
            errors.extend(check_section_refs(md_path, known))

    if errors:
        print(f"docs check failed ({len(errors)} problem(s)):\n")
        for error in errors:
            print(f"  {error}")
        return 1

    print(f"docs check passed ({len(known)} spec sections indexed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
