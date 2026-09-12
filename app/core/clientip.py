"""Work out who the client actually is, behind a reverse proxy.

⚠️ **这不是锦上添花，缺了它有两个具体后果**（实现闸门第二轮指出）：

1. **按来源限流会变成全局限流。**经 nginx 时每个请求的直连对端都是 nginx 的
   地址，于是所有用户共用一个桶 —— 任何人发到第 21 个认证请求，**所有人**都拿
   429。一个本该「谁滥用限谁」的控制，变成了「一个人能把所有人挡在门外」。
2. **审计里的 `ip_address` 全是 nginx 的地址**，取证时毫无用处 —— 而 §66 要求
   记录它的理由正是取证。

⚠️ **`X-Forwarded-For` 是客户端可以随便写的头。**只有在**直连对端本身是可信代理**
时才允许采信它；否则任何人都能伪造来源，把限流和审计一起骗过去。这也是为什么
「可信代理清单」必须是配置项，不能默认信任。
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)

FORWARDED_FOR_HEADER = "X-Forwarded-For"


def parse_trusted_proxies(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the comma-separated CIDR list from configuration.

    写坏的条目**直接忽略并告警**，不让整个配置失效：一个笔误把可信代理清单变空，
    会让 XFF 全部不被采信 —— 表现就是上面那条「全局限流」，而没有任何报错。
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring an unparseable trusted-proxy entry", extra={"entry": entry})
    return tuple(networks)


def _is_trusted(
    address: str, trusted: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted)


def resolve_client_ip(
    *,
    peer: str | None,
    forwarded_for: str | None,
    trusted: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """Return the caller's address, honouring `X-Forwarded-For` only from trusted peers.

    从**右往左**扫 XFF 并跳过可信代理：右端是最靠近我们的一跳，左端是客户端自己
    写的、完全不可信的部分。取最右边那个「不是我们自己代理」的地址，就是我们能
    确认的最远一跳。

    ⚠️ 取最左边那个（很多示例代码那么写）等于直接采信客户端写的值。
    """
    if peer is None:
        return None
    if not trusted or not forwarded_for:
        # 没配可信代理 = 我们没有直连 nginx 之外的依据，只能用直连对端。
        # 直接跑 uvicorn 时这恰好就是对的。
        return peer
    if not _is_trusted(peer, trusted):
        # 直连对端不是我们的代理 —— 它写的 XFF 一个字都不能信。
        return peer

    for candidate in reversed([part.strip() for part in forwarded_for.split(",")]):
        if not candidate or _is_trusted(candidate, trusted):
            continue
        # ⚠️ **必须解析得出来才用。**转发头的内容完全由外部控制，一个随手写的
        # 字符串会原样变成限流的键与审计里的地址：前者让攻击者每换一个「地址」
        # 就拿到一个新桶（限流形同虚设），后者往审计表里灌垃圾。
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    # 整条链都是我们自己的代理、或者全是垃圾：没有更多可信信息了。
    return peer
