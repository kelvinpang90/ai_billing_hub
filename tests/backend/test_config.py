"""Settings 的取值约束（spec §53、设计闸门 #37）。

配置项的失败方式是**静默**的：一个越界的值不会报错，只会让某条路径行为怪异。
所以能靠类型系统挡住的，就不要靠「记得别设错」。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from app.core.config import Settings

AI_DURATION_FIELDS = (
    "ai_provider_timeout_seconds",
    "ai_request_deadline_seconds",
    "ai_db_pool_timeout_seconds",
    "ai_db_connect_timeout_seconds",
    "ai_db_rw_timeout_seconds",
)


def test_the_ai_settings_default_to_the_approved_design_values() -> None:
    settings = Settings()

    assert settings.ai_anthropic_key_file == "/run/secrets/billing_anthropic_key"
    assert tuple(getattr(settings, field) for field in AI_DURATION_FIELDS) == (
        60.0,
        75.0,
        2.0,
        2.0,
        5.0,
    )
    assert all(isinstance(getattr(settings, field), float) for field in AI_DURATION_FIELDS)
    assert (
        settings.ai_db_threads,
        settings.ai_db_queue_max,
        settings.ai_max_request_bytes,
        settings.ai_max_output_tokens,
        settings.ai_held_buffer_max,
        settings.ai_held_retry_seconds,
    ) == (10, 20, 65_536, 4_000, 1_000, 30)


def test_the_ai_settings_reject_invalid_limits_and_infeasible_deadlines() -> None:
    for field in (
        *AI_DURATION_FIELDS,
        "ai_db_threads",
        "ai_db_queue_max",
        "ai_max_request_bytes",
        "ai_max_output_tokens",
        "ai_held_buffer_max",
        "ai_held_retry_seconds",
    ):
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                Settings(**{field: bad})
    with pytest.raises(ValidationError):
        Settings(ai_max_request_bytes=81_921)

    with pytest.raises(ValidationError):
        Settings(ai_request_deadline_seconds=32)
    assert Settings(ai_request_deadline_seconds=33).ai_request_deadline_seconds == 33


def test_the_ai_deadline_lower_bound_tracks_database_timeouts() -> None:
    database_timeouts = {
        "ai_db_pool_timeout_seconds": 1,
        "ai_db_connect_timeout_seconds": 1,
        "ai_db_rw_timeout_seconds": 1,
    }
    assert (
        Settings(ai_request_deadline_seconds=11, **database_timeouts).ai_request_deadline_seconds
        == 11
    )
    with pytest.raises(ValidationError):
        Settings(ai_request_deadline_seconds=10, **database_timeouts)

    database_timeouts["ai_db_rw_timeout_seconds"] = 10
    assert (
        Settings(ai_request_deadline_seconds=47, **database_timeouts).ai_request_deadline_seconds
        == 47
    )
    with pytest.raises(ValidationError):
        Settings(ai_request_deadline_seconds=46, **database_timeouts)


def test_pending_token_ttls_must_be_positive(tmp_path) -> None:
    """非正值**必须让进程起不来**，不能签出一张立即过期的令牌。

    ⚠️ 这条挡的是一种「配置对了一半」的故障：TTL 设成 0 或负数时，pending 令牌
    签发即过期，于是 ADMIN 永远走不完 spec §54 强制的 2FA 注册 —— **全部新管理员
    被锁在门外**，而症状是「密码明明对却一直说令牌无效」，指不回配置。

    宁可起不来，也不要一个「看起来能用、实际谁都登不进」的部署（设计闸门 #37）。
    """
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")

    for field in ("pending_token_ttl_seconds", "enrolment_pending_token_ttl_seconds"):
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                Settings(jwt_secret_file=str(key), **{field: bad})

    # 正数照常通过 —— 少了这一句，上面那组断言在「构造 Settings 本来就会炸」
    # 时也会全绿，测的就不是约束本身了。
    ok = Settings(jwt_secret_file=str(key), pending_token_ttl_seconds=1)
    assert ok.pending_token_ttl_seconds == 1


def test_pending_token_ttl_defaults_differ_by_path() -> None:
    """两条路径的默认值不同，且注册那条更长。

    默认值被拉平的话，「注册路径来不及」这个缺陷会**悄悄回归**：功能全对、
    测试全绿，只有真人第一次注册 2FA 时才发现（设计闸门 #37）。
    """
    settings = Settings()
    assert settings.pending_token_ttl_seconds == 120
    assert settings.enrolment_pending_token_ttl_seconds == 600
    assert settings.enrolment_pending_token_ttl_seconds > settings.pending_token_ttl_seconds


def test_the_new_delivery_settings_reject_non_positive_values() -> None:
    """T0.8d 新增的这几项同样只接受正数。

    ⚠️ 每一项设成 0 或负数都会带来一种「配置对了一半」的故障，而且都不报错：

    - `password_reset_ttl_seconds` = 0 → 令牌签发即过期，**没有人能重置密码**，
      症状是「链接一点开就说失效」
    - `outbox_max_attempts` = 0 → 每一封信第一次失败就进死信，**永不重试**
    - `outbox_retry_base_seconds` = 0 → 退避没有间隔，一个持续故障会把 worker
      变成一台空转的机器
    - `outbox_recovery_batch` = 0 → 周期扫描一行也捡不起来，**恢复机制静默失效**，
      而这正是 Invariant 14 的那一半
    """
    fields = (
        "password_reset_ttl_seconds",
        "outbox_max_attempts",
        "outbox_retry_base_seconds",
        "outbox_recovery_batch",
    )
    for field in fields:
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                Settings(**{field: bad})

    # 正数照常通过 —— 少了这一句，上面那组断言在「构造 Settings 本来就会炸」
    # 时也会全绿。
    ok = Settings(password_reset_ttl_seconds=1, outbox_max_attempts=1)
    assert ok.password_reset_ttl_seconds == 1


def test_the_smtp_port_must_be_a_real_port() -> None:
    """0 或 70000 连不上任何东西，而现象是「连接超时」—— 指不回配置。"""
    for bad in (0, -1, 65_536):
        with pytest.raises(ValidationError):
            Settings(smtp_port=bad)

    assert Settings(smtp_port=465).smtp_port == 465


def test_the_ingest_batch_limits_default_to_100_events_and_1_mib() -> None:
    """设计闸门 #180 v3 §2：spec §39 的默认 100 条；请求体 1 MiB，与 nginx 集成前缀块相同。"""
    settings = Settings()

    assert settings.ingest_batch_max == 100
    assert settings.ingest_batch_max_bytes == 1024 * 1024


def test_the_ingest_batch_limits_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_INGEST_BATCH_MAX", "10")
    monkeypatch.setenv("BILLING_INGEST_BATCH_MAX_BYTES", "65536")

    settings = Settings()

    assert (settings.ingest_batch_max, settings.ingest_batch_max_bytes) == (10, 65_536)


def test_the_ingest_batch_limits_reject_values_that_disable_the_endpoint() -> None:
    """0 条的上限让每一批都 `BATCH_TOO_LARGE`；小于单条上限 16 KiB 的请求体上限连一个满长的
    元素都装不下。两种都是「端点在、却什么也收不了」，必须让进程起不来。
    """
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(ingest_batch_max=bad)
    for bad in (0, -1, 16 * 1024 - 1):
        with pytest.raises(ValidationError):
            Settings(ingest_batch_max_bytes=bad)

    # 边界值照常通过 —— 少了这一句，上面的断言在「构造 Settings 本来就会炸」时也会全绿。
    ok = Settings(ingest_batch_max=1, ingest_batch_max_bytes=16 * 1024)
    assert (ok.ingest_batch_max, ok.ingest_batch_max_bytes) == (1, 16 * 1024)


def test_the_usage_billing_lease_and_attempt_limit_default_to_120_seconds_and_10() -> None:
    """设计闸门 #181 v2 §2：`BILLING_USAGE_LEASE_SECONDS` 默认 120，`BILLING_USAGE_MAX_ATTEMPTS`
    默认 10。"""
    settings = Settings()

    assert (settings.usage_lease_seconds, settings.usage_max_attempts) == (120, 10)


def test_the_usage_billing_settings_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_USAGE_LEASE_SECONDS", "300")
    monkeypatch.setenv("BILLING_USAGE_MAX_ATTEMPTS", "5")

    settings = Settings()

    assert (settings.usage_lease_seconds, settings.usage_max_attempts) == (300, 5)


def test_the_usage_billing_settings_reject_non_positive_values() -> None:
    """租约 0 让每次认领立刻过期，回收与处理抢同一批事件；上限 0 让第一次失败就 FAILED_FINAL。"""
    for field in ("usage_lease_seconds", "usage_max_attempts"):
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                Settings(**{field: bad})

    ok = Settings(usage_lease_seconds=1, usage_max_attempts=1)
    assert (ok.usage_lease_seconds, ok.usage_max_attempts) == (1, 1)


STATUS_WEBHOOK_FIELDS = (
    "status_webhook_timeout_seconds",
    "status_webhook_retry_base_seconds",
    "status_webhook_retry_cap_seconds",
    "status_webhook_lease_seconds",
    "status_webhook_dispatch_budget_seconds",
)

AI_DELIVERY_FIELDS = (
    "ai_delivery_sweep_seconds",
    "ai_delivery_batch",
    "ai_delivery_lease_seconds",
    "ai_delivery_run_budget_seconds",
)


def test_ai_delivery_settings_default_to_the_approved_design_values() -> None:
    settings = Settings()

    assert settings.ai_ingest_base_url == "http://api:8000"
    assert tuple(getattr(settings, name) for name in AI_DELIVERY_FIELDS) == (10, 20, 60, 45)


def test_ai_delivery_settings_reject_invalid_timing_and_ingest_protocol() -> None:
    for name in AI_DELIVERY_FIELDS:
        with pytest.raises(ValidationError):
            Settings(**{name: 0})
    for invalid in ("https://api", "http://api/path", "http://user:pass@api"):
        with pytest.raises(ValidationError):
            Settings(ai_ingest_base_url=invalid)
    with pytest.raises(ValidationError):
        Settings(ai_delivery_run_budget_seconds=47)
    with pytest.raises(ValidationError):
        Settings(ai_delivery_lease_seconds=59)


def test_the_status_webhook_settings_default_to_the_design_values() -> None:
    """设计闸门 #241 v6 §2「配置」：超时 5、退避基数 30、上限 3600、租约 60、预算 12。"""
    settings = Settings()

    assert tuple(getattr(settings, name) for name in STATUS_WEBHOOK_FIELDS) == (
        5,
        30,
        3600,
        60,
        12,
    )


def test_the_status_webhook_settings_come_from_the_environment(monkeypatch) -> None:
    """`BILLING_` 前缀。"""
    for name, value in zip(STATUS_WEBHOOK_FIELDS, ("3", "10", "600", "45", "9"), strict=True):
        monkeypatch.setenv(f"BILLING_{name.upper()}", value)

    settings = Settings()

    assert tuple(getattr(settings, name) for name in STATUS_WEBHOOK_FIELDS) == (3, 10, 600, 45, 9)


def test_the_status_webhook_settings_reject_values_that_break_delivery() -> None:
    """超时 0 等于不设超时；退避 0 让失败的项目空转；租约 0 让每次领取立刻过期、被重复领取；
    预算 0 让 dispatch 一行也不领。租约不大于超时、上限小于基数同样让进程起不来。
    """
    for name in STATUS_WEBHOOK_FIELDS:
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                Settings(**{name: bad})
    with pytest.raises(ValidationError):
        Settings(status_webhook_timeout_seconds=60, status_webhook_lease_seconds=60)
    with pytest.raises(ValidationError):
        Settings(status_webhook_retry_base_seconds=60, status_webhook_retry_cap_seconds=59)

    # 边界值照常通过 —— 少了这一句，上面的断言在「构造 Settings 本来就会炸」时也会全绿。
    ok = Settings(
        status_webhook_timeout_seconds=1,
        status_webhook_lease_seconds=2,
        status_webhook_retry_base_seconds=7,
        status_webhook_retry_cap_seconds=7,
        status_webhook_dispatch_budget_seconds=1,
    )
    assert (ok.status_webhook_lease_seconds, ok.status_webhook_retry_cap_seconds) == (2, 7)


def test_email_is_unconfigured_by_default() -> None:
    """ADR-0009：**空 host = 未配置，是合法状态** —— 开发与 CI 不需要真实凭据。

    ⚠️ 而且默认值里不许出现任何真实主机名（仓库是公开的）。
    """
    settings = Settings()

    assert settings.smtp_host == ""
    assert settings.smtp_password_file == ""
    assert settings.frontend_base_url == ""


# --- FX 拉取（AIH-TASK-040，设计闸门 #183 v3 §2「BNM 适配器」） -----------------------


def test_the_fx_defaults_fetch_usd_from_bnm_over_https() -> None:
    """默认：BNM、只拉 USD、超时 10 秒。

    默认地址是 BNM 公开接口的根地址 —— ADR-0001 派生要求登记的第二个主机名例外，只限
    app/core/config.py 那一项，所以这里不重写主机名，只断言它是一个 HTTPS 根地址。
    """
    settings = Settings()

    assert settings.fx_source == "bnm"
    default_url = urlsplit(settings.fx_bnm_base_url)
    assert default_url.scheme == "https"
    assert default_url.hostname
    assert (default_url.path, default_url.query, default_url.fragment) == ("", "", "")
    assert settings.fx_currencies == "USD"
    assert settings.fx_currency_codes == ("USD",)
    assert settings.fx_fetch_timeout_seconds == 10


def test_the_fx_currencies_are_parsed_in_order() -> None:
    settings = Settings(fx_currencies="USD, EUR,JPY")

    assert settings.fx_currency_codes == ("USD", "EUR", "JPY")
    assert settings.fx_currencies == "USD,EUR,JPY"


def test_the_fx_currencies_reject_anything_but_distinct_upper_case_codes() -> None:
    """逗号分隔、大写三字母、不含 MYR、不为空、不重复（设计 §2 配置）。

    ⚠️ 配错的值必须让进程起不来：不转大写、不跳过空项。配错的项被悄悄跳过的话，
    结果就是「每天都在跑、一个币种也没拉」，要等 `fx_stale` 五天后才发现。
    """
    for bad in (
        "",
        " ",
        "usd",
        "US",
        "USDX",
        "USD,",
        ",USD",
        "USD,,EUR",
        "MYR",
        "USD,MYR",
        "USD,USD",
        "U5D",
        "USD;EUR",
    ):
        with pytest.raises(ValidationError):
            Settings(fx_currencies=bad)


def test_the_fx_currencies_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_FX_CURRENCIES", "USD,EUR")

    assert Settings().fx_currency_codes == ("USD", "EUR")


def test_the_bnm_base_url_must_be_https() -> None:
    """只接受 HTTPS 的 `base_url`：汇率走明文就能被中间人改成任何数。"""
    for bad in (
        "http://bnm.example.com",
        "ftp://bnm.example.com",
        "bnm.example.com",
        "https://",
        "https://bnm.example.com?x=1",
        "https://bnm.example.com#top",
        "https://someone@bnm.example.com",
        "",
    ):
        with pytest.raises(ValidationError):
            Settings(fx_bnm_base_url=bad)

    settings = Settings(fx_bnm_base_url="https://bnm.example.com/")
    assert settings.fx_bnm_base_url == "https://bnm.example.com"


def test_the_fx_source_and_timeout_are_checked() -> None:
    """来源只有 `bnm`；超时 0 或负数等于不设超时，worker 会被一个卡住的连接永久占着。"""
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(fx_fetch_timeout_seconds=bad)
    with pytest.raises(ValidationError):
        Settings(fx_source="manual")

    assert Settings(fx_fetch_timeout_seconds=3).fx_fetch_timeout_seconds == 3
