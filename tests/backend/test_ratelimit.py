"""The in-process rate-limit backstop (spec §53).

⚠️ 这一层是**兜底**，主控是 nginx 的 `limit_req`。但它防的东西很具体：
登录为了不泄露「邮箱是否存在」，在用户不存在时**也会跑一次 Argon2 校验**。
那条控制同时是一条资源放大通道 —— 换着不存在的邮箱发请求，账号锁定永远不会
触发，而每个请求都稳定烧掉一次 Argon2。
"""

from __future__ import annotations

import pytest

from app.core.ratelimit import RateLimited, TokenBucket


def test_burst_is_allowed_then_refused() -> None:
    bucket = TokenBucket(per_minute=60, burst=3)
    for _ in range(3):
        bucket.check("1.2.3.4", now=0.0)
    with pytest.raises(RateLimited):
        bucket.check("1.2.3.4", now=0.0)


def test_sources_are_isolated() -> None:
    """一个来源打满不能影响别人 —— 否则限流器自己变成拒绝服务的工具。"""
    bucket = TokenBucket(per_minute=60, burst=1)
    bucket.check("1.1.1.1", now=0.0)
    with pytest.raises(RateLimited):
        bucket.check("1.1.1.1", now=0.0)
    bucket.check("2.2.2.2", now=0.0)


def test_the_bucket_refills_over_time() -> None:
    bucket = TokenBucket(per_minute=60, burst=1)  # 每秒回 1 个
    bucket.check("1.1.1.1", now=0.0)
    with pytest.raises(RateLimited):
        bucket.check("1.1.1.1", now=0.5)
    bucket.check("1.1.1.1", now=1.0)


def test_a_refused_request_still_refills_later() -> None:
    """⚠️ 回归用例。

    被拒的那一支如果不更新 last_seen，桶就再也不回填了 —— 一次超限会把这个
    来源**永久**挡住，而表现是「限流没有恢复窗口」，很难从现象看出原因。
    """
    bucket = TokenBucket(per_minute=60, burst=1)
    bucket.check("1.1.1.1", now=0.0)
    for _ in range(5):
        with pytest.raises(RateLimited):
            bucket.check("1.1.1.1", now=0.1)
    bucket.check("1.1.1.1", now=2.0)


def test_retry_after_is_populated() -> None:
    """没有 Retry-After，客户端只能瞎猜多久以后重试。"""
    bucket = TokenBucket(per_minute=60, burst=1)
    bucket.check("1.1.1.1", now=0.0)
    with pytest.raises(RateLimited) as excinfo:
        bucket.check("1.1.1.1", now=0.0)
    assert excinfo.value.headers["Retry-After"] == str(excinfo.value.retry_after_seconds)
    assert excinfo.value.retry_after_seconds >= 1


def test_the_message_never_mentions_an_account() -> None:
    """限流按来源计数，与邮箱无关。漏一个字就把它变成用户枚举通道。"""
    bucket = TokenBucket(per_minute=60, burst=1)
    bucket.check("1.1.1.1", now=0.0)
    with pytest.raises(RateLimited) as excinfo:
        bucket.check("1.1.1.1", now=0.0)
    lowered = excinfo.value.message.lower()
    assert "account" not in lowered
    assert "email" not in lowered
    assert "locked" not in lowered


def test_tracked_sources_are_bounded() -> None:
    """⚠️ 不设上限的话，换着 IP 发请求就能把内存撑爆 —— 限流器自己成了入口。"""
    bucket = TokenBucket(per_minute=60, burst=1)
    for index in range(12_000):
        try:
            bucket.check(f"10.0.{index // 256}.{index % 256}", now=0.0)
        except RateLimited:  # pragma: no cover - 不该发生，每个来源都是新的
            pytest.fail("distinct sources must not exhaust each other's allowance")
    assert len(bucket._buckets) <= 10_000  # noqa: SLF001 - 这条断言的对象就是内部上限


def test_invalid_configuration_fails_loudly() -> None:
    with pytest.raises(ValueError):
        TokenBucket(per_minute=0, burst=1)
