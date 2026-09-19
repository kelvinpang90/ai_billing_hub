"""部署流水线与部署脚本的守卫（spec §99；T0.9）。

⚠️ **这两样东西都没有真正的测试环境。**workflow 在生产之外跑不起来，而部署脚本
只能在本地栈上演练。所以这个文件钉的不是「它能不能工作」，而是**它不许长成什么样**
—— 那些一旦出现就会在生产上造成后果、而在别处看不出来的写法。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def test_merging_to_main_deploys_and_manual_dispatch_stays() -> None:
    """spec §99：合并到 `main` 触发部署。2026-09-18 §9.4 的前置清单全部关闭后加回。

    ⚠️ 这条用例原来钉的是反面（「刻意只能手动触发」），逼加回触发器成为一次有意识
    的改动 —— 现在就是那一次。三件事要一起成立：

    - 只有 `main` 触发：别的分支一推就部署，等于绕过 PR 与 CI
    - `workflow_dispatch` 必须留着：手工回滚（`ref` 填 `.last-good-deploy` 里的 SHA）
      只有这一条路
    - deploy job 仍挂 `environment: production`：它的「只允许 main」是手动触发时
      填错分支的最后一道闸
    """
    workflow = uncommented(WORKFLOW)
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]
    assert re.search(r"^  push:\n    branches: \[main\]\n", trigger, re.M), trigger
    assert "workflow_dispatch" in trigger
    assert "tags:" not in trigger and "branches-ignore" not in trigger
    assert "environment: production" in workflow


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
DRILL = REPO_ROOT / "deploy" / "restore_drill.sh"
MONITOR = REPO_ROOT / "deploy" / "monitor.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"
LOG_SHIP = REPO_ROOT / "deploy" / "log_ship.sh"


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
    for path in (BACKUP, RESTORE, BINLOG_SHIP, DRILL, LOG_SHIP):
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
    for path in (SCRIPT, BACKUP, RESTORE, BINLOG_SHIP, DRILL, MONITOR, LOG_SHIP):
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


def test_every_deploy_reloads_the_edge_proxy_configuration() -> None:
    """⚠️ 只改了 nginx 配置的部署，不 reload 就永远不会生效。

    配置从部署目录挂进容器，`git checkout` 换掉了磁盘上的文件；而 billing_nginx 的镜像与
    compose 定义没变，`up -d` 不会重建它。T0.9 首次加安全响应头时生产上就是这样：部署
    成功、冒烟通过，外网一个头都没有。本地演练复现过：去掉 reload 的变异，新配置不生效。
    """
    script = uncommented(SCRIPT)
    starts = script.index("up -d --no-build ||")
    checks = script.index("exec -T billing_nginx nginx -t")
    reloads = script.index("exec -T billing_nginx nginx -s reload")
    smoke = script.index('log "smoke: GET')
    assert starts < checks < reloads < smoke
    # 配置无效要算部署失败（走回滚），不能 reload 失败了还报成功。
    reload_block = script[checks : script.index("\nfi", reloads)]
    assert reload_block.count("HEALTHY=0") == 2


def test_the_smoke_test_fails_when_the_edge_runs_a_stale_configuration() -> None:
    """⚠️ 只看 /healthz 返回 200 不够：一个没 reload 上的边缘照样返回 200。

    本地演练：变异掉 reload 那一步、磁盘上是带安全头的新配置 —— 冒烟因为缺
    `X-Frame-Options: DENY` 失败并回滚。这是 reload 漏掉时唯一能发现的地方。
    """
    script = uncommented(SCRIPT)
    start = script.index('log "smoke: GET')
    smoke = script[start : script.index('if [ "$HEALTHY" = "1" ] && [ "$SMOKE" = "1" ]', start)]
    assert "x-frame-options: *DENY" in smoke
    assert "not sending its security headers" in smoke


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


def test_freshness_is_checked_even_before_the_first_successful_push() -> None:
    """⚠️ R3 阻断项：心跳文件不存在时直接返回，首次推送卡住就永远不报。

    新部署（或心跳文件丢失）时从「开始看守」的时刻起算。Ubuntu 容器真 flock 验证过：
    无心跳、第一次看到 → 只起表不报；锁被占、无心跳、已看守 10 分钟 →
    `user.err … no binlog has left this host since monitoring started`。
    """
    script = uncommented(BINLOG_SHIP)
    body = script[script.index("check_freshness() {") : script.index("mark_success() {")]
    assert '[ -f "$LAST_OK" ] || return 0' not in body
    assert 'ref="$WATCH_SINCE"' in body
    assert '[ -f "$WATCH_SINCE" ] || date +%s > "$WATCH_SINCE"' in body


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


def test_backup_jobs_report_to_their_own_heartbeat_check() -> None:
    """⚠️ 「没有日志」的故障只有外部心跳发现得了：cron 没跑、VPS 挂了、脚本卡死。

    两个任务用两个检查：频率不同（每分钟 / 每天），混成一个的话每分钟的 binlog 心跳会把
    「全量三天没跑」盖住。失败立刻发 /fail，不等宽限期。ping 必须有超时且失败不拖垮本轮 ——
    binlog 那边卡住的 ping 会一直占着锁。
    """
    for path, own, other in (
        (BACKUP, "BILLING_HEALTHCHECK_BACKUP_URL", "BILLING_HEALTHCHECK_BINLOG_URL"),
        (BINLOG_SHIP, "BILLING_HEALTHCHECK_BINLOG_URL", "BILLING_HEALTHCHECK_BACKUP_URL"),
    ):
        script = uncommented(path)
        assert f"env_value {own}" in script
        assert other not in script
        assert "heartbeat /fail" in script
        assert "curl -fsS -m 10" in script
        assert '|| log "heartbeat ping failed' in script


def test_a_skipped_or_rehearsal_run_never_reports_success() -> None:
    """⚠️ 成功心跳只能在真的做完之后发。

    binlog 被锁挡住跳过的那一轮如果发成功心跳，一次卡死的推送会被每分钟的「成功」盖住；
    全量的 --dry-run 没有上传，发了就等于告诉监控「今天有离机备份」。
    """
    ship = uncommented(BINLOG_SHIP)
    start = ship.index("if ! flock -n 9; then")
    skipped = ship[start : ship.index("\nfi\ncheck_freshness", start)]
    assert "heartbeat" not in skipped
    assert ship.index("nothing to ship") < ship.index('heartbeat ""')
    assert ship.rstrip().endswith('heartbeat ""')
    # alarm 在锁与新鲜度检查里就会被调用，它要读的心跳地址必须在那之前就读好。
    reads_url = ship.index('HEARTBEAT_URL="$(env_value')
    assert ship.index("env_value() {") < reads_url < ship.index("alarm() {")

    backup = uncommented(BACKUP)
    assert backup.rstrip().endswith('[ "$DRY_RUN" = "1" ] || heartbeat "" "$(basename "$CIPHER")"')


def test_the_weekly_drill_restores_the_offsite_copy_with_the_real_restore_script() -> None:
    """⚠️ 演练验的必须是出事时真会用的东西：R2 上那份，用 restore.sh 恢复。

    从本机 backups/ 取的话，验证不了「离机那份能不能用」；自己另写一套导入逻辑的话，
    验证不了 restore.sh。每周日跑一次（Kelvin 2026-09-15），排在当天全量之后。
    """
    drill = uncommented(DRILL)
    assert '--prefix "full/"' in drill
    assert 'bash deploy/restore.sh "$KEY" "$DRILL_DB"' in drill
    cron = [line for line in uncommented(CRON).splitlines() if "restore_drill.sh" in line]
    assert len(cron) == 1
    assert cron[0].startswith("47 4 * * 0 ")


def test_the_weekly_drill_can_only_ever_drop_its_own_database() -> None:
    """⚠️ 演练在生产那台 MySQL 上做，而清理会无条件 DROP 演练库。

    库名写死、再与生产库名比一次；DROP 关 binlog —— 否则这条语句被推到 R2，下次演练
    重放到它会删掉正在恢复的库。无论成败都清理（整库副本、整库密文、440 MB 镜像）。
    """
    drill = uncommented(DRILL)
    assert 'DRILL_DB="billing_autodrill"' in drill
    assert "BILLING_DRILL" not in drill.replace("BILLING_HEALTHCHECK_DRILL_URL", "")
    assert '[ "$DRILL_DB" != "$LIVE_DB" ]' in drill
    assert "SET sql_log_bin=0; DROP DATABASE IF EXISTS \\`${DRILL_DB}\\`" in drill
    assert drill.index("trap cleanup EXIT") < drill.index("bash deploy/restore.sh")
    assert 'rm -rf "$WORK_DIR"' in drill
    assert 'WORK_DIR="./restore/autodrill"' in drill


def test_the_weekly_drill_opens_a_restored_secret_without_leaking_it() -> None:
    """⚠️ 库导得进来不等于能用：主密钥与密文对不上时，所有集成凭据与 TOTP 注册全部作废。

    解密在断网、以应用用户运行、密钥只读挂入的容器里做；**只打印 decrypted** ——
    这一轮的输出进 syslog，任何码或密钥都不许出现在里面。
    """
    drill = uncommented(DRILL)
    run = drill[drill.index('RESULT="$(MSYS_NO_PATHCONV=1 docker run') : drill.index("unset TOKEN")]
    for flag in ("--network none", "--user 10001:10001", ':/drill/master.key:ro"', "-e TOKEN "):
        assert flag in run
    assert 'print("decrypted")' in run
    assert run.count("print(") == 1
    assert "pyotp" not in run


def test_the_weekly_drill_reports_to_its_own_heartbeat_check() -> None:
    """失败立刻 /fail；成功只在所有核对都过了之后发（放在脚本最后）。"""
    drill = uncommented(DRILL)
    assert "env_value BILLING_HEALTHCHECK_DRILL_URL" in drill
    assert 'heartbeat /fail "restore drill failed: $*"' in drill
    assert drill.rstrip().endswith('heartbeat "" "$SUMMARY"')


def test_the_full_backup_closes_the_binlog_its_anchor_points_into() -> None:
    """⚠️ 锚点落在正在写的 binlog 上；之后没有写入的话 binlog_ship.sh 不 FLUSH，它永远到不了 R2。

    VPS 演练撞到过：最新那份全量恢复时报「anchor binlog has not been shipped」。
    FLUSH 必须在上传确认之后 —— 失败时好备份已经在桶里。
    """
    script = uncommented(BACKUP)
    flush = script.index("-e 'FLUSH BINARY LOGS'")
    assert script.index("uploaded and confirmed") < flush
    assert flush < script.index("keep_copy() {")


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


def test_monthly_and_yearly_copies_are_kept_once_and_never_overwritten() -> None:
    """R2 的 lifecycle 只能按「前缀 + 上传后天数」删，所以月 / 年备份要复制到独立前缀。

    ⚠️ 已存在就绝不覆盖：lifecycle 按上传时间计天数，覆盖一次等于重新计时，还会把
    月初那份快照换成更晚的。⚠️ 存在与否用 list 判断、且查询出错直接停 —— 用 head-object
    的失败当「不存在」的话，一次网络抖动就会去覆盖。
    """
    script = uncommented(BACKUP)
    assert 'keep_copy "monthly/billing-${MYT_MONTH}.sql.enc"' in script
    assert 'keep_copy "yearly/billing-${MYT_MONTH:0:4}.sql.enc"' in script
    body = script[script.index("keep_copy() {") :]
    assert body.index("list-objects-v2") < body.index("already exists") < body.index("s3 cp")
    assert "cannot check whether" in body
    # ⚠️ KeyCount 在 aws-cli 分页后恒为 None —— 用它判断，月备份永远建不出来（本地演练踩到）。
    assert "KeyCount" not in body
    assert 'if [ "$found" = "$key" ]; then' in body
    # ⚠️ 「查 + 复制」不是原子的，两轮同时跑会互相覆盖（Codex #51 R1）。锁必须在脚本里、
    # 在第一次查之前拿到 —— cron 行上的 flock 管不到手工运行。
    lock = script.index("flock -w 300 9")
    assert script.index('exec 9>"${BACKUP_DIR}/.keep-copy.lock"') < lock
    assert lock < script.index('keep_copy "monthly/')


def test_the_month_of_a_backup_is_taken_in_malaysian_time_without_tzdata() -> None:
    """03:17 那一轮是 UTC 前一天 19:17 —— 按 UTC 归月，月备份会晚一天、多带一天数据。

    ⚠️ 不用 `TZ=Asia/Kuala_Lumpur`：主机缺 tzdata 时它静默回落成 UTC。
    """
    script = uncommented(BACKUP)
    assert 'MYT_MONTH="$(date -u -d "@$((NOW + 8 * 3600))" +%Y%m)"' in script
    assert "TZ=" not in script


def test_restore_never_falls_back_to_full_only_on_its_own() -> None:
    """⚠️ binlog 链不全时自动只恢复全量，会在推送早已坏掉的那一天静默丢掉一整天的数据。

    所以只恢复全量必须显式开启，而且不能与停止时间同时给。
    """
    script = uncommented(RESTORE)
    assert 'FULL_ONLY="${BILLING_RESTORE_FULL_ONLY:-0}"' in script
    assert "it cannot stop at" in script
    # 链检查与重放都只在显式开启时才跳过，默认仍然拒绝不完整的链。
    assert 'if [ "$FULL_ONLY" = "1" ]; then' in script
    assert 'if [ "$FULL_ONLY" = "0" ]; then' in script
    assert "gap in the binlog chain" in script


def test_restore_keeps_its_own_writes_out_of_the_binlog() -> None:
    """⚠️ 演练就在生产那台 MySQL 上做。

    导入写进 binlog 的话，下一次恢复进同名演练库时，重放会把上一次的建库与导入再放
    一遍。变异验证过：去掉这两处，第二次演练在重放时失败（ERROR 1007）。
    """
    script = uncommented(RESTORE)
    assert "SET sql_log_bin=0; CREATE DATABASE" in script
    assert "printf 'SET sql_log_bin=0;\\n'; cat \"$PLAIN\"" in script


# --- 生产巡检（§94 磁盘告警 / §95 告警通道；T0.9）-----------------------------


def test_the_monitor_reports_each_dimension_to_its_own_check() -> None:
    """⚠️ 三个维度合成一个检查时，**第二个故障不会有通知**。

    外部服务只在状态翻转时通知人：磁盘先红了之后 MySQL 再挂，检查早已是 down，
    不会再翻转一次。三个各自一个检查，才能各自翻转、各自恢复。
    """
    monitor = uncommented(MONITOR)
    urls = (
        "BILLING_HEALTHCHECK_SERVICES_URL",
        "BILLING_HEALTHCHECK_READYZ_URL",
        "BILLING_HEALTHCHECK_DISK_URL",
    )
    for url in urls:
        assert f"publish {url} " in monitor
    assert len(set(urls)) == 3
    # 不复用备份那两个检查：频率与含义都不是一回事。
    for taken in ("BILLING_HEALTHCHECK_BACKUP_URL", "BILLING_HEALTHCHECK_BINLOG_URL"):
        assert taken not in monitor


def test_the_monitor_pings_even_when_everything_is_fine() -> None:
    """⚠️ dead man's switch：全绿时不 ping 的话，**cron 没跑 / VPS 挂了**无人知晓。

    那正是最需要告警的场景，而它不写任何日志。
    """
    monitor = uncommented(MONITOR)
    assert 'heartbeat "$url_name" "" "$ok_summary"' in monitor
    assert 'heartbeat "$url_name" /fail "$problems"' in monitor


def test_a_failed_monitor_ping_does_not_change_the_verdict() -> None:
    """⚠️ 监控服务抖动不该被记成「生产有问题」，否则告警自己成了噪声源。"""
    monitor = uncommented(MONITOR)
    assert '|| log "heartbeat ping failed' in monitor
    assert "curl -fsS -m 10" in monitor


def test_the_monitor_carries_the_named_redis_alert() -> None:
    """⚠️ `billing_readiness_degraded_redis` 是 runbook 与告警之间的**稳定契约**。

    而且判据不能只看状态码：Redis 不可用时 `/readyz` **刻意**返回 200
    （app/api/health.py 写了理由），所以必须读响应体。
    """
    monitor = uncommented(MONITOR)
    assert "billing_readiness_degraded_redis" in monitor
    assert '*\'"status":"ok"\'*' in monitor
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    assert "billing_readiness_degraded_redis" in runbook


def test_the_monitor_watches_every_service_that_has_a_probe() -> None:
    """⚠️ beat 崩掉是本平台最安静的故障：探针早就有了，缺的是**把 unhealthy 送出去**。

    盯的名单必须与 compose 里配了 healthcheck 的服务完全一致 —— 少写一个，
    就等于那个服务永远不会告警，而且没有任何迹象。
    """
    monitor = uncommented(MONITOR)
    watched: set[str] = set()
    for name in ("SERVICES_P1", "SERVICES_P2"):
        found = re.search(rf'{name}="\$\{{[A-Z0-9_]+:-([^}}]+)\}}"', monitor)
        assert found is not None, name
        watched |= set(found.group(1).split())

    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    with_probe: set[str] = set()
    current = None
    for line in compose.splitlines():
        if re.fullmatch(r"  [a-zA-Z_][a-zA-Z0-9_-]*:", line):
            current = line.strip().rstrip(":")
        elif line.strip() == "healthcheck:" and current is not None:
            with_probe.add(current)
    assert watched == with_probe, f"watched={watched} probed={with_probe}"
    assert "celery-beat" in watched


def test_the_monitor_alerts_before_the_disk_is_full() -> None:
    """⚠️ §94 明写：日志撑爆磁盘必须在**威胁到 MySQL / 文档存储之前**告警。

    阈值贴着 100% 的话这条告警就没有意义了 —— 它响的时候已经来不及了。
    """
    monitor = uncommented(MONITOR)
    warn = re.search(r"BILLING_MONITOR_DISK_WARN_PERCENT:-(\d+)", monitor)
    crit = re.search(r"BILLING_MONITOR_DISK_CRIT_PERCENT:-(\d+)", monitor)
    assert warn is not None and crit is not None
    assert int(warn.group(1)) <= 85
    assert int(warn.group(1)) < int(crit.group(1)) <= 95
    # ⚠️ `df` 不带 -P：长设备名会换行，字段跟着错位。
    assert "df -P" in monitor
    assert not re.search(r"df (?!-P)", monitor)


def test_the_monitor_confirms_a_problem_before_it_wakes_anyone() -> None:
    """⚠️ 一次正常部署看起来和故障一模一样：换版本时容器有半分钟不是 healthy。

    不复核的话每次部署都误报一条 —— 而被误报训练过的人不会再看告警。
    """
    monitor = uncommented(MONITOR)
    recheck = re.search(r"BILLING_MONITOR_RECHECK_SECONDS:-(\d+)", monitor)
    assert recheck is not None and int(recheck.group(1)) >= 30
    assert 'sleep "$RECHECK_SECONDS"' in monitor
    # 复核必须把三个检查**原样再跑一遍**，不能只重试失败的那一个：
    # 第一轮红、第二轮绿的服务如果不重跑，就会带着过期的结论去 ping。
    after = monitor.split('sleep "$RECHECK_SECONDS"', 1)[1]
    for check in ("check_services", "check_readyz", "check_disk"):
        assert f"$({check} || true)" in after


def test_the_monitor_never_sources_the_env_file() -> None:
    """⚠️ `.env` 里有数据库口令与主密钥口令：source 它等于执行里面的内容。"""
    monitor = uncommented(MONITOR)
    assert 'sed -n "s/^${1}=//p" "$ENV_FILE"' in monitor
    assert "source " not in monitor
    assert not re.search(r"^\s*\.\s+\"?\$ENV_FILE", monitor, re.MULTILINE)


def test_the_cron_runs_the_monitor_unlocked() -> None:
    """⚠️ 巡检是只读的，重叠无害；而 flock 会让一次卡死把之后每一轮都挡在脚本外面。

    那种状态下心跳照样停发 —— 但没有任何一轮真的检查过，日志里也看不出为什么。
    """
    lines = [line for line in uncommented(CRON).splitlines() if "monitor.sh" in line]
    assert len(lines) == 1
    assert lines[0].startswith("*/5 * * * *")
    assert "flock" not in lines[0]
    assert "logger -t billing-monitor" in lines[0]


# --- 旧镜像的保留窗口（§94 磁盘；2026-09-16 生产实测）-------------------------


def test_the_deploy_script_bounds_how_many_old_images_it_keeps() -> None:
    """⚠️ `docker image prune -f` **只清悬空镜像**，清不掉带 commit SHA 标签的旧版本。

    这条曾经是生产上一个安静的洞：脚本里写着「清掉悬空镜像……不清的话它会慢慢
    把磁盘吃光」，而它清的恰恰不是在长的那一类。2026-09-16 实测：8 个版本的
    api + frontend 镜像堆在 VPS 上，只有 1 个在跑，占掉约 2.4 GB。
    所以成功分支里除了 prune，还必须有一个**有界**的保留窗口。
    """
    script = uncommented(SCRIPT)
    success = script.split('if [ "$HEALTHY" = "1" ]', 1)[1].split("exit 0", 1)[0]
    assert "docker image prune -f" in success
    assert "prune_old_images" in success, "成功部署后没有回收旧版本镜像"

    keep = re.search(r"BILLING_IMAGE_KEEP:-(\d+)", script)
    assert keep is not None, "保留个数必须可配"
    # ⚠️ 下限是 2 而不是 1：留 1 个就等于把回滚目标删了。
    assert int(keep.group(1)) >= 2
    assert '[ "$keep" -ge 2 ] || return 0' in script
    # ⚠️ 一个值只能解析一次。`test` 按十进制读、算术展开按八进制读，
    # 两处各自解析就会在 `08` 上分岔（一个放行、一个报错）。
    assert "keep=$(( 10#$IMAGE_KEEP ))" in script
    # ⚠️ 往返比对：64 位溢出不报错，而回绕可以落在很小的正数上（
    # `10#18446744073709551619` = 3）—— 那会把镜像真的删掉。
    assert '[ "$keep" = "$digits" ] || return 0' in script
    # ⚠️ 窗口算法不得再做 `keep + 1`：`keep` 取到 2^63-1 时那个加法溢出成
    # 负数，`tail` 会报错到 stderr，而往返比对拦不住它（2^63-1 是合法十进制）。
    assert "keep + 1" not in script
    assert "awk -v k=\"$keep\" 'NR > k'" in script


def test_a_failed_image_cleanup_never_fails_a_successful_deploy() -> None:
    """⚠️ 清理是**善后**，不是部署的一部分。

    删镜像失败（比如被别的项目的容器引用着）就让整个部署红掉的话，人会被叫醒
    去处理一件对线上毫无影响的事 —— 而下一次他就会开始忽略部署失败。
    """
    script = uncommented(SCRIPT)
    remove = [line for line in script.splitlines() if "docker image rm" in line]
    assert len(remove) == 1
    assert "|| log" in remove[0] or remove[0].rstrip().endswith("\\")
    assert "die" not in remove[0]


# --- prune_old_images 的行为（拿假 docker 真跑一遍）---------------------------
#
# ⚠️ 本文件其余用例钉的是「脚本不许长成什么样」，这一组不一样：它**真的执行**
# prune_old_images。理由是这个函数会删东西 —— 排序排反了、整词匹配写错了，静态
# 断言一个都看不出来，而后果是把正在跑的镜像删掉。写它的时候就真踩了一个：
# `docker ps` 的多行输出没 `tr` 成一行，`case " $in_use "` 那道防线形同虚设。

FAKE_DOCKER = """#!/usr/bin/env bash
case "$1 $2" in
    "ps -a") cat "$FAKE_STATE/in_use" ;;
    "images --filter")
        # 枚举失败的开关（比如 docker daemon 此刻不应答）。
        [ -e "$FAKE_STATE/fail_images" ] && { echo "boom" >&2; exit 1; }
        ref="${3#reference=}"
        repo="${ref%:*}"
        while IFS=$'\\t' read -r created image; do
            [ "${image%:*}" = "$repo" ] || continue
            printf '%s\\t%s\\n' "$created" "$image"
        done < "$FAKE_STATE/images"
        ;;
    "image rm")
        # 真 docker 也会拒绝删在用的镜像。这里照样拒绝，好让「防线漏了」这件事
        # 在用例里以「多出一条 could not remove」的形式暴露出来。
        grep -qxF "$3" "$FAKE_STATE/in_use" && exit 1
        echo "$3" >> "$FAKE_STATE/removed"
        ;;
    *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

# ⚠️ 先 `cd` 再用 `$PWD` 拼 PATH，不能直接拿传进来的路径：
# pytest 的 tmp_path 在 Windows 上长成 `C:/...`，而 PATH 是**冒号**分隔的 ——
# 盘符后面那个冒号会把条目切成两截，假 docker 就找不到了。
# git-bash 里 `cd` 之后的 `$PWD` 是 `/c/...`，没有这个问题。
RUNNER = """#!/usr/bin/env bash
set -euo pipefail
cd "$1"
export FAKE_STATE="$PWD"
export PATH="$PWD/bin:$PATH"
log() { printf 'LOG %s\\n' "$*"; }
source "$PWD/fn.sh"
prune_old_images
# ⚠️ 这一行是哨兵：runner 开着 `set -euo pipefail`，和 `deploy.sh` 一样。
# 清理里任何未被兜住的失败都会把脚本提前提掉，这句就打不出来。
echo "DEPLOY CONTINUES"
"""

IMAGES = """2026-09-16 12:53:01 +0800 +08\tghcr.io/o/r:new3
2026-09-16 02:00:00 +0800 +08\tghcr.io/o/r:new2
2026-09-15 10:00:00 +0800 +08\tghcr.io/o/r:new1
2026-09-14 10:00:00 +0800 +08\tghcr.io/o/r:old1
2026-09-13 10:00:00 +0800 +08\tghcr.io/o/r:old2
2026-09-12 10:00:00 +0800 +08\tghcr.io/o/r:pinned
2026-09-16 12:53:01 +0800 +08\tghcr.io/o/r-frontend:new3
2026-09-14 10:00:00 +0800 +08\tghcr.io/o/r-frontend:old1
"""

# ⚠️ `pinned` 排在最老，却被一个容器引用着 —— 窗口算法一旦忘了查在用，
# 它就是第一个被删的。`mysql:8.4` 是**别的项目**的容器，用来钉住「查的是这台机器
# 上所有容器，不只是本栈」。
IN_USE = "ghcr.io/o/r:pinned\nmysql:8.4\n"


WORKER_NO_BASH_REASON = (
    "OpenClaw Worker runs checks with a PATH of only the pinned Python directory and System32, "
    "which has no bash; skipped only when ACUVEN_GIT_LS_FILES_MANIFEST is set and bash is "
    "missing, local and CI run this case"
)


def require_bash() -> str:
    """Return bash for the cases that run deploy.sh functions for real.

    ⚠️ 只在 Worker 模式（设了 `ACUVEN_GIT_LS_FILES_MANIFEST`）**且**确实找不到 bash 时 skip。
    本地没装 bash 仍然直接失败 —— 那是开发机缺工具，不该被静默跳过；CI 不设该变量，
    这些用例照常全跑。
    """
    bash = shutil.which("bash")
    if bash is None and "ACUVEN_GIT_LS_FILES_MANIFEST" in os.environ:
        pytest.skip(WORKER_NO_BASH_REASON)
    assert bash is not None, "需要 bash（CI 是 ubuntu-latest，本地用 git-bash）"
    return bash


def test_missing_bash_is_skipped_only_in_worker_mode(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("ACUVEN_GIT_LS_FILES_MANIFEST", "manifest")

    with pytest.raises(pytest.skip.Exception, match="ACUVEN_GIT_LS_FILES_MANIFEST"):
        require_bash()


def test_missing_bash_still_fails_outside_worker_mode(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delenv("ACUVEN_GIT_LS_FILES_MANIFEST", raising=False)

    with pytest.raises(AssertionError, match="需要 bash"):
        require_bash()


def test_present_bash_is_used_even_in_worker_mode(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setenv("ACUVEN_GIT_LS_FILES_MANIFEST", "manifest")

    assert require_bash() == "/usr/bin/bash"


def run_prune(tmp_path, fail_images: bool = False, **env):
    """把 deploy.sh 里的 prune_old_images 原样抠出来跑，返回被删掉的镜像列表。"""
    bash = require_bash()

    body = re.search(
        r"^prune_old_images\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.M | re.S
    )
    assert body is not None, "deploy.sh 里找不到 prune_old_images"

    state = tmp_path / "state"
    (state / "bin").mkdir(parents=True)

    # ⚠️ `newline="\\n"` 一个都不能漏。Windows 上默认会写成 CRLF，
    # 于是 `cut` 切出来的镜像名拖着一个 `\\r` —— `[ "$img" = "$repo:$TAG" ]`
    # 永远不成立，而跟着 CRLF 的 in_use 比却恰好成立。结果是用例在 Windows
    # 上假报、在 CI 上又是绿的 —— 比它直接坏掉还难查（2026-09-16 真踩了）。
    def put(rel: str, text: str) -> Path:
        target = state / rel
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    put("fn.sh", body.group(0) + "\n")
    put("images", IMAGES)
    put("in_use", IN_USE)
    put("removed", "")
    fake = put("bin/docker", FAKE_DOCKER)
    fake.chmod(0o755)
    runner = put("run.sh", RUNNER)
    if fail_images:
        put("fail_images", "")

    full = {**os.environ, "TAG": "", "PREVIOUS_IMAGE": "", "IMAGE_KEEP": "3", **env}
    full.pop("BILLING_IMAGE_REPO", None)
    if "BILLING_IMAGE_REPO" in env:
        full["BILLING_IMAGE_REPO"] = env["BILLING_IMAGE_REPO"]

    done = subprocess.run(
        [bash, str(runner), str(state)],
        env=full,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    # ⚠️ stderr 必须是**空**的。函数里每一条 docker 调用都自带 `2>/dev/null`，
    # 所以这里出现的任何东西都是 **shell 自己**的报错。光看退出码不够 ——
    # bash 碍于算术展开出错时会把函数**惄无声息地提前结束**、返回 0，
    # 于是清理根本没做而一切看起来都正常（`IMAGE_KEEP=08` 就是这个样子）。
    assert done.stderr == "", done.stderr
    # ⚠️ 清理是**善后**，不是部署的一部分：无论里面出什么事，调用方都必须
    # 能继续往下走。这条断言对每一个场景都生效，不只是枚举失败那一个。
    assert "DEPLOY CONTINUES" in done.stdout, done.stdout
    # ⚠️ 顺带钉死日志：漏查在用镜像时这里会冒出 could not remove。
    assert "could not remove" not in done.stdout, done.stdout
    return [line for line in (state / "removed").read_text(encoding="utf-8").splitlines() if line]


def test_the_window_keeps_the_newest_and_drops_the_rest(tmp_path) -> None:
    """留最近 3 个，更老的删掉 —— 但在跑的那个一个都不许动。"""
    removed = run_prune(
        tmp_path,
        TAG="new3",
        PREVIOUS_IMAGE="ghcr.io/o/r:new2",
        IMAGE_KEEP="3",
        BILLING_IMAGE_REPO="ghcr.io/o/r",
    )
    assert removed == ["ghcr.io/o/r:old1", "ghcr.io/o/r:old2"]
    # frontend 仓库只有 2 个，还没满窗口，所以一个都不该删。
    assert not [r for r in removed if "-frontend" in r]


def test_an_image_a_container_still_uses_is_never_removed(tmp_path) -> None:
    """⚠️ `pinned` 是**最老**的一个，却有容器在用。

    真 docker 会拒绝删它，所以这里删不掉不等于安全 —— 危险的是那串报错会淹掉
    部署日志，而且说明「在用」这道过滤根本没生效。
    """
    removed = run_prune(
        tmp_path,
        TAG="new3",
        PREVIOUS_IMAGE="ghcr.io/o/r:new2",
        IMAGE_KEEP="3",
        BILLING_IMAGE_REPO="ghcr.io/o/r",
    )
    # ⚠️ 光查「不在里面」会被空列表蒙混过去（假 docker 没被找到就是这个样子）。
    assert removed, "一个都没删，这个用例实际什么都没验证"
    assert "ghcr.io/o/r:pinned" not in removed


def test_a_rollback_target_outside_the_window_survives(tmp_path) -> None:
    """⚠️ 回滚到一个很旧的版本之后，**新**镜像才是回滚目标 —— 它必须活着。

    窗口按时间算，而这时候「这一次部署的标签」是老的、「回滚目标」是新的，
    两个都可能落在窗口外。少挡一个，下一次出事就没得退。
    """
    removed = run_prune(
        tmp_path,
        TAG="old2",
        PREVIOUS_IMAGE="ghcr.io/o/r:new3",
        IMAGE_KEEP="2",
        BILLING_IMAGE_REPO="ghcr.io/o/r",
    )
    assert removed, "一个都没删，这个用例实际什么都没验证"
    assert "ghcr.io/o/r:old2" not in removed  # 这一次部署的
    assert "ghcr.io/o/r:new3" not in removed  # 回滚目标
    assert "ghcr.io/o/r:pinned" not in removed  # 在用的


def test_a_failed_enumeration_does_not_sink_a_successful_deploy(tmp_path) -> None:
    """⚠️ `deploy.sh` 开着 `set -euo pipefail`，而这个函数在**部署已经成功**之后才跑。

    枚举镜像那条管道一旦返回非零（daemon 此刻不应答就够了），`set -e` 会把
    一次**健康检查与冒烟都已经过了**的部署扔成失败 —— 人被叫醒去处理一件对
    线上毫无影响的事，而下一次他就会开始忽略部署失败。
    （Codex 审查 PR #62 指出的阻断项；修之前假 docker 一报错，runner 直接 exit 1。）
    """
    assert (
        run_prune(
            tmp_path,
            fail_images=True,
            TAG="new3",
            PREVIOUS_IMAGE="ghcr.io/o/r:new2",
            IMAGE_KEEP="3",
            BILLING_IMAGE_REPO="ghcr.io/o/r",
        )
        == []
    )


def test_rehearsal_mode_removes_nothing(tmp_path) -> None:
    """⚠️ 本地演练（BILLING_IMAGE_REPO 未设）不知道仓库名，必须一个都不碰。"""
    assert run_prune(tmp_path, TAG="new3", IMAGE_KEEP="3") == []


def test_a_keep_count_with_a_leading_zero_is_read_as_decimal(tmp_path) -> None:
    """⚠️ bash 的**算术展开**把 `08` / `09` 当非法八进制，直接报错。

    而 `test` 的 `-ge` 按十进制读 —— 于是 `08` 能一路过完校验，再在 `$(( ))` 里炸掉。
    ⚠️ 而它**不**会把部署弄成红的：bash 把函数惄无声息地提前结束并返回 0，
    清理根本没做而一切看起来都正常 —— 比直接失败难查得多。所以 `run_prune`
    钉的是 **stderr 为空**，光看退出码这个变异活得好好的。
    `010` 又是另一种坏法：不报错，静默地变成 8。（Codex 审查 PR #62 R2）

    夹具里 `ghcr.io/o/r` 有 6 个版本，所以 `08` / `09` 的窗口比它们还宽 ——
    一个都不该删，**但也一定不能崩**。
    """
    for keep in ("08", "09"):
        assert (
            run_prune(
                tmp_path / f"wide{keep}",
                TAG="new3",
                PREVIOUS_IMAGE="ghcr.io/o/r:new2",
                IMAGE_KEEP=keep,
                BILLING_IMAGE_REPO="ghcr.io/o/r",
            )
            == []
        ), keep

    # `04` 要真的当成 4：窗口 = new3 / new2 / new1 / old1，`old2` 掉出去、
    # `pinned` 也掉出去但被容器引用着 —— 所以恰好只删一个。
    assert run_prune(
        tmp_path / "four",
        TAG="new3",
        PREVIOUS_IMAGE="ghcr.io/o/r:new2",
        IMAGE_KEEP="04",
        BILLING_IMAGE_REPO="ghcr.io/o/r",
    ) == ["ghcr.io/o/r:old2"]


def test_a_keep_count_that_overflows_removes_nothing(tmp_path) -> None:
    """⚠️ bash 的整数是 64 位，**溢出不报错**，而回绕可以落在一个很小的正数上。

    `10#18446744073709551619` = **3**。一个「想多留点」的配置于是变成只留 3 个，
    而且是**真的去删** —— 这是这个函数里唯一一类不可逆的后果。

    ⚠️ 光抽样几个大数是不够的：`99999999999999999999` 回绕成巨大正数（窗口大到
    删不着任何东西）、`9223372036854775808` 回绕成负数（被 `-ge 2` 挡掉），
    两个恰好都安全 —— 只按它们下结论会以为溢出都无害。（Codex 审查 PR #62 R3）
    """
    for keep in (
        "18446744073709551619",  # 2**64 + 3 → 回绕成 3，会真的删
        "18446744073709551621",  # 2**64 + 5 → 回绕成 5
        "99999999999999999999",  # 回绕成巨大正数
        "9223372036854775808",  # 2**63 → 回绕成负数
        # ⚠️ 这一个**能过往返比对**（它本身是合法十进制），卡的是下一道：
        # 窗口算法不得再做 `keep + 1` 那种会溢出的算术。
        "9223372036854775807",  # 2**63-1 → keep+1 溢出成负数
    ):
        assert (
            run_prune(
                tmp_path / f"of{keep}",
                TAG="new3",
                PREVIOUS_IMAGE="ghcr.io/o/r:new2",
                IMAGE_KEEP=keep,
                BILLING_IMAGE_REPO="ghcr.io/o/r",
            )
            == []
        ), keep


def test_a_nonsense_keep_count_removes_nothing(tmp_path) -> None:
    """⚠️ 配歪了宁可不清 —— 「清多了」在这里是不可逆的。"""
    for keep in ("1", "0", "abc", ""):
        assert (
            run_prune(
                tmp_path / keep if keep else tmp_path / "empty",
                TAG="new3",
                PREVIOUS_IMAGE="ghcr.io/o/r:new2",
                IMAGE_KEEP=keep,
                BILLING_IMAGE_REPO="ghcr.io/o/r",
            )
            == []
        ), keep


# --- 日志外送（§94 保留期 / 磁盘上限 / 安全删除 / 异地留存，§112；T0.9）--------


def test_the_log_archive_is_encrypted_before_it_leaves_the_host() -> None:
    """⚠️ 日志里有脱敏之后仍能拼出业务轮廓的东西（租户、路径、金额量级）。

    与备份同一条（决策 ⑦a）：**自己加密**，不依赖 R2 的服务端加密 —— 那样密钥在
    Cloudflare 手里，归档对它是明文。
    """
    ship = uncommented(LOG_SHIP)
    assert "openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt" in ship
    # 上传的一定是密文，不是打包出来的明文 tar
    assert 's3 cp "/data/$(basename "$CIPHER")"' in ship
    assert 's3 cp "/data/$(basename "$TAR")"' not in ship


def test_the_log_archive_round_trips_before_upload() -> None:
    """⚠️ 一个配错口令产出的归档，和好的那个长得一模一样。

    而需要日志的那天通常没有第二份 —— 所以解回来逐字节比对之后才允许上传。
    """
    ship = uncommented(LOG_SHIP)
    assert "openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000" in ship
    assert 'cmp -s "$TAR" "$VERIFY"' in ship
    assert "NOT uploading" in ship
    # 验证必须排在上传之前
    assert ship.index('cmp -s "$TAR" "$VERIFY"') < ship.index("s3 cp ")


def test_log_shipping_refuses_to_be_local_only() -> None:
    """⚠️ 没配 R2 时**不能**静默跳过：只留在本机的日志不满足 §94 的异地留存，

    而机器没了的那一刻，正是最需要那份日志的时候。
    """
    ship = uncommented(LOG_SHIP)
    assert "does NOT satisfy spec §94" in ship


def test_the_log_window_only_advances_after_a_confirmed_upload() -> None:
    """⚠️ 提前推进状态的话，一次失败的外送会让那段窗口**再也不会被抓第二次** ——

    而它多半正是出问题的那一段。
    """
    ship = uncommented(LOG_SHIP)
    assert 'printf \'%s\\n\' "$NOW" > "$STATE_FILE"' in ship
    assert ship.index("uploaded and confirmed") < ship.index('> "$STATE_FILE"')
    # head-object 核对大小：cp 返回 0 只说明客户端认为它发完了
    assert "head-object" in ship
    assert "size mismatch after upload" in ship


def test_the_log_window_is_clamped() -> None:
    """⚠️ 状态文件丢了的时候，没有上限就会一口气去抓几个月 —— 一次例行外送变成一次事故。"""
    ship = uncommented(LOG_SHIP)
    hours = re.search(r"BILLING_LOG_MAX_WINDOW_HOURS:-(\d+)", ship)
    assert hours is not None and int(hours.group(1)) <= 24 * 14
    assert 'SINCE_EPOCH="$OLDEST"' in ship


def test_log_retention_and_cap_are_both_enforced() -> None:
    """⚠️ 保留期管「多久」，总量上限管「最坏占多少」——**两个都要**。

    日志量突然放大时只有上限拦得住；而只有上限的话，安静的几个月会把保留期变成空话。
    ⚠️ 顺序必须是先按保留期删、再按总量删：反过来会让一次暴涨挤掉还在保留期内的归档。
    """
    ship = uncommented(LOG_SHIP)
    days = re.search(r"BILLING_LOG_RETENTION_DAYS:-(\d+)", ship)
    cap = re.search(r"BILLING_LOG_ARCHIVE_CAP_MB:-(\d+)", ship)
    assert days is not None and int(days.group(1)) >= 30  # 决策 ⑤：暂定 30 天
    assert cap is not None and int(cap.group(1)) > 0
    assert ship.index('-mtime +"$RETENTION_DAYS"') < ship.index("over its cap")
    # 撞上限是异常，要单独以 err 级别报出来
    assert "logger -p user.err -t billing-logs" in ship
    # ⚠️ 绝不删本轮刚传上去的那一份
    assert '[ "$old" = "${CIPHER:-}" ] && continue' in ship


def test_log_archives_are_deleted_securely() -> None:
    """⚠️ §112 要求安全删除：`rm` 只摘掉目录项，数据块还在盘上。

    脚本里**每一处**删除归档或明文都要走 secure_rm，漏一处就等于没做。
    """
    ship = uncommented(LOG_SHIP)
    assert "shred -u -n 1" in ship
    for needle in ('secure_rm "$old"', 'secure_rm "$TAR"'):
        assert needle in ship
    # 明文 tar 与解密出来的校验文件都不许用裸 rm 留在盘上
    assert 'rm -f "$TAR"' not in ship
    assert 'rm -f "$VERIFY"' not in ship


def test_the_plaintext_logs_never_survive_the_run() -> None:
    """⚠️ 明文日志落盘的那一段是这个脚本最脆弱的窗口 —— 失败路径也要清掉。"""
    ship = uncommented(LOG_SHIP)
    assert "trap cleanup EXIT" in ship
    cleanup = ship[ship.index("cleanup() {") : ship.index("trap cleanup EXIT")]
    assert "$WORK" in cleanup and "$TAR" in cleanup and "$VERIFY" in cleanup


def test_shipped_logs_carry_timestamps_per_service() -> None:
    """⚠️ 没有时间戳的日志在事故调查里几乎没用（对不上时间线、拼不起多个服务）。

    而混成一个文件的话，某个服务刷屏会把别人淹掉。
    """
    ship = uncommented(LOG_SHIP)
    assert "--timestamps" in ship
    assert '> "$out"' in ship
    services = re.search(r'SERVICES="\$\{[A-Z0-9_]+:-([^}]+)\}"', ship)
    assert services is not None
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    declared = {
        line.strip().rstrip(":")
        for line in compose.splitlines()
        if re.fullmatch(r"  [a-zA-Z_][a-zA-Z0-9_-]*:", line)
        and line.strip().rstrip(":") not in {"build", "environment", "secrets", "depends_on"}
    }
    shipped = set(services.group(1).split())
    # 七个服务一个都不能漏：漏掉的那个出事时没有任何历史可查
    assert shipped <= declared
    assert len(shipped) == 7


def test_an_empty_window_is_reported_but_not_a_failure() -> None:
    """⚠️ 一台没人访问的机器就是零行。让它失败会把「安静的周末」变成每周一次的假警报，

    而被假警报训练过的人不会再看告警。
    """
    ship = uncommented(LOG_SHIP)
    assert "WARNING: every service returned zero lines" in ship
    assert 'die "every service' not in ship


def test_the_cron_runs_log_shipping_after_the_full_backup() -> None:
    """⚠️ 两者都要停 I/O、都要用 R2，撞在同一分钟只会互相拖慢。"""
    # ⚠️ 不能只匹配 `log_ship.sh`：`binlog_ship.sh` 的结尾正好是同一串。
    lines = [line for line in uncommented(CRON).splitlines() if "deploy/log_ship.sh" in line]
    assert len(lines) == 1
    assert lines[0].startswith("47 3 * * *")
    assert "flock" in lines[0]
    assert "logger -t billing-logs" in lines[0]
    backup = [line for line in uncommented(CRON).splitlines() if "backup.sh" in line][0]
    assert int(backup.split()[0]) < int(lines[0].split()[0])


def test_retention_runs_on_the_failure_path_too() -> None:
    """⚠️ 失败的那一轮照样在本机留下一份加密归档（Codex #64 R1）。

    R2 挂一周就攒一周，没有任何东西会去删它们 —— 而「日志把磁盘撑爆、威胁到 MySQL」
    正是 §94 要防的那件事。所以 `die` 也要清理，且清理本身出错不能盖住真正的失败原因。
    """
    ship = uncommented(LOG_SHIP)
    die_body = ship[ship.index("die() {") : ship.index("used_mb() {")]
    assert "enforce_retention || true" in die_body
    # 成功路径那次调用仍在（独立一行，不带 `|| true`）
    assert re.search(r"(?m)^enforce_retention$", ship)
    # 本轮刚产出的那份在两条路上都不许被上限逻辑删掉
    assert '[ "$old" = "${CIPHER:-}" ] && continue' in ship


# --- 回滚的两个缺口（T0.9）----------------------------------------------------


def test_rollback_takes_the_frontend_with_it() -> None:
    """⚠️ 前端是**独立的镜像**。只换回后端的话，栈会停在「后端旧、前端新」——

    两个容器都健康、没有任何报错，只有用户点到某个新页面才发现它在打一个不存在的接口。
    """
    script = uncommented(SCRIPT)
    rollback = script[script.index('log "rolling back to $PREVIOUS_IMAGE"') :]
    up = rollback.index("$COMPOSE up -d --no-build")
    # 两个镜像都必须在 `compose up` **之前**导出
    assert rollback.index('export BILLING_IMAGE="$PREVIOUS_IMAGE"') < up
    assert rollback.index('export BILLING_FRONTEND_IMAGE="$PREVIOUS_FRONTEND_IMAGE"') < up
    # 部署开始时就记下前端在跑的是什么（和后端同一时刻，改动之前）
    assert "PREVIOUS_FRONTEND_IMAGE=\"$($COMPOSE ps --format '{{.Image}}' frontend" in script
    assert script.index("PREVIOUS_FRONTEND_IMAGE=") < script.index("$COMPOSE pull")


def test_the_frontend_rollback_target_has_a_fallback() -> None:
    """⚠️ 前端容器此刻可能压根没起来（`ps` 列不出停掉的），那样就推不出回滚目标。

    两个镜像同一个 commit 构建，所以按后端那一版的标签推。⚠️ 连这条都推不出来时
    （演练模式没有仓库名）必须**说出来**，不能让人以为整栈都回去了。
    """
    script = uncommented(SCRIPT)
    # ⚠️ 钉整条赋值，不只钉那个后缀展开：只查子串的话，把赋值换成别的语句
    # （值还在字符串里）照样过 —— 变异验证时真漏过一次。
    assert (
        'PREVIOUS_FRONTEND_IMAGE="${BILLING_FRONTEND_IMAGE_REPO:-${BILLING_IMAGE_REPO}-frontend}'
        ':${PREVIOUS_IMAGE##*:}"'
    ) in script
    assert "WARNING: no previous frontend image is known" in script


def test_the_last_good_deploy_is_recorded_only_after_it_is_proven() -> None:
    """⚠️ 记早了的话，一次失败的部署会把「上一个好的」覆盖成那个坏的 ——

    而这份记录正是出事那天用来填 `ref` 的唯一依据。
    """
    script = uncommented(SCRIPT)
    success = script[script.index('if [ "$HEALTHY" = "1" ] && [ "$SMOKE" = "1" ]; then') :]
    assert "record_last_good" in success.split("exit 0")[0]
    # 失败路径不许写这份记录：回滚回去的那个**就是**记录里的 last good
    rollback = script[script.index('log "rolling back to $PREVIOUS_IMAGE"') :]
    assert "record_last_good" not in rollback


def test_the_last_good_record_is_written_atomically() -> None:
    """⚠️ 直接覆写时，写到一半被打断就留下一个残缺的记录 —— 而它是出事时唯一的依据。"""
    script = uncommented(SCRIPT)
    assert 'tmp="${DEPLOY_STATE}.tmp"' in script
    assert 'mv -f "$tmp" "$DEPLOY_STATE"' in script


RECORD_RUNNER = """#!/usr/bin/env bash
set -euo pipefail
cd "$1"
log() { printf 'LOG %s\\n' "$*"; }
source "$PWD/fn.sh"
record_last_good
# ⚠️ 哨兵：runner 和 deploy.sh 一样开着 `set -euo pipefail`。记录这一步里任何
# 没被兜住的失败都会把脚本提前提掉，这句就打不出来 —— 而它绝不能让一次
# **已经成功**的部署变成失败。
echo "DEPLOY CONTINUES"
"""


def run_record_last_good(tmp_path, state_path: str, **env):
    """把 deploy.sh 里的 record_last_good 原样抠出来跑。"""
    bash = require_bash()

    body = re.search(
        r"^record_last_good\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.M | re.S
    )
    assert body is not None, "deploy.sh 里找不到 record_last_good"

    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "fn.sh").write_text(body.group(0) + "\n", encoding="utf-8", newline="\n")
    runner = work / "run.sh"
    runner.write_text(RECORD_RUNNER, encoding="utf-8", newline="\n")

    full = {
        **os.environ,
        "TAG": "deadbeef",
        "BILLING_IMAGE": "ghcr.io/o/r:deadbeef",
        "BILLING_FRONTEND_IMAGE": "ghcr.io/o/r-frontend:deadbeef",
        "DEPLOY_STATE": state_path,
        **env,
    }
    return subprocess.run([bash, str(runner), str(work)], env=full, capture_output=True, text=True)


def test_recording_the_last_good_deploy_writes_the_tag_and_both_images(tmp_path) -> None:
    """记录要能直接回答两个问题：回滚填哪个 `ref`，以及那一版的两个镜像是什么。"""
    state = tmp_path / "state" / ".last-good-deploy"
    state.parent.mkdir(parents=True)
    done = run_record_last_good(tmp_path, str(state))
    assert done.returncode == 0, done.stderr
    assert "DEPLOY CONTINUES" in done.stdout
    written = state.read_text(encoding="utf-8")
    assert "tag=deadbeef" in written
    assert "api_image=ghcr.io/o/r:deadbeef" in written
    assert "frontend_image=ghcr.io/o/r-frontend:deadbeef" in written
    assert "deployed_at=" in written
    # 历史文件追加一行：回滚目标本身也坏掉时要往前再找一个
    history = state.parent / ".last-good-deploy-history"
    assert history.exists() and "deadbeef" in history.read_text(encoding="utf-8")
    # 临时文件不留下
    assert not (state.parent / ".last-good-deploy.tmp").exists()


def test_a_failed_recording_never_fails_the_deploy(tmp_path) -> None:
    """⚠️ 部署已经健康、冒烟也过了。**记不下来是个警告，不是一次失败的部署** ——

    把它扔成失败会触发回滚，而那才是真正制造停机的那一步。
    """
    missing = tmp_path / "nope" / "deeper" / ".last-good-deploy"
    done = run_record_last_good(tmp_path, str(missing))
    assert done.returncode == 0, done.stderr
    assert "DEPLOY CONTINUES" in done.stdout
    assert "could not record the last good deploy" in done.stdout
    assert done.stderr == "", f"记录失败时不该有 shell 报错漏出来：{done.stderr!r}"


# --- 部署后手工起栈找得到镜像（T0.9）----------------------------------------


def test_the_deployed_images_are_pinned_only_after_the_deploy_is_proven() -> None:
    """⚠️ 钉早了的话，一次失败的部署会让之后手工的 `compose up` 把坏镜像起回来。"""
    script = uncommented(SCRIPT)
    success = script[script.index('if [ "$HEALTHY" = "1" ] && [ "$SMOKE" = "1" ]; then') :]
    assert "pin_deployed_images" in success.split("exit 0")[0]
    # 回滚路径不钉：`.env` 里留着的恰好就是回滚回去的那一版
    rollback = script[script.index('log "rolling back to $PREVIOUS_IMAGE"') :]
    assert "pin_deployed_images" not in rollback


PIN_RUNNER = """#!/usr/bin/env bash
set -euo pipefail
cd "$1"
log() { printf 'LOG %s\\n' "$*"; }
source "$PWD/fn.sh"
pin_deployed_images
# ⚠️ 哨兵：与 RECORD_RUNNER 同理，钉不下来绝不能让一次已经成功的部署变成失败。
echo "DEPLOY CONTINUES"
"""

ENV_BEFORE = (
    "# production\n"
    "BILLING_MYSQL_ROOT_PASSWORD=s3cr=t with spaces\n"
    "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml\n"
    "\n"
    "BILLING_SMTP_HOST=smtp.example.com"  # 故意没有结尾换行
)


def run_pin_deployed_images(tmp_path, env_path: str, **env):
    """把 deploy.sh 里的 pin_deployed_images 原样抠出来跑。"""
    bash = require_bash()

    body = re.search(
        r"^pin_deployed_images\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.M | re.S
    )
    assert body is not None, "deploy.sh 里找不到 pin_deployed_images"

    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "fn.sh").write_text(body.group(0) + "\n", encoding="utf-8", newline="\n")
    runner = work / "run.sh"
    runner.write_text(PIN_RUNNER, encoding="utf-8", newline="\n")

    full = {
        **os.environ,
        "TAG": "deadbeef",
        "BILLING_IMAGE_REPO": "ghcr.io/o/r",
        "BILLING_IMAGE": "ghcr.io/o/r:deadbeef",
        "BILLING_FRONTEND_IMAGE": "ghcr.io/o/r-frontend:deadbeef",
        "ENV_FILE": env_path,
        **env,
    }
    return subprocess.run([bash, str(runner), str(work)], env=full, capture_output=True, text=True)


def test_pinning_writes_both_images_and_keeps_every_other_line(tmp_path) -> None:
    """`.env` 装着口令：除了这两个键，其余每一行都必须逐字保留。"""
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_BEFORE.encode("utf-8"))
    done = run_pin_deployed_images(tmp_path, str(env_file))
    assert done.returncode == 0, done.stderr
    assert "DEPLOY CONTINUES" in done.stdout
    assert "pinned the deployed images" in done.stdout
    assert env_file.read_text(encoding="utf-8") == (
        ENV_BEFORE + "\n"
        "BILLING_IMAGE=ghcr.io/o/r:deadbeef\n"
        "BILLING_FRONTEND_IMAGE=ghcr.io/o/r-frontend:deadbeef\n"
    )
    assert not (tmp_path / ".env.deploy-tmp").exists()


def test_pinning_again_replaces_the_previous_pin(tmp_path) -> None:
    """第二次部署要**换掉**上一次的两行，而不是再追加两行 —— 重复的键谁生效是没人记得住的事。"""
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_BEFORE.encode("utf-8"))
    assert run_pin_deployed_images(tmp_path, str(env_file)).returncode == 0
    done = run_pin_deployed_images(
        tmp_path,
        str(env_file),
        TAG="cafef00d",
        BILLING_IMAGE="ghcr.io/o/r:cafef00d",
        BILLING_FRONTEND_IMAGE="ghcr.io/o/r-frontend:cafef00d",
    )
    assert done.returncode == 0, done.stderr
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if ln.startswith("BILLING_IMAGE=")] == [
        "BILLING_IMAGE=ghcr.io/o/r:cafef00d"
    ]
    assert [ln for ln in lines if ln.startswith("BILLING_FRONTEND_IMAGE=")] == [
        "BILLING_FRONTEND_IMAGE=ghcr.io/o/r-frontend:cafef00d"
    ]
    assert "BILLING_MYSQL_ROOT_PASSWORD=s3cr=t with spaces" in lines


@pytest.mark.skipif(os.name == "nt", reason="Windows 上没有 POSIX 权限位；CI 在 ubuntu 上跑")
def test_pinning_keeps_the_env_file_private(tmp_path) -> None:
    """⚠️ 换文件（`mv`）时权限不能退回默认的 0644 —— 那等于把口令文件对全机可读。"""
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_BEFORE.encode("utf-8"))
    env_file.chmod(0o600)
    assert run_pin_deployed_images(tmp_path, str(env_file)).returncode == 0
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_rehearsal_mode_leaves_the_env_file_alone(tmp_path) -> None:
    """演练模式没拉镜像：`.env` 一个字节都不许动。"""
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_BEFORE.encode("utf-8"))
    done = run_pin_deployed_images(tmp_path, str(env_file), BILLING_IMAGE_REPO="")
    assert done.returncode == 0, done.stderr
    assert env_file.read_bytes() == ENV_BEFORE.encode("utf-8")


def test_a_failed_pin_never_fails_the_deploy(tmp_path) -> None:
    """钉不下来是警告、不是失败（失败会触发回滚，那才制造停机）；但警告要说清后果。"""
    missing = tmp_path / "nope" / ".env"
    done = run_pin_deployed_images(tmp_path, str(missing))
    assert done.returncode == 0, done.stderr
    assert "DEPLOY CONTINUES" in done.stdout
    assert "could not pin the deployed images" in done.stdout
    assert "re-run deploy/deploy.sh deadbeef" in done.stdout
    assert done.stderr == "", f"钉失败时不该有 shell 报错漏出来：{done.stderr!r}"
    # 不许凭空造出一个只有两行的 `.env`
    assert not missing.exists()


# --- binlog 的本地磁盘上限（§98.1 / T0.9）------------------------------------


def test_the_binlog_budget_is_checked_with_the_disk_dimension() -> None:
    """⚠️ MySQL 8.4 **没有** `binlog_space_limit`（生产那台 8.4.11 实测：变量不存在）。

    它自己只按**时间**清（本地 3 天），三天之内能攒出多少没有上限 —— 而磁盘写满时
    MySQL 直接停止写入。所以总量口径只能在 MySQL 外面盯。
    ⚠️ 并进磁盘那个维度，不另开检查：两条通知指向同一个原因时，人第一反应是「又抽风了」。
    """
    monitor = uncommented(MONITOR)
    budget = re.search(r"BILLING_MONITOR_BINLOG_BUDGET_MB:-(\d+)", monitor)
    assert budget is not None
    # 预算要留在「磁盘告警之前就响」的量级：太大就失去了早期信号的意义
    assert 256 <= int(budget.group(1)) <= 8192
    # 在 check_disk 里调用 —— 也就是走 disk 那条心跳
    disk_body = monitor[monitor.index("check_disk() {") : monitor.index("SERVICES_OUT=")]
    assert "check_binlog" in disk_body


def test_an_unreachable_mysql_does_not_raise_a_binlog_alert() -> None:
    """⚠️ mysql 没起来是**服务维度**的事，在磁盘维度再报一次只是双响。"""
    monitor = uncommented(MONITOR)
    body = monitor[monitor.index("check_binlog() {") : monitor.index("check_disk() {")]
    assert "skipping the budget check" in body
    assert 'if [ -z "$kb" ]; then' in body
    # 跳过的那一支必须 return 0，不能落到下面的比较（`$kb` 为空时算术展开会炸）
    assert body.index('if [ -z "$kb" ]') < body.index("return 0") < body.index("mb=$((kb / 1024))")


def test_local_binlog_retention_outlives_one_full_backup_cycle() -> None:
    """⚠️ 本地保留期短于「全量间隔 + 余量」的话，最近一份全量的 PITR 链会缺前半截。

    全量每天一次，所以至少要留两天；现在留 3 天。
    """
    compose = COMPOSE.read_text(encoding="utf-8")
    expire = re.search(r"BILLING_BINLOG_EXPIRE_SECONDS:-(\d+)", compose)
    assert expire is not None
    assert int(expire.group(1)) >= 2 * 24 * 3600
    size = re.search(r"BILLING_MAX_BINLOG_SIZE:-(\d+)", compose)
    assert size is not None
    # 单文件太大时，「推走已关闭的文件」这件事会被拖很久（离机粒度 = RPO 的一部分）
    assert int(size.group(1)) <= 256 * 1024 * 1024


# --- start_database 的行为（拿假 docker 真跑一遍）----------------------------
#
# ⚠️ 这一组和 prune_old_images 那组同理由：**这条路径 2026-09-16 真把生产搞停过**。
# `up -d mysql` 在项目网络定义变了的时候，会先停掉 mysql、再去删网，而另外六个容器
# 还挂在网上 —— 删除失败，mysql 再也没起来，`/readyz` 直接 503，而且失败发生在健康
# 检查之前，回滚那条路按设计「保持现场不动」，所以**没有任何自动补救**。
# 静态断言看不出「重试顺序对不对」「stop 有没有变成 down」，所以这里真的执行它。

FAKE_DOCKER_DB = """#!/usr/bin/env bash
# ⚠️ 这个假 docker **按端点建模**，不是「stop 之后就放行」的捷径（Codex 审查 PR #70 指出）。
# 真实约束是：网络定义变了时 compose 要重建那张网，而**还挂在网上的容器**会让删除失败
# （`network ... has active endpoints`）。本地实测确认了两件事：
#   1. `docker stop <容器>` 之后，它就**不在**该网络的端点列表里了
#   2. 全部容器停掉之后，`docker network rm` 成功
# 生产事故日志同样佐证：报错只点名那 6 个**还在跑**的容器，刚被 compose 停掉的 mysql 不在其中。
#
# $FAKE_STATE/attached  —— 还挂在旧网上的服务（每行一个）
# $FAKE_STATE/net_changed —— 存在时表示网络定义变了，起容器前必须先重建网络
printf '%s\\n' "$*" >> "$FAKE_STATE/calls"
attached() { [ -s "$FAKE_STATE/attached" ] && cat "$FAKE_STATE/attached"; }
case "$2 $3" in
    "up -d")
        # 与网络无关的失败（镜像拉不到、磁盘满……）：原样失败，不该被补救掩盖
        if [ -e "$FAKE_STATE/unrelated" ]; then
            cat "$FAKE_STATE/unrelated" >&2
            exit 1
        fi
        if [ -e "$FAKE_STATE/net_changed" ]; then
            # compose 先停掉它要起的那个（mysql），再删网
            grep -vx mysql "$FAKE_STATE/attached" > "$FAKE_STATE/attached.new" 2>/dev/null || true
            mv "$FAKE_STATE/attached.new" "$FAKE_STATE/attached"
            if [ -s "$FAKE_STATE/attached" ]; then
                names=""
                while read -r svc; do
                    names="${names}name:\\"${svc}\\" "
                done < "$FAKE_STATE/attached"
                echo "Error response from daemon: error while removing network:" >&2
                echo "  network ai_billing_hub_default has active endpoints (${names})" >&2
                exit 1
            fi
            rm -f "$FAKE_STATE/net_changed"
        fi
        echo "mysql" >> "$FAKE_STATE/attached"
        echo "Container mysql Started"
        ;;
    "stop "*|"stop")
        # 停掉 = 端点被释放（本地实测）
        : > "$FAKE_STATE/attached"
        echo "Container api Stopped"
        ;;
    *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

DB_RUNNER = """#!/usr/bin/env bash
set -euo pipefail
cd "$1"
export FAKE_STATE="$PWD"
export PATH="$PWD/bin:$PATH"
log() { printf 'LOG %s\\n' "$*"; }
COMPOSE="docker compose"
source "$PWD/fn.sh"
start_database && echo "RC=0" || echo "RC=$?"
echo "DEPLOY CONTINUES"
"""


STACK_SERVICES = (
    "mysql",
    "redis",
    "api",
    "celery-worker",
    "celery-beat",
    "frontend",
    "billing_nginx",
)


def run_start_database(
    tmp_path, network_changed: bool = False, unrelated_failure: str | None = None
):
    """把 deploy.sh 里的 start_database 原样抠出来跑，返回 (stdout, 调用序列)。"""
    bash = require_bash()

    body = re.search(
        r"^start_database\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.M | re.S
    )
    assert body is not None, "deploy.sh 里找不到 start_database"

    state = tmp_path / "dbstate"
    (state / "bin").mkdir(parents=True)

    def put(rel: str, text: str) -> Path:
        target = state / rel
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    put("fn.sh", body.group(0) + "\n")
    put("calls", "")
    # 旧网上挂着的东西：本栈七个服务
    put("attached", "\n".join(STACK_SERVICES) + "\n")
    fake = put("bin/docker", FAKE_DOCKER_DB)
    fake.chmod(0o755)
    runner = put("run.sh", DB_RUNNER)
    if network_changed:
        put("net_changed", "")
    if unrelated_failure is not None:
        put("unrelated", unrelated_failure)

    done = subprocess.run(
        [bash, str(runner), str(state)], capture_output=True, text=True, env={**os.environ}
    )
    assert "DEPLOY CONTINUES" in done.stdout, done.stderr
    calls = (state / "calls").read_text(encoding="utf-8").splitlines()
    return done.stdout, calls


def test_starting_the_database_normally_touches_nothing_else(tmp_path) -> None:
    """正常部署里 mysql 本来就在跑，`up -d` 什么也不做 —— 更不该去停别的服务。"""
    out, calls = run_start_database(tmp_path)
    assert "RC=0" in out
    assert [c for c in calls if " stop" in c] == []


def test_a_changed_network_stops_the_stack_then_retries(tmp_path) -> None:
    """⚠️ 这就是 2026-09-16 把生产搞停的那一幕。

    compose 为了重建项目网络先停了 mysql、再删网，而别的容器还挂在网上：
    `network ... has active endpoints`。正确的处置是**让整栈先离开那张网，再重试**。
    """
    out, calls = run_start_database(tmp_path, network_changed=True)
    assert "RC=0" in out, "识别到这个特征之后应当重试成功"
    ups = [i for i, c in enumerate(calls) if c.startswith("compose up")]
    stops = [i for i, c in enumerate(calls) if c.startswith("compose stop")]
    assert len(ups) == 2 and len(stops) == 1
    # 顺序：先试一次 → stop → 再试一次
    assert ups[0] < stops[0] < ups[1]


def test_the_recovery_never_downs_the_stack(tmp_path) -> None:
    """⚠️ 停就够了，没有理由去删容器。

    本地实测：`docker stop` 之后容器就不在网络端点列表里，全停之后 `network rm` 成功。
    既然 `stop` 能达到同样效果，`down` 就只是多牵动别的东西（孤儿容器、外部网络）——
    部署脚本在这一步该做**最小的那个动作**。
    """
    script = uncommented(SCRIPT)
    body = script[script.index("start_database() {") : script.index("prune_old_images() {")]
    assert "$COMPOSE stop" in body
    assert "down" not in body


def test_an_unrelated_failure_is_not_papered_over(tmp_path) -> None:
    """⚠️ 只对「网络要重建」这一个特征做补救。

    别的失败（镜像拉不到、磁盘满、配置写错）照样要失败 —— 把它们也重试一遍，
    只会在真正的原因上面盖一层噪声。
    """
    out, calls = run_start_database(tmp_path, unrelated_failure="Error: no space left on device")
    assert "RC=1" in out
    assert [c for c in calls if c.startswith("compose stop")] == []


# --- 部署配置快照（§98.1 备份四层的第三层；T0.9）------------------------------

CONFIG_SNAPSHOT = REPO_ROOT / "deploy" / "config_snapshot.sh"


def test_the_snapshot_never_interpolates_compose() -> None:
    """⚠️ `docker compose config` **默认会把值代进去** —— 那一份里有数据库口令。

    必须用 `--no-interpolate`，占位符保持 `${VAR}` 原样（本地实测：把口令塞进环境变量
    再导出，值不会出现）。
    """
    script = uncommented(CONFIG_SNAPSHOT)
    assert "$COMPOSE config --no-interpolate" in script
    assert not re.search(r"\$COMPOSE config(?! --no-interpolate)", script)


def test_only_env_key_names_are_collected() -> None:
    """⚠️ `.env` 那一段只取**键名**：值在 Bitwarden，不该出现在任何离机文件里。"""
    script = uncommented(CONFIG_SNAPSHOT)
    assert r"sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'" in script
    # 不许出现任何「把整行 .env 抄进去」的写法
    assert not re.search(r'cat "\$ENV_FILE"', script)


def test_the_snapshot_checks_itself_for_secrets_before_uploading() -> None:
    """⚠️ 这道自查是给**未来的改动**兜底的：哪天有人去掉 `--no-interpolate`，

    或者往键名那一段里塞了值，都要在上传之前当场拦住 —— 一份带口令的快照传上去就收不回来。
    ⚠️ 心跳地址同样算凭据：知道它的人能伪造「成功」。
    """
    script = uncommented(CONFIG_SNAPSHOT)
    assert "refusing to upload a snapshot that contains secret values" in script
    assert "refusing to upload a snapshot that contains ${key}" in script
    for key in ("BILLING_MYSQL_ROOT_PASSWORD", "BILLING_BACKUP_PASSPHRASE", "R2_SECRET_ACCESS_KEY"):
        assert key in script
    for key in ("BILLING_HEALTHCHECK_BACKUP_URL", "BILLING_HEALTHCHECK_CONFIG_URL"):
        assert key in script
    # 自查必须排在加密与上传之前
    assert script.index("refusing to upload") < script.index("openssl enc -aes-256-cbc")


def test_the_snapshot_round_trips_before_upload() -> None:
    """⚠️ 与备份同一条：能传上去不算数，能解开才算。"""
    script = uncommented(CONFIG_SNAPSHOT)
    assert "openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000" in script
    assert 'cmp -s "$PLAIN" "$VERIFY"' in script
    assert "head-object" in script and "size mismatch after upload" in script


def test_the_snapshot_carries_what_git_cannot() -> None:
    """⚠️ compose / nginx 的**文件内容**在 git 里，再抄一遍是做样子。

    这一层要带走的是**不在 git 里、灾难恢复时又必须知道**的东西。
    """
    script = uncommented(CONFIG_SNAPSHOT)
    assert "$DEPLOY_STATE" in script  # 部署的是哪个 commit
    assert "$CRON_FILE" in script  # cron 装成什么样
    assert "sha256sum deploy/nginx/*" in script  # 线上那一版的校验和
    assert "$COMPOSE images" in script  # 真正在跑的镜像


def test_the_cron_runs_the_snapshot_after_the_log_shipping() -> None:
    """三件事都要用 R2，错开分钟数，别在同一分钟互相拖慢。"""
    lines = [line for line in uncommented(CRON).splitlines() if "config_snapshot.sh" in line]
    assert len(lines) == 1
    assert lines[0].startswith("57 3 * * *")
    assert "flock" in lines[0] and "logger -t billing-config" in lines[0]
    logs = [line for line in uncommented(CRON).splitlines() if "deploy/log_ship.sh" in line][0]
    assert int(logs.split()[0]) < int(lines[0].split()[0])
