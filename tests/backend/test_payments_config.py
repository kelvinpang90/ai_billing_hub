"""支付配置项与 Stripe 凭据读取（设计闸门 #580 v3 §2「配置」、§6，AIH-TASK-173）。

配错的值让进程起不来；凭据读不到时返回 `None` 并记 ERROR（只带路径与原因，不带密钥）。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, StripeCredentials, read_stripe_credentials
from app.payments import StripeGateway, get_payment_gateway

SECRET_KEY = "sk_test_payments_config_secret_value"
LIVE_SECRET_KEY = "sk_live_payments_config_secret_value"
WEBHOOK_SECRET = "whsec_payments_config_signing_value"
API_BASE_URL = "https://stripe-api.example.test"
TENANT_A = "3f2c8a1e-9b4d-4c6e-8f1a-2b3c4d5e6f70"
TENANT_B = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def enabled_settings(tmp_path: Path, *, secret: str = SECRET_KEY, **overrides) -> Settings:
    values = {
        "payments_enabled": True,
        "stripe_secret_key_file": write(tmp_path / "stripe.key", f"  {secret}\n"),
        "stripe_webhook_secret_file": write(tmp_path / "stripe-webhook.key", f"{WEBHOOK_SECRET}\n"),
        "stripe_api_base_url": API_BASE_URL,
    }
    values.update(overrides)
    return Settings(**values)


# --- 配置项 -------------------------------------------------------------------


def test_the_payment_settings_default_to_the_design_table() -> None:
    settings = Settings()

    assert settings.payments_enabled is False
    assert settings.payment_topups_open is False
    assert settings.payment_topup_tenant_allowlist == ""
    assert settings.payment_topup_tenant_ids == frozenset()
    assert settings.stripe_secret_key_file == ""
    assert settings.stripe_webhook_secret_file == ""
    assert settings.stripe_api_base_url == ""
    assert settings.stripe_api_timeout_seconds == 10
    assert settings.payment_topup_min == Decimal("10.00")
    assert isinstance(settings.payment_topup_min, Decimal)
    assert settings.payment_topup_max is None
    assert settings.payment_pending_timeout_seconds == 3600
    assert settings.payment_reuse_window_seconds == 60
    assert settings.payment_webhook_tolerance_seconds == 300
    assert settings.payment_event_poll_seconds == 15


def test_the_payment_settings_come_from_billing_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("BILLING_PAYMENT_TOPUPS_OPEN", "true")
    monkeypatch.setenv("BILLING_PAYMENT_TOPUP_MIN", "20.50")
    monkeypatch.setenv("BILLING_PAYMENT_TOPUP_MAX", "5000")
    monkeypatch.setenv("BILLING_STRIPE_API_BASE_URL", f"{API_BASE_URL}/")
    monkeypatch.setenv("BILLING_PAYMENT_EVENT_POLL_SECONDS", "30")

    settings = Settings()

    assert settings.payments_enabled is True
    assert settings.payment_topups_open is True
    assert settings.payment_topup_min == Decimal("20.50")
    assert settings.payment_topup_max == Decimal("5000")
    assert settings.stripe_api_base_url == API_BASE_URL
    assert settings.payment_event_poll_seconds == 30


def test_an_empty_topup_max_means_no_maximum(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PAYMENT_TOPUP_MAX", "")
    assert Settings().payment_topup_max is None
    assert Settings(payment_topup_max="  ").payment_topup_max is None


@pytest.mark.parametrize(
    ("field", "lowest", "highest"),
    [
        ("stripe_api_timeout_seconds", 1, 60),
        ("payment_pending_timeout_seconds", 2400, 86_400),
        ("payment_reuse_window_seconds", 0, 300),
        ("payment_webhook_tolerance_seconds", 60, 600),
        ("payment_event_poll_seconds", 5, 300),
    ],
)
def test_the_integer_payment_settings_have_the_design_ranges(
    field: str, lowest: int, highest: int
) -> None:
    assert getattr(Settings(**{field: lowest}), field) == lowest
    assert getattr(Settings(**{field: highest}), field) == highest
    for bad in (lowest - 1, highest + 1):
        with pytest.raises(ValidationError):
            Settings(**{field: bad})


def test_the_topup_minimum_is_not_below_the_myr_floor() -> None:
    assert Settings(payment_topup_min="2.00").payment_topup_min == Decimal("2.00")
    for bad in ("1.99", "0", "-10", "NaN"):
        with pytest.raises(ValidationError):
            Settings(payment_topup_min=bad)


def test_the_topup_maximum_is_between_the_minimum_and_the_stripe_ceiling() -> None:
    assert Settings(payment_topup_max="10.00").payment_topup_max == Decimal("10.00")
    assert Settings(payment_topup_max="999999.99").payment_topup_max == Decimal("999999.99")
    for bad in ("9.99", "1000000.00", "999999.991"):
        with pytest.raises(ValidationError):
            Settings(payment_topup_max=bad)
    with pytest.raises(ValidationError):
        Settings(payment_topup_min="50.00", payment_topup_max="49.99")


def test_the_stripe_api_base_url_is_empty_or_an_https_root() -> None:
    assert Settings(stripe_api_base_url="").stripe_api_base_url == ""
    assert Settings(stripe_api_base_url=f"{API_BASE_URL}/").stripe_api_base_url == API_BASE_URL
    for bad in (
        "http://stripe-api.example.test",
        f"{API_BASE_URL}?x=1",
        f"{API_BASE_URL}#top",
        "https://user:pass@stripe-api.example.test",
        "stripe-api.example.test",
    ):
        with pytest.raises(ValidationError):
            Settings(stripe_api_base_url=bad)


def test_the_tenant_allowlist_is_parsed_into_canonical_public_ids() -> None:
    settings = Settings(
        payment_topup_tenant_allowlist=f" {TENANT_A.upper()} ,, {TENANT_B.replace('-', '')} ,"
    )

    assert settings.payment_topup_tenant_ids == frozenset({TENANT_A, TENANT_B})
    assert Settings(payment_topup_tenant_allowlist=" , ").payment_topup_tenant_ids == frozenset()


def test_the_tenant_allowlist_reads_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST", f"{TENANT_A},{TENANT_B}")
    assert Settings().payment_topup_tenant_ids == frozenset({TENANT_A, TENANT_B})


@pytest.mark.parametrize("bad", ["tenant-a", f"{TENANT_A},nope", f"{TENANT_A}0", "12345"])
def test_a_non_uuid_allowlist_entry_fails_at_startup(bad: str) -> None:
    with pytest.raises(ValidationError):
        Settings(payment_topup_tenant_allowlist=bad)


# --- 凭据读取 -----------------------------------------------------------------


def test_credentials_are_not_read_while_payments_are_off(tmp_path: Path, caplog) -> None:
    settings = Settings(
        payments_enabled=False,
        stripe_secret_key_file=str(tmp_path / "missing.key"),
        stripe_webhook_secret_file=str(tmp_path / "missing-webhook.key"),
        stripe_api_base_url=API_BASE_URL,
    )

    with caplog.at_level(logging.DEBUG):
        assert read_stripe_credentials(settings) is None
        assert get_payment_gateway(settings) is None
    assert caplog.records == []


def test_credentials_are_read_stripped_with_livemode_from_the_prefix(tmp_path: Path) -> None:
    credentials = read_stripe_credentials(enabled_settings(tmp_path))

    assert credentials == (SECRET_KEY, WEBHOOK_SECRET, False)
    assert isinstance(credentials, StripeCredentials)
    assert credentials.livemode is False

    live = read_stripe_credentials(enabled_settings(tmp_path, secret=LIVE_SECRET_KEY))
    assert live is not None
    assert live.livemode is True


def test_the_gateway_factory_builds_a_stripe_gateway(tmp_path: Path) -> None:
    gateway = get_payment_gateway(enabled_settings(tmp_path))

    assert isinstance(gateway, StripeGateway)
    assert gateway.livemode is False


def unusable_cases(tmp_path: Path) -> list[tuple[str, dict[str, object], str]]:
    missing = str(tmp_path / "missing.key")
    empty = write(tmp_path / "empty.key", "  \n")
    wrong_key = write(tmp_path / "wrong.key", "rk_live_restricted_secret_value")
    wrong_webhook = write(tmp_path / "wrong-webhook.key", "sk_test_not_a_webhook_secret")
    return [
        ("secret file unset", {"stripe_secret_key_file": ""}, "stripe_secret_key_file"),
        ("webhook file unset", {"stripe_webhook_secret_file": ""}, "stripe_webhook_secret_file"),
        ("secret missing", {"stripe_secret_key_file": missing}, missing),
        ("webhook missing", {"stripe_webhook_secret_file": missing}, missing),
        ("secret empty", {"stripe_secret_key_file": empty}, empty),
        ("webhook empty", {"stripe_webhook_secret_file": empty}, empty),
        ("secret prefix", {"stripe_secret_key_file": wrong_key}, wrong_key),
        ("webhook prefix", {"stripe_webhook_secret_file": wrong_webhook}, wrong_webhook),
        ("no base url", {"stripe_api_base_url": ""}, "stripe_api_base_url"),
    ]


def test_unusable_credentials_return_none_and_log_only_the_path(tmp_path: Path, caplog) -> None:
    for name, overrides, mentioned in unusable_cases(tmp_path):
        caplog.clear()
        settings = enabled_settings(tmp_path, **overrides)
        with caplog.at_level(logging.ERROR, logger="app.core.config"):
            assert read_stripe_credentials(settings) is None, name
            assert get_payment_gateway(settings) is None, name
        errors = [record for record in caplog.records if record.levelno == logging.ERROR]
        assert errors, name
        text = caplog.text
        assert mentioned in text, name
        for secret in (
            SECRET_KEY,
            WEBHOOK_SECRET,
            "rk_live_restricted_secret_value",
            "sk_test_not_a_webhook_secret",
        ):
            assert secret not in text, name


def test_credentials_never_appear_in_reprs(tmp_path: Path) -> None:
    settings = enabled_settings(tmp_path)
    credentials = read_stripe_credentials(settings)
    gateway = get_payment_gateway(settings)

    for shown in (repr(settings), repr(credentials), str(credentials), repr(gateway)):
        assert SECRET_KEY not in shown
        assert WEBHOOK_SECRET not in shown
