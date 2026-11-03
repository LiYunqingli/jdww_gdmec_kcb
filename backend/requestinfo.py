"""从请求里取出访客信息：IP 与设备。

`X-Forwarded-For` / `X-Real-IP` 是**客户端可以随便写**的请求头，只有在确认请求
来自自己配置里的反向代理时才可信。默认（`http.trusted_proxies` 为空）一律忽略
这两个头，直接用 TCP 对端地址——否则伪造一个随机 XFF 就能绕过管理后台的失败
限流，还能让 `_fails` 字典无限增长。
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from starlette.requests import Request

from . import config, useragent

log = logging.getLogger("kcb.request")

# 配置里写了才信任，进程内解析一次
_NETS: list[ipaddress._BaseNetwork] | None = None


def _networks() -> list[ipaddress._BaseNetwork]:
    global _NETS
    if _NETS is not None:
        return _NETS
    nets: list[ipaddress._BaseNetwork] = []
    for item in config.get("http.trusted_proxies") or []:
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            log.warning("http.trusted_proxies 里的项不是合法 IP/网段，已忽略：%r", item)
    _NETS = nets
    if nets:
        log.info("信任的反向代理网段：%s", ", ".join(str(n) for n in nets))
    return nets


def _norm(ip: str) -> ipaddress._BaseAddress | None:
    """解析成地址对象，顺手把 ::ffff:127.0.0.1 这类映射地址还原成 IPv4。"""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped or addr


def _from_trusted_proxy(peer: str) -> bool:
    nets = _networks()
    if not nets or not peer:
        return False
    addr = _norm(peer)
    return bool(addr) and any(addr in net for net in nets)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if _from_trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if _norm(first):  # 只接受真的能解析成 IP 的值，挡住乱填和超长垃圾
                return first
        real = (request.headers.get("x-real-ip") or "").strip()
        if _norm(real):
            return real
    return peer or "-"


def client_meta(request: Request) -> dict[str, Any]:
    """给审计事件用的一组字段：ip / ua / browser / os / device。"""
    ua = request.headers.get("user-agent", "")
    parsed = useragent.parse(ua)
    return {
        "ip": client_ip(request),
        "ua": ua[:256],
        "browser": parsed["browser"],
        "os": parsed["os"],
        "device": parsed["device"],
    }
