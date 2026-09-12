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
