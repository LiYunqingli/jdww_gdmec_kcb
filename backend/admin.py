"""管理后台接口：登录、数据总览、日志尾部。

鉴权是独立的：admin 的 token 与学生的登录 token 分开存，互相不能顶替。
账号密码在 config.json 的 admin 段，明文（个人自用项目），启动时如果是默认密码会告警。
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import audit, config
from .requestinfo import client_meta

log = logging.getLogger("kcb.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])

_lock = threading.Lock()
_tokens: dict[str, dict[str, Any]] = {}
_fails: dict[str, list[float]] = {}

_DEFAULT_PASSWORD = "kcb-admin"


def _cfg() -> dict[str, Any]:
    return config.get("admin") or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def admin_name() -> str:
    return str(_cfg().get("username") or "admin")


def is_default_password() -> bool:
    return str(_cfg().get("password") or "") == _DEFAULT_PASSWORD


def _session_seconds() -> int:
    return int(_cfg().get("session_hours") or 12) * 3600


def _prune_tokens() -> None:
    now = time.time()
    for token in [t for t, v in _tokens.items() if v["exp"] <= now]:
        _tokens.pop(token, None)


def _too_many_fails(ip: str) -> int:
    """返回还需等待的秒数，0 表示可以继续尝试。"""
    window = int(_cfg().get("fail_window_minutes") or 10) * 60
    limit = int(_cfg().get("max_login_fails") or 5)
    now = time.time()
    with _lock:
        history = [t for t in _fails.get(ip, []) if now - t < window]
        _fails[ip] = history
        if len(history) >= limit:
            return int(window - (now - history[0])) + 1
    return 0


def _note_fail(ip: str) -> None:
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def _clear_fails(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)


def _require(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "需要管理员令牌")
    token = authorization.split(" ", 1)[1].strip()
    _prune_tokens()
    info = _tokens.get(token)
    if not info:
        raise HTTPException(401, "管理员登录已失效，请重新登录")
    return token


@router.post("/login")
def admin_login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    if not enabled():
        raise HTTPException(403, "管理后台已在 config.json 中关闭")
    meta = client_meta(request)
    ip = meta["ip"]
    username = str(payload.get("username") or "").strip()

    wait = _too_many_fails(ip)
    if wait:
        log.warning("管理后台登录被限流：ip=%s 需等待 %d 秒", ip, wait)
        audit.store().record("admin_login", username=username or "-", ok=False, reason="限流", **meta)
        raise HTTPException(429, f"尝试过于频繁，请 {wait} 秒后再试")

    password = str(payload.get("password") or "")
    expect_user = admin_name()
    expect_pass = str(_cfg().get("password") or "")

    ok = hmac.compare_digest(username, expect_user) and hmac.compare_digest(password, expect_pass)
    if not ok:
        _note_fail(ip)
        log.warning("管理后台登录失败：user=%s ip=%s device=%s", username or "-", ip, meta["device"])
        audit.store().record("admin_login", username=username or "-", ok=False,
                             reason="账号或密码错误", **meta)
        raise HTTPException(401, "账号或密码不正确")

    _clear_fails(ip)
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + _session_seconds()
    with _lock:
        _tokens[token] = {"username": username, "exp": expires_at, "ip": ip, "login_at": time.time()}
    log.info("管理后台登录成功：user=%s ip=%s device=%s", username, ip, meta["device"])
    audit.store().record("admin_login", username=username, ok=True, **meta)
    return JSONResponse({
        "ok": True,
        "token": token,
        "username": username,
        "expires_in": _session_seconds(),
    })


@router.post("/logout")
def admin_logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _require(authorization)
    with _lock:
        info = _tokens.pop(token, None)
    log.info("管理后台退出：user=%s", (info or {}).get("username", "-"))
    return {"ok": True}


@router.get("/stats")
def admin_stats(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require(authorization)
    data = audit.store().snapshot()
    with _lock:
        data["cards"]["admin_online"] = len(_tokens)
    return {"ok": True, **data}


@router.get("/logs")
def admin_logs(
    lines: int = Query(default=120, ge=10, le=1000),
    source: str = Query(default="app"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require(authorization)
    if source == "events":
        events = audit.store().tail(lines)
        text = [
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(e.get('ts') or 0)))}  "
            f"{e.get('kind'):<11} {e.get('username') or '-'}  {e.get('path') or e.get('reason') or ''}".rstrip()
            for e in events
        ]
        return {"ok": True, "source": "events", "lines": text}

    file_cfg = config.get("logging.file") or {}
    path = config.resolve(str(file_cfg.get("path") or "logs/app.log"))
    return {"ok": True, "source": "app", "file": str(path), "lines": _tail_lines(path, lines)}


def _tail_lines(path: Path, limit: int) -> list[str]:
    """只读文件尾部，别为了看 100 行日志把整个文件读进内存。"""
    if not path.exists():
        return [f"（日志文件尚不存在：{path}）"]
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, 128 * 1024)
            fh.seek(size - chunk)
            raw = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return [f"（读取日志失败：{exc}）"]
    rows = raw.splitlines()
    return rows[-limit:]
