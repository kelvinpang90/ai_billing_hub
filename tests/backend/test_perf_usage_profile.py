"""Regression tests for scripts/perf_usage_profile.py (AIH-TASK-055).

只测参数解析、护栏复用、百分位与占比的计算、`cpu.stat` 解析、`--json-out` 不可写时提前失败，
以及派发节奏（Beat 节奏下每 10 秒派与生产 Beat 相同个数的扫描、`--continuous` 不变，AIH-TASK-142）。
负载本身不在这里跑、也不连数据库 —— 它要一个真的 `billing_perf`，是合并后的运维步骤。

⚠️ 文件放在 `tests/backend/` 的理由与 test_perf_usage.py 相同：脚本大量 import `app.*`。
"""

from __future__ import annotations

import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.celery_app import (
    USAGE_BILLING_SWEEP_EXPIRES_SECONDS,
    USAGE_BILLING_SWEEP_PARALLELISM,
    USAGE_BILLING_SWEEP_SECONDS,
    USAGE_STALE_RECOVERY_EXPIRES_SECONDS,
)
from app.core.config import Settings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT = SCRIPTS / "perf_usage_profile.py"


def _load():
    spec = importlib.util.spec_from_file_location("perf_usage_profile", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先登记再 exec：模块里的 `@dataclass` 会反查 `sys.modules[cls.__module__]`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


profile = _load()

STACK_URL = "mysql+pymysql://billing:pw@mysql:3306/billing?charset=utf8mb4"
PERF_URL = "mysql+pymysql://root:pw@mysql:3306/billing_perf?charset=utf8mb4"


class ArgumentTests(unittest.TestCase):
    def test_the_defaults(self) -> None:
        args = profile.parse_args([])
        self.assertEqual(args.database_url, "")
        self.assertEqual(args.events, 2000)
        self.assertEqual(args.workers, (1, 2))
        self.assertFalse(args.continuous)
        self.assertIsNone(args.json_out)

    def test_workers_are_a_deduplicated_comma_list_in_order(self) -> None:
        self.assertEqual(profile.parse_args(["--workers", "2, 1,2"]).workers, (2, 1))
        self.assertEqual(profile.parse_args(["--workers", "4"]).workers, (4,))

    def test_continuous_is_a_flag(self) -> None:
        self.assertTrue(profile.parse_args(["--continuous"]).continuous)

    def test_bad_values_are_rejected(self) -> None:
        for argv in (
            ["--events", "0"],
            ["--events", "-5"],
            ["--workers", "0"],
            ["--workers", "1,-2"],
            ["--workers", ","],
            ["--workers", "two"],
            ["--drain-timeout", "0"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    profile.parse_args(argv)


class GuardReuseTests(unittest.TestCase):
    """护栏来自 scripts/perf_usage.py 本身，不是一份复制。"""

    def setUp(self) -> None:
        self.settings = Settings(database_url=STACK_URL)

    def test_the_guards_are_loaded_from_perf_usage(self) -> None:
        self.assertEqual(Path(profile.perf.__file__).resolve(), SCRIPTS / "perf_usage.py")
        for name in (
            "_measurement_settings",
            "_check_environment",
            "_assert_measurement_server",
            "_same_target",
            "_identity",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(profile, name), f"{name} must not be copied")

    def test_any_schema_but_billing_perf_is_refused(self) -> None:
        for url in (
            "",
            STACK_URL,
            "mysql+pymysql://root:pw@mysql:3306/billing_perf2",
            "mysql+pymysql://root:pw@mysql:3306/BILLING_PERF",
            "mysql+pymysql://root:pw@mysql:3306",
            "not a url",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SystemExit):
                    profile.guarded_settings(url, self.settings)

    def test_production_only_allows_billing_perf(self) -> None:
        production = Settings(database_url=STACK_URL, environment="production")
        with self.assertRaises(SystemExit):
            profile.guarded_settings(STACK_URL, production)
        measured = profile.guarded_settings(PERF_URL, production)
        self.assertEqual(measured.database_url, PERF_URL)

    def test_billing_perf_is_refused_when_it_is_the_stacks_own_database(self) -> None:
        stack_on_perf = Settings(database_url="mysql+pymysql://billing:pw@mysql/billing_perf")
        with self.assertRaises(SystemExit):
            profile.guarded_settings(PERF_URL, stack_on_perf)


class SummaryTests(unittest.TestCase):
    def test_mean_and_nearest_rank_percentiles_in_milliseconds(self) -> None:
        values = [n / 1000 for n in range(1, 101)]
        stats = profile.summarize_ms(values)
        assert stats is not None
        self.assertEqual(stats["n"], 100)
        self.assertAlmostEqual(stats["mean_ms"], 50.5)
        self.assertAlmostEqual(stats["p50_ms"], 50.0)
        self.assertAlmostEqual(stats["p95_ms"], 95.0)

    def test_no_samples_is_none_not_zero(self) -> None:
        self.assertIsNone(profile.summarize_ms([]))
        self.assertIsNone(profile.ratio(1.0, 0.0))
        self.assertEqual(profile.ratio(1.0, 4.0), 0.25)


def _sample(**changes):
    row = {
        "finished": 0.0,
        "wall": 0.095,
        "sql_count": 20,
        "sql_seconds": 0.040,
        "event_pk": 1,
        "outcome": "PROCESSED",
        "cpu": 0.030,
        "sql_in_lock": 0.006,
        "lock_wait": 0.001,
        "lock_hold": 0.009,
        "lock_window": 0.010,
        "resolve": 0.020,
        "price": 0.001,
        "posting": 0.008,
    }
    row.update(changes)
    return row


def _claim(**changes):
    row = {"finished": 0.0, "wall": 0.010, "sql_count": 22, "sql_seconds": 0.008, "claimed": 20}
    row.update(changes)
    return row


class StageTableTests(unittest.TestCase):
    def test_shares_and_lock_free_time_per_event(self) -> None:
        table = profile.stage_table([_sample(), _sample(event_pk=2)], [_claim()])
        self.assertEqual(table["events"], 2)
        self.assertAlmostEqual(table["db_share_of_wall"], 0.040 / 0.095)
        self.assertAlmostEqual(table["cpu_share_of_wall"], 0.030 / 0.095)
        self.assertAlmostEqual(table["stages"]["锁外（墙钟 − 持锁）"]["mean_ms"], 86.0)
        self.assertAlmostEqual(table["stages"]["锁外的数据库耗时"]["mean_ms"], 34.0)
        self.assertEqual(table["sql_per_event"]["mean"], 20)
        self.assertEqual(table["empty_claims"], 0)

    def test_only_processed_events_count_and_unlocked_ones_skip_lock_stages(self) -> None:
        rows = [
            _sample(),
            _sample(event_pk=2, outcome="FAILED", wall=5.0),
            _sample(event_pk=3, lock_wait=None, lock_hold=None, lock_window=None),
        ]
        table = profile.stage_table(rows, [])
        self.assertEqual(table["events"], 2)
        self.assertEqual(table["stages"]["持钱包锁"]["n"], 1)
        self.assertEqual(table["stages"]["process_event 墙钟"]["n"], 2)
        self.assertIsNone(table["stages"]["claim_events（每次）"])


class BreakdownTests(unittest.TestCase):
    def test_the_four_categories_partition_the_worker_time(self) -> None:
        # 2 个 worker × 100 秒；忙 90 秒：锁 10、锁外数据库 50，余下 30 是 CPU。
        result = profile.breakdown(
            window_seconds=100.0, workers=2, busy_seconds=90.0, lock_seconds=10.0, db_seconds=50.0
        )
        shares = result["shares"]
        self.assertAlmostEqual(sum(shares.values()), 1.0)
        self.assertAlmostEqual(shares[profile.LOCK], 0.05)
        self.assertAlmostEqual(shares[profile.DB], 0.25)
        self.assertAlmostEqual(shares[profile.CPU], 0.15)
        self.assertAlmostEqual(shares[profile.IDLE], 0.55)
        self.assertEqual(result["largest"], profile.IDLE)

    def test_the_largest_category_is_named(self) -> None:
        result = profile.breakdown(
            window_seconds=10.0, workers=1, busy_seconds=10.0, lock_seconds=1.0, db_seconds=6.0
        )
        self.assertEqual(result["largest"], profile.DB)

    def test_noise_is_clamped_so_nothing_goes_negative(self) -> None:
        result = profile.breakdown(
            window_seconds=10.0, workers=1, busy_seconds=11.0, lock_seconds=4.0, db_seconds=9.0
        )
        seconds = result["seconds"]
        self.assertTrue(all(value >= 0 for value in seconds.values()))
        self.assertAlmostEqual(sum(result["shares"].values()), 1.0)
        self.assertEqual(seconds[profile.IDLE], 0.0)
        self.assertEqual(seconds[profile.CPU], 0.0)

    def test_an_empty_window_has_no_largest_category(self) -> None:
        result = profile.breakdown(
            window_seconds=0.0, workers=2, busy_seconds=0.0, lock_seconds=0.0, db_seconds=0.0
        )
        self.assertIsNone(result["largest"])


CPU_STAT = """usage_usec 123456
user_usec 100000
system_usec 23456
nr_periods 500
nr_throttled 42
throttled_usec 987654
nr_bursts 0
"""
CPU_STAT_LATER = """usage_usec 223456
nr_periods 600
nr_throttled 50
throttled_usec 1000000
"""


class CpuStatTests(unittest.TestCase):
    def test_parse(self) -> None:
        values = profile.parse_cpu_stat(CPU_STAT + "garbage line here\nbad notanumber\n")
        self.assertEqual(values["nr_throttled"], 42)
        self.assertEqual(values["throttled_usec"], 987654)
        self.assertNotIn("bad", values)

    def test_read_and_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpu.stat"
            path.write_text(CPU_STAT, encoding="utf-8")
            before = profile.read_throttle(path)
            path.write_text(CPU_STAT_LATER, encoding="utf-8")
            after = profile.read_throttle(path)
        self.assertEqual(before, {"nr_throttled": 42, "throttled_usec": 987654})
        self.assertEqual(
            profile.counter_delta(before, after), {"nr_throttled": 8, "throttled_usec": 12346}
        )

    def test_a_missing_file_or_missing_keys_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "no-such" / "cpu.stat"
            self.assertIsNone(profile.read_throttle(missing))
            partial = Path(directory) / "cpu.stat"
            partial.write_text("usage_usec 1\nnr_throttled 3\n", encoding="utf-8")
            self.assertIsNone(profile.read_throttle(partial))
        self.assertIsNone(profile.counter_delta(None, {"nr_throttled": 1}))
        self.assertIsNone(profile.counter_delta({"nr_throttled": 1}, None))


class JsonOutTests(unittest.TestCase):
    def test_an_unwritable_path_fails_before_anything_else(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = str(Path(directory) / "missing-dir" / "out.json")
            with mock.patch.object(profile, "guarded_settings") as guard:
                with self.assertRaises(SystemExit) as raised:
                    profile.main(["--database-url", PERF_URL, "--json-out", bad])
            guard.assert_not_called()
        self.assertIn("not writable", str(raised.exception))

    def test_a_writable_path_passes_and_keeps_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            path.write_text('{"old": true}', encoding="utf-8")
            profile.check_json_out(str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
        profile.check_json_out(None)


class _Pipe:
    """worker 子进程那一端管道的替身：只记下送回主进程的消息。"""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send(self, message: tuple) -> None:
        self.sent.append(message)


class DispatchTests(unittest.TestCase):
    """派发线程的节奏：直接驱动 `_dispatch`，用假的时刻，不起子进程、不连库。"""

    def _pool(self, *, continuous: bool):
        pool = profile.ProfilePool(Settings(database_url=PERF_URL), 2, continuous=continuous)
        pool._next_sweep = pool._next_recovery = 1000.0
        return pool

    @staticmethod
    def _sweeps(pool) -> list[tuple[str, float]]:
        return [task for task in pool._pending if task[0] == "sweep"]

    def test_the_beat_cadence_queues_as_many_sweeps_as_production_beat(self) -> None:
        pool = self._pool(continuous=False)
        for tick in range(3):
            with self.subTest(tick=tick):
                now = 1000.0 + tick * USAGE_BILLING_SWEEP_SECONDS
                pool._pending.clear()
                pool._dispatch(now)
                sweeps = self._sweeps(pool)
                self.assertEqual(len(sweeps), USAGE_BILLING_SWEEP_PARALLELISM)
                self.assertEqual(
                    {expires for _kind, expires in sweeps},
                    {now + USAGE_BILLING_SWEEP_EXPIRES_SECONDS},
                )
                pool._pending.clear()
                pool._dispatch(now + USAGE_BILLING_SWEEP_SECONDS / 2)
                self.assertEqual(self._sweeps(pool), [])

    def test_continuous_gives_every_idle_worker_one_sweep_regardless_of_the_beat(self) -> None:
        pool = self._pool(continuous=True)
        pool.workers = [
            SimpleNamespace(busy=False),
            SimpleNamespace(busy=True),
            SimpleNamespace(busy=False),
        ]
        pool._dispatch(1000.0)
        self.assertEqual(self._sweeps(pool), [("sweep", math.inf)] * 2)
        # 已排着的扫描够空闲的 worker 拿：到了下一个 Beat 时刻也不再多派。
        pool._dispatch(1000.0 + USAGE_BILLING_SWEEP_SECONDS)
        self.assertEqual(len(self._sweeps(pool)), 2)
        pool._pending.clear()
        pool.workers[1].busy = False
        pool._dispatch(1000.0 + USAGE_BILLING_SWEEP_SECONDS + 1)
        self.assertEqual(len(self._sweeps(pool)), 3)

    def test_stale_recovery_is_dispatched_in_both_modes(self) -> None:
        for continuous in (False, True):
            with self.subTest(continuous=continuous):
                pool = self._pool(continuous=continuous)
                pool._dispatch(1000.0)
                self.assertIn(
                    ("recover", 1000.0 + USAGE_STALE_RECOVERY_EXPIRES_SECONDS), pool._pending
                )


class WorkerSweepTests(unittest.TestCase):
    """worker 子进程里的一次扫描：替换服务函数，不连库。"""

    def test_a_sweep_is_bill_pending_events_with_the_production_parallelism(self) -> None:
        billing = profile.usage_billing
        claim = SimpleNamespace(id=7)
        batches = iter([[claim], []])

        def bill(factory, **kwargs):
            # 与 `bill_pending_events` 的循环一样，按模块全局名调用认领与处理。
            while claims := billing.claim_events(factory, lease_seconds=kwargs["lease_seconds"]):
                for item in claims:
                    billing.process_event(factory, item, max_attempts=kwargs["max_attempts"])
            return billing.SweepResult(claimed=1)

        pipe = _Pipe()
        with (
            mock.patch.object(billing, "claim_events", side_effect=lambda *a, **k: next(batches)),
            mock.patch.object(billing, "process_event", return_value=billing.Outcome.PROCESSED),
            mock.patch.object(billing, "bill_pending_events", side_effect=bill) as service,
        ):
            originals = (billing.claim_events, billing.process_event)
            profile._profile_sweep("factory", pipe, lease=120, attempts=5)
            self.assertEqual((billing.claim_events, billing.process_event), originals)
        service.assert_called_once()
        self.assertEqual(service.call_args.kwargs["parallelism"], USAGE_BILLING_SWEEP_PARALLELISM)
        # 每次认领一条 batch：第一次认领到 1 条并处理，第二次是空认领。
        self.assertEqual([message[0] for message in pipe.sent], ["batch", "batch"])
        (_, first_claim, first_rows), (_, second_claim, second_rows) = pipe.sent
        self.assertEqual(first_claim["claimed"], 1)
        self.assertEqual(
            [(row["event_pk"], row["outcome"]) for row in first_rows], [(7, "PROCESSED")]
        )
        self.assertEqual(second_claim["claimed"], 0)
        self.assertEqual(second_rows, [])


if __name__ == "__main__":
    unittest.main()
