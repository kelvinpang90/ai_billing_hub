"""Access tokens, token typing, and the refresh-token hash (spec §53)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.config import Settings
from app.core.tokens import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_PENDING_2FA,
    AuthNotConfigured,
    InvalidToken,
    decode_token,
    generate_refresh_token,
    hash_refresh_token,
    issue_access_token,
    issue_pending_2fa_token,
)


def _now() -> dt.datetime:
    """⚠️ 用真实当前时间，不要写死一个未来时间戳。

    PyJWT 2.10 起会校验 `iat` 不得在未来（签发时间在未来的令牌被判为「尚未
    生效」）。写死一个未来时刻的用例会以「令牌无效」失败，而失败原因看起来
    像签名或过期问题 —— 排查方向全错。
    """
    return dt.datetime.now(dt.UTC)


@pytest.fixture
def settings(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return Settings(jwt_secret_file=str(key))


def test_access_token_round_trips(settings: Settings) -> None:
    now = _now()
    token = issue_access_token(settings, user_id=7, role="ADMIN", session_id="fam", now=now)
    payload = decode_token(settings, token, expected_type=TOKEN_TYPE_ACCESS)
    assert payload["sub"] == "7"
    assert payload["role"] == "ADMIN"
    assert payload["sid"] == "fam"


def test_a_pending_2fa_token_is_not_accepted_as_an_access_token(settings: Settings) -> None:
    """⚠️ 这条挡的是一次完整的第二因子绕过。

    pending 令牌是「密码过了、验证码还没过」的凭据。它要是能当访问令牌用，
    2FA 就等于不存在 —— 而且不会有任何东西报错。
    """
    now = _now()
    pending = issue_pending_2fa_token(settings, user_id=7, now=now)

    # 用对类型能解开……
    assert decode_token(settings, pending, expected_type=TOKEN_TYPE_PENDING_2FA)["sub"] == "7"
    # ……当访问令牌用就不行。
    with pytest.raises(InvalidToken):
        decode_token(settings, pending, expected_type=TOKEN_TYPE_ACCESS)


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    past = dt.datetime(2026, 9, 12, 12, 0, 0, tzinfo=dt.UTC) - dt.timedelta(days=1)
    token = issue_access_token(settings, user_id=1, role="ADMIN", session_id="f", now=past)
    with pytest.raises(InvalidToken):
        decode_token(settings, token, expected_type=TOKEN_TYPE_ACCESS)


def test_a_token_signed_with_another_key_is_rejected(settings: Settings, tmp_path) -> None:
    now = _now()
    token = issue_access_token(settings, user_id=1, role="ADMIN", session_id="f", now=now)

    other_key = tmp_path / "other.key"
    other_key.write_text("a-completely-different-signing-key", encoding="utf-8")
    with pytest.raises(InvalidToken):
        decode_token(
            Settings(jwt_secret_file=str(other_key)), token, expected_type=TOKEN_TYPE_ACCESS
        )


def test_without_a_key_file_the_service_refuses_instead_of_inventing_one() -> None:
    """⚠️ **绝不能**在没配密钥时临时生成一个。

    那会让「忘了配」变成静默的，而且每次重启都让所有已签发令牌失效 ——
    表现出来是「用户随机掉登录」，几乎不可能从现象推回原因。
    """
    now = _now()
    with pytest.raises(AuthNotConfigured):
        issue_access_token(
            Settings(jwt_secret_file=""), user_id=1, role="ADMIN", session_id="f", now=now
        )


def test_a_missing_key_file_does_not_leak_its_path(tmp_path) -> None:
    """路径会进 §107 的错误响应，而它泄露宿主机布局。"""
    missing = tmp_path / "nope" / "jwt.key"
    settings = Settings(jwt_secret_file=str(missing))
    now = _now()
    with pytest.raises(AuthNotConfigured) as excinfo:
        issue_access_token(settings, user_id=1, role="ADMIN", session_id="f", now=now)
    assert "nope" not in str(excinfo.value)


def test_a_naive_utc_timestamp_is_not_read_as_local_time(settings: Settings) -> None:
    """⚠️ 回归用例，**在 UTC 的机器上看不见这个 bug**。

    数据库列按 §109 存 naive UTC，所以服务层传进来的 `now` 是 naive 的。
    `datetime.timestamp()` 对 naive 值的解释是本机时区：在 UTC+8 上，一个
    naive UTC 时刻会被算成 8 小时前的时间戳，于是令牌**一签出就是过期的**。

    这条用例显式传 naive 值，并断言解出来的 exp 落在未来。
    """
    naive_now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    token = issue_access_token(settings, user_id=1, role="ADMIN", session_id="f", now=naive_now)

    payload = decode_token(settings, token, expected_type=TOKEN_TYPE_ACCESS)
    assert payload["exp"] > dt.datetime.now(dt.UTC).timestamp()
    # 签发时刻也不能漂：差一个时区就是几小时。
    assert abs(payload["iat"] - dt.datetime.now(dt.UTC).timestamp()) < 60


def test_refresh_tokens_are_unique_and_hashed_deterministically() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    # ⚠️ 确定性是**必须**的：查表靠等值索引。用带盐的 Argon2 存刷新令牌，
    # 同一个令牌两次哈希结果不同，根本查不出来。
    assert hash_refresh_token(a) == hash_refresh_token(a)
    assert hash_refresh_token(a) != hash_refresh_token(b)
    assert len(hash_refresh_token(a)) == 64


def test_the_stored_hash_is_not_the_token_itself() -> None:
    token = generate_refresh_token()
    assert hash_refresh_token(token) != token
