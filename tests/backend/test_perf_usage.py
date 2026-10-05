"""Regression tests for scripts/perf_usage.py (AIH-TASK-033).

只测脚本**不许做什么**与**怎么下结论**：护栏、参数解析、百分位计算、各场景的通过线与方案 C
判据。负载本身不在这里跑 —— 它要一个真的 `billing_perf` 与几十分钟，是合并后的运维步骤。

⚠️ 文件放在 `tests/backend/` 而不是 `tests/`，理由与 test_perf_baseline.py 相同：脚本大量
import `app.*`，CI 的 `policy` 项（`unittest discover -s tests`）的环境里没有这些依赖。
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from app.core.config import Settings
from app.schemas.usage_ingest import EVENT_ID_PATTERN

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "perf_usage.py"


def _load():
    spec = importlib.util.spec_from_file_location("perf_usage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先登记再 exec：模块里的 `@dataclass` 会反查 `sys.modules[cls.__module__]`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


perf = _load()

STACK_URL = "mysql+pymysql://billing:pw@mysql:3306/billing?charset=utf8mb4"
PERF_URL = "mysql+pymysql://root:pw@mysql:3306/billing_perf?charset=utf8mb4"


class MeasurementDatabaseTests(unittest.TestCase):
    """量哪个库必须显式给出，只能叫 billing_perf，且不能是栈自己那个。"""

    def setUp(self) -> None:
        self.settings = Settings(database_url=STACK_URL)

    def test_it_refuses_to_fall_back_to_the_configured_database(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            perf._measurement_settings("", self.settings)
        self.assertIn("never falls back", str(raised.exception))

    def test_it_refuses_any_schema_but_billing_perf(self) -> None:
        for url in (
            STACK_URL,
            "mysql+pymysql://root:pw@mysql:3306/billing_perf2",
            "mysql+pymysql://root:pw@mysql:3306/BILLING_PERF",
            "mysql+pymysql://root:pw@mysql:3306/perf",
            "mysql+pymysql://root:pw@mysql:3306",
            "not a url",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SystemExit):
                    perf._measurement_settings(url, self.settings)

    def test_it_refuses_billing_perf_when_that_is_the_stacks_own_database(self) -> None:
        """栈自己挂在 billing_perf 上时，它的 worker 会去计费脚本写的事件。"""
        stack_on_perf = Settings(database_url="mysql+pymysql://billing:pw@mysql/billing_perf")
        with self.assertRaises(SystemExit) as raised:
            perf._measurement_settings(PERF_URL, stack_on_perf)
        self.assertIn("stack's own database", str(raised.exception))

    def test_it_accepts_billing_perf_and_changes_nothing_else(self) -> None:
        measured = perf._measurement_settings(PERF_URL, self.settings)
        self.assertEqual(measured.database_url, PERF_URL)
        self.assertEqual(measured.environment, self.settings.environment)
        self.assertEqual(measured.usage_lease_seconds, self.settings.usage_lease_seconds)
        self.assertEqual(measured.usage_max_attempts, self.settings.usage_max_attempts)


class ProductionTests(unittest.TestCase):
    def test_production_only_allows_billing_perf(self) -> None:
        production = Settings(database_url=STACK_URL, environment="production")
        with self.assertRaises(SystemExit):
            perf._check_environment(production, STACK_URL)
        with self.assertRaises(SystemExit):
            perf._check_environment(production, "")
        perf._check_environment(production, PERF_URL)

    def test_local_leaves_the_name_check_to_the_measurement_guard(self) -> None:
        local = Settings(database_url=STACK_URL, environment="local")
        perf._check_environment(local, STACK_URL)
        with self.assertRaises(SystemExit):
            perf._measurement_settings(STACK_URL, local)


class RunDataTests(unittest.TestCase):
    def test_the_prefix_is_unique_per_run(self) -> None:
        first, second = perf.new_run_prefix(), perf.new_run_prefix()
        self.assertNotEqual(first, second)
        # 它同时是供应商代码：小写字母、数字、连字符。
        self.assertRegex(first, r"^[a-z0-9][a-z0-9_-]{0,63}$")

    def test_event_ids_are_uuid7_that_ingestion_accepts(self) -> None:
        ids = {perf.uuid7() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for event_id in ids:
            self.assertIsNotNone(re.fullmatch(EVENT_ID_PATTERN.strip("^$"), event_id))


class ArgumentTests(unittest.TestCase):
    def test_the_defaults_are_the_design_load(self) -> None:
        args = perf.parse_args([])
        self.assertEqual(args.database_url, "")
        self.assertEqual(args.scenarios, ("S1", "S2", "S3", "S4"))
        self.assertEqual(args.hot_rate, 100.0)
        self.assertEqual(args.others, 3)
        self.assertEqual(args.other_rate, 5.0)
        self.assertEqual(args.duration, 300.0)
        self.assertEqual(args.backlog, 30_000)
        self.assertEqual(args.kills, 3)
        self.assertEqual(args.fault_rate, 0.01)
        self.assertEqual(args.publish_interval, 10.0)
        self.assertEqual(args.workers, 2)

    def test_scenarios_are_a_deduplicated_comma_list(self) -> None:
        args = perf.parse_args(["--scenarios", "s3, S1,s3"])
        self.assertEqual(args.scenarios, ("S3", "S1"))

    def test_bad_values_are_rejected(self) -> None:
        for argv in (
            ["--scenarios", "S5"],
            ["--scenarios", ","],
            ["--hot-rate", "0"],
            ["--hot-rate", "nan"],
            ["--backlog", "-1"],
            ["--kills", "-1"],
            ["--fault-rate", "1"],
            ["--fault-rate", "-0.1"],
            ["--workers", "0"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    perf.parse_args(argv)


class PercentileTests(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        values = [float(n) for n in range(1, 101)]
        self.assertEqual(perf.percentile(values, 0.50), 50.0)
        self.assertEqual(perf.percentile(values, 0.95), 95.0)
        self.assertEqual(perf.percentile(values, 0.99), 99.0)
        self.assertEqual(perf.percentile(values, 1.0), 100.0)

    def test_order_does_not_matter_and_small_samples_clamp(self) -> None:
        self.assertEqual(perf.percentile([3.0, 1.0, 2.0], 0.5), 2.0)
        self.assertEqual(perf.percentile([7.0], 0.99), 7.0)

    def test_unsettled_events_push_the_tail_to_infinity(self) -> None:
        values = [1.0] * 98 + [math.inf] * 2
        self.assertEqual(perf.percentile(values, 0.95), 1.0)
        self.assertTrue(math.isinf(perf.percentile(values, 0.99)))

    def test_empty_samples_and_bad_fractions_raise(self) -> None:
        with self.assertRaises(ValueError):
            perf.percentile([], 0.5)
        with self.assertRaises(ValueError):
            perf.percentile([1.0], 0.0)


class S1Tests(unittest.TestCase):
    def test_exactly_on_the_lines_passes(self) -> None:
        latencies = [1.0] * 94 + [60.0] * 4 + [300.0] * 2
        verdict = perf.s1_verdict(latencies, load_reached=True)
        self.assertEqual(verdict.status, perf.PASS)
        self.assertEqual(verdict.measured["p50_seconds"], 1.0)
        self.assertEqual(verdict.measured["p95_seconds"], 60.0)
        self.assertEqual(verdict.measured["p99_seconds"], 300.0)

    def test_p95_over_60_seconds_fails(self) -> None:
        verdict = perf.s1_verdict([1.0] * 94 + [61.0] * 6, load_reached=True)
        self.assertEqual(verdict.status, perf.FAIL)
        self.assertIn("p95", verdict.reasons[0])

    def test_p99_over_5_minutes_fails(self) -> None:
        verdict = perf.s1_verdict([1.0] * 98 + [301.0] * 2, load_reached=True)
        self.assertEqual(verdict.status, perf.FAIL)
        self.assertIn("p99", verdict.reasons[0])

    def test_a_load_that_was_not_reached_is_invalid_not_a_pass(self) -> None:
        verdict = perf.s1_verdict([1.0] * 100, load_reached=False)
        self.assertEqual(verdict.status, perf.INVALID)

    def test_events_stuck_in_an_error_state_make_the_run_invalid(self) -> None:
        verdict = perf.s1_verdict([1.0] * 100, load_reached=True, errored=3)
        self.assertEqual(verdict.status, perf.INVALID)

    def test_no_events_is_invalid(self) -> None:
        self.assertEqual(perf.s1_verdict([], load_reached=True).status, perf.INVALID)

    def test_the_load_check_uses_the_wall_clock_rate(self) -> None:
        reached = {"hot": {"target_per_second": 100.0, "achieved_per_second": 95.0}}
        short = {"hot": {"target_per_second": 100.0, "achieved_per_second": 94.9}}
        self.assertTrue(perf.load_reached(reached))
        self.assertFalse(perf.load_reached(short))

    def test_only_terminal_errors_count_as_errored(self) -> None:
        statuses = {"PROCESSED": 10, "PRICING_ERROR": 2, "FAILED_FINAL": 1, "RECEIVED": 4}
        self.assertEqual(perf.errored_count(statuses), 3)


class S2Tests(unittest.TestCase):
    def test_500_per_second_passes(self) -> None:
        verdict = perf.s2_verdict(30_000, 30_000, 60.0, backlog=30_000)
        self.assertEqual(verdict.status, perf.PASS)
        self.assertEqual(verdict.measured["events_per_second"], 500.0)

    def test_below_500_per_second_fails(self) -> None:
        verdict = perf.s2_verdict(30_000, 30_000, 60.1, backlog=30_000)
        self.assertEqual(verdict.status, perf.FAIL)

    def test_not_drained_in_time_fails(self) -> None:
        verdict = perf.s2_verdict(30_000, 12_000, None, backlog=30_000)
        self.assertEqual(verdict.status, perf.FAIL)

    def test_a_short_backlog_or_errored_events_are_invalid(self) -> None:
        short = perf.s2_verdict(29_999, 29_999, 10.0, backlog=30_000)
        errored = perf.s2_verdict(30_000, 29_000, 10.0, backlog=30_000)
        self.assertEqual(short.status, perf.INVALID)
        self.assertEqual(errored.status, perf.INVALID)


def _balance(billed: str, closing: str):
    return perf.TenantBalance("s3-hot", Decimal("100"), Decimal(billed), Decimal(closing))


def _consistency(**changes):
    values = {
        "processed_billable": 10,
        "ai_usage_rows": 10,
        "balances": (_balance("58.359375", "41.640625"),),
        "wallet_problems": (("s3-hot", ()),),
        "ledger_conflicts": 0,
        "unsettled": 0,
    }
    values.update(changes)
    return perf.Consistency(**values)


class S3Tests(unittest.TestCase):
    def test_a_consistent_run_passes(self) -> None:
        verdict = perf.s3_verdict(_consistency(), kills=3, kills_planned=3, faults=12)
        self.assertEqual(verdict.status, perf.PASS, verdict.reasons)

    def test_each_of_the_four_checks_fails_the_whole_run(self) -> None:
        broken = {
            "processed": _consistency(ai_usage_rows=9),
            "balance": _consistency(balances=(_balance("58", "41.640625"),)),
            "verify_wallet": _consistency(wallet_problems=(("s3-hot", ("BALANCE_MISMATCH",)),)),
            "ledger_conflict": _consistency(ledger_conflicts=1),
        }
        for name, check in broken.items():
            with self.subTest(check=name):
                verdict = perf.s3_verdict(check, kills=3, kills_planned=3, faults=0)
                self.assertEqual(verdict.status, perf.FAIL)
                self.assertEqual(len(verdict.reasons), 1)

    def test_events_still_in_flight_fail_the_run(self) -> None:
        verdict = perf.s3_verdict(_consistency(unsettled=2), kills=3, kills_planned=3, faults=0)
        self.assertEqual(verdict.status, perf.FAIL)

    def test_fewer_kills_than_planned_is_invalid(self) -> None:
        verdict = perf.s3_verdict(_consistency(), kills=2, kills_planned=3, faults=0)
        self.assertEqual(verdict.status, perf.INVALID)

    def test_balances_compare_as_decimals(self) -> None:
        same = perf.TenantBalance("t", Decimal("100.00000000"), Decimal("0.5"), Decimal("99.5"))
        self.assertTrue(same.consistent)


FAST = [0.2] * 90


def _s4(s1_part, publishes, checked: int = 200, mismatches: int = 0):
    return perf.s4_verdict(
        s1_part, publishes, reprice_checked=checked, reprice_mismatches=mismatches
    )


class S4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.s1_ok = perf.s1_verdict([1.0] * 100, load_reached=True, label="S4/S1")

    def test_all_three_lines_met_passes(self) -> None:
        verdict = _s4(self.s1_ok, FAST)
        self.assertEqual(verdict.status, perf.PASS, verdict.reasons)

    def test_slow_publishes_fail(self) -> None:
        verdict = _s4(self.s1_ok, [0.2] * 85 + [5.5] * 5)
        self.assertEqual(verdict.status, perf.FAIL)

    def test_a_snapshot_mismatch_fails(self) -> None:
        verdict = _s4(self.s1_ok, FAST, mismatches=1)
        self.assertEqual(verdict.status, perf.FAIL)

    def test_the_s1_lines_must_still_hold(self) -> None:
        s1_slow = perf.s1_verdict([1.0] * 90 + [70.0] * 10, load_reached=True, label="S4/S1")
        verdict = _s4(s1_slow, FAST)
        self.assertEqual(verdict.status, perf.FAIL)

    def test_nothing_published_or_nothing_repriced_is_invalid(self) -> None:
        self.assertEqual(_s4(self.s1_ok, []).status, perf.INVALID)
        self.assertEqual(_s4(self.s1_ok, FAST, checked=0).status, perf.INVALID)


def _verdict(name: str, status: str):
    return perf.Verdict(name, status)


class PlanCTests(unittest.TestCase):
    def test_lock_budget_is_strictly_more_than_one_second_per_second(self) -> None:
        self.assertFalse(perf.lock_bound(0.01, 100.0))
        self.assertTrue(perf.lock_bound(0.0101, 100.0))
        self.assertTrue(perf.lock_bound(0.003, 500.0))

    def test_both_passing_means_no_plan_c(self) -> None:
        decision = perf.plan_c(
            _verdict("S1", perf.PASS),
            _verdict("S2", perf.PASS),
            s1_hold_mean=0.5,
            s2_hold_mean=0.5,
            s1_target_rate=100.0,
        )
        self.assertEqual(decision.conclusion, perf.PLAN_C_NOT_NEEDED)

    def test_a_lock_bound_s1_failure_needs_plan_c(self) -> None:
        decision = perf.plan_c(
            _verdict("S1", perf.FAIL),
            _verdict("S2", perf.PASS),
            s1_hold_mean=0.02,
            s2_hold_mean=None,
            s1_target_rate=100.0,
        )
        self.assertEqual(decision.conclusion, perf.PLAN_C_NEEDED)
        self.assertTrue(any("钱包行锁" in line for line in decision.basis))

    def test_a_lock_bound_s2_failure_is_judged_at_500_per_second(self) -> None:
        decision = perf.plan_c(
            _verdict("S1", perf.PASS),
            _verdict("S2", perf.FAIL),
            s1_hold_mean=None,
            s2_hold_mean=0.003,
            s1_target_rate=100.0,
        )
        self.assertEqual(decision.conclusion, perf.PLAN_C_NEEDED)

    def test_a_failure_elsewhere_does_not_switch_to_plan_c(self) -> None:
        decision = perf.plan_c(
            _verdict("S1", perf.FAIL),
            _verdict("S2", perf.FAIL),
            s1_hold_mean=0.005,
            s2_hold_mean=0.001,
            s1_target_rate=100.0,
        )
        self.assertEqual(decision.conclusion, perf.PLAN_C_NOT_NEEDED)
        self.assertTrue(all("不在钱包行锁" in line for line in decision.basis))

    def test_a_lock_bound_scenario_that_passed_does_not_need_plan_c(self) -> None:
        decision = perf.plan_c(
            _verdict("S1", perf.PASS),
            _verdict("S2", perf.PASS),
            s1_hold_mean=0.05,
            s2_hold_mean=0.05,
            s1_target_rate=100.0,
        )
        self.assertEqual(decision.conclusion, perf.PLAN_C_NOT_NEEDED)

    def test_invalid_or_unmeasured_results_are_undecided(self) -> None:
        invalid = perf.plan_c(
            _verdict("S1", perf.INVALID),
            _verdict("S2", perf.PASS),
            s1_hold_mean=0.05,
            s2_hold_mean=0.0,
            s1_target_rate=100.0,
        )
        unmeasured = perf.plan_c(
            _verdict("S1", perf.FAIL),
            _verdict("S2", perf.PASS),
            s1_hold_mean=None,
            s2_hold_mean=None,
            s1_target_rate=100.0,
        )
        nothing = perf.plan_c(
            _verdict("S1", perf.NOT_RUN),
            None,
            s1_hold_mean=None,
            s2_hold_mean=None,
            s1_target_rate=100.0,
        )
        for decision in (invalid, unmeasured, nothing):
            with self.subTest(basis=decision.basis):
                self.assertEqual(decision.conclusion, perf.PLAN_C_UNDECIDED)

    def test_a_lock_bound_failure_wins_over_an_invalid_sibling(self) -> None:
        decision = perf.plan_c(
            _verdict("S1", perf.INVALID),
            _verdict("S2", perf.FAIL),
            s1_hold_mean=None,
            s2_hold_mean=0.01,
            s1_target_rate=100.0,
        )
        self.assertEqual(decision.conclusion, perf.PLAN_C_NEEDED)


class FaultInjectionTests(unittest.TestCase):
    def test_the_faulty_clock_raises_once_then_tells_the_time(self) -> None:
        clock = perf._FaultyClock(True)
        with self.assertRaises(perf.InjectedFault):
            clock()
        self.assertIsNotNone(clock())

    def test_a_healthy_clock_never_raises(self) -> None:
        clock = perf._FaultyClock(False)
        clock()
        clock()


if __name__ == "__main__":
    unittest.main()
