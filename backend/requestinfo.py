"""从请求里取出访客信息：IP 与设备。IP 优先信任反代给的 X-Forwarded-For。"""
from __future__ import annotations

from typing import Any

from starlette.requests import Request

from . import useragent


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "-"


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
