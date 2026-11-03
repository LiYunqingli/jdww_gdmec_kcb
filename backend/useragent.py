"""User-Agent 解析：从 UA 串里认出浏览器、系统和设备类型。

只做管理后台展示需要的粗粒度判断，不追求完整 UA 库。原则：
- 认不出来就给"未知"，不猜
- 手机 / 平板 / 电脑三分类够仪表盘用
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

_BROWSERS: list[tuple[str, str]] = [
    (r"MicroMessenger/([\d.]+)", "微信"),
    (r"DingTalk/([\d.]+)", "钉钉"),
    (r"QQBrowser/([\d.]+)", "QQ 浏览器"),
    (r"UCBrowser/([\d.]+)", "UC 浏览器"),
    (r"Edg(?:e|A|iOS)?/([\d.]+)", "Edge"),
    (r"OPR/([\d.]+)", "Opera"),
    (r"SamsungBrowser/([\d.]+)", "三星浏览器"),
    (r"Firefox/([\d.]+)", "Firefox"),
    (r"CriOS/([\d.]+)", "Chrome"),
    (r"Chrome/([\d.]+)", "Chrome"),
    (r"Version/([\d.]+).*Safari", "Safari"),
    (r"Safari/([\d.]+)", "Safari"),
    (r"curl/([\d.]+)", "curl"),
    (r"python-httpx/([\d.]+)", "httpx"),
    (r"okhttp/([\d.]+)", "OkHttp"),
]


def _major(version: str) -> str:
    return version.split(".")[0] if version else ""


@lru_cache(maxsize=256)
def parse(ua: str | None) -> dict[str, Any]:
    """返回 {browser, os, device, mobile: bool}，都是给人看的短标签。

    结果缓存：同一批人用的 UA 就那么几种，正则跑一次就够。调用方要复制
    着用，别改返回的 dict。
    """
    text = (ua or "").strip()
    if not text:
        return {"browser": "未知", "os": "未知", "device": "未知", "mobile": False}

    browser = "其他"
    for pattern, name in _BROWSERS:
        m = re.search(pattern, text)
        if m:
            major = _major(m.group(1))
            browser = f"{name} {major}".strip() if major else name
            break

    if "Windows NT 10" in text:
        os_name = "Windows"
    elif "Windows NT 6.3" in text:
        os_name = "Windows 8.1"
    elif "Windows" in text:
        os_name = "Windows"
    elif m := re.search(r"(?:iPhone|CPU) OS (\d+)[._](\d+)", text):
        os_name = f"iOS {m.group(1)}.{m.group(2)}"
    elif m := re.search(r"Mac OS X (\d+)[._](\d+)", text):
        os_name = f"macOS {m.group(1)}.{m.group(2)}"
    elif m := re.search(r"Android (\d+(?:\.\d+)?)", text):
        os_name = f"Android {m.group(1)}"
    elif "HarmonyOS" in text or "OpenHarmony" in text:
        os_name = "HarmonyOS"
    elif "CrOS" in text:
        os_name = "ChromeOS"
    elif "Linux" in text:
        os_name = "Linux"
    else:
        os_name = "未知"

    is_tablet = bool(re.search(r"iPad|Tablet|PlayBook|Nexus 7|Nexus 10", text)) or (
        "Android" in text and "Mobile" not in text
    )
    is_mobile = bool(re.search(r"Mobile|iPhone|iPod|Android.*Mobile|Windows Phone", text))
    if is_tablet:
        device = "平板"
    elif is_mobile:
        device = "手机"
    elif "bot" in text.lower() or "spider" in text.lower():
        device = "爬虫"
    else:
        device = "电脑"

    return {"browser": browser, "os": os_name, "device": device, "mobile": is_mobile and not is_tablet}
