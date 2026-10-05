"""最热租户压测：用量计费的 S1–S4 与方案 C 判据（AIH-TASK-033，ADR-0011）。

场景与通过线逐字取自 docs/design/AIH-TASK-032-usage-billing.md §2「最热租户」：

  S1 突发          单个租户，100 事件 / 秒持续 5 分钟（§119 突发），同时 3 个其他租户各 5 事件 / 秒
                   通过线：事件到扣费延迟 p95 ≤ 60 秒、p99 ≤ 5 分钟（§119）；报告 p50 / p95 / p99
  S2 回补          单个租户预先积压 30 000 条 `RECEIVED`
                   通过线：清空速率 ≥ 500 事件 / 秒（§119「≥ 峰值 5 倍」）
  S3 崩溃与重试    S1 进行中随机杀掉 worker 进程 3 次、注入 1% 的意外异常
                   通过线：结束后：`PROCESSED` 事件数 = 账本 `AI_USAGE` 行数（不含 0 元事件）；每个租户
                   `期初余额 − Σ billable_cost = 期末余额`；`verify_wallet` 全部通过；无 `LEDGER_CONFLICT`
  S4 与发布并发    S1 进行中每 10 秒发布一次价格 / 规则 / 汇率
                   通过线：S1 的通过线仍满足；发布请求 p95 ≤ 5 秒；抽样事件用快照重算
                   （T-G `reprice_from_snapshot`）与快照一致

方案 C 的切换判据：S1 的 p95 或 p99 不达标、或 S2 的清空速率 < 500 / 秒，**且**瓶颈经测量是同一钱包
行锁上的串行提交（最热租户的单事件事务平均持锁时间 × 目标速率 > 1 秒 / 秒）—— 则另开任务实现
方案 C，另过设计闸门。若瓶颈在别处（例如数据库整体吞吐、worker 数量），先调那一处，不切方案 C。

⚠️ **事件走真实的代码路径**：摄取经 `ingest_one`（AIH-TASK-029，跳过的只有 HTTP 层的验签与
nonce —— 它们不在「事件到扣费」的路径上），计费经 `claim_events` / `process_event` /
`recover_stale_processing`（AIH-TASK-032）。脚本**不直接写** `usage_events`、账本或余额：
租户、钱包、凭据、目录、价格、规则、汇率与充值都经各自的服务函数建立，值是虚构的。

⚠️ **worker 是真的进程**。`billing_perf` 上没有 celery worker，所以脚本自己起 `--workers` 个子进程
（默认 2，等于 compose 的 `BILLING_CELERY_CONCURRENCY`），由主进程里的一个线程按 Beat 的节奏派
任务：每 10 秒一次扫描（9 秒过期）、每 60 秒一次卡住回收（54 秒过期），数值取自
`app/core/celery_app.py`。S3 用 SIGKILL 杀的就是这些进程，杀完立刻补一个（等同 prefork 补子进程）。

⚠️ 它往库里写行，而且**不删**（账本只增）。所以：
  - `--database-url` 必填，不回落到 `BILLING_DATABASE_URL`；
  - 库名必须是 `billing_perf`，连上之后再问一次服务器 `DATABASE()`；
  - 不能是栈自己那个库（URL 归一化比对 + 连上后比 `@@server_uuid` 与 `DATABASE()`）；
  - `BILLING_ENVIRONMENT=production` 时只允许指向 `billing_perf`；
  - 每轮一个唯一前缀，所有造出来的数据都带它，报告里逐项列出。

用法（`scripts/` 不在镜像里，要挂进去；命令见 docs/perf-baseline.md 第 10 节）：

    docker compose run --rm \\
      -v "$PWD/scripts:/srv/billing/scripts:ro" \\
      api python scripts/perf_usage.py --database-url "$PERF_URL"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import multiprocessing
import os
import platform
import random
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from multiprocessing.connection import Connection, wait

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from app.core.celery_app import (
    USAGE_BILLING_SWEEP_EXPIRES_SECONDS,
    USAGE_BILLING_SWEEP_SECONDS,
    USAGE_STALE_RECOVERY_EXPIRES_SECONDS,
    USAGE_STALE_RECOVERY_SECONDS,
)
from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.core.passwords import hash_password
from app.models.auth import User, UserRole, UserStatus
from app.models.integration import IntegrationCredential
from app.models.pricing_rules import PricingScope, PricingStrategy
from app.models.tenancy import Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.repositories import wallet as wallet_repository
from app.schemas.provider_prices import ComponentInput
from app.services import (
    ai_catalog,
    customers,
    fx_rates,
    integration_access,
    pricing_rules,
    provider_prices,
    usage_billing,
    wallet_adjustments,
)
from app.services.auth import RequestContext, utc_now
from app.services.pricing_engine import ChargeSnapshot, reprice_from_snapshot
from app.services.usage_ingest import AuthenticatedCredential, ingest_one

logger = logging.getLogger("perf_usage")

CONTEXT = RequestContext(ip_address="127.0.0.1", user_agent="perf-usage")

# 唯一允许的测量库名。
PERF_DATABASE = "billing_perf"

# --- 通过线（设计 §2「最热租户」，逐字的数值） --------------------------------------------

S1_P95_LIMIT_SECONDS = 60.0
S1_P99_LIMIT_SECONDS = 300.0
S2_DRAIN_RATE_FLOOR = 500.0
S4_PUBLISH_P95_LIMIT_SECONDS = 5.0
# 方案 C 判据：平均持锁时间 × 目标速率 > 1 秒 / 秒。
LOCK_BUDGET_SECONDS_PER_SECOND = 1.0

# 负载本身没有达到目标时，这一轮测的不是设计说的那个负载。⚠️ 这不是通过线，是
# 「我量的东西真的发生了吗」那道守卫（docs/perf-baseline.md 第 8 节）。
LOAD_TOLERANCE = 0.95

# 场景结论。INVALID：负载没达到、或事件没走完 —— 数字不能拿去判通过与否。
PASS = "PASS"
FAIL = "FAIL"
INVALID = "INVALID"
NOT_RUN = "NOT_RUN"

PLAN_C_NEEDED = "需要方案 C"
PLAN_C_NOT_NEEDED = "不需要方案 C"
PLAN_C_UNDECIDED = "无法判定"

SCENARIOS = ("S1", "S2", "S3", "S4")

# 一个事件离开这些状态之外的任何状态（RECEIVED / PROCESSING / FAILED_RETRYABLE）都算还没走完。
TERMINAL_STATUSES = (
    UsageEventStatus.PROCESSED,
    UsageEventStatus.MODEL_UNKNOWN,
    UsageEventStatus.PRICING_ERROR,
    UsageEventStatus.FX_RATE_ERROR,
    UsageEventStatus.FAILED_FINAL,
)

# 虚构的价格：USD、每 1 000 token，与 tests/backend/test_usage_billing.py 同一组数。
USD_RATES = {
    "LLM_INPUT_TOKEN": Decimal("0.0625"),
    "LLM_OUTPUT_TOKEN": Decimal("0.25"),
    "LLM_CACHE_WRITE_TOKEN": Decimal("0.125"),
    "LLM_CACHE_READ_TOKEN": Decimal("0.0078125"),
}
PER_THOUSAND = "1000"
FX_RATES = (Decimal("4.5"), Decimal("4.25"))
MARKUPS = (Decimal("2"), Decimal("2.5"))
TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_creation_input_tokens": 500,
    "cache_read_input_tokens": 3000,
}
# 每个事件约 5.84 MYR；S1 / S3 / S4 的最热租户各约 3 万条、S2 3 万条，期初余额要远大于它们，
# 余额跨零的停机跃迁不是这里要测的东西。
DEFAULT_OPENING_BALANCE = Decimal("1000000")


# --------------------------------------------------------------------------
# 统计与判据（纯函数，tests/backend/test_perf_usage.py 测的就是这一段）
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float:
    """最近秩法（与 scripts/perf_baseline.py 同一算法）：`ceil(n*p)` 的下标，夹在两端之内。

    ⚠️ 没走完的事件以 `inf` 计入：它的延迟至少是整个测量窗口，把它丢掉会让尾部变好看。
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    # 减去 1e-9：`0.07 * 100` 是 7.000000000000001，直接 ceil 会多进一位。
    rank = math.ceil(len(ordered) * fraction - 1e-9)
    index = min(len(ordered) - 1, max(0, rank - 1))
    return ordered[index]


@dataclass
class Verdict:
    scenario: str
    status: str
    measured: dict[str, object] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == PASS


def _seconds_text(value: float) -> str:
    return "∞" if math.isinf(value) else f"{value:.3f}s"


def s1_verdict(
    latencies: Sequence[float], *, load_reached: bool, errored: int = 0, label: str = "S1"
) -> Verdict:
    """p95 ≤ 60 秒、p99 ≤ 5 分钟（§119）；报告 p50 / p95 / p99。

    `errored`：停在错误状态（`PRICING_ERROR` 等）的事件数。它们说明造的价格 / 规则 / 汇率不对，
    测到的是错误路径 —— 这一轮无效，而不是「慢」。
    """
    if not latencies:
        return Verdict(label, INVALID, reasons=["没有任何事件，什么也没量到"])
    p50, p95, p99 = (percentile(latencies, f) for f in (0.50, 0.95, 0.99))
    measured: dict[str, object] = {
        "samples": len(latencies),
        "p50_seconds": p50,
        "p95_seconds": p95,
        "p99_seconds": p99,
        "unsettled": sum(1 for value in latencies if math.isinf(value)),
    }
    if not load_reached:
        return Verdict(label, INVALID, measured, ["负载没有达到目标速率，见 load"])
    if errored:
        return Verdict(label, INVALID, measured, [f"{errored} 条事件停在错误状态，见 statuses"])
    reasons = []
    if p95 > S1_P95_LIMIT_SECONDS:
        reasons.append(f"p95 {_seconds_text(p95)} > {S1_P95_LIMIT_SECONDS:.0f}s")
    if p99 > S1_P99_LIMIT_SECONDS:
        reasons.append(f"p99 {_seconds_text(p99)} > {S1_P99_LIMIT_SECONDS:.0f}s")
    return Verdict(label, FAIL if reasons else PASS, measured, reasons)


def s2_verdict(
    created: int, processed: int, drain_seconds: float | None, *, backlog: int
) -> Verdict:
    """清空速率 ≥ 500 事件 / 秒。

    `created`：开始清空前停在 `RECEIVED` 的条数；`processed`：清空后 `PROCESSED` 的条数；
    `drain_seconds` 为空 = 没在超时内清空。
    """
    measured: dict[str, object] = {"backlog": backlog, "created": created, "processed": processed}
    if created < backlog:
        return Verdict("S2", INVALID, measured, [f"积压只造出 {created} / {backlog} 条"])
    if drain_seconds is None:
        return Verdict("S2", FAIL, measured, ["积压没有在超时内清空"])
    if processed < created:
        return Verdict(
            "S2", INVALID, measured, [f"只有 {processed} / {created} 条走到 PROCESSED，见 statuses"]
        )
    rate = processed / drain_seconds if drain_seconds > 0 else math.inf
    measured.update({"drain_seconds": drain_seconds, "events_per_second": rate})
    if rate < S2_DRAIN_RATE_FLOOR:
        return Verdict("S2", FAIL, measured, [f"清空速率 {rate:.1f}/s < {S2_DRAIN_RATE_FLOOR:.0f}/s"])
    return Verdict("S2", PASS, measured)


@dataclass(frozen=True)
class TenantBalance:
    label: str
    opening: Decimal
    billed: Decimal
    closing: Decimal

    @property
    def consistent(self) -> bool:
        return self.opening - self.billed == self.closing


@dataclass(frozen=True)
class Consistency:
    """S3 的一致性核对要的全部事实，由 `read_consistency` 从库里读出。"""

    processed_billable: int
    ai_usage_rows: int
    balances: tuple[TenantBalance, ...]
    wallet_problems: tuple[tuple[str, tuple[str, ...]], ...]
    ledger_conflicts: int
    unsettled: int

    def as_dict(self) -> dict[str, object]:
        return {
            "processed_billable": self.processed_billable,
            "ai_usage_rows": self.ai_usage_rows,
            "balances": [
                {
                    "tenant": item.label,
                    "opening": str(item.opening),
                    "billed": str(item.billed),
                    "closing": str(item.closing),
                    "consistent": item.consistent,
                }
                for item in self.balances
            ],
            "wallet_problems": {label: list(codes) for label, codes in self.wallet_problems},
            "ledger_conflicts": self.ledger_conflicts,
            "unsettled": self.unsettled,
        }


def consistency_reasons(check: Consistency) -> list[str]:
    """设计写定的四条，任一不满足即失败；再加一条「事件都走完了」—— 否则核对的是一个中间态。"""
    reasons = []
    if check.processed_billable != check.ai_usage_rows:
        reasons.append(
            f"PROCESSED（不含 0 元）{check.processed_billable} ≠ AI_USAGE 账本行 {check.ai_usage_rows}"
        )
    for item in check.balances:
        if not item.consistent:
            reasons.append(
                f"{item.label}: 期初 {item.opening} − Σ billable_cost {item.billed} "
                f"≠ 期末 {item.closing}"
            )
    for label, codes in check.wallet_problems:
        if codes:
            reasons.append(f"{label}: verify_wallet 报 {', '.join(codes)}")
    if check.ledger_conflicts:
        reasons.append(f"LEDGER_CONFLICT {check.ledger_conflicts} 条")
    if check.unsettled:
        reasons.append(f"{check.unsettled} 条事件在超时内没有走完（核对的会是中间态）")
    return reasons


def s3_verdict(check: Consistency, *, kills: int, kills_planned: int, faults: int) -> Verdict:
    measured: dict[str, object] = {
        **check.as_dict(),
        "worker_kills": kills,
        "injected_faults": faults,
    }
    if kills < kills_planned:
        return Verdict("S3", INVALID, measured, [f"只杀掉了 {kills} / {kills_planned} 次 worker"])
    reasons = consistency_reasons(check)
    return Verdict("S3", FAIL if reasons else PASS, measured, reasons)


def s4_verdict(
    s1_part: Verdict,
    publish_latencies: Sequence[float],
    *,
    reprice_checked: int,
    reprice_mismatches: int,
) -> Verdict:
    """S1 的通过线仍满足；发布请求 p95 ≤ 5 秒；抽样重算与快照一致。"""
    measured: dict[str, object] = {
        "s1": s1_part.measured,
        "publishes": len(publish_latencies),
        "reprice_checked": reprice_checked,
        "reprice_mismatches": reprice_mismatches,
    }
    if s1_part.status == INVALID:
        return Verdict("S4", INVALID, measured, s1_part.reasons)
    if not publish_latencies:
        return Verdict("S4", INVALID, measured, ["一次发布也没有做成"])
    if reprice_checked == 0:
        return Verdict("S4", INVALID, measured, ["没有可重算的已处理事件"])
    publish_p95 = percentile(publish_latencies, 0.95)
    measured["publish_p95_seconds"] = publish_p95
    reasons = [f"S1 线：{reason}" for reason in s1_part.reasons]
    if publish_p95 > S4_PUBLISH_P95_LIMIT_SECONDS:
        reasons.append(f"发布 p95 {publish_p95:.3f}s > {S4_PUBLISH_P95_LIMIT_SECONDS:.0f}s")
    if reprice_mismatches:
        reasons.append(f"快照重算不一致 {reprice_mismatches} / {reprice_checked}")
    return Verdict("S4", FAIL if reasons else PASS, measured, reasons)


@dataclass(frozen=True)
class PlanC:
    conclusion: str
    basis: tuple[str, ...]


def lock_bound(hold_mean_seconds: float, target_rate: float) -> bool:
    """最热租户的单事件事务平均持锁时间 × 目标速率 > 1 秒 / 秒。"""
    return hold_mean_seconds * target_rate > LOCK_BUDGET_SECONDS_PER_SECOND


def plan_c(
    s1: Verdict | None,
    s2: Verdict | None,
    *,
    s1_hold_mean: float | None,
    s2_hold_mean: float | None,
    s1_target_rate: float,
    s2_target_rate: float = S2_DRAIN_RATE_FLOOR,
) -> PlanC:
    """判据的两个条件都要成立才切方案 C：S1 或 S2 不达标，**且**不达标那一个是锁瓶颈。"""
    basis: list[str] = []
    needed = False
    undecided = False
    judged = 0
    for verdict, hold, rate in ((s1, s1_hold_mean, s1_target_rate), (s2, s2_hold_mean, s2_target_rate)):
        if verdict is None or verdict.status == NOT_RUN:
            continue
        name = verdict.scenario
        if verdict.status == INVALID:
            undecided = True
            basis.append(f"{name} 测量无效（{'; '.join(verdict.reasons)}），不能据它判定")
            continue
        judged += 1
        if verdict.passed:
            basis.append(f"{name} 达标")
            continue
        if hold is None:
            undecided = True
            basis.append(f"{name} 不达标，但没有量到最热租户的持锁时间，瓶颈无法判定")
            continue
        product = hold * rate
        if lock_bound(hold, rate):
            needed = True
            basis.append(
                f"{name} 不达标；最热租户平均持锁 {hold * 1000:.2f} ms × {rate:.0f}/s = "
                f"{product:.3f} 秒/秒 > 1 → 瓶颈是同一钱包行锁上的串行提交"
            )
        else:
            basis.append(
                f"{name} 不达标；最热租户平均持锁 {hold * 1000:.2f} ms × {rate:.0f}/s = "
                f"{product:.3f} 秒/秒 ≤ 1 → 瓶颈不在钱包行锁（先调数据库整体吞吐、worker 数量等那一处）"
            )
    if needed:
        return PlanC(PLAN_C_NEEDED, tuple(basis))
    if undecided or judged == 0:
        if judged == 0:
            basis.append("S1 与 S2 都没有有效结果")
        return PlanC(PLAN_C_UNDECIDED, tuple(basis))
    return PlanC(PLAN_C_NOT_NEEDED, tuple(basis))


# --------------------------------------------------------------------------
# 参数与护栏
# --------------------------------------------------------------------------


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {value}")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value}")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return number


def _fraction(value: str) -> float:
    number = float(value)
    if not 0.0 <= number < 1.0:
        raise argparse.ArgumentTypeError(f"must be in [0, 1): {value}")
    return number


def _scenarios(value: str) -> tuple[str, ...]:
    chosen = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    unknown = [name for name in chosen if name not in SCENARIOS]
    if not chosen or unknown:
        raise argparse.ArgumentTypeError(f"scenarios are a comma list of {', '.join(SCENARIOS)}")
    return chosen


def build_parser() -> argparse.ArgumentParser:
    """默认值就是设计 §2 写定的负载；改小只用于试跑，⚠️ 改过的结果不能回填为基线。"""
    parser = argparse.ArgumentParser(prog="python scripts/perf_usage.py")
    parser.add_argument(
        "--database-url",
        default="",
        help=f"测量专用的库。⚠️ 必填，库名必须是 {PERF_DATABASE}，不回落到 BILLING_DATABASE_URL",
    )
    parser.add_argument("--scenarios", type=_scenarios, default=SCENARIOS, help="例如 S1,S3")
    parser.add_argument("--hot-rate", type=_positive_float, default=100.0, help="最热租户 事件/秒")
    parser.add_argument("--others", type=_non_negative_int, default=3, help="其他租户个数")
    parser.add_argument("--other-rate", type=_positive_float, default=5.0, help="其他租户 事件/秒")
    parser.add_argument("--duration", type=_positive_float, default=300.0, help="S1/S3/S4 秒数")
    parser.add_argument("--backlog", type=_positive_int, default=30_000, help="S2 积压条数")
    parser.add_argument("--kills", type=_non_negative_int, default=3, help="S3 杀 worker 次数")
    parser.add_argument("--fault-rate", type=_fraction, default=0.01, help="S3 意外异常比例")
    parser.add_argument(
        "--publish-interval", type=_positive_float, default=10.0, help="S4 每隔几秒发布一次"
    )
    parser.add_argument("--reprice-samples", type=_positive_int, default=200)
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=2,
        help="计费 worker 进程数（compose 的 BILLING_CELERY_CONCURRENCY 默认 2）",
    )
    parser.add_argument("--ingest-threads", type=_positive_int, default=8)
    parser.add_argument(
        "--drain-timeout", type=_positive_float, default=900.0, help="负载结束后等事件走完的上限"
    )
    parser.add_argument("--seed", type=int, default=None, help="随机数种子（杀进程时刻、注入）")
    parser.add_argument("--json-out", default=None, help="把结果另存成 JSON")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _database_name(raw_url: str) -> str | None:
    try:
        return make_url(raw_url).database
    except ArgumentError:
        return None


def _measurement_settings(raw_url: str, settings) -> object:
    """把测量用的库换成调用方显式指定的那一个，并且只能是 `billing_perf`。

    ⚠️ 与 scripts/perf_baseline.py 同一套护栏，再加一条库名：这个脚本**不删任何行**（账本只增），
    写错库的后果不能靠「跑完清理」挽回，所以只认一个名字。
    """
    if not raw_url:
        raise SystemExit(
            "--database-url is required and must name the "
            f"{PERF_DATABASE} schema. It never falls back to BILLING_DATABASE_URL. "
            "See docs/perf-baseline.md section 10."
        )
    name = _database_name(raw_url)
    if name != PERF_DATABASE:
        raise SystemExit(
            f"refusing to run: the measurement database must be {PERF_DATABASE}, "
            f"not {name!r}. This script never deletes what it writes."
        )
    if settings.database_url and _same_target(raw_url, settings.database_url):
        raise SystemExit(
            "refusing to measure against the stack's own database (BILLING_DATABASE_URL): "
            "its celery worker would bill the events this script writes."
        )
    return settings.model_copy(update={"database_url": raw_url})


def _check_environment(settings, raw_url: str) -> None:
    """`BILLING_ENVIRONMENT=production` 时只允许指向 `billing_perf`。

    ⚠️ 与 perf_baseline.py 不同，这里**不**整体拒绝生产：ADR-0011 要的是生产同规格上的数字，
    而第 9 节的做法（临时把一次性容器的环境改成 local）等于每次都要人去绕一道护栏。
    换成正面的条件：生产上只认那一个库。
    """
    if settings.environment == "production" and _database_name(raw_url) != PERF_DATABASE:
        raise SystemExit(
            f"BILLING_ENVIRONMENT=production: only the {PERF_DATABASE} schema may be measured"
        )


def _same_target(left: str, right: str) -> bool:
    """两个连接串是不是指向同一个 schema（host / port / database，见 perf_baseline.py）。"""
    a, b = make_url(left), make_url(right)
    return (
        (a.host or "").lower() == (b.host or "").lower()
        and (a.port or 3306) == (b.port or 3306)
        and a.database == b.database
    )


def _identity(engine) -> tuple[str, str] | None:
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT @@server_uuid, DATABASE()")).one()
    except SQLAlchemyError:
        return None
    return (str(row[0]), str(row[1]))


def _assert_measurement_server(measurement_engine, stack_settings) -> None:
    """连上之后问服务器：当前库确实叫 `billing_perf`，且不是栈自己那个 schema。

    ⚠️ 生产上栈的库连不上时**拒绝**（perf_baseline.py 是放行）：生产上它不该连不上，
    连不上说明连接串或网络不对，这时最不该做的就是开始往一个说不清的库里写三万行。
    """
    measured = _identity(measurement_engine)
    if measured is None:
        raise SystemExit("cannot connect to the measurement database")
    if measured[1] != PERF_DATABASE:
        raise SystemExit(f"the server says the current schema is {measured[1]!r}, not {PERF_DATABASE}")
    if not stack_settings.database_url:
        return
    stack_engine = create_engine(stack_settings.database_url, pool_pre_ping=True)
    try:
        stack = _identity(stack_engine)
    finally:
        stack_engine.dispose()
    if stack is None:
        if stack_settings.environment == "production":
            raise SystemExit(
                "BILLING_ENVIRONMENT=production but the stack's database is unreachable: "
                "refusing to run without confirming the measurement schema is a different one"
            )
        return
    if measured == stack:
        raise SystemExit(
            "refusing to measure against the stack's own database: the server reports the "
            f"same instance and schema ({stack[1]})"
        )


def new_run_prefix() -> str:
    """每轮唯一。所有造出来的数据（账号、客户、供应商、模型、事件的 request_id）都带它。"""
    return f"perf-usage-{uuid.uuid4().hex[:10]}"


def uuid7() -> str:
    """一个 UUIDv7（小写、带连字符）：摄取只收 UUIDv7 或 ULID，Python 3.12 还没有 `uuid.uuid7`。"""
    millis = time.time_ns() // 1_000_000
    random_bits = int.from_bytes(os.urandom(10), "big")
    rand_a = (random_bits >> 62) & 0xFFF
    rand_b = random_bits & ((1 << 62) - 1)
    value = (millis & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))


def _iso_z(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------
# 造数据：全部经服务函数，值是虚构的
# --------------------------------------------------------------------------


@dataclass
class PerfTenant:
    label: str
    public_id: str
    tenant_id: int
    credential: AuthenticatedCredential
    opening: Decimal = Decimal(0)


@dataclass
class PerfModel:
    """一个场景自己的模型：价格与规则都只挂在它上面，S4 的发布不碰别的场景。"""

    code: str
    model_public_id: str
    publications: int = 0


class World:
    """这一轮造的全部东西，以及造它们的那几个服务调用。"""

    def __init__(self, factory, settings, prefix: str, opening: Decimal) -> None:
        self.factory = factory
        self.settings = settings
        self.prefix = prefix
        self.opening = opening
        self.created: list[str] = []
        with factory() as session:
            admin = User(
                email=f"{prefix}+admin@example.com",
                # ⚠️ 每轮随机、不落盘：账号留在库里，但没有人知道它的口令（perf_baseline.py 同理）。
                password_hash=hash_password(f"{uuid.uuid4().hex}{uuid.uuid4().hex}"),
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(admin)
            session.commit()
            self.admin = admin
        self.created.append(f"ADMIN 账号 {admin.email}（口令随机、未保存）")
        self.provider = ai_catalog.create_provider(
            factory,
            actor=self.admin,
            code=prefix,
            display_name=f"Fictional provider {prefix}",
            context=CONTEXT,
        )
        self.created.append(f"AI 供应商 {prefix}")
        self.fx_publications = 0
        self.publish_fx()

    # -- 目录、价格、规则、汇率 --

    def model(self, scenario: str) -> PerfModel:
        code = f"{self.prefix}-{scenario.lower()}"
        view = ai_catalog.create_model(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            code=code,
            display_name=f"Fictional model {code}",
            context=CONTEXT,
        )
        self.created.append(f"AI 模型 {code}")
        model = PerfModel(code=code, model_public_id=view.id)
        self.publish_price(model)
        self.publish_rule(model)
        return model

    def publish_price(self, model: PerfModel) -> float:
        """草稿 + 发布；返回**发布**那一次调用的耗时（S4 的「发布请求」）。"""
        factor = Decimal(1 + model.publications % 2)
        components = [
            ComponentInput.model_validate(
                {"component_code": code, "unit_quantity": PER_THOUSAND, "rate_amount": str(rate * factor)}
            )
            for code, rate in USD_RATES.items()
        ]
        draft = provider_prices.create_draft(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=model.model_public_id,
            source_currency="USD",
            source_reference=f"Fictional price page ({self.prefix})",
            components=components,
            context=CONTEXT,
        )
        started = time.perf_counter()
        provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=draft.id,
            effective_from=None,
            context=CONTEXT,
        )
        elapsed = time.perf_counter() - started
        model.publications += 1
        self.created.append(f"价格版本 {draft.id}（{model.code}，USD，×{factor}）")
        return elapsed

    def publish_rule(self, model: PerfModel) -> float:
        multiplier = MARKUPS[model.publications % len(MARKUPS)]
        draft = pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=PricingScope.GLOBAL_PROVIDER_MODEL,
            customer_id=None,
            provider_id=self.provider.id,
            model_id=model.model_public_id,
            strategy=PricingStrategy.MARKUP,
            markup_multiplier=multiplier,
            components=None,
            context=CONTEXT,
        )
        started = time.perf_counter()
        pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=draft.id,
            effective_from=None,
            context=CONTEXT,
        )
        elapsed = time.perf_counter() - started
        self.created.append(f"定价规则 {draft.id}（{model.code}，MARKUP ×{multiplier}）")
        return elapsed

    def publish_fx(self) -> float:
        """⚠️ 汇率是全局的（按币种）：它会截断 `billing_perf` 里上一轮留下的 USD 版本。"""
        rate = FX_RATES[self.fx_publications % len(FX_RATES)]
        draft = fx_rates.create_draft(
            self.factory,
            actor=self.admin,
            base_currency="USD",
            rate=rate,
            observed_at=utc_now().replace(microsecond=0),
            source_reference=f"Fictional quote ({self.prefix})",
            context=CONTEXT,
        )
        started = time.perf_counter()
        fx_rates.publish(
            self.factory,
            actor=self.admin,
            fx_rate_id=draft.id,
            effective_from=None,
            context=CONTEXT,
        )
        elapsed = time.perf_counter() - started
        self.fx_publications += 1
        self.created.append(f"汇率版本 {draft.id}（USD→MYR {rate}）")
        return elapsed

    # -- 客户、钱包、凭据 --

    def tenant(self, scenario: str, label: str) -> PerfTenant:
        name = f"{scenario.lower()}-{label}"
        detail = customers.create_customer(
            self.factory,
            actor=self.admin,
            company_name=f"Fictional {self.prefix} {name}",
            email=f"{self.prefix}+{name}@example.com",
            context=CONTEXT,
        )
        project = customers.create_project(
            self.factory,
            actor=self.admin,
            customer_id=detail.id,
            name=f"{self.prefix} {name}",
            context=CONTEXT,
        )
        issued = integration_access.create_credential(
            self.factory,
            self.settings,
            actor=self.admin,
            customer_id=detail.id,
            project_id=project.id,
            context=CONTEXT,
        )
        with self.factory() as session:
            row = session.execute(
                select(IntegrationCredential).where(
                    IntegrationCredential.public_api_key == issued.api_key,
                    IntegrationCredential.key_version == issued.key_version,
                )
            ).scalar_one()
            credential = AuthenticatedCredential(
                id=row.id,
                tenant_id=row.tenant_id,
                project_id=row.project_id,
                api_key=row.public_api_key,
                key_version=row.key_version,
            )
        # 充值走管理员调账的服务（设计闸门 #111），不直接写账本。
        wallet_adjustments.post_adjustment(
            self.factory,
            actor=self.admin,
            customer_id=detail.id,
            transaction_type=TransactionType.ADJUSTMENT_CREDIT,
            amount=self.opening,
            reason=f"Fictional opening balance for {self.prefix}",
            idempotency_key=str(uuid.uuid4()),
            context=CONTEXT,
        )
        tenant = PerfTenant(name, detail.id, credential.tenant_id, credential)
        tenant.opening = _balance(self.factory, tenant.tenant_id)
        self.created.append(
            f"客户 {detail.id}（{name}）、项目 {project.id}、凭据 {issued.api_key}、"
            f"调账充值 {self.opening} MYR"
        )
        return tenant


def _balance(factory, tenant_id: int) -> Decimal:
    with factory() as session:
        return session.execute(select(Wallet.balance).where(Wallet.tenant_id == tenant_id)).scalar_one()


# --------------------------------------------------------------------------
# 摄取：经 ingest_one
# --------------------------------------------------------------------------


class Ingestor:
    """按时间表把事件交给 `ingest_one`，记下每条被持久接收（已提交）的时刻。"""

    def __init__(self, factory, prefix: str, threads: int) -> None:
        self.factory = factory
        self.prefix = prefix
        self.threads = threads
        self.accepted: dict[str, float] = {}
        self.rejected: Counter[str] = Counter()
        self.per_tenant: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._numbers = iter(range(1, 1 << 62))

    def emit(self, tenant: PerfTenant, model: PerfModel) -> None:
        with self._lock:
            number = next(self._numbers)
        payload = {
            "schema_version": "1.0",
            "event_id": uuid7(),
            "request_id": f"{self.prefix}-{number}",
            "provider": self.prefix,
            "model": model.code,
            "usage_type": "LLM_TOKEN",
            **TOKENS,
            "occurred_at": _iso_z(utc_now()),
        }
        try:
            outcome = ingest_one(
                self.factory, tenant.credential, payload, context=CONTEXT, now=utc_now()
            )
        except Exception as error:  # noqa: BLE001 —— 记下来、算进负载是否达标
            with self._lock:
                self.rejected[type(error).__name__] += 1
            return
        accepted_at = time.time()
        with self._lock:
            self.accepted[outcome.event_id] = accepted_at
            self.per_tenant[tenant.label] += 1

    def paced(
        self,
        streams: Sequence[tuple[PerfTenant, PerfModel, float]],
        duration: float,
    ) -> dict[str, dict[str, float]]:
        """每个流 `rate` 条 / 秒、持续 `duration` 秒；返回每个租户的目标与实际速率。

        ⚠️ 实际速率由**墙钟**算（被接收的条数 / 从开跑到最后一条被接收），不由计划算 ——
        摄取跟不上时，计划照样「发出」了 100/s，库里却没有。
        """
        schedule: list[tuple[float, PerfTenant, PerfModel]] = []
        for tenant, model, rate in streams:
            count = int(rate * duration)
            schedule.extend((index / rate, tenant, model) for index in range(count))
        schedule.sort(key=lambda item: item[0])
        before = Counter(self.per_tenant)
        started = time.time()
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            for offset, tenant, model in schedule:
                delay = started + offset - time.time()
                if delay > 0:
                    time.sleep(delay)
                pool.submit(self.emit, tenant, model)
        wall = max(time.time() - started, duration)
        load: dict[str, dict[str, float]] = {}
        for tenant, _model, rate in streams:
            done = self.per_tenant[tenant.label] - before[tenant.label]
            load[tenant.label] = {"target_per_second": rate, "achieved_per_second": done / wall}
        return load

    def burst(self, tenant: PerfTenant, model: PerfModel, count: int) -> float:
        """尽快灌 `count` 条（S2 的积压）。返回耗时。"""
        started = time.time()
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            for _ in range(count):
                pool.submit(self.emit, tenant, model)
        return time.time() - started


def errored_count(statuses: dict[str, int]) -> int:
    """停在错误终态（不是 PROCESSED）的事件数。"""
    errors = {status.value for status in TERMINAL_STATUSES} - {UsageEventStatus.PROCESSED.value}
    return sum(count for status, count in statuses.items() if status in errors)


def load_reached(load: dict[str, dict[str, float]]) -> bool:
    return all(
        item["achieved_per_second"] >= LOAD_TOLERANCE * item["target_per_second"]
        for item in load.values()
    )


# --------------------------------------------------------------------------
# worker 进程：经 claim_events / process_event / recover_stale_processing
# --------------------------------------------------------------------------


class InjectedFault(RuntimeError):
    """S3 注入的「意外异常」。"""


class _FaultyClock:
    """第一次被调用时抛 `InjectedFault`，之后照常给时间。

    `process_event` 里时钟恰好被调用两次：⑤（三把共享锁都拿到之后，处理事务里）与失败记录的
    短事务。于是异常落在处理事务中途 —— 回滚、`FAILED_RETRYABLE` + 退避 —— 而失败记录本身照常写入。
    """

    def __init__(self, fail: bool) -> None:
        self.fail = fail

    def __call__(self) -> dt.datetime:
        if self.fail:
            self.fail = False
            raise InjectedFault("injected by scripts/perf_usage.py")
        return utc_now()


# 这个进程里最近一次拿到钱包行锁的时刻与租户（见 `_instrument_wallet_lock`）。
_WALLET_LOCK: dict[str, float | int] = {}


def _instrument_wallet_lock() -> None:
    """量「单事件事务的持锁时间」：钱包行锁拿到的那一刻 → 处理事务提交之后。

    ⚠️ 只在 worker 子进程里包一层 `_lock_wallet`（`post_transaction` 按模块全局名调用它），
    不改 app/ 的任何文件；包装只记时刻，返回值原样交回。
    """
    original = wallet_repository._lock_wallet

    def timed(session, tenant_id):
        wallet = original(session, tenant_id)
        _WALLET_LOCK["acquired"] = time.perf_counter()
        _WALLET_LOCK["tenant_id"] = tenant_id
        return wallet

    wallet_repository._lock_wallet = timed  # type: ignore[assignment]


def _sweep(factory, connection: Connection, *, lease: int, attempts: int, rng, fault_rate: float):
    """与 `usage_billing.bill_pending_events` 同一个循环（8 秒预算、每批 20 条），逐条记完成时刻。

    ⚠️ 不直接调 `bill_pending_events`：它只回计数，量不出每个事件的完成时刻与持锁时间。
    认领与处理调用的是同样两个函数，预算与批量取自同一组常量。
    """
    started = time.monotonic()
    faults = 0
    while time.monotonic() - started < usage_billing.SWEEP_BUDGET_SECONDS:
        claims = usage_billing.claim_events(factory, lease_seconds=lease)
        if not claims:
            break
        rows = []
        for claim in claims:
            inject = fault_rate > 0 and rng.random() < fault_rate
            faults += inject
            _WALLET_LOCK.clear()
            outcome = usage_billing.process_event(
                factory, claim, max_attempts=attempts, clock=_FaultyClock(inject)
            )
            done = time.perf_counter()
            finished = time.time()
            acquired = _WALLET_LOCK.get("acquired")
            hold = done - acquired if acquired is not None else None
            rows.append((claim.id, outcome.value, finished, _WALLET_LOCK.get("tenant_id"), hold))
        connection.send(("results", rows))
    if faults:
        connection.send(("faults", faults))


def _worker_main(
    database_url: str, connection: Connection, lease: int, attempts: int, fault_rate: float, seed
) -> None:
    """一个计费 worker 进程：收主进程按 Beat 节奏派来的任务，做完回一声 idle。"""
    logging.basicConfig(level=logging.ERROR)
    settings = get_settings().model_copy(update={"database_url": database_url})
    factory = create_session_factory(create_database_engine(settings))
    _instrument_wallet_lock()
    rng = random.Random(seed)
    while True:
        message = connection.recv()
        if message[0] == "stop":
            return
        if message[0] == "sweep":
            _sweep(factory, connection, lease=lease, attempts=attempts, rng=rng, fault_rate=fault_rate)
        elif message[0] == "recover":
            recovered = usage_billing.recover_stale_processing(factory, max_attempts=attempts)
            connection.send(("recovered", recovered))
        connection.send(("idle",))


@dataclass
class _Worker:
    process: multiprocessing.process.BaseProcess
    connection: Connection
    busy: bool = False


class WorkerPool:
    """N 个 worker 进程 + 一个 Beat 线程。

    ⚠️ 每个 worker 一条自己的管道，**不共用一个 `multiprocessing.Queue`**：被 SIGKILL 的进程如果
    正拿着队列的读锁，其他进程会永远等在那把锁上。一条管道坏了只影响被杀的那一个。
    """

    def __init__(self, settings, size: int, *, fault_rate: float = 0.0, seed=None) -> None:
        self.settings = settings
        self.size = size
        self.fault_rate = fault_rate
        self.rng = random.Random(seed)
        self.context = multiprocessing.get_context("spawn")
        self.workers: list[_Worker] = []
        self.finished: dict[int, float] = {}
        self.holds: dict[int, list[float]] = {}
        self.outcomes: Counter[str] = Counter()
        self.recovered = 0
        self.faults = 0
        self.kills: list[float] = []
        self.dropped_tasks = 0
        self._kill_requests = 0
        self._pending: list[tuple[str, float]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spawn(self) -> _Worker:
        parent, child = self.context.Pipe()
        process = self.context.Process(
            target=_worker_main,
            args=(
                self.settings.database_url,
                child,
                self.settings.usage_lease_seconds,
                self.settings.usage_max_attempts,
                self.fault_rate,
                self.rng.randrange(1 << 30),
            ),
            daemon=True,
        )
        process.start()
        child.close()
        return _Worker(process, parent)

    def start(self) -> None:
        self.workers = [self._spawn() for _ in range(self.size)]
        self._thread = threading.Thread(target=self._run, name="beat", daemon=True)
        self._thread.start()

    def request_kill(self) -> None:
        with self._lock:
            self._kill_requests += 1

    def _kill_one(self) -> None:
        victim = self.rng.choice(self.workers)
        victim.process.kill()
        victim.process.join(timeout=10)
        victim.connection.close()
        self.workers.remove(victim)
        self.kills.append(time.time())
        # 等同 prefork 补一个子进程（或 compose 的 restart）。
        self.workers.append(self._spawn())

    def _lost(self, worker: _Worker) -> None:
        """进程没了（不是我们杀的也一样）：关掉管道；还在跑就补一个。"""
        worker.connection.close()
        worker.busy = False
        if worker in self.workers:
            self.workers.remove(worker)
            if not self._stop.is_set():
                self.workers.append(self._spawn())

    def _handle(self, worker: _Worker) -> None:
        try:
            message = worker.connection.recv()
        except (EOFError, OSError):
            self._lost(worker)
            return
        kind = message[0]
        if kind == "results":
            for event_pk, outcome, finished, tenant_id, hold in message[1]:
                self.outcomes[outcome] += 1
                if outcome == usage_billing.Outcome.PROCESSED.value:
                    self.finished[event_pk] = finished
                    if hold is not None and tenant_id is not None:
                        self.holds.setdefault(tenant_id, []).append(hold)
        elif kind == "faults":
            self.faults += message[1]
        elif kind == "recovered":
            self.recovered += message[1]
        elif kind == "idle":
            worker.busy = False

    def _run(self) -> None:
        next_sweep = next_recovery = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now >= next_sweep:
                self._pending.append(("sweep", now + USAGE_BILLING_SWEEP_EXPIRES_SECONDS))
                next_sweep += USAGE_BILLING_SWEEP_SECONDS
            if now >= next_recovery:
                self._pending.append(("recover", now + USAGE_STALE_RECOVERY_EXPIRES_SECONDS))
                next_recovery += USAGE_STALE_RECOVERY_SECONDS
            with self._lock:
                kills, self._kill_requests = self._kill_requests, 0
            for _ in range(kills):
                self._kill_one()
            # 过期的任务丢弃（celery 的 expires）。
            alive = [task for task in self._pending if task[1] >= now]
            self.dropped_tasks += len(self._pending) - len(alive)
            self._pending = alive
            for worker in list(self.workers):
                if not self._pending:
                    break
                if not worker.busy:
                    task = self._pending.pop(0)
                    try:
                        worker.connection.send((task[0],))
                    except OSError:
                        self._pending.insert(0, task)
                        self._lost(worker)
                        continue
                    worker.busy = True
            connections = {worker.connection: worker for worker in self.workers}
            for ready in wait(list(connections), timeout=0.05):
                self._handle(connections[ready])

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        for worker in list(self.workers):
            try:
                # 正在扫描的 worker 做完这一轮再收 stop；先把它的结果读完。
                deadline = time.time() + usage_billing.SWEEP_BUDGET_SECONDS + 30
                while worker.busy and time.time() < deadline:
                    if worker.connection.poll(0.1):
                        self._handle(worker)
                if not worker.connection.closed:
                    worker.connection.send(("stop",))
            except (EOFError, OSError):
                pass
            worker.process.join(timeout=30)
            if worker.process.is_alive():
                worker.process.kill()
                worker.process.join()
        self.workers = []

    def hold_mean(self, tenant_id: int) -> float | None:
        samples = self.holds.get(tenant_id, [])
        return sum(samples) / len(samples) if samples else None


# --------------------------------------------------------------------------
# 读回与核对
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EventRow:
    pk: int
    event_id: str
    tenant_id: int
    status: UsageEventStatus
    received_at: dt.datetime
    processed_at: dt.datetime | None


def _events(factory, tenant_ids: Iterable[int]) -> list[EventRow]:
    statement = select(
        UsageEvent.id,
        UsageEvent.event_id,
        UsageEvent.tenant_id,
        UsageEvent.status,
        UsageEvent.received_at,
        UsageEvent.processed_at,
    ).where(UsageEvent.tenant_id.in_(list(tenant_ids)))
    with factory() as session:
        return [EventRow(*row) for row in session.execute(statement).all()]


def _unsettled(factory, tenant_ids: Sequence[int]) -> int:
    statement = (
        select(func.count())
        .select_from(UsageEvent)
        .where(UsageEvent.tenant_id.in_(tenant_ids), UsageEvent.status.not_in(TERMINAL_STATUSES))
    )
    with factory() as session:
        return int(session.execute(statement).scalar_one())


def wait_until_settled(factory, tenant_ids: Sequence[int], timeout: float) -> float | None:
    """等这些租户的事件全部走到终态。返回等了多久；超时返回 None。"""
    started = time.time()
    while time.time() - started < timeout:
        if _unsettled(factory, tenant_ids) == 0:
            return time.time() - started
        time.sleep(2.0)
    return None


def event_latencies(
    rows: Sequence[EventRow], accepted: dict[str, float], finished: dict[int, float]
) -> list[float]:
    """事件到扣费：被持久接收（`ingest_one` 返回）→ 处理事务提交（`process_event` 返回）。

    worker 被杀之前已提交、但结果没来得及回报的事件，退回用库里的 `processed_at − received_at`
    （整秒分辨率）。没走到 PROCESSED 的记作 `inf`。
    """
    values = []
    for row in rows:
        if row.status is not UsageEventStatus.PROCESSED:
            values.append(math.inf)
            continue
        start, end = accepted.get(row.event_id), finished.get(row.pk)
        if start is not None and end is not None:
            values.append(max(0.0, end - start))
        else:
            assert row.processed_at is not None
            values.append(max(0.0, (row.processed_at - row.received_at).total_seconds()))
    return values


def read_consistency(factory, tenants: Sequence[PerfTenant]) -> Consistency:
    ids = [tenant.tenant_id for tenant in tenants]
    with factory() as session:
        processed_billable = session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.tenant_id.in_(ids),
                UsageEvent.status == UsageEventStatus.PROCESSED,
                UsageEvent.billable_cost > 0,
            )
        ).scalar_one()
        ai_usage_rows = session.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(
                WalletTransaction.tenant_id.in_(ids),
                WalletTransaction.transaction_type == TransactionType.AI_USAGE,
            )
        ).scalar_one()
        conflicts = session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.tenant_id.in_(ids),
                UsageEvent.error_code == usage_billing.LEDGER_CONFLICT,
            )
        ).scalar_one()
        balances = []
        problems = []
        for tenant in tenants:
            billed = session.execute(
                select(func.coalesce(func.sum(UsageEvent.billable_cost), 0)).where(
                    UsageEvent.tenant_id == tenant.tenant_id,
                    UsageEvent.status == UsageEventStatus.PROCESSED,
                )
            ).scalar_one()
            closing = session.execute(
                select(Wallet.balance)
                .where(Wallet.tenant_id == tenant.tenant_id)
                .execution_options(populate_existing=True)
            ).scalar_one()
            balances.append(TenantBalance(tenant.label, tenant.opening, Decimal(billed), closing))
            codes = wallet_repository.verify_wallet(session, tenant.tenant_id)
            problems.append((tenant.label, tuple(codes)))
    return Consistency(
        processed_billable=int(processed_billable),
        ai_usage_rows=int(ai_usage_rows),
        balances=tuple(balances),
        wallet_problems=tuple(problems),
        ledger_conflicts=int(conflicts),
        unsettled=_unsettled(factory, ids),
    )


def status_counts(factory, tenant_ids: Sequence[int]) -> dict[str, int]:
    statement = (
        select(UsageEvent.status, func.count())
        .where(UsageEvent.tenant_id.in_(tenant_ids))
        .group_by(UsageEvent.status)
    )
    with factory() as session:
        return {UsageEventStatus(status).value: int(n) for status, n in session.execute(statement)}


def reprice_sample(factory, tenant_ids: Sequence[int], samples: int, rng) -> tuple[int, int]:
    """抽样已处理事件，用快照引用的版本重算（T-G `reprice_from_snapshot`），与快照逐列比。"""
    with factory() as session:
        ids = list(
            session.execute(
                select(UsageEvent.id).where(
                    UsageEvent.tenant_id.in_(tenant_ids),
                    UsageEvent.status == UsageEventStatus.PROCESSED,
                )
            ).scalars()
        )
        chosen = rng.sample(ids, min(samples, len(ids)))
        mismatches = 0
        for event_pk in chosen:
            event = session.get(UsageEvent, event_pk)
            assert event is not None and event.provider_price_version_id is not None
            assert event.pricing_rule_id is not None
            snapshot = ChargeSnapshot(
                usage_meter_type_id=event.usage_meter_type_id,
                provider_price_version_id=event.provider_price_version_id,
                fx_rate_version_id=event.fx_rate_version_id,
                pricing_rule_id=event.pricing_rule_id,
            )
            again = reprice_from_snapshot(
                session, snapshot, {name: getattr(event, name) for name in TOKENS}
            )
            stored = (
                event.provider_source_currency,
                event.provider_source_cost,
                event.fx_rate_applied,
                event.estimated_provider_cost_myr,
                event.billable_cost,
            )
            fresh = (
                again.provider_source_currency,
                again.provider_source_cost,
                again.fx_rate_applied,
                again.estimated_provider_cost_myr,
                again.billable_cost,
            )
            mismatches += stored != fresh
        session.rollback()
    return len(chosen), mismatches


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


@dataclass
class Report:
    environment: dict[str, object] = field(default_factory=dict)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    details: dict[str, dict[str, object]] = field(default_factory=dict)
    holds: dict[str, float | None] = field(default_factory=dict)


def _settle_wait() -> None:
    # 规则的第一次发布从 `boundary_after(now)` 起生效（下一个整秒）。早于它的事件会进
    # PRICING_ERROR —— 那测的就是错误路径了。
    time.sleep(2.0)


def _burst_scenario(
    name: str,
    world: World,
    ingestor: Ingestor,
    args: argparse.Namespace,
    *,
    fault_rate: float = 0.0,
    during: Callable[[WorkerPool, World, PerfModel, threading.Event], None] | None = None,
) -> tuple[list[PerfTenant], PerfModel, WorkerPool, dict[str, object]]:
    """S1 的负载：最热租户 `hot_rate`/s + 其他租户各 `other_rate`/s，持续 `duration` 秒。"""
    model = world.model(name)
    hot = world.tenant(name, "hot")
    others = [world.tenant(name, f"other-{index}") for index in range(1, args.others + 1)]
    tenants = [hot, *others]
    _settle_wait()
    pool = WorkerPool(world.settings, args.workers, fault_rate=fault_rate, seed=args.seed)
    pool.start()
    load_done = threading.Event()
    side: threading.Thread | None = None
    if during is not None:
        side = threading.Thread(target=during, args=(pool, world, model, load_done), daemon=True)
        side.start()
    streams = [(hot, model, args.hot_rate), *((other, model, args.other_rate) for other in others)]
    load = ingestor.paced(streams, args.duration)
    load_done.set()
    if side is not None:
        side.join()
    ids = [tenant.tenant_id for tenant in tenants]
    waited = wait_until_settled(world.factory, ids, args.drain_timeout)
    pool.stop()
    detail: dict[str, object] = {
        "load": load,
        "drain_wait_seconds": waited,
        "statuses": status_counts(world.factory, ids),
        "worker_outcomes": dict(pool.outcomes),
        "recovered_stale": pool.recovered,
        "dropped_beat_tasks": pool.dropped_tasks,
    }
    return tenants, model, pool, detail


def run_s1(world: World, ingestor: Ingestor, args, report: Report) -> None:
    tenants, _model, pool, detail = _burst_scenario("S1", world, ingestor, args)
    rows = _events(world.factory, [tenant.tenant_id for tenant in tenants])
    latencies = event_latencies(rows, ingestor.accepted, pool.finished)
    hot_rows = [row for row in rows if row.tenant_id == tenants[0].tenant_id]
    hot_latencies = event_latencies(hot_rows, ingestor.accepted, pool.finished)
    verdict = s1_verdict(
        latencies,
        load_reached=load_reached(detail["load"]),  # type: ignore[arg-type]
        errored=errored_count(detail["statuses"]),  # type: ignore[arg-type]
    )
    if hot_latencies:
        verdict.measured["hot_p50_seconds"] = percentile(hot_latencies, 0.50)
        verdict.measured["hot_p95_seconds"] = percentile(hot_latencies, 0.95)
        verdict.measured["hot_p99_seconds"] = percentile(hot_latencies, 0.99)
    report.verdicts["S1"] = verdict
    report.details["S1"] = detail
    report.holds["S1"] = pool.hold_mean(tenants[0].tenant_id)


def run_s2(world: World, ingestor: Ingestor, args, report: Report) -> None:
    model = world.model("S2")
    hot = world.tenant("S2", "hot")
    _settle_wait()
    # 积压阶段**没有 worker**：事件全部停在 RECEIVED，正是「预先积压」。
    ingest_seconds = ingestor.burst(hot, model, args.backlog)
    ids = [hot.tenant_id]
    statuses_before = status_counts(world.factory, ids)
    backlog = statuses_before.get(UsageEventStatus.RECEIVED.value, 0)
    pool = WorkerPool(world.settings, args.workers, seed=args.seed)
    started = time.time()
    pool.start()
    waited = wait_until_settled(world.factory, ids, args.drain_timeout)
    pool.stop()
    drain = None
    if waited is not None and pool.finished:
        # 清空用时 = 第一次 Beat 派发 → 最后一条事件的处理事务提交（不含轮询的 2 秒粒度）。
        drain = max(pool.finished.values()) - started
    statuses = status_counts(world.factory, ids)
    processed = statuses.get(UsageEventStatus.PROCESSED.value, 0)
    verdict = s2_verdict(backlog, processed, drain, backlog=args.backlog)
    report.verdicts["S2"] = verdict
    report.details["S2"] = {
        "ingest_seconds": ingest_seconds,
        "statuses_before_drain": statuses_before,
        "statuses": statuses,
        "worker_outcomes": dict(pool.outcomes),
        "dropped_beat_tasks": pool.dropped_tasks,
    }
    report.holds["S2"] = pool.hold_mean(hot.tenant_id)


def run_s3(world: World, ingestor: Ingestor, args, report: Report) -> None:
    rng = random.Random(args.seed)
    # 杀进程的时刻落在负载的 10%–90% 之间：开头没东西可杀，结尾杀了也撞不上处理中的事件。
    moments = sorted(rng.uniform(0.1, 0.9) * args.duration for _ in range(args.kills))

    def killer(pool: WorkerPool, _world: World, _model: PerfModel, done: threading.Event) -> None:
        started = time.time()
        for moment in moments:
            delay = started + moment - time.time()
            if delay > 0 and done.wait(delay):
                return
            pool.request_kill()

    tenants, _model, pool, detail = _burst_scenario(
        "S3", world, ingestor, args, fault_rate=args.fault_rate, during=killer
    )
    check = read_consistency(world.factory, tenants)
    report.verdicts["S3"] = s3_verdict(
        check, kills=len(pool.kills), kills_planned=args.kills, faults=pool.faults
    )
    report.details["S3"] = {**detail, "kill_offsets_seconds": moments}


def run_s4(world: World, ingestor: Ingestor, args, report: Report) -> None:
    publishes: list[tuple[str, float]] = []
    failures: Counter[str] = Counter()

    def publisher(_pool: WorkerPool, world: World, model: PerfModel, done: threading.Event) -> None:
        while not done.wait(args.publish_interval):
            for kind, action in (
                ("price", lambda: world.publish_price(model)),
                ("rule", lambda: world.publish_rule(model)),
                ("fx", world.publish_fx),
            ):
                try:
                    publishes.append((kind, action()))
                except Exception as error:  # noqa: BLE001 —— 失败的发布照实报告
                    failures[f"{kind}:{type(error).__name__}"] += 1

    tenants, _model, pool, detail = _burst_scenario("S4", world, ingestor, args, during=publisher)
    ids = [tenant.tenant_id for tenant in tenants]
    rows = _events(world.factory, ids)
    s1_part = s1_verdict(
        event_latencies(rows, ingestor.accepted, pool.finished),
        load_reached=load_reached(detail["load"]),  # type: ignore[arg-type]
        errored=errored_count(detail["statuses"]),  # type: ignore[arg-type]
        label="S4/S1",
    )
    checked, mismatches = reprice_sample(
        world.factory, ids, args.reprice_samples, random.Random(args.seed)
    )
    verdict = s4_verdict(
        s1_part,
        [seconds for _kind, seconds in publishes],
        reprice_checked=checked,
        reprice_mismatches=mismatches,
    )
    if failures:
        verdict.status = FAIL if verdict.status == PASS else verdict.status
        verdict.reasons.append(f"发布失败：{dict(failures)}")
    by_kind: dict[str, object] = {}
    for kind in ("price", "rule", "fx"):
        samples = [seconds for name, seconds in publishes if name == kind]
        if samples:
            by_kind[kind] = {"n": len(samples), "p95_seconds": percentile(samples, 0.95)}
    report.verdicts["S4"] = verdict
    report.details["S4"] = {**detail, "publish_by_kind": by_kind, "publish_failures": dict(failures)}


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def _describe_environment(factory, settings, args) -> dict[str, object]:
    """spec §119 要求报出硬件与数据集规模（perf_baseline.py 同理）。"""
    with factory() as session:
        mysql_version = session.execute(text("SELECT VERSION()")).scalar_one()
        events = session.execute(select(func.count()).select_from(UsageEvent)).scalar_one()
        ledger = session.execute(select(func.count()).select_from(WalletTransaction)).scalar_one()
        tenants = session.execute(select(func.count()).select_from(Tenant)).scalar_one()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "mysql": mysql_version,
        "environment": settings.environment,
        "workers": args.workers,
        "ingest_threads": args.ingest_threads,
        "lease_seconds": settings.usage_lease_seconds,
        "max_attempts": settings.usage_max_attempts,
        "beat_sweep_seconds": USAGE_BILLING_SWEEP_SECONDS,
        "sweep_budget_seconds": usage_billing.SWEEP_BUDGET_SECONDS,
        "claim_batch": usage_billing.CLAIM_BATCH,
        "dataset_before_run": {
            "usage_events": events,
            "wallet_transactions": ledger,
            "tenants": tenants,
        },
        "load": {
            "hot_rate": args.hot_rate,
            "others": args.others,
            "other_rate": args.other_rate,
            "duration": args.duration,
            "backlog": args.backlog,
            "kills": args.kills,
            "fault_rate": args.fault_rate,
            "publish_interval": args.publish_interval,
        },
    }


def _print_verdict(verdict: Verdict) -> None:
    print(f"  {verdict.scenario:<4} {verdict.status}")
    for key, value in verdict.measured.items():
        if isinstance(value, float):
            value = _seconds_text(value) if key.endswith("_seconds") else f"{value:.2f}"
        print(f"         {key}: {value}")
    for reason in verdict.reasons:
        print(f"         ✗ {reason}")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return str(value)


def _infinite_to_text(value: object) -> object:
    # json.dump 会把 inf 写成非法的 `Infinity`：先换掉。
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, dict):
        return {key: _infinite_to_text(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_infinite_to_text(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.ERROR)

    stack_settings = get_settings()
    _check_environment(stack_settings, args.database_url)
    settings = _measurement_settings(args.database_url, stack_settings)
    engine = create_database_engine(settings)
    _assert_measurement_server(engine, stack_settings)
    factory = create_session_factory(engine)

    report = Report()
    report.environment = _describe_environment(factory, settings, args)
    prefix = new_run_prefix()
    report.environment["run_prefix"] = prefix
    print(json.dumps(report.environment, indent=2, ensure_ascii=False, default=_json_default))
    print()

    world = World(factory, settings, prefix, DEFAULT_OPENING_BALANCE)
    ingestor = Ingestor(factory, prefix, args.ingest_threads)
    runners = {"S1": run_s1, "S2": run_s2, "S3": run_s3, "S4": run_s4}
    for name in SCENARIOS:
        if name not in args.scenarios:
            report.verdicts[name] = Verdict(name, NOT_RUN)
            continue
        print(f"--- {name} ---", flush=True)
        runners[name](world, ingestor, args, report)
        _print_verdict(report.verdicts[name])
        print(flush=True)

    decision = plan_c(
        report.verdicts.get("S1"),
        report.verdicts.get("S2"),
        s1_hold_mean=report.holds.get("S1"),
        s2_hold_mean=report.holds.get("S2"),
        s1_target_rate=args.hot_rate,
    )
    print(f"方案 C：{decision.conclusion}")
    for line in decision.basis:
        print(f"  - {line}")
    print()
    print(f"这一轮造的数据（前缀 {prefix}；脚本不删除任何行，账本只增）：")
    for line in world.created:
        print(f"  - {line}")
    print(f"  - 经 ingest_one 接收的用量事件 {len(ingestor.accepted)} 条，被拒 {dict(ingestor.rejected)}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                _infinite_to_text(
                    {
                        "environment": report.environment,
                        "verdicts": {
                            name: {
                                "status": verdict.status,
                                "measured": verdict.measured,
                                "reasons": verdict.reasons,
                            }
                            for name, verdict in report.verdicts.items()
                        },
                        "details": report.details,
                        "hot_tenant_lock_hold_mean_seconds": report.holds,
                        "plan_c": {"conclusion": decision.conclusion, "basis": list(decision.basis)},
                        "created": world.created,
                        "events_accepted": len(ingestor.accepted),
                        "events_rejected": dict(ingestor.rejected),
                    }
                ),
                handle,
                indent=2,
                ensure_ascii=False,
                default=_json_default,
            )
    failed = any(verdict.status in (FAIL, INVALID) for verdict in report.verdicts.values())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
