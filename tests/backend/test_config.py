"""Settings 的取值约束（spec §53、设计闸门 #37）。

配置项的失败方式是**静默**的：一个越界的值不会报错，只会让某条路径行为怪异。
所以能靠类型系统挡住的，就不要靠「记得别设错」。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


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


def test_email_is_unconfigured_by_default() -> None:
    """ADR-0009：**空 host = 未配置，是合法状态** —— 开发与 CI 不需要真实凭据。

    ⚠️ 而且默认值里不许出现任何真实主机名（仓库是公开的）。
    """
    settings = Settings()

    assert settings.smtp_host == ""
    assert settings.smtp_password_file == ""
    assert settings.frontend_base_url == ""
