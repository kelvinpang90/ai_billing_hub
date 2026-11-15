"""Decide whether a CI run needs the code checks (backend tests, backend build, frontend).

CI 的 `changes` job 调用它：事件名从参数传入，改动的文件从标准输入逐行读入，向标准输出写一行
`code=true` 或 `code=false`，追加进 `$GITHUB_OUTPUT`。

只有同时满足下面三条才输出 `false`（跳过代码检查）：
  1. 事件是 `pull_request`。推到 main 一律跑全量，跳错了合并后 main 上的 CI 会立刻变红；
  2. 改动列表非空；
  3. 每个文件都在 SKIPPABLE_PREFIXES / SKIPPABLE_FILES 里。

清单故意窄：只放登记 PR 与收尾 PR 会改的文件，而且后端测试、前端构建都不读它们。
⚠️ 有测试开始读这里的某个文件时，必须把它从清单里拿掉（目前后端测试读的文档只有
docs/runbook.md 与 docs/adr/ADR-0008-sst-tax-treatment.md，都不在清单里）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

SKIPPABLE_PREFIXES = (".platform/", "docs/design/")
SKIPPABLE_FILES = frozenset({"docs/TODO.md", "docs/adr/README.md"})


def is_skippable(path: str) -> bool:
    return path in SKIPPABLE_FILES or path.startswith(SKIPPABLE_PREFIXES)


def code_checks_needed(event: str, paths: Iterable[str]) -> bool:
    if event != "pull_request":
        return True
    changed = [path.strip() for path in paths if path.strip()]
    if not changed:
        return True
    return not all(is_skippable(path) for path in changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="github.event_name")
    args = parser.parse_args(argv)
    needed = code_checks_needed(args.event, sys.stdin.read().splitlines())
    print(f"code={'true' if needed else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
