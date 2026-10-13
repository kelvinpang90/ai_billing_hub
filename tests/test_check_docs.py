"""scripts/check_docs.py 里 AGENTS.md / CLAUDE.md 一致性检查的回归。"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_docs.py"
SPEC = importlib.util.spec_from_file_location("check_docs", SCRIPT)
check_docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_docs)


class AgentRulesSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, claude: bytes | None, agents: bytes | None) -> list[str]:
        if claude is not None:
            (self.root / "CLAUDE.md").write_bytes(claude)
        if agents is not None:
            (self.root / "AGENTS.md").write_bytes(agents)
        return check_docs.check_agent_rules_sync(self.root)

    def test_identical_files_pass(self) -> None:
        self.assertEqual(self.write(b"# a\nb\n", b"# a\nb\n"), [])

    def test_repository_files_are_in_sync(self) -> None:
        self.assertEqual(check_docs.check_agent_rules_sync(ROOT), [])

    def test_content_difference_names_first_differing_line(self) -> None:
        errors = self.write(b"# a\nb\nc\n", b"# a\nb\nX\n")
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("AGENTS.md:3 "), errors[0])

    def test_extra_trailing_line_is_reported_after_shorter_file(self) -> None:
        errors = self.write(b"# a\nb\n", b"# a\nb\nextra\n")
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("AGENTS.md:3 "), errors[0])

    def test_crlf_and_lf_copies_are_equal(self) -> None:
        # core.autocrlf=true 下一份检出成 CRLF、一份写回成 LF 是常态，提交后内容相同。
        self.assertEqual(self.write(b"# a\nb\n", b"# a\r\nb\r\n"), [])

    def test_trailing_newline_difference_is_reported(self) -> None:
        errors = self.write(b"# a\nb\n", b"# a\nb")
        self.assertEqual(len(errors), 1)
        self.assertIn("文件末尾换行", errors[0])

    def test_missing_file_is_reported(self) -> None:
        errors = self.write(b"# a\n", None)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("AGENTS.md "), errors[0])


if __name__ == "__main__":
    unittest.main()
