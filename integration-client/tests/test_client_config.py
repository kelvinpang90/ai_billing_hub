"""BillingClientConfig and from_env (design gate #571 v24 §2「模块与公开接口」, §6, §7).

⚠️ 示例密钥一律是全零占位值：仓库是公开的。
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import acuven_billing_client
import pytest
from acuven_billing_client import BillingClientConfig

SECRET = "sk_" + "0" * 64
WEBHOOK_SECRET_1 = "whs_" + "0" * 64
WEBHOOK_SECRET_2 = "whs_" + "1" * 64
PREFIX = "ACUVEN_BILLING_"

BASE = {
    "base_url": "https://billing.example.test",
    "api_key": "ak_" + "0" * 32,
    "key_version": 1,
    "secret": SECRET,
    "tenant_id": "00000000-0000-4000-8000-000000000001",
    "project_id": "00000000-0000-4000-8000-000000000002",
    "outbox_path": "/var/lib/app/outbox.db",
}

ENV = {
    "BASE_URL": "https://billing.example.test",
    "API_KEY": "ak_" + "0" * 32,
    "KEY_VERSION": "3",
    "API_SECRET": SECRET,
    "TENANT_ID": "00000000-0000-4000-8000-000000000001",
    "PROJECT_ID": "00000000-0000-4000-8000-000000000002",
    "OUTBOX_PATH": "/var/lib/app/outbox.db",
}


def make(**changes: object) -> BillingClientConfig:
    return BillingClientConfig(**{**BASE, **changes})  # type: ignore[arg-type]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    for name in [*ENV, "WEBHOOK_SECRETS"]:
        monkeypatch.delenv(PREFIX + name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(PREFIX + name, value)

    def set_(name: str, value: str | None) -> None:
        if value is None:
            monkeypatch.delenv(PREFIX + name, raising=False)
        else:
            monkeypatch.setenv(PREFIX + name, value)

    return set_


# --- 包骨架 ---------------------------------------------------------------------


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "acuven-billing-client"
    assert project["version"] == acuven_billing_client.__version__ == "0.1.0"
    assert project["dependencies"] == []


# --- 缺省值与不可变 ---------------------------------------------------------------


def test_defaults() -> None:
    config = make()
    assert config.webhook_secrets == {}
    assert config.delivery_interval_seconds == 10
    assert config.batch_max_events == 100
    assert config.batch_max_bytes == 1_000_000
    assert config.lease_seconds == 60
    assert config.http_timeout_seconds == 10
    assert config.reconcile_interval_seconds == 240
    assert config.reconcile_retry_seconds == 30
    assert config.delivered_retention_seconds == 7 * 86_400
    assert config.held_max_events == 1000
    assert config.pending_incidents_max == 10_000


def test_config_is_immutable() -> None:
    config = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.secret = "x"  # type: ignore[misc]


def test_webhook_secrets_are_copied() -> None:
    secrets = {1: WEBHOOK_SECRET_1}
    config = make(webhook_secrets=secrets)
    secrets[2] = WEBHOOK_SECRET_2
    assert config.webhook_secrets == {1: WEBHOOK_SECRET_1}


def test_outbox_path_accepts_a_path(tmp_path: Path) -> None:
    assert make(outbox_path=tmp_path / "outbox.db").outbox_path == str(tmp_path / "outbox.db")


# --- 构造时的校验 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": ""},
        {"base_url": "ftp://billing.example.test"},
        {"base_url": "billing.example.test"},
        {"base_url": "https://"},
        {"api_key": ""},
        {"api_key": "   "},
        {"secret": ""},
        {"tenant_id": ""},
        {"project_id": ""},
        {"outbox_path": ""},
        {"api_key": None},
        {"key_version": 0},
        {"key_version": -1},
        {"key_version": True},
        {"key_version": "1"},
        {"key_version": 1.0},
        {"webhook_secrets": {0: WEBHOOK_SECRET_1}},
        {"webhook_secrets": {True: WEBHOOK_SECRET_1}},
        {"webhook_secrets": {"1": WEBHOOK_SECRET_1}},
        {"webhook_secrets": {1: ""}},
        {"webhook_secrets": {1: "whs_ 0"}},
        {"webhook_secrets": {1: None}},
        {"webhook_secrets": [(1, WEBHOOK_SECRET_1)]},
    ],
)
def test_invalid_fields_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make(**changes)


def test_http_and_https_base_urls_are_accepted() -> None:
    assert make(base_url="http://localhost:8000").base_url == "http://localhost:8000"
    assert make(base_url="https://x.test/prefix/").base_url == "https://x.test/prefix/"


@pytest.mark.parametrize(
    "name",
    ["batch_max_events", "batch_max_bytes", "held_max_events", "pending_incidents_max"],
)
@pytest.mark.parametrize("value", [0, -1, True, 100.0, "100", None])
def test_count_parameters_must_be_positive_ints(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        make(**{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "delivery_interval_seconds",
        "lease_seconds",
        "http_timeout_seconds",
        "reconcile_interval_seconds",
        "reconcile_retry_seconds",
        "delivered_retention_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 0.0, True, float("inf"), float("nan"), "10", None])
def test_seconds_parameters_must_be_positive_finite_numbers(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        make(**{name: value})


def test_seconds_parameters_accept_int_and_float() -> None:
    config = make(delivery_interval_seconds=0.5, lease_seconds=90, http_timeout_seconds=2.5)
    assert config.delivery_interval_seconds == 0.5
    assert config.lease_seconds == 90


def test_batch_max_bytes_lower_bound() -> None:
    assert make(batch_max_bytes=17_000).batch_max_bytes == 17_000
    with pytest.raises(ValueError):
        make(batch_max_bytes=16_999)


def test_delivered_retention_lower_bound() -> None:
    assert make(delivered_retention_seconds=86_400).delivered_retention_seconds == 86_400
    with pytest.raises(ValueError):
        make(delivered_retention_seconds=86_400 - 1)
    with pytest.raises(ValueError):
        make(delivered_retention_seconds=86_399.5)


# --- 不泄露密钥 -------------------------------------------------------------------


def test_repr_and_str_hide_secrets() -> None:
    config = make(webhook_secrets={1: WEBHOOK_SECRET_1, 2: WEBHOOK_SECRET_2})
    for text in (repr(config), str(config), f"{config}"):
        assert SECRET not in text
        assert WEBHOOK_SECRET_1 not in text
        assert WEBHOOK_SECRET_2 not in text
        assert "0" * 64 not in text
        assert "1" * 64 not in text


def test_validation_errors_do_not_contain_secrets() -> None:
    with pytest.raises(ValueError) as raised:
        make(webhook_secrets={0: WEBHOOK_SECRET_1})
    assert WEBHOOK_SECRET_1 not in str(raised.value)
    with pytest.raises(ValueError) as raised:
        make(key_version=0)
    assert SECRET not in str(raised.value)


# --- from_env ------------------------------------------------------------------


def test_from_env_reads_the_variables(env) -> None:
    env("WEBHOOK_SECRETS", f"1:{WEBHOOK_SECRET_1}, 2:{WEBHOOK_SECRET_2}")
    config = BillingClientConfig.from_env()
    assert config.base_url == ENV["BASE_URL"]
    assert config.api_key == ENV["API_KEY"]
    assert config.key_version == 3
    assert config.secret == SECRET
    assert config.tenant_id == ENV["TENANT_ID"]
    assert config.project_id == ENV["PROJECT_ID"]
    assert config.outbox_path == ENV["OUTBOX_PATH"]
    assert config.webhook_secrets == {1: WEBHOOK_SECRET_1, 2: WEBHOOK_SECRET_2}


def test_from_env_custom_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv("OTHER_" + name, value)
    monkeypatch.delenv("OTHER_WEBHOOK_SECRETS", raising=False)
    assert BillingClientConfig.from_env(prefix="OTHER_").key_version == 3


def test_from_env_strips_required_values(env) -> None:
    env("API_KEY", "  ak_x \t")
    env("KEY_VERSION", " 7 ")
    env("BASE_URL", " https://billing.example.test ")
    config = BillingClientConfig.from_env()
    assert config.api_key == "ak_x"
    assert config.key_version == 7
    assert config.base_url == "https://billing.example.test"


@pytest.mark.parametrize("value", [None, "", "   ", "\t"])
def test_from_env_without_webhook_secrets(env, value: str | None) -> None:
    env("WEBHOOK_SECRETS", value)
    assert BillingClientConfig.from_env().webhook_secrets == {}


@pytest.mark.parametrize("name", list(ENV))
@pytest.mark.parametrize("value", [None, "", "   "])
def test_from_env_missing_variable_is_named(env, name: str, value: str | None) -> None:
    env(name, value)
    with pytest.raises(ValueError) as raised:
        BillingClientConfig.from_env()
    message = str(raised.value)
    assert PREFIX + name in message
    assert SECRET not in message


@pytest.mark.parametrize(
    "value", ["0", "-1", "+1", "1_0", "1.0", "01", "1 2", "١", "12345678901234567890", "x"]
)
def test_from_env_key_version_format(env, value: str) -> None:
    env("KEY_VERSION", value)
    with pytest.raises(ValueError) as raised:
        BillingClientConfig.from_env()
    assert PREFIX + "KEY_VERSION" in str(raised.value)


def test_from_env_key_version_upper_bound(env) -> None:
    env("KEY_VERSION", "9" * 19)
    assert BillingClientConfig.from_env().key_version == int("9" * 19)


@pytest.mark.parametrize("value", ["ftp://x.test", "x.test", "https://"])
def test_from_env_base_url_protocol(env, value: str) -> None:
    env("BASE_URL", value)
    with pytest.raises(ValueError) as raised:
        BillingClientConfig.from_env()
    assert PREFIX + "BASE_URL" in str(raised.value)


@pytest.mark.parametrize(
    ("value", "item"),
    [
        # 缺冒号
        (f"{WEBHOOK_SECRET_1}", 1),
        (f"1:{WEBHOOK_SECRET_1},{WEBHOOK_SECRET_2}", 2),
        # 版本不是正整数
        (f"x:{WEBHOOK_SECRET_1}", 1),
        (f"0:{WEBHOOK_SECRET_1}", 1),
        (f"-1:{WEBHOOK_SECRET_1}", 1),
        (f"+1:{WEBHOOK_SECRET_1}", 1),
        (f"1_0:{WEBHOOK_SECRET_1}", 1),
        (f"01:{WEBHOOK_SECRET_1}", 1),
        # 冒号两侧有空白
        (f"1 :{WEBHOOK_SECRET_1}", 1),
        (f"1: {WEBHOOK_SECRET_1}", 1),
        # 密钥为空或含空白
        ("1:", 1),
        (f"1:{WEBHOOK_SECRET_1},2:", 2),
        (f"1:whs_0 {WEBHOOK_SECRET_1}", 1),
        # 空项
        (f"1:{WEBHOOK_SECRET_1},", 2),
        (f",1:{WEBHOOK_SECRET_1}", 1),
        (f"1:{WEBHOOK_SECRET_1},,2:{WEBHOOK_SECRET_2}", 2),
        # 版本重复
        (f"1:{WEBHOOK_SECRET_1},1:{WEBHOOK_SECRET_2}", 2),
    ],
)
def test_from_env_malformed_webhook_secrets(env, value: str, item: int) -> None:
    env("WEBHOOK_SECRETS", value)
    with pytest.raises(ValueError) as raised:
        BillingClientConfig.from_env()
    message = str(raised.value)
    assert PREFIX + "WEBHOOK_SECRETS" in message
    assert f"item {item}" in message
    for secret in (WEBHOOK_SECRET_1, WEBHOOK_SECRET_2, "0" * 64, "1" * 64, value):
        assert secret not in message


def test_from_env_webhook_secret_may_contain_colons(env) -> None:
    # 只在第一个冒号处切开。
    env("WEBHOOK_SECRETS", "5:whs_a:b")
    assert BillingClientConfig.from_env().webhook_secrets == {5: "whs_a:b"}


def test_from_env_error_messages_never_contain_values(env) -> None:
    env("KEY_VERSION", "not-a-version-" + SECRET)
    with pytest.raises(ValueError) as raised:
        BillingClientConfig.from_env()
    assert SECRET not in str(raised.value)
    assert "not-a-version" not in str(raised.value)
