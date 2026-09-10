"""Regression tests for scripts/check_repo_policy.py.

每个用例对应一次真实发生过的缺陷，不是凑覆盖率。来源见 docs/REVIEW-LOG.md
的「反复出现的问题」。误判用例同样重要：这个检查会跑在每个 PR 上，
一旦把代码示例或普通 Markdown 链接当成任务项，它会立刻被加进忽略名单，
然后就再也拦不住真正的问题了。
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_repo_policy.py"
SPEC = importlib.util.spec_from_file_location("check_repo_policy", SCRIPT)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)

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

class ExitCodeTests(unittest.TestCase):
    def test_bad_sha_argument_is_a_check_error_not_a_violation(self):
        with TempRepo() as repo:
            with self.assertRaises(policy.PolicyError):
                policy.validate_commit(repo.root, "not-a-sha")

    def test_short_sha_is_rejected(self):
        # 短 SHA 会随仓库增长而歧义，证据必须是完整 40 位
        with TempRepo() as repo:
            with self.assertRaises(policy.PolicyError):
                policy.validate_commit(repo.root, repo.head[:12])


if __name__ == "__main__":
    unittest.main(verbosity=2)
