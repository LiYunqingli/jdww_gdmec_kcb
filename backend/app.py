"""课表代理后端：登录教务系统并把课表数据转发给前端。

启动（在项目根目录执行）：
    双击 start.bat                          ← 推荐，自动找系统 Python
    python -m uvicorn backend.app:app --port 8000
    python -m backend.app                   （等价，走 __main__ 块）
注意：别用 python backend/app.py，包内相对导入会失败。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import httpx

from . import timetable as tt
from .jwxt import JwxtClient, JwxtError

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSION_FILE = DATA_DIR / "sessions.json"

app = FastAPI(title="课表代理", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_clients: dict[str, JwxtClient] = {}


# ---------------- 会话持久化 ----------------
def _load_sessions() -> None:
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                _sessions.update(data)
        except Exception:
            pass


def _save_sessions() -> None:
    try:
        SESSION_FILE.write_text(json.dumps(_sessions, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


_load_sessions()


def _get_client(token: str) -> JwxtClient:
    sess = _sessions.get(token)
    if not sess:
        raise HTTPException(401, "未登录或登录已失效")
    client = _clients.get(token)
    if client is None:
        client = JwxtClient(sess.get("cookies") or {})
        _clients[token] = client
    return client


def _refresh_cookies(token: str, client: JwxtClient) -> None:
    if token in _sessions:
        _sessions[token]["cookies"] = client.cookie_list()
        _sessions[token]["ts"] = int(time.time())
        _save_sessions()


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少访问令牌")
    return authorization.split(" ", 1)[1].strip()


# ---------------- 接口 ----------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": int(time.time()), "sessions": len(_sessions)}


@app.post("/api/login")
def login(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """用统一身份认证（auth.gdmec.edu.cn）的账号密码登录，再单点进教务系统。"""
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    mode = str(payload.get("mode") or "sso")
    if not username or not password:
        raise HTTPException(400, "账号和密码不能为空")

    client = JwxtClient()
    try:
        if mode == "local":
            client.login_local(username, password)
        else:
            client.login_sso(username, password)
    except JwxtError as exc:
        client.close()
        return JSONResponse(
            status_code=401,
            content={
                "ok": False,
                "message": str(exc),
                "hint": "登录走的是统一身份认证（auth.gdmec.edu.cn）→ 教务单点（jwxt.gdmec.edu.cn/sso/ddlogin）。"
                        "如果提示账号密码错误，请先在 auth.gdmec.edu.cn 网页确认能登录。",
                "fallback": "cookie",
            },
        )
    except httpx.HTTPError as exc:
        client.close()
        return JSONResponse(
            status_code=502,
            content={"ok": False, "message": f"网络请求失败：{exc}", "fallback": "cookie"},
        )

    token = uuid.uuid4().hex
    with _lock:
        _sessions[token] = {
            "username": username,
            "cookies": client.cookie_list(),
            "ts": int(time.time()),
        }
        _clients[token] = client
        _save_sessions()
    return JSONResponse({"ok": True, "token": token, "username": username})


@app.post("/api/login-cookie")
def login_cookie(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, "Cookie 不能为空")
    client = JwxtClient()
    try:
        client.login_with_cookie(cookie)
    except JwxtError as exc:
        client.close()
        return JSONResponse(status_code=401, content={"ok": False, "message": str(exc)})

    token = uuid.uuid4().hex
    with _lock:
        _sessions[token] = {
            "username": payload.get("username") or "cookie-user",
            "cookies": client.cookie_list(),
            "ts": int(time.time()),
        }
        _clients[token] = client
        _save_sessions()
    return JSONResponse({"ok": True, "token": token})


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    client = _get_client(token)
    try:
        info = client.student_info()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"读取学生信息失败：{exc}") from exc
    _refresh_cookies(token, client)
    return {"ok": True, "username": _sessions[token].get("username"), **info}


@app.get("/api/terms")
def terms(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    client = _get_client(token)
    try:
        data = client.terms()
    except JwxtError as exc:
        raise HTTPException(401, str(exc)) from exc
    _refresh_cookies(token, client)
    return {"ok": True, **data}


@app.get("/api/timetable")
def timetable(
    xnm: str,
    xqm: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _bearer(authorization)
    client = _get_client(token)
    try:
        raw = client.timetable(xnm, xqm)
    except JwxtError as exc:
        raise HTTPException(502, str(exc)) from exc
    _refresh_cookies(token, client)
    data = tt.normalize(raw)
    data["term"] = {"xnm": xnm, "xqm": xqm, "label": f"{xnm} 学年 · {tt_map(xqm)}"}
    return {"ok": True, **data}


def tt_map(xqm: str) -> str:
    return {"3": "第一学期", "12": "第二学期", "16": "第三学期"}.get(str(xqm), f"学期{xqm}")


@app.get("/api/raw")
def raw_timetable(
    xnm: str,
    xqm: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """原始响应，方便排查字段差异。"""
    token = _bearer(authorization)
    client = _get_client(token)
    try:
        return {"ok": True, "raw": client.timetable(xnm, xqm)}
    except JwxtError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    with _lock:
        _sessions.pop(token, None)
        client = _clients.pop(token, None)
        _save_sessions()
    if client:
        client.close()
    return {"ok": True}


# ---------------- 静态资源 ----------------
FRONTEND = ROOT / "frontend"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
