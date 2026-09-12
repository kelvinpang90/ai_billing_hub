"""Regression tests for scripts/perf_baseline.py.

两条用例对应复审第一轮的两个阻断项。它们钉的都是**这个脚本不许做什么**，
而不是它量得准不准 —— 测量本身没有「正确答案」可断言，但「它会不会把真实
重置令牌发出去」「它会不会删掉别人的账号」有。

⚠️ 测量脚本没有别的兜底。产品代码写错了有整套用例接着，而一个只在手工跑的
脚本写错了，唯一会发现的时刻是它已经造成后果之后。

⚠️ **这个文件在 `tests/backend/` 而不是 `tests/`，尽管它测的是一个脚本。**
两处的分界是**依赖环境**，不是「是不是脚本」：CI 的 `policy` 项跑
`unittest discover -s tests`，那个环境**只装流程脚本要的东西、没有 pydantic**。
`scripts/perf_baseline.py` 大量 import `app.*`，放在 `tests/` 会让 `policy` 直接
`ModuleNotFoundError`（本地全装着，所以只有 CI 看得见 —— 这条注释就是为此写的）。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from app.core.config import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "perf_baseline.py"


def _load():
    spec = importlib.util.spec_from_file_location("perf_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ⚠️ 必须先登记进 sys.modules 再 exec：模块里的 `@dataclass` 会反查
    # `sys.modules[cls.__module__]`，没登记的话报的是
    # 「'NoneType' object has no attribute '__dict__'」—— 一句完全指不回原因的话。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STACK_URL = "mysql+pymysql://billing:pw@mysql:3306/billing?charset=utf8mb4"
PERF_URL = "mysql+pymysql://root:pw@mysql:3306/billing_perf?charset=utf8mb4"


class MeasurementDatabaseTests(unittest.TestCase):
    """量哪个库，必须是显式选的，而且不能是栈自己那个。"""

    def setUp(self) -> None:
        self.perf = _load()
        self.settings = Settings(database_url=STACK_URL)

    def test_it_refuses_the_stacks_own_database(self) -> None:
        """⚠️ 这是复审第一轮的阻断项，不是假想的风险。

        S2 会写出上千行 `PENDING` 的 outbox 行，payload 里带着**真实的重置令牌**。
        compose 里的 celery-beat 每 60 秒跑一次恢复扫描，worker 在**它自己的进程**
        里投递它们 —— 脚本里那个 `NullTransport` 只换掉了脚本自己进程的传输层。

        本地栈刻意不配 SMTP，所以这件事在本地跑不出来；配了 SMTP 的环境会真的把
        上千封带令牌的信发出去。
        """
        with self.assertRaises(SystemExit) as raised:
            self.perf._measurement_settings(STACK_URL, self.settings)
        self.assertIn("celery worker", str(raised.exception))

    def test_it_refuses_to_fall_back_to_the_configured_database(self) -> None:
        """不给 `--database-url` 时**直接失败**，不是悄悄用 `BILLING_DATABASE_URL`。

        「没给就用默认的」在这里正好等于「用栈自己那个库」—— 也就是上面那条
        阻断项的默认行为。
        """
        with self.assertRaises(SystemExit):
            self.perf._measurement_settings("", self.settings)

    def test_it_accepts_a_separate_schema(self) -> None:
        measured = self.perf._measurement_settings(PERF_URL, self.settings)
        self.assertEqual(measured.database_url, PERF_URL)
        # ⚠️ 换库不能顺手改掉别的配置：反枚举的耗时地板、退避参数等等都还要照原样。
        self.assertEqual(measured.environment, self.settings.environment)


class BaselineAccountTests(unittest.TestCase):
    def test_the_account_address_is_unique_per_run(self) -> None:
        """⚠️ 复审第一轮的第二个阻断项。

        清理阶段是**无条件删除**，连审计记录（§66）一起。地址固定 + 「有就复用」
        的话，库里已有的同名账号会被这个脚本连审计轨迹一起销毁。
        """
        perf = _load()
        template = perf.BASELINE_EMAIL_TEMPLATE
        self.assertIn("{run}", template)
        self.assertNotEqual(template.format(run="a" * 12), template.format(run="b" * 12))


if __name__ == "__main__":
    unittest.main()
