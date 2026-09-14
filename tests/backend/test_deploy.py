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
    """⚠️ 仓库是公开的：真实主机名 / IP 绝不能进来。

    它们全部走 GitHub secret，名字沿用本工作区其它项目的 `VPS_*` 约定
    （`rs-roof-pms`、`crm_os`、`erp_os` 等八个项目都这么叫）。这条用例是
    `secret-scan` 的补充 —— 那一层找的是凭据形状的东西，认不出一个普通的主机名。
    """
    workflow = uncommented(WORKFLOW)
    for needle in ("VPS_HOST", "VPS_USER", "VPS_SSH_KEY", "VPS_PORT", "VPS_FINGERPRINT"):
        assert f"secrets.{needle}" in workflow
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", workflow)
    assert not re.search(r"[a-z0-9_-]+@[a-z0-9.-]+\.[a-z]{2,}", workflow)


def test_the_workflow_verifies_the_host_key() -> None:
    """⚠️ 不校验主机指纹的 SSH，等于把部署私钥交给任何能做中间人的人。

    本工作区其它项目的 deploy workflow **都没有**做这一条。这里补上，而且
    `appleboy/ssh-action` 的 `fingerprint` 留空时是**静默跳过**校验 —— 所以光传进去
    不够，还要在前面单独检查它非空，空就让 job 红掉。
    """
    workflow = uncommented(WORKFLOW)
    assert "fingerprint: ${{ secrets.VPS_FINGERPRINT }}" in workflow
    assert "refusing to SSH without verifying the host key" in workflow


def test_the_vps_checks_out_the_exact_commit_it_was_built_from() -> None:
    """⚠️ `git pull` 拿的是 main 的最新 HEAD，不是这一次构建的 commit。

    构建完成后又合进来一个 commit 的话，VPS 上跑的 deploy.sh 与 compose 就和镜像
    不是同一个版本。本工作区其它项目用的是 `git pull --ff-only`，这里刻意没沿用。
    """
    workflow = uncommented(WORKFLOW)
    assert "git checkout --quiet --detach" in workflow
    assert "git pull" not in workflow


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
    # ⚠️ 恢复时拉回来的也是整库备份。这一条也是真发生过的：`git check-ignore`
    # 对一个刚删掉的目录报了「已忽略」，而规则里根本没有它 —— 用真文件一试就漏了。
    assert "/restore/" in ignored


def test_the_backup_is_verified_before_it_is_uploaded() -> None:
    """⚠️ 「备份在跑」和「备份能恢复」是两件事，只有第二件算数。

    一个口令配错、或者写盘出错的备份，看起来和好的一模一样：都是一个大小合理的
    文件静静躺在桶里，直到真出事那天才发现打不开。所以解密验证必须在上传**之前**。
    """
    script = uncommented(BACKUP)
    verifies = script.index("cannot be decrypted")
    # 上传现在走容器里的 aws-cli（见 backup.sh 的 aws_cli / s3 辅助函数）。
    uploads = script.index("uploading to")
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


RESTORE = REPO_ROOT / "deploy" / "restore.sh"
BINLOG_SHIP = REPO_ROOT / "deploy" / "binlog_ship.sh"
CRON = REPO_ROOT / "deploy" / "cron.d" / "ai_billing_hub"


def test_restore_refuses_to_overwrite_the_live_database() -> None:
    """⚠️ 恢复是导入一份 dump，而 dump 里是一连串 DROP TABLE / CREATE TABLE。

    对着正在服务的库跑，等于在事故现场再制造一次事故，而且会把「恢复前那一刻的
    数据」一起抹掉 —— 那可能正是你要找回来的东西。
    """
    assert "refusing to restore over the live database" in uncommented(RESTORE)


def test_restore_counts_rows_exactly() -> None:
    """⚠️ 不能用 `information_schema.tables.table_rows` —— 它对 InnoDB 只是估计值。

    这条是恢复演练抓到的：一张确实有数据的 `users` 被报成了 **0 行**
    （`SELECT COUNT(*)` 是 1）。一个会把有数据报成空的核对比没有核对更糟：
    真出事时看到 0 行的人会以为备份是空的。
    """
    script = uncommented(RESTORE)
    assert "SELECT COUNT(*)" in script
    assert "table_rows" not in script


def test_s3_credentials_never_appear_on_a_command_line() -> None:
    """⚠️ `docker run -e NAME=value` 会让口令出现在 `ps` 能看到的参数里。

    同机任何用户都读得到，而那台 VPS 上还跑着另外八个项目。必须用不带值的
    `-e NAME`，让 docker 从自己的环境里取。
    """
    for path in (BACKUP, RESTORE, BINLOG_SHIP):
        script = uncommented(path)
        assert "-e AWS_ACCESS_KEY_ID" in script
        assert "-e AWS_ACCESS_KEY_ID=" not in script
        assert "-e AWS_SECRET_ACCESS_KEY=" not in script


def test_the_upload_is_confirmed_against_the_remote_object() -> None:
    """⚠️ `cp` 返回 0 只说明客户端认为它发完了。

    一次被中间设备截断的上传，本地看起来同样是成功的。所以传完要拉一次元数据
    回来核对大小。
    """
    script = uncommented(BACKUP)
    assert "head-object" in script
    assert "size mismatch after upload" in script


def test_no_comment_breaks_a_line_continuation() -> None:
    """⚠️ 反斜杠续行的中间夹一行注释，续行就在那里断掉。

    这是本任务里真踩到的：注释插在 `AWS_SECRET_ACCESS_KEY=... \\` 与
    `docker run` 之间，前两个凭据变成当前 shell 里**没有导出**的变量，
    容器拿不到，报 `Unable to locate credentials` —— 看着像凭据填错了。
    """
    for path in (SCRIPT, BACKUP, RESTORE, BINLOG_SHIP):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, (line, following) in enumerate(zip(lines, lines[1:], strict=False), start=1):
            if line.rstrip().endswith("\\") and following.strip().startswith("#"):
                raise AssertionError(
                    f"{path.name}:{number} continues onto a comment, which ends the command"
                )


# --- Codex #42 R1 ------------------------------------------------------------


def test_the_first_deploy_starts_the_database_before_waiting_for_it() -> None:
    """⚠️ 只等不拉的话，首次部署永远走不到迁移（Codex #42 R1 阻断项 1）。

    空栈上 mysql 容器还没被创建，`wait_for_health mysql` 会一直等到超时。变异验证过：
    去掉 `up -d --no-build mysql` 那一行，空栈部署卡在 `containers not created yet`
    直到超时、exit 1；带着它，同一个空栈部署 exit 0。
    """
    script = uncommented(SCRIPT)
    starts = script.index("up -d --no-build mysql")
    waits = script.index("wait_for_health mysql")
    assert starts < waits


def test_the_dispatch_ref_is_validated_before_any_shell_sees_it() -> None:
    """⚠️ `${{ inputs.ref }}` 写进 `run:` 是**先原样替换进脚本再交给 bash**（阻断项 2）。

    填一个 `"; curl … | sh #` 就是在 runner 上执行命令，而它随后还会被拼进 VPS 上
    握着部署私钥的脚本。所以 `inputs.ref` 只允许出现在 env 里，经整串 40 位 SHA 校验，
    下游一律只用校验那一步的输出。
    """
    workflow = uncommented(WORKFLOW)
    uses = [line.strip() for line in workflow.splitlines() if "inputs.ref" in line]
    assert uses == ["REF: ${{ inputs.ref || github.sha }}"]
    assert '[[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]' in workflow
    # 校验排在检出之前，检出与部署都只用校验后的输出。
    assert workflow.index("=~ ^[0-9a-f]{40}$") < workflow.index("actions/checkout")
    assert "ref: ${{ steps.resolve.outputs.tag }}" in workflow


def test_binlogs_leave_the_host_every_minute_and_the_lock_is_not_in_cron() -> None:
    """⚠️ RPO ≤ 5 分钟靠 binlog 离机，不靠每天一次的全量（R1 阻断项 3）。

    R2 阻断项：`*/4` + cron 行上的 `flock -n`，上一轮没跑完时本轮被静默跳过，
    离机间隔变成 8 分钟。现在每分钟一次（跳过只损失一分钟），而且**锁不在 cron 行上**
    —— 锁在那里的话，一次卡死的运行会让之后每一轮在进脚本之前就被挡掉，脚本里的
    新鲜度检查永远不会执行。全量也必须被调度。
    """
    cron = uncommented(CRON)
    binlog = [line for line in cron.splitlines() if "binlog_ship.sh" in line]
    assert len(binlog) == 1
    assert binlog[0].startswith("* * * * * ")
    assert "flock" not in binlog[0]
    assert any("deploy/backup.sh" in line for line in cron.splitlines())
    # ⚠️ 真实部署账号不进公开仓库。
    assert "@DEPLOY_USER@" in cron


def test_a_skipped_binlog_run_still_checks_freshness() -> None:
    """⚠️ 调度保证不了 RPO（上传本身可以慢过 5 分钟），只能让它破掉时不可能不被发现。

    锁与新鲜度检查排在读配置、连数据库**之前**，被跳过的那一轮也要先查一次。
    在 Ubuntu 容器里用真 flock 验证过：锁被占着、心跳 10 分钟前 → 跳过**并且**
    `logger -p user.err … RPO breached`；心跳新鲜时安静跳过。
    """
    script = uncommented(BINLOG_SHIP)
    lock = script.index("flock -n 9")
    assert lock < script.index("env_value BILLING_BACKUP_PASSPHRASE")
    assert lock < script.index("FLUSH BINARY LOGS")
    skip_branch = script[lock : script.index("skipping this one")]
    assert "check_freshness" in skip_branch
    assert "RPO breached" in script
    # 失败与破 RPO 以 err 级别单独写 syslog，不和每分钟的 info 输出混在一起。
    assert "logger -p user.err" in script
    assert 'die() { alarm "$*"; exit 1; }' in script


def test_the_freshness_clock_starts_at_the_flush_not_at_the_end_of_the_run() -> None:
    """⚠️ FLUSH 之后写入的数据要等下一轮才离机。

    用本轮结束的时刻记心跳，会把暴露窗口少算一整次上传的耗时 —— 恰好是上传变慢、
    最需要报警的时候少算得最多。
    """
    script = uncommented(BINLOG_SHIP)
    assert script.index('FLUSHED_AT="$(date +%s)"') < script.index("mysql_q 'FLUSH BINARY LOGS'")
    assert 'mark_success "$FLUSHED_AT"' in script


def test_an_idle_run_never_strands_unshipped_binlogs() -> None:
    """空闲时不 FLUSH（否则每分钟造一个空文件），但「空闲」的判据有两道，各自变异验证过。

    1. 位置记录**只在整轮成功之后**写。变异（FLUSH 后立刻写）：R2 断掉那一轮失败，
       恢复后那一轮报 nothing to ship，积压留在本机
    2. 还要确认最新一个已关闭的文件就是推过的最后一个。只保留这一道、去掉第 1 道的
       变异照样把积压推走了 —— 位置记录因任何原因失准都不会搁浅文件
    """
    script = uncommented(BINLOG_SHIP)
    record = script.index('> "${IDLE_MARK}.tmp"')
    assert record > script.index("done 3<<EOF")
    assert '[ "$NEWEST_CLOSED" = "$(cat "$MARK" 2>/dev/null || true)" ]' in script


def test_binlog_shipping_closes_the_active_file_and_refuses_gaps() -> None:
    """⚠️ 不 FLUSH 的话，写入少的时候一个 binlog 要好几天才写满轮转 —— RPO 就是好几天。

    断档必须是显式失败：缺一个文件，PITR 就只能恢复到缺口之前，而之后每一轮推送
    看起来都照常成功。
    """
    script = uncommented(BINLOG_SHIP)
    assert "FLUSH BINARY LOGS" in script
    assert "gap in the binlog chain" in script
    assert "RPO is NOT met" in script


def test_binlog_shipping_reads_its_file_list_off_stdin() -> None:
    """⚠️ 循环体里的 `docker compose exec` 会吞掉 stdin。

    清单从 stdin 读的话，每次运行只推第一个文件，其余悄悄留到下一轮 —— 积压越来越多，
    而每一轮都报成功。这是演练时实测踩到的（三次运行各推一个）。
    """
    script = uncommented(BINLOG_SHIP)
    assert "while read -r -u 3 " in script
    assert "done 3<<EOF" in script


def test_each_binlog_is_verified_before_upload_and_confirmed_after() -> None:
    """与全量备份同一套三道检查，外加一条：与 MySQL 自己记的文件大小比对。

    半途断掉的 `exec cat` 留下的截断文件，加密往返比对是抓不到的。
    """
    script = uncommented(BINLOG_SHIP)
    assert script.index("MySQL reports") < script.index("encryption failed")
    assert script.index("NOT uploading") < script.index("s3 cp")
    assert "head-object" in script
    # ⚠️ 远端按 server_uuid 分目录：换主机后编号从 000001 重来，不分会覆盖旧链。
    assert 'KEY="binlog/${SERVER_UUID}/' in script


def test_the_full_backup_names_its_binlog_chain() -> None:
    """锚点只给出 `binlog.000002` 这样的编号，而编号每台主机都从 000001 开始。

    恢复时靠 dump 里这一行 server_uuid 才知道去哪条链上找。
    """
    assert "-- billing-server-uuid:" in uncommented(BACKUP)


def test_restore_replays_binlogs_from_the_anchor_into_the_target_only() -> None:
    """⚠️ 只导全量，恢复点就是全量那一刻 —— 最多丢一整天。

    三个参数各挡一件事：`--start-position` 跳过 dump 已含的部分；`--rewrite-db` +
    `--database` 只把生产库的事件放进目标库；`--disable-log-bin` 让重放本身不进 binlog。
    """
    script = uncommented(RESTORE)
    for flag in ("--start-position=", "--rewrite-db=", "--database=", "--disable-log-bin"):
        assert flag in script
    assert "gap in the binlog chain" in script


def test_restore_checks_the_chain_before_touching_any_database() -> None:
    """⚠️ 第一版导入完才查链，缺口一出现就留下一个导了一半的库。

    演练确认过改完之后：删掉中间一个 binlog，恢复被拒绝，目标库**根本没被创建**。
    """
    script = uncommented(RESTORE)
    assert script.index("gap in the binlog chain") < script.index("CREATE DATABASE")


def test_restore_keeps_its_own_writes_out_of_the_binlog() -> None:
    """⚠️ 演练就在生产那台 MySQL 上做。

    导入写进 binlog 的话，下一次恢复进同名演练库时，重放会把上一次的建库与导入再放
    一遍。变异验证过：去掉这两处，第二次演练在重放时失败（ERROR 1007）。
    """
    script = uncommented(RESTORE)
    assert "SET sql_log_bin=0; CREATE DATABASE" in script
    assert "printf 'SET sql_log_bin=0;\\n'; cat \"$PLAIN\"" in script
