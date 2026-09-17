"""Regression tests for scripts/check_repo_policy.py.

每个用例对应一次真实发生过的缺陷，不是凑覆盖率。来源见 docs/REVIEW-LOG.md
的「反复出现的问题」。误判用例同样重要：这个检查会跑在每个 PR 上，
一旦把代码示例或普通 Markdown 链接当成任务项，它会立刻被加进忽略名单，
然后就再也拦不住真正的问题了。
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_repo_policy.py"
SPEC = importlib.util.spec_from_file_location("check_repo_policy", SCRIPT)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)

# Worker 里刻意没有真实 Git，只有控制面给的只读 manifest。要建临时仓库或读提交图的用例
# 只在设置了该变量时跳过，原因固定；CI 不设变量，全量运行。Worker 里的 skipped 不是 passed。
requires_git = unittest.skipIf(
    policy.MANIFEST_ENV in os.environ,
    "requires real Git; Worker mode (ACUVEN_GIT_LS_FILES_MANIFEST set) provides only a read-only manifest",
)

GIT_IDENTITY = (
    "-c", "user.email=test@example.com",
    "-c", "user.name=test",
    "-c", "commit.gpgsign=false",
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *GIT_IDENTITY, *args],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout.strip()


class TempRepo:
    """A throwaway repo with two commits, so ancestry and file:line can be checked for real."""

    def __enter__(self) -> "TempRepo":
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "TODO.md").write_text("\n".join(f"line {n}" for n in range(1, 21)), encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        (self.root / "docs" / "TODO.md").write_text("\n".join(f"line {n}" for n in range(1, 31)), encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "change")
        self.head = git(self.root, "rev-parse", "HEAD")
        return self

    def orphan(self) -> str:
        """A real commit that is NOT an ancestor of head — a stale SHA pasted from an old round."""
        git(self.root, "checkout", "-q", "--orphan", "side")
        (self.root / "other.md").write_text("x", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "side")
        sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "main")
        return sha

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


# --------------------------------------------------------------------------
# 复选框：`[~]` 是我自己发明的状态，扫 `[ ]` 找待办的人会整个跳过那一条
# --------------------------------------------------------------------------

class CheckboxTests(unittest.TestCase):
    def errors(self, body: str) -> list[str]:
        return policy.check_checkboxes(body, "f.md")

    def test_supported_states_pass(self):
        self.assertEqual(self.errors("- [ ] todo\n- [x] done\n"), [])

    def test_invented_state_is_rejected(self):
        self.assertEqual(len(self.errors("- [~] 部分完成\n")), 1)

    def test_uppercase_x_is_rejected(self):
        # GitHub 会渲染成勾选，但本仓库的约定只写了小写；两种写法并存迟早分叉
        self.assertEqual(len(self.errors("- [X] done\n")), 1)

    def test_numbered_and_other_bullets_are_checked_too(self):
        self.assertEqual(len(self.errors("1. [?] a\n")), 1)
        self.assertEqual(len(self.errors("* [-] a\n")), 1)

    def test_inline_link_is_not_a_task(self):
        self.assertEqual(self.errors("- [ADR-0001](adr/ADR-0001.md) 仓库可见性\n"), [])

    def test_reference_link_is_not_a_task(self):
        self.assertEqual(self.errors("- [spec][1] 见规格\n"), [])

    def test_invented_state_followed_by_a_link_is_still_rejected(self):
        # 曾经按「后面跟着链接就不是任务项」豁免，于是 `[~]` 后面加个链接就能绕过，
        # 扫 `[ ]` 找待办的人照样看不见这一条
        self.assertEqual(len(self.errors("- [~] [任务](链接)\n")), 1)
        self.assertEqual(len(self.errors("- [~] (部分完成)\n")), 1)
        self.assertEqual(len(self.errors("- [X] [文档](a.md)\n")), 1)

    def test_link_label_longer_than_one_character_is_never_a_checkbox(self):
        # 豁免的判据改成「方括号里不止一个字符」——链接标签是它，复选框状态不是
        self.assertEqual(self.errors("- [spec] [1] 见规格\n"), [])
        self.assertEqual(self.errors("- [ ] [ADR-0001](adr/ADR-0001.md) 已完成\n"), [])

    def test_fenced_example_is_not_scanned(self):
        # WORKFLOW / REVIEW-LOG 里会举反例，正文里的示例不能触发检查
        self.assertEqual(self.errors("```text\n- [~] 反例\n```\n"), [])

    def test_html_comment_is_not_scanned(self):
        # PR 模板整段说明都在 <!-- --> 里
        self.assertEqual(self.errors("<!--\n- [~] 说明\n-->\n"), [])

    def test_line_number_survives_stripping(self):
        # 剥掉注释与代码块之后行号必须还是原文行号，否则报错定位没用
        body = "<!--\nc\n-->\n```\nx\n```\n- [~] bad\n"
        self.assertEqual(self.errors(body), ["f.md:7: unsupported task checkbox [~]"])


# --------------------------------------------------------------------------
# PR 正文：TODO 影响声明 —— 「改了 ADR 没改 TODO」在本仓库出现过 6 次
# --------------------------------------------------------------------------

BODY = """## 任务

设计闸门：不适用

收口 D9。

## 改了什么

- 改了一个文件

## 触碰的不变量 / REQ

无

## 如何验证

python scripts/check_docs.py 通过

## 自检

- [x] 本地检查通过

## 已知未做 / 留给后续

无

## TODO 影响

TODO impact: updated
TODO target: docs/TODO.md:12
"""


@requires_git
class PrBodyTests(unittest.TestCase):
    def check(self, body: str, changed=("docs/TODO.md",), repo=None) -> list[str]:
        return policy.check_pr(body, set(changed), root=repo.root, head=repo.head)

    def test_complete_body_passes(self):
        with TempRepo() as repo:
            self.assertEqual(self.check(BODY, repo=repo), [])

    def test_claimed_todo_update_without_touching_todo(self):
        # 正是「声称做了实际没做」那一类，只不过这次机器能抓
        with TempRepo() as repo:
            errors = self.check(BODY, changed=("docs/other.md",), repo=repo)
            self.assertTrue(any("docs/TODO.md in the PR diff" in e for e in errors))

    def test_todo_target_line_must_exist_at_head(self):
        with TempRepo() as repo:
            body = BODY.replace("docs/TODO.md:12", "docs/TODO.md:9999")
            errors = self.check(body, repo=repo)
            self.assertTrue(any("TODO target does not exist" in e for e in errors), errors)

    def test_todo_impact_none_needs_a_real_reason(self):
        with TempRepo() as repo:
            body = BODY.replace("TODO impact: updated\nTODO target: docs/TODO.md:12", "TODO impact: none")
            self.assertTrue(any("concrete TODO reason" in e for e in self.check(body, changed=(), repo=repo)))
            body_ok = body.replace("TODO impact: none", "TODO impact: none\nTODO reason: 纯 CI 配置，无任务状态变化")
            self.assertEqual(self.check(body_ok, changed=(), repo=repo), [])

    def test_todo_impact_none_rejects_empty_words(self):
        with TempRepo() as repo:
            body = BODY.replace("TODO impact: updated\nTODO target: docs/TODO.md:12",
                                "TODO impact: none\nTODO reason: 无")
            self.assertTrue(any("concrete TODO reason" in e for e in self.check(body, changed=(), repo=repo)))

    def test_missing_todo_section(self):
        with TempRepo() as repo:
            body = BODY.split("## TODO 影响")[0]
            errors = self.check(body, repo=repo)
            self.assertTrue(any("TODO 影响" in e for e in errors))

    def test_placeholder_section_is_not_acceptable(self):
        # 「待填」「TBD」「<...>」留在模板里等于没写
        with TempRepo() as repo:
            for filler in ("TBD", "待填写", "<一句话>", "…"):
                body = BODY.replace("- 改了一个文件", filler)
                self.assertTrue(any("改了什么" in e for e in self.check(body, repo=repo)), filler)

    def test_design_gate_line_must_be_exactly_one_and_well_formed(self):
        with TempRepo() as repo:
            self.assertTrue(any("设计闸门" in e for e in self.check(BODY.replace("设计闸门：不适用", ""), repo=repo)))
            twice = BODY.replace("设计闸门：不适用", "设计闸门：不适用\n设计闸门：#3")
            self.assertTrue(any("设计闸门" in e for e in self.check(twice, repo=repo)))
            bad = BODY.replace("设计闸门：不适用", "设计闸门：待定")
            self.assertTrue(any("设计闸门" in e for e in self.check(bad, repo=repo)))
            good = BODY.replace("设计闸门：不适用", "设计闸门：#12")
            self.assertEqual(self.check(good, repo=repo), [])

    def test_invented_checkbox_in_pr_body_is_caught(self):
        with TempRepo() as repo:
            body = BODY.replace("- [x] 本地检查通过", "- [~] 本地检查通过")
            self.assertTrue(any("PR body" in e for e in self.check(body, repo=repo)))


# --------------------------------------------------------------------------
# 回应证据：「已修」必须给 SHA · 文件:行 —— 「声称做了实际没做」出现过 3 次
# --------------------------------------------------------------------------

def response(rows: str, reviewed: str) -> str:
    return (
        "## 🔧 CLAUDE RESPONSE\n\n"
        f"reviewed-head: {reviewed}\n\n"
        "| 意见 | 处理 | 证据 | 验证 |\n"
        "| --- | --- | --- | --- |\n"
        f"{rows}\n"
    )


@requires_git
class ResponseTests(unittest.TestCase):
    def check(self, body: str, repo) -> list[str]:
        return policy.check_response(body, root=repo.root, head=repo.head)

    def test_valid_evidence_passes(self):
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | 本地检查通过 |"
            self.assertEqual(self.check(response(row, repo.head), repo), [])

    def test_prose_instead_of_evidence_is_rejected(self):
        # 「已写进收口条件」这类断言正是三次假引用的写法
        with TempRepo() as repo:
            row = "| 阻断 1 | 已修 | 已写进 ADR 的收口条件 | 看过了 |"
            self.assertTrue(any("full SHA · file:line" in e for e in self.check(response(row, repo.head), repo)))

    def test_stale_sha_from_another_branch_is_rejected(self):
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | {repo.orphan()} · docs/TODO.md:12 | ok |"
            self.assertTrue(any("not an ancestor" in e for e in self.check(response(row, repo.head), repo)))

    def test_line_beyond_end_of_file_is_rejected(self):
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:9999 | ok |"
            self.assertTrue(any("does not exist" in e for e in self.check(response(row, repo.head), repo)))

    def test_line_valid_now_but_not_at_the_cited_commit(self):
        # 引的是旧提交，那时文件还没那么长 —— 证据必须在它声称的那个提交上成立
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | {repo.base} · docs/TODO.md:25 | ok |"
            self.assertTrue(any("does not exist" in e for e in self.check(response(row, repo.head), repo)))

    def test_path_traversal_is_rejected(self):
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | {repo.head} · ../outside.md:1 | ok |"
            self.assertTrue(any("does not exist" in e for e in self.check(response(row, repo.head), repo)))

    def test_not_fixed_and_escalated_need_no_sha(self):
        with TempRepo() as repo:
            rows = "| 建议 1 | 不改 | spec §41 已规定 | 引用核对过 |\n| 建议 2 | 升级 | 需 Kelvin 拍板 | 已在 PR 说明 |"
            self.assertEqual(self.check(response(rows, repo.head), repo), [])

    def test_unknown_handling_word_is_rejected(self):
        # 只有三种处理：改 / 不改 / 升级。「稍后处理」等于沉默跳过
        with TempRepo() as repo:
            row = "| 建议 1 | 稍后处理 | 记下了 | 无 |"
            self.assertTrue(any("unsupported response handling" in e for e in self.check(response(row, repo.head), repo)))

    def test_missing_reviewed_head_is_rejected(self):
        with TempRepo() as repo:
            body = response(f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | ok |", repo.head)
            body = body.replace(f"reviewed-head: {repo.head}\n", "")
            self.assertTrue(any("one full reviewed-head SHA" in e for e in self.check(body, repo)))

    def test_wrong_prefix_is_rejected(self):
        # 前缀是「谁在说话」的唯一凭据，两边共用同一个 GitHub 账号
        with TempRepo() as repo:
            body = response(f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | ok |", repo.head)
            body = body.replace("## 🔧 CLAUDE RESPONSE", "## 回应")
            self.assertTrue(any("must start with" in e for e in self.check(body, repo)))

    def test_empty_table_is_rejected(self):
        # 有意见却交一张空表 = 沉默跳过
        with TempRepo() as repo:
            self.assertTrue(any("nonempty" in e for e in self.check(response("", repo.head), repo)))

    def test_placeholder_cell_is_rejected(self):
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | TBD |"
            self.assertTrue(any("four non-placeholder cells" in e for e in self.check(response(row, repo.head), repo)))


# --------------------------------------------------------------------------
# 退出码契约：2 = 检查跑不动，1 = 查出违规。混在一起会让基础设施故障被当成结论
# --------------------------------------------------------------------------

@requires_git
class ValidateCommitTests(unittest.TestCase):
    def test_bad_sha_argument_is_a_check_error_not_a_violation(self):
        with TempRepo() as repo:
            with self.assertRaises(policy.PolicyError):
                policy.validate_commit(repo.root, "not-a-sha")

    def test_short_sha_is_rejected(self):
        # 短 SHA 会随仓库增长而歧义，证据必须是完整 40 位
        with TempRepo() as repo:
            with self.assertRaises(policy.PolicyError):
                policy.validate_commit(repo.root, repo.head[:12])


# main() 是 CI 与 codex-review.ps1 唯一依赖的入口。它的 0/1/2 映射之前没有任何用例
# —— REVIEW-LOG 却写着「ExitCodeTests 覆盖退出码契约」，是预审指出的假引用。
# 这里对着真实仓库跑（check_local 与 git 都以 ROOT 为根），不 mock。

def real_repo_shas() -> tuple[str, str]:
    root = policy.ROOT
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD~1"], capture_output=True, text=True, check=True).stdout.strip()
    return base, head


def event_file(tmpdir: Path, payload) -> Path:
    path = tmpdir / "event.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


NONE_BODY = BODY.replace("TODO impact: updated\nTODO target: docs/TODO.md:12",
                         "TODO impact: none\nTODO reason: 纯测试用例，无任务状态变化")


class MainExitCodeTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return policy.main(argv)

    def test_no_arguments_runs_local_checks_only(self):
        self.assertEqual(self.run_main([]), 0)

    def test_push_event_runs_local_checks_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), {"ref": "refs/heads/main", "after": "0" * 40})
            self.assertEqual(self.run_main(["--event-file", str(path)]), 0)

    @requires_git
    def test_pull_request_event_with_valid_body_passes(self):
        base, head = real_repo_shas()
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), {"pull_request": {"body": NONE_BODY, "base": {"sha": base}, "head": {"sha": head}}})
            self.assertEqual(self.run_main(["--event-file", str(path)]), 0)

    @requires_git
    def test_pull_request_event_with_placeholder_body_is_a_violation(self):
        base, head = real_repo_shas()
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), {"pull_request": {"body": "## 任务\n\nTBD", "base": {"sha": base}, "head": {"sha": head}}})
            self.assertEqual(self.run_main(["--event-file", str(path)]), 1)

    @requires_git
    def test_pull_request_event_with_null_body_is_a_violation_not_a_crash(self):
        base, head = real_repo_shas()
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), {"pull_request": {"body": None, "base": {"sha": base}, "head": {"sha": head}}})
            self.assertEqual(self.run_main(["--event-file", str(path)]), 1)

    def test_bad_json_is_a_check_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), "{not json")
            self.assertEqual(self.run_main(["--event-file", str(path)]), 2)

    def test_unknown_event_shape_is_a_check_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), {"something": "else"})
            self.assertEqual(self.run_main(["--event-file", str(path)]), 2)

    @requires_git
    def test_unknown_base_sha_is_a_check_error_not_a_violation(self):
        # 本地没 fetch 到 base 时，是「跑不动」不是「正文写错」—— 两者的排障路径完全不同
        _, head = real_repo_shas()
        with tempfile.TemporaryDirectory() as tmp:
            path = event_file(Path(tmp), {"pull_request": {"body": NONE_BODY, "base": {"sha": "f" * 40}, "head": {"sha": head}}})
            self.assertEqual(self.run_main(["--event-file", str(path)]), 2)

    def test_body_file_without_base_and_head_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "b.md"
            body.write_text(NONE_BODY, encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(["--body-file", str(body)])
            self.assertEqual(ctx.exception.code, 2)

    @requires_git
    def test_response_file_path_maps_violation_to_1(self):
        _, head = real_repo_shas()
        with tempfile.TemporaryDirectory() as tmp:
            resp = Path(tmp) / "r.md"
            resp.write_text(response("| 阻断 1 | 已修 | 已写进收口条件 | 看过了 |", head), encoding="utf-8")
            self.assertEqual(self.run_main(["--response-file", str(resp), "--head", head]), 1)


# --------------------------------------------------------------------------
# check_local 与 sections：CI policy job 走的就是这两条路，之前零测试
# --------------------------------------------------------------------------

class LocalAndSectionTests(unittest.TestCase):
    @requires_git
    def test_check_local_scans_tracked_and_untracked_markdown(self):
        with TempRepo() as repo:
            (repo.root / "docs" / "new.md").write_text("- [~] 未跟踪的文件也要查\n", encoding="utf-8")
            errors = policy.check_local(repo.root)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("docs/new.md:1", errors[0])

    @requires_git
    def test_check_local_ignores_gitignored_files(self):
        # 审查脚本的临时材料在 .gitignore 里，不能被当成仓库内容
        with TempRepo() as repo:
            (repo.root / ".gitignore").write_text(".codex-*.md\n", encoding="utf-8")
            (repo.root / ".codex-input-1.md").write_text("- [~] 临时材料\n", encoding="utf-8")
            self.assertEqual(policy.check_local(repo.root), [])

    def test_duplicate_section_is_reported(self):
        _, errors = policy.sections("## 任务\n\na\n\n## 任务\n\nb\n")
        self.assertEqual(errors, ["duplicate PR section: 任务"])


# --------------------------------------------------------------------------
# Worker 模式的只读 manifest：读不懂就退出 2，绝不退化成「没有文件」——
# 那样策略检查会在空集上静默通过。这一组不需要真实 Git，Worker 里照常运行。
# --------------------------------------------------------------------------

LS_FILES = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]


class ManifestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        (self.root / "docs").mkdir(parents=True)
        self.control = os.path.join(tmp.name, "control")
        os.mkdir(self.control)
        self.manifest = os.path.join(self.control, "manifest")

    def env(self, location=None):
        return patch.dict(os.environ, {policy.MANIFEST_ENV: self.manifest if location is None else location})

    def no_git(self):
        return patch.object(policy.subprocess, "run", side_effect=AssertionError("git must not start"))

    def local(self, data: bytes) -> list[str]:
        Path(self.manifest).write_bytes(data)
        with self.env(), patch.object(policy, "ROOT", self.root), self.no_git():
            return policy.check_local(self.root)

    def assert_check_error(self, data: bytes) -> None:
        with self.assertRaises(policy.PolicyError) as ctx:
            self.local(data)
        self.assertNotIn(self.control, str(ctx.exception))

    def read_error(self, location=None) -> str:
        with self.assertRaises(policy.PolicyError) as ctx:
            policy.read_manifest(self.manifest if location is None else location)
        message = str(ctx.exception)
        # manifest 路径是本机信息，报错不回显
        self.assertNotIn(self.control, message)
        return message

    def test_unset_variable_still_runs_git_ls_files(self):
        with patch.dict(os.environ), patch.object(policy.subprocess, "run") as run:
            os.environ.pop(policy.MANIFEST_ENV, None)
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.assertEqual(policy.check_local(policy.ROOT), [])
        argv = run.call_args.args[0]
        self.assertEqual(argv, [LS_FILES[0], "-C", str(policy.ROOT), *LS_FILES[1:]])

    def test_temporary_root_never_reads_the_manifest(self):
        # 变量指向一个不存在的文件：若误读 manifest 就会是 PolicyError，而不是调用 Git
        with self.env(), patch.object(policy.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.assertEqual(policy.check_local(self.root), [])
        self.assertEqual(run.call_args.args[0], [LS_FILES[0], "-C", str(self.root), *LS_FILES[1:]])

    def test_manifest_records_drive_the_scan_without_git(self):
        (self.root / "docs" / "bad.md").write_text("- [~] x\n", encoding="utf-8")
        (self.root / "docs" / "with space.md").write_text("- [x] ok\n", encoding="utf-8")
        # 不在 manifest 里的文件（等同被 .gitignore 挡住）不扫
        (self.root / "ignored.md").write_text("- [~] 临时材料\n", encoding="utf-8")
        errors = self.local(b"docs/with space.md\0docs/bad.md\0")
        self.assertEqual(errors, ["docs/bad.md:1: unsupported task checkbox [~]"])

    def test_records_correspond_to_ls_files_z_output(self):
        data = "a b.md\0docs/中文.md\0-lead.md\0".encode()
        self.assertEqual(policy.parse_manifest(data), ["a b.md", "docs/中文.md", "-lead.md"])

    def test_nul_framing_errors(self):
        for data in (b"", b"a.md", b"a.md\0b.md", b"\0", b"a.md\0\0", b"\0a.md\0"):
            with self.subTest(data=data):
                self.assert_check_error(data)

    def test_invalid_utf8(self):
        for data in (b"\xff.md\0", b"a\xc3.md\0", b"\xc0\xaf.md\0", b"\xed\xa0\x80.md\0"):
            with self.subTest(data=data):
                self.assert_check_error(data)

    def test_absolute_traversal_dot_backslash_and_ambiguous_records(self):
        records = (
            "/etc/x.md", "../x.md", "a/../b.md", "./a.md", "a/./b.md", "a\\b.md", "C:/x.md",
            "a:b.md", "a//b.md", "docs/", "a\tb.md", "a\x7f.md", "a\x85.md", "\ufeffa.md",
        )
        for record in records:
            with self.subTest(record=record):
                self.assert_check_error(record.encode() + b"\0")
        self.assert_check_error(b"a.md\0b.md\0a.md\0")

    def test_size_limit(self):
        Path(self.manifest).write_bytes(b"a" * policy.MANIFEST_MAX_BYTES)
        self.assertEqual(len(policy.read_manifest(self.manifest)), policy.MANIFEST_MAX_BYTES)
        Path(self.manifest).write_bytes(b"a" * (policy.MANIFEST_MAX_BYTES + 1))
        self.read_error()

    def test_location_must_be_absolute_and_normalized(self):
        Path(self.manifest).write_bytes(b"a.md\0")
        for location in ("", "manifest", os.path.join("control", "manifest"),
                         os.path.join(self.control, "..", "control", "manifest"),
                         os.path.join(self.control, ".", "manifest")):
            with self.subTest(location=location):
                self.read_error(location)

    def test_missing_directory_and_unreadable(self):
        self.read_error()
        self.read_error(self.control)
        Path(self.manifest).write_bytes(b"a.md\0")
        with patch.object(policy.os, "open", side_effect=PermissionError(13, "denied", self.manifest)):
            self.read_error()

    def test_link_and_reparse_point_are_rejected(self):
        Path(self.manifest).write_bytes(b"a.md\0")
        link = types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        reparse = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        for fake in (link, reparse):
            with self.subTest(mode=fake.st_mode), patch.object(policy.os, "lstat", return_value=fake):
                self.assertIn("link or reparse point", self.read_error())

    def test_file_swapped_after_check_is_rejected(self):
        Path(self.manifest).write_bytes(b"a.md\0")
        other = os.path.join(self.control, "other")
        Path(other).write_bytes(b"b.md\0")
        with patch.object(policy.os, "fstat", return_value=os.stat(other)):
            self.read_error()

    def test_main_exit_codes_in_worker_mode(self):
        (self.root / "docs" / "bad.md").write_text("- [~] x\n", encoding="utf-8")
        (self.root / "docs" / "ok.md").write_text("- [x] ok\n", encoding="utf-8")
        for data, code in ((b"docs/ok.md\0", 0), (b"docs/ok.md\0docs/bad.md\0", 1), (b"docs/ok.md", 2), (b"../x.md\0", 2)):
            Path(self.manifest).write_bytes(data)
            stderr = io.StringIO()
            with self.subTest(data=data), self.env(), patch.object(policy, "ROOT", self.root), self.no_git(), \
                    redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(policy.main([]), code)
                self.assertNotIn(self.control, stderr.getvalue())
        with self.env(os.path.join(self.control, "missing")), patch.object(policy, "ROOT", self.root), self.no_git(), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(policy.main([]), 2)

    @requires_git
    def test_real_ls_files_output_gives_the_same_result(self):
        # manifest 是 `git ls-files -z ...` 的原始字节：两条路径在同一个仓库上必须逐条一致
        with TempRepo() as repo:
            (repo.root / ".gitignore").write_text(".codex-*.md\n", encoding="utf-8")
            (repo.root / ".codex-input-1.md").write_text("- [~] 临时材料\n", encoding="utf-8")
            (repo.root / "docs" / "中文 new.md").write_text("- [~] 未跟踪\n", encoding="utf-8")
            raw = subprocess.run(["git", "-C", str(repo.root), *LS_FILES[1:]], capture_output=True, check=True).stdout
            Path(self.manifest).write_bytes(raw)
            with patch.object(policy, "ROOT", repo.root):
                expected = policy.check_local(repo.root)
                with self.env(), self.no_git():
                    self.assertEqual(policy.check_local(repo.root), expected)
            self.assertEqual(len(expected), 1, expected)


# --------------------------------------------------------------------------
# 宽容度：合法但略有差异的写法不该被拒 —— 预审实测三种都曾被拒且报错看不出原因
# --------------------------------------------------------------------------

@requires_git
class ToleranceTests(unittest.TestCase):
    def test_response_header_with_leading_blank_line_and_trailing_space(self):
        # 首行规则必须与 ps1 的 Test-ResponseHeader 一致：第一个非空行、trim 后相等
        with TempRepo() as repo:
            body = "\n" + response(f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | ok |", repo.head)
            body = body.replace("## 🔧 CLAUDE RESPONSE\n", "## 🔧 CLAUDE RESPONSE \n", 1)
            self.assertEqual(policy.check_response(body, root=repo.root, head=repo.head), [])

    def test_backticked_evidence_is_accepted(self):
        with TempRepo() as repo:
            row = f"| 阻断 1 | 已修 | `{repo.head}` · `docs/TODO.md:12` | ok |"
            self.assertEqual(policy.check_response(response(row, repo.head), root=repo.root, head=repo.head), [])

    def test_quoted_reviewed_head_line_is_ignored(self):
        with TempRepo() as repo:
            body = response(f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | ok |", repo.head)
            body = body.replace("| 意见 |", f"> reviewed-head: {repo.head}\n\n| 意见 |", 1)
            self.assertEqual(policy.check_response(body, root=repo.root, head=repo.head), [])

    def test_trailing_spaces_on_todo_and_reviewed_head_lines(self):
        with TempRepo() as repo:
            body = BODY.replace("TODO impact: updated\n", "TODO impact: updated  \n").replace("docs/TODO.md:12\n", "docs/TODO.md:12 \n")
            self.assertEqual(policy.check_pr(body, {"docs/TODO.md"}, root=repo.root, head=repo.head), [])
            resp = response(f"| 阻断 1 | 已修 | {repo.head} · docs/TODO.md:12 | ok |", repo.head)
            resp = resp.replace(f"reviewed-head: {repo.head}\n", f"reviewed-head: {repo.head}  \n")
            self.assertEqual(policy.check_response(resp, root=repo.root, head=repo.head), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
