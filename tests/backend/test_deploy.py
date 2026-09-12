"""部署流水线与部署脚本的守卫（spec §99；T0.9）。

⚠️ **这两样东西都没有真正的测试环境。**workflow 在生产之外跑不起来，而部署脚本
只能在本地栈上演练。所以这个文件钉的不是「它能不能工作」，而是**它不许长成什么样**
—— 那些一旦出现就会在生产上造成后果、而在别处看不出来的写法。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
SCRIPT = REPO_ROOT / "deploy" / "deploy.sh"


def uncommented(path: Path) -> str:
    """剥掉整行注释。

    ⚠️ 不剥的话断言是假的：把一行改成注释，`"xxx" in text` 照样为真。
    `tests/backend/test_compose.py` 的 `instructions()` 是同一条教训，
    变异测试在那边抓到过。
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_the_deploy_script_refuses_mutable_tags() -> None:
    """⚠️ §99 要求「immutable image tags identify the exact commit」。

    用 `latest` 的话「线上跑的是哪个 commit」没有答案，**回滚也无从谈起** ——
    回到哪一个 latest？这条护栏在脚本里，因为脚本才是最后一道关。
    """
    script = uncommented(SCRIPT)
    assert "latest|main|master" in script
    assert "refusing a mutable tag" in script


def test_the_workflow_tags_images_with_the_commit() -> None:
    """镜像标签必须来自 commit SHA，不能是任何固定字符串。"""
    workflow = uncommented(WORKFLOW)
    assert "github.sha" in workflow
    # ⚠️ `:latest` 出现在任何一个 tags: 行上都是错的。
    assert not re.search(r"tags:.*:latest", workflow)


def test_the_workflow_never_hardcodes_the_target_host() -> None:
    """⚠️ 仓库是公开的：真实主机名 / IP / 路径绝不能进来。

    它们全部走 GitHub secret。这条用例是 `secret-scan` 那一层的补充 ——
    secret-scan 找的是凭据形状的东西，认不出一个看着普通的主机名。
    """
    workflow = uncommented(WORKFLOW)
    for needle in ("DEPLOY_TARGET", "DEPLOY_PATH", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        assert f"secrets.{needle}" in workflow
    # 形如 user@host 或裸 IP 的字面量都不该出现。
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", workflow)
    assert not re.search(r"[a-z0-9_-]+@[a-z0-9.-]+\.[a-z]{2,}", workflow)


def test_the_workflow_verifies_the_host_key() -> None:
    """⚠️ 不校验主机指纹的 SSH，等于把部署凭据交给任何能做中间人的人。"""
    assert "known_hosts" in uncommented(WORKFLOW)


def test_deployments_do_not_run_concurrently() -> None:
    """⚠️ 并发部署会让两次迁移交叉。

    而且**不能 cancel-in-progress**：取消一个跑到一半的部署会把栈停在中间状态
    （迁移跑完了、容器还没换），比等一会儿糟糕得多。
    """
    workflow = uncommented(WORKFLOW)
    assert "group: deploy-production" in workflow
    assert "cancel-in-progress: false" in workflow


def test_the_script_records_the_rollback_target_before_changing_anything() -> None:
    """⚠️ 记录当前版本必须在**任何改动之前**。

    放在「部署成功之后再记」的话，第一次失败的部署就没有可回滚的目标了 ——
    而那恰恰是最需要回滚的时刻。
    """
    script = uncommented(SCRIPT)
    recorded = script.index("PREVIOUS_IMAGE=")
    changed = script.index("alembic upgrade head")
    assert recorded < changed


def test_a_failed_deploy_exits_non_zero_even_after_a_successful_rollback() -> None:
    """⚠️ 回滚成功**不等于**这次部署成功。

    回滚让服务恢复了，但「这个 commit 上不了线」这件事不能被一个绿色的 CD 掩盖 ——
    那样下一个人会以为它已经上线了。
    """
    script = uncommented(SCRIPT)
    assert "rolled back to" in script
    # `die` 里是 `exit 1`，所以回滚那条走的是 die 而不是 exit 0。
    assert 'die "rolled back to' in script


def test_the_script_waits_for_the_database_before_migrating() -> None:
    """⚠️ 这条是本地演练抓到的。

    第一版直接跑迁移，而 MySQL 刚起来还在初始化 —— 迁移 Connection refused，
    被当成「这次部署失败」，实际只是早了几秒。首次部署与 MySQL 重启后各会中一次，
    而那正是最容易手忙脚乱的两个时刻。
    """
    script = uncommented(SCRIPT)
    waits = script.index("wait_for_health mysql")
    migrates = script.index("alembic upgrade head")
    assert waits < migrates


def test_deploying_is_a_deliberate_act_not_a_side_effect_of_merging() -> None:
    """⚠️ 现在**刻意只能手动触发**。

    挂上 `push: branches: [main]` 的话，这个 workflow 会在合并的那一刻开火 ——
    而生产主机还没就绪、四个 secret 也还没配。后果不只是「一次红色的 CD」：
    build 那一步会**真的把镜像推到 ghcr**，那是个对外的副作用，不该由一次
    「先把代码合进去」顺带触发。

    等 `docs/deployment.md` §9.4 的前置清单关闭之后再加回来 —— 那应该是一次
    有意识的改动。这条用例存在的意义就是逼它成为有意识的：加回触发器的人
    必须同时改掉这里，而那一刻他会读到上面这段话。
    """
    workflow = uncommented(WORKFLOW)
    assert "workflow_dispatch" in workflow
    assert "branches: [main]" not in workflow


BACKUP = REPO_ROOT / "deploy" / "backup.sh"


def test_backups_are_never_committed() -> None:
    """⚠️ 仓库是公开的，而 `backups/` 里是**整个计费库**。

    它是加密的，但「加密了所以可以提交」是错的推理：口令一旦泄漏，历史里那份
    密文永远拿不回来 —— git 删不掉已经推出去的东西。

    这条是真发生过的：备份脚本第一次跑完，`git status` 里就躺着一个未被忽略的
    `backups/`。
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/backups/" in ignored


def test_the_backup_is_verified_before_it_is_uploaded() -> None:
    """⚠️ 「备份在跑」和「备份能恢复」是两件事，只有第二件算数。

    一个口令配错、或者写盘出错的备份，看起来和好的一模一样：都是一个大小合理的
    文件静静躺在桶里，直到真出事那天才发现打不开。所以解密验证必须在上传**之前**。
    """
    script = uncommented(BACKUP)
    verifies = script.index("cannot be decrypted")
    uploads = script.index("aws s3 cp")
    assert verifies < uploads


def test_the_backup_rejects_a_truncated_dump() -> None:
    """⚠️ 往返比对抓不到这一种。

    截断发生在加密**之前**，所以明文和解回来的密文一致、`cmp` 照样通过 ——
    变异测试确认了这一点：把这条检查换掉之后，一份被截断的 dump 一路畅通。
    所以它不是冗余的，是唯一的防线。
    """
    assert "Dump completed" in uncommented(BACKUP)


def test_the_backup_records_the_binlog_position() -> None:
    """⚠️ 没有它，PITR 无从下手。

    你有一份全量和一堆 binlog，却不知道该从哪一条开始重放 —— 少放会丢数据，
    多放会重复。`--source-data=2` 把导出那一刻的位置写进 dump 的注释里。
    """
    assert "--source-data=2" in uncommented(BACKUP)


def test_a_local_only_backup_is_an_explicit_failure() -> None:
    """⚠️ 没配 R2 **不能**静默跳过上传。

    只在本地留备份的系统满足不了 §98.1 的 off-VPS 要求，而它看起来一切正常 ——
    那正是最危险的形态：每天都「成功」，直到机器整个没了。
    """
    assert "does NOT satisfy spec" in uncommented(BACKUP)
