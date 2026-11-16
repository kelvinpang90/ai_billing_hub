"""scripts/ci_scope.py（文档 PR 跳过代码检查的判定）的回归。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci_scope.py"
SPEC = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
ci_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_scope)


class CodeChecksNeededTest(unittest.TestCase):
    def test_registration_and_closeout_prs_skip(self) -> None:
        for paths in (
            [".platform/tasks.yaml", "docs/TODO.md"],
            [".platform/tasks.yaml", "docs/TODO.md", "docs/design/AIH-TASK-209-x.md"],
            ["docs/adr/README.md", "docs/design/a/b.md"],
        ):
            with self.subTest(paths=paths):
                self.assertFalse(ci_scope.code_checks_needed("pull_request", paths))

    def test_any_other_file_runs_the_checks(self) -> None:
        for other in (
            "app/main.py",
            "frontend/src/main.tsx",
            "tests/backend/test_compose.py",
            ".github/workflows/ci.yml",
            "scripts/ci_scope.py",
            "pyproject.toml",
            ".env.example",
            # 后端测试读的两个文档
            "docs/runbook.md",
            "docs/adr/ADR-0008-sst-tax-treatment.md",
            "docs/api.md",
            "docs/WORKFLOW.md",
        ):
            with self.subTest(other=other):
                paths = [".platform/tasks.yaml", "docs/TODO.md", other]
                self.assertTrue(ci_scope.code_checks_needed("pull_request", paths))

    def test_lookalike_paths_are_not_skippable(self) -> None:
        for path in (
            "docs/TODO.md.bak",
            "docs/designs/x.md",
            "docs/adr/README.md/x",
            "platform/tasks.yaml",
            "x/.platform/tasks.yaml",
            "docs/todo.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.code_checks_needed("pull_request", [path]))

    def test_files_ruff_checks_run_the_checks_even_in_skippable_dirs(self) -> None:
        # backend-build 的 ruff 检查整棵树（审查 #731）
        for path in (
            "docs/design/x.py",
            "docs/design/x.pyi",
            "docs/design/x.ipynb",
            "docs/design/pyproject.toml",
            ".platform/tool.py",
            ".platform/pyproject.toml",
        ):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.code_checks_needed("pull_request", [path]))
        # 不被 ruff 认的扩展名照旧可跳过，例如设计文档里附的代码摘录
        self.assertFalse(
            ci_scope.code_checks_needed("pull_request", ["docs/design/ref/client.py.txt"])
        )

    def test_non_pull_request_events_always_run(self) -> None:
        for event in ("push", "workflow_dispatch", "", "pull_request_target"):
            with self.subTest(event=event):
                self.assertTrue(ci_scope.code_checks_needed(event, [".platform/tasks.yaml"]))

    def test_an_empty_change_list_runs_the_checks(self) -> None:
        self.assertTrue(ci_scope.code_checks_needed("pull_request", []))
        self.assertTrue(ci_scope.code_checks_needed("pull_request", ["", "  "]))


class MainTest(unittest.TestCase):
    def run_main(self, event: str, stdin: str) -> str:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
            self.assertEqual(ci_scope.main(["--event", event]), 0)
        return out.getvalue()

    def test_prints_one_github_output_line(self) -> None:
        self.assertEqual(
            self.run_main("pull_request", ".platform/tasks.yaml\ndocs/TODO.md\n"), "code=false\n"
        )
        self.assertEqual(self.run_main("pull_request", "app/main.py\n"), "code=true\n")
        self.assertEqual(self.run_main("push", ""), "code=true\n")

    def test_tolerates_windows_line_endings(self) -> None:
        self.assertEqual(
            self.run_main("pull_request", ".platform/tasks.yaml\r\ndocs/TODO.md\r\n"),
            "code=false\n",
        )


if __name__ == "__main__":
    unittest.main()
