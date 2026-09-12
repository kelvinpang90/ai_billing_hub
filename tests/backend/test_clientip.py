"""Resolving the real client address behind a reverse proxy.

⚠️ 这个模块存在的理由是实现闸门第二轮指出的一个具体缺陷：不解析真实地址时，
经 nginx 的所有请求共用同一个「来源」，于是**按来源限流退化成全局限流** ——
任何人发到第 21 个认证请求，所有人都拿 429。
"""

from __future__ import annotations

from app.core.clientip import parse_trusted_proxies, resolve_client_ip

DOCKER = parse_trusted_proxies("172.16.0.0/12,10.0.0.0/8")


def test_without_trusted_proxies_the_peer_is_used() -> None:
    """直接跑 uvicorn 时直连对端就是客户端，这正好是对的。"""
    assert (
        resolve_client_ip(peer="203.0.113.9", forwarded_for="1.2.3.4", trusted=()) == "203.0.113.9"
    )


def test_a_forwarded_header_from_an_untrusted_peer_is_ignored() -> None:
    """⚠️ `X-Forwarded-For` 是客户端可以随便写的头。

    直连对端不是我们的代理时采信它，等于让任何人自选来源 —— 限流与审计
    一起被骗过去，而且不会有任何报错。
    """
    assert (
        resolve_client_ip(peer="203.0.113.9", forwarded_for="10.9.9.9", trusted=DOCKER)
        == "203.0.113.9"
    )


def test_a_forwarded_header_from_a_trusted_proxy_is_honoured() -> None:
    assert (
        resolve_client_ip(peer="172.20.0.5", forwarded_for="198.51.100.7", trusted=DOCKER)
        == "198.51.100.7"
    )


def test_the_rightmost_untrusted_hop_wins() -> None:
    """⚠️ 从**右往左**扫，不是取最左边那个。

    左端是客户端自己写的、完全不可信的部分；右端是最靠近我们的一跳。
    很多示例代码取最左边 —— 那等于直接采信客户端写的值。
    """
    assert (
        resolve_client_ip(
            peer="172.20.0.5",
            # 前两个是攻击者伪造的，第三个才是真实客户端，最后一个是我们自己的代理
            forwarded_for="1.1.1.1, 2.2.2.2, 198.51.100.7, 172.20.0.9",
            trusted=DOCKER,
        )
        == "198.51.100.7"
    )


def test_two_clients_behind_one_proxy_are_different_sources() -> None:
    """⚠️ 这条就是那个 bug 的核心。

    不解析 XFF 的话，这两个请求的「来源」是同一个（nginx 的地址），于是共用
    一个限流桶 —— 一个用户的重试能把另一个用户挡在门外。
    """
    first = resolve_client_ip(peer="172.20.0.5", forwarded_for="198.51.100.7", trusted=DOCKER)
    second = resolve_client_ip(peer="172.20.0.5", forwarded_for="203.0.113.4", trusted=DOCKER)
    assert first != second


def test_a_chain_of_only_trusted_proxies_falls_back_to_the_peer() -> None:
    assert (
        resolve_client_ip(peer="172.20.0.5", forwarded_for="10.1.1.1, 172.20.0.9", trusted=DOCKER)
        == "172.20.0.5"
    )


def test_a_missing_peer_stays_missing() -> None:
    assert resolve_client_ip(peer=None, forwarded_for="1.2.3.4", trusted=DOCKER) is None


def test_unparseable_trusted_entries_are_dropped_not_fatal() -> None:
    """⚠️ 一个笔误把整份清单变空，会静默地退回全局限流。

    所以坏条目单独丢掉并告警，好的那些照常生效。
    """
    networks = parse_trusted_proxies("172.16.0.0/12, not-an-address, 10.0.0.0/8")
    assert len(networks) == 2


def test_an_unparseable_forwarded_value_falls_back_to_the_peer() -> None:
    """⚠️ 解析不出来的候选必须丢掉，**不能原样当成来源**。

    转发头完全由外部控制。原样采用的话：攻击者每换一个随手写的字符串就拿到
    一个新的限流桶（限流形同虚设），审计表里也会被灌进垃圾。
    """
    assert (
        resolve_client_ip(peer="172.20.0.5", forwarded_for="not-an-ip", trusted=DOCKER)
        == "172.20.0.5"
    )


def test_garbage_cannot_be_used_to_mint_fresh_rate_limit_buckets() -> None:
    """同一个代理后面，两个不同的垃圾值必须归到同一个来源。"""
    first = resolve_client_ip(peer="172.20.0.5", forwarded_for="bucket-a", trusted=DOCKER)
    second = resolve_client_ip(peer="172.20.0.5", forwarded_for="bucket-b", trusted=DOCKER)
    assert first == second == "172.20.0.5"
