"""Regression tests; all GitHub calls are mocked."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gh_verified_write.py"
SPEC = importlib.util.spec_from_file_location("gh_verified_write", SCRIPT)
writer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(writer)
REPO = "owner/repo"
TARGET = "https://github.com/owner/repo/pull/7"
BODY = "中文 `literal` $(secret)\n  preserve spaces  \n"


def pr(body=BODY):
    return {"number": 7, "url": "https://api.github.com/repos/owner/repo/pulls/7", "html_url": TARGET, "body": body}


def comment(body=BODY):
    return {
        "id": 99, "url": "https://api.github.com/repos/owner/repo/issues/comments/99",
        "html_url": TARGET + "#issuecomment-99",
        "issue_url": "https://api.github.com/repos/owner/repo/issues/7", "body": body,
    }


class VerifiedWriteTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        self.redirect = redirect_stderr(self.stderr)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)

    @patch.object(writer, "api")
    def test_pr_patch_and_exact_get(self, api):
        api.side_effect = [pr(), pr(BODY.replace("\n", "\r\n"))]
        self.assertEqual(writer.verified_write(REPO, 7, "pr-body", BODY), TARGET)
        self.assertEqual(api.call_args_list, [
            unittest.mock.call("PATCH", "repos/owner/repo/pulls/7", BODY),
            unittest.mock.call("GET", "repos/owner/repo/pulls/7"),
        ])

    @patch.object(writer, "api")
    def test_comment_reads_created_id(self, api):
        api.side_effect = [pr(), comment(), comment()]
        self.assertEqual(writer.verified_write(REPO, 7, "pr-comment", BODY), TARGET + "#issuecomment-99")
        self.assertEqual(api.call_args.args, ("GET", "repos/owner/repo/issues/comments/99"))
        self.assertIn("Created comment id: 99", self.stderr.getvalue())

    @patch.object(writer, "api")
    def test_issue_comment(self, api):
        identity = pr()
        identity.update(url="https://api.github.com/repos/owner/repo/issues/7", html_url=TARGET.replace("pull/", "issues/"))
        response = comment()
        response["html_url"] = response["html_url"].replace("pull/", "issues/")
        api.side_effect = [identity, response, response]
        self.assertIn("/issues/7#issuecomment-99", writer.verified_write(REPO, 7, "issue-comment", BODY))

    @patch.object(writer, "api")
    def test_readback_mismatch_preserves_spaces(self, api):
        api.side_effect = [pr(), pr(BODY.replace("  preserve spaces  ", "preserve spaces"))]
        with self.assertRaises(writer.WriteError):
            writer.verified_write(REPO, 7, "pr-body", BODY)
        self.assertEqual(api.call_count, 2)

    @patch.object(writer, "api")
    def test_wrong_target_missing_fields_and_invalid_id(self, api):
        for field, value in (("number", True), ("html_url", TARGET + "1"), ("body", None), ("url", "https://api.github.com/repos/other/repo/pulls/7")):
            with self.subTest(field=field):
                response = pr()
                response[field] = value
                api.reset_mock()
                api.side_effect = [response]
                with self.assertRaises(writer.WriteError):
                    writer.verified_write(REPO, 7, "pr-body", BODY)
                self.assertEqual(api.call_count, 1)
        for value in (None, True, -1, "99"):
            response = comment()
            response["id"] = value
            api.side_effect = [pr(), response]
            with self.assertRaises(writer.WriteError):
                writer.verified_write(REPO, 7, "pr-comment", BODY)

    @patch.object(writer.subprocess, "run")
    def test_json_stdin_without_shell(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(pr()), "")
        writer.api("PATCH", "repos/owner/repo/pulls/7", BODY)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn(BODY, args[0])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(kwargs["input"]), {"body": BODY})
        self.assertEqual(args[0][-2:], ["--input", "-"])

    @patch.object(writer.subprocess, "run")
    def test_bad_json_and_command_failure_no_retry(self, run):
        for code, output in ((0, "not json"), (0, "[]"), (0, "null"), (1, "")):
            with self.subTest(code=code, output=output):
                run.reset_mock()
                run.return_value = subprocess.CompletedProcess([], code, output, "private credential")
                with self.assertRaises(writer.WriteError) as error:
                    writer.api("POST", "repos/owner/repo/issues/7/comments", BODY)
                self.assertNotIn("private credential", str(error.exception))
                self.assertEqual(run.call_count, 1)

    @patch.object(writer, "api")
    def test_file_with_spaces_unicode_and_relative_path(self, api):
        with tempfile.TemporaryDirectory(prefix="verified write ") as directory:
            path = Path(directory) / "中文 body.md"
            path.write_text(BODY, encoding="utf-8-sig")
            api.side_effect = [pr(), pr()]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(writer.main(["--repo", REPO, "--number", "7", "--kind", "pr-body", "--body-file", str(path)]), 0)

    def test_cli_errors_exit_two(self):
        for extra in ([], ["--unknown"], ["--repo", REPO, "--number", "bad", "--kind", "pr-body", "--body-file", "missing"]):
            result = subprocess.run([sys.executable, str(SCRIPT), *extra], capture_output=True)
            self.assertEqual(result.returncode, 2)

    @patch.object(writer, "api")
    def test_invalid_inputs_fail_before_mutation(self, api):
        for repo, number, kind, body in (("../repo", 7, "pr-body", BODY), (REPO, 0, "pr-body", BODY), (REPO, True, "pr-body", BODY), (REPO, 7, "pr-body", " \n"), (REPO, 7, "other", BODY)):
            with self.assertRaises(writer.WriteError):
                writer.verified_write(repo, number, kind, body)
        api.assert_not_called()


import sys

if __name__ == "__main__":
    unittest.main()
