#!/usr/bin/env python3
"""Offline repository and pull-request admission checks (exit codes 0/1/2)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHA_RE = re.compile(r"[0-9a-f]{40}")
SECTIONS = (
    "任务", "改了什么", "触碰的不变量 / REQ", "如何验证", "自检",
    "已知未做 / 留给后续", "TODO 影响",
)


class PolicyError(Exception):
    """The check could not run reliably, rather than finding a policy violation."""


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        encoding="utf-8", errors="strict", check=False,
    )
    if result.returncode:
        raise PolicyError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def visible_lines(body: str) -> list[tuple[int, str]]:
    """Ignore HTML comments and fenced examples without changing source line numbers."""
    body = re.sub(r"<!--.*?(?:-->|\Z)", lambda m: "\n" * m[0].count("\n"), body, flags=re.S)
    result = []
    fence = None
    for number, line in enumerate(body.splitlines(), 1):
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
            continue
        if match:
            fence = match[1]
            continue
        result.append((number, line))
    return result


def check_checkboxes(body: str, label: str) -> list[str]:
    errors = []
    for number, line in visible_lines(body):
        match = re.match(r"^\s*(?:[-+*]|\d+[.)])\s+\[([^]\r\n]*)\](.*)$", line)
        if not match or not match[2] or not match[2][0].isspace():
            continue
        # Markdown reference/inline links are not task items.
        if match[2].lstrip().startswith(("(", "[")):
            continue
        if match[1] not in {" ", "x"}:
            errors.append(f"{label}:{number}: unsupported task checkbox [{match[1]}]")
    return errors


def check_local(root: Path = ROOT) -> list[str]:
    errors = []
    paths = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    for relative in sorted(set(paths)):
        path = root / relative
        if relative and path.suffix.lower() == ".md" and path.is_file():
            errors.extend(check_checkboxes(path.read_text(encoding="utf-8-sig"), relative))
    return errors


def meaningful(value: str) -> bool:
    value = value.strip().strip("`*_ ")
    if not value:
        return False
    if re.fullmatch(r"(?i)(?:tbd|todo|n/?a|pending|\.\.\.|…+|待填(?:写)?|待补(?:充)?|请填写|占位|<[^>]*>)", value):
        return False
    return True


def sections(body: str) -> tuple[dict[str, str], list[str]]:
    found: dict[str, list[str]] = {}
    errors = []
    current = None
    for _, line in visible_lines(body):
        heading = re.fullmatch(r"## (.+?)\s*", line)
        if heading:
            current = heading[1]
            if current in found:
                errors.append(f"duplicate PR section: {current}")
            found.setdefault(current, [])
        elif current:
            found[current].append(line)
    return {key: "\n".join(value).strip() for key, value in found.items()}, errors


def git_diff_paths(root: Path, base: str, head: str) -> set[str]:
    validate_commit(root, base)
    validate_commit(root, head)
    return set(filter(None, git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}").split("\0")))


def validate_commit(root: Path, commit: str) -> None:
    if not SHA_RE.fullmatch(commit):
        raise PolicyError("commit must be a full lowercase 40-character SHA")
    git(root, "cat-file", "-e", f"{commit}^{{commit}}")


def location_exists(root: Path, commit: str, path: str, line: int) -> bool:
    if path.startswith(("/", "\\")) or ":" in path or "\\" in path or ".." in path.split("/"):
        return False
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        capture_output=True, encoding="utf-8", errors="strict", check=False,
    )
    return result.returncode == 0 and 0 < line <= len(result.stdout.splitlines())


def check_pr(body: str, changed_paths: set[str], *, root: Path = ROOT, head: str = "HEAD") -> list[str]:
    content, errors = sections(body)
    for section in SECTIONS:
        value = content.get(section, "")
        if section == "任务":
            value = re.sub(r"(?m)^设计闸门：.*$", "", value).strip()
        if not meaningful(value):
            errors.append(f"PR section missing or placeholder: {section}")
    plain = "\n".join(line for _, line in visible_lines(body))
    gates = re.findall(r"(?m)^设计闸门：([^\r\n]*)$", plain)
    if len(gates) != 1 or not re.fullmatch(r"(?:#[1-9]\d*|不适用)", gates[0].strip()):
        errors.append("PR must contain exactly one 设计闸门：#<number> or 设计闸门：不适用")
    todo = content.get("TODO 影响", "")
    impacts = re.findall(r"(?m)^TODO impact: (.*)$", todo)
    if len(impacts) != 1 or impacts[0] not in {"updated", "none"}:
        errors.append("TODO impact must appear exactly once as updated or none")
    elif impacts[0] == "updated":
        targets = re.findall(r"(?m)^TODO target: docs/TODO\.md:([1-9]\d*)$", todo)
        if "docs/TODO.md" not in changed_paths:
            errors.append("TODO impact updated requires docs/TODO.md in the PR diff")
        if len(targets) != 1:
            errors.append("TODO impact updated requires one TODO target: docs/TODO.md:<line>")
        elif not location_exists(root, head, "docs/TODO.md", int(targets[0])):
            errors.append("TODO target does not exist at the PR head")
    else:
        reasons = re.findall(r"(?m)^TODO reason: (.*)$", todo)
        if len(reasons) != 1 or not meaningful(reasons[0]) or reasons[0].strip() in {"无", "不适用", "none"}:
            errors.append("TODO impact none requires a concrete TODO reason")
    errors.extend(check_checkboxes(body, "PR body"))
    return errors


def is_ancestor(root: Path, commit: str, head: str) -> bool:
    result = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", commit, head], capture_output=True)
    if result.returncode not in {0, 1}:
        raise PolicyError("git could not verify evidence ancestry")
    return result.returncode == 0


def check_response(body: str, *, root: Path = ROOT, head: str = "HEAD") -> list[str]:
    errors = []
    lines = [line for _, line in visible_lines(body)]
    if not body.splitlines() or body.splitlines()[0] != "## 🔧 CLAUDE RESPONSE":
        errors.append("response must start with ## 🔧 CLAUDE RESPONSE")
    reviewed = re.findall(r"(?m)^reviewed-head: ([0-9a-f]{40})$", "\n".join(lines))
    if len(reviewed) != 1:
        errors.append("response requires one full reviewed-head SHA")
    else:
        validate_commit(root, reviewed[0])
        if not is_ancestor(root, reviewed[0], head):
            errors.append("reviewed-head is not an ancestor of current head")
    table = []
    in_table = False
    for line in lines:
        if line.strip() == "| 意见 | 处理 | 证据 | 验证 |":
            if in_table or table:
                errors.append("response must contain only one evidence table")
            in_table = True
            continue
        if in_table:
            if not line.strip().startswith("|"):
                if line.strip():
                    in_table = False
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            table.append(cells)
    if not table:
        errors.append("response requires a nonempty | 意见 | 处理 | 证据 | 验证 | table")
    for cells in table:
        if len(cells) != 4 or not all(meaningful(cell) for cell in cells):
            errors.append("each response row requires four non-placeholder cells")
            continue
        opinion, handling, evidence, _ = cells
        if handling == "已修":
            match = re.fullmatch(r"([0-9a-f]{40}) · (.+):([1-9]\d*)", evidence)
            if not match:
                errors.append(f"{opinion}: 已修 requires full SHA · file:line")
                continue
            commit, path, line = match.groups()
            validate_commit(root, commit)
            if not is_ancestor(root, commit, head):
                errors.append(f"{opinion}: evidence commit is not an ancestor of head")
            if not location_exists(root, commit, path, int(line)):
                errors.append(f"{opinion}: evidence file/line does not exist at the cited commit")
        elif handling not in {"不改", "升级"}:
            errors.append(f"{opinion}: unsupported response handling: {handling}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--body-file", type=Path)
    source.add_argument("--event-file", type=Path)
    source.add_argument("--response-file", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args(argv)
    if args.body_file and not (args.base and args.head):
        parser.error("--body-file requires --base and --head")
    if args.response_file and not args.head:
        parser.error("--response-file requires --head")
    if args.event_file and (args.base or args.head):
        parser.error("--event-file supplies its own base and head")
    if not (args.body_file or args.response_file or args.event_file) and (args.base or args.head):
        parser.error("--base/--head requires a body or response")
    try:
        errors = check_local()
        if args.event_file:
            event = json.loads(args.event_file.read_text(encoding="utf-8-sig"))
            if not isinstance(event, dict):
                raise PolicyError("event payload must be a JSON object")
            if "pull_request" in event:
                pr = event["pull_request"]
                body, base, head = pr["body"] or "", pr["base"]["sha"], pr["head"]["sha"]
                if not isinstance(body, str):
                    raise PolicyError("PR body must be text")
                errors.extend(check_pr(body, git_diff_paths(ROOT, base, head), head=head))
            elif "ref" not in event or "after" not in event:
                raise PolicyError("event is neither a pull_request nor a push payload")
        elif args.body_file:
            errors.extend(check_pr(args.body_file.read_text(encoding="utf-8-sig"), git_diff_paths(ROOT, args.base, args.head), head=args.head))
        elif args.response_file:
            validate_commit(ROOT, args.head)
            errors.extend(check_response(args.response_file.read_text(encoding="utf-8-sig"), head=args.head))
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("Repository policy checks passed.")
        return 0
    except (PolicyError, OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        print(f"CHECK ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
