"""课表代理后端：登录教务系统并把课表数据转发给前端。

启动（在项目根目录执行）：
    双击 start.bat                          ← 推荐，自动找系统 Python、挑空闲端口
    python -m backend.app                   ← 等价，host/port 读 config.json
    python -m uvicorn backend.app:app --port 8000   ← 手动指定端口时用
注意：别用 python backend/app.py，包内相对导入会失败。

所有可调参数都在项目根目录的 config.json 里，见 backend/config.py。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import httpx

from . import config
from . import timetable as tt
from .jwxt import JwxtClient, JwxtError
from .logging_setup import setup_logging

setup_logging()
log = logging.getLogger("kcb.app")

DATA_DIR = config.resolve(str(config.get("paths.data_dir", "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = DATA_DIR / str(config.get("paths.session_file", "sessions.json"))
FRONTEND = config.resolve(str(config.get("paths.frontend_dir", "frontend")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "服务已启动：%s v%s · 数据目录 %s · 会话 %d 个",
        app.title, app.version, DATA_DIR, len(_sessions),
    )
    yield
    log.info("服务已停止（%d 个会话仍在磁盘留存）", len(_sessions))


app = FastAPI(
    title=str(config.get("server.title", "课表代理")),
    version=str(config.get("server.version", "1.0")),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.get("server.cors_origins") or ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_clients: dict[str, JwxtClient] = {}


def _mask(name: Any) -> str:
    """日志里不出现完整学号：1928xxxx → 19****78。"""
    text = str(name or "").strip()
    if not text:
        return "-"
    if len(text) <= 2:
        return text[0] + "*"
    if len(text) <= 4:
        return text[0] + "*" * (len(text) - 1)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


# ---------------- 会话持久化 ----------------
def _load_sessions() -> None:
    if not SESSION_FILE.exists():
        log.debug("会话文件不存在，从空会话启动：%s", SESSION_FILE)
        return
    try:
        data = json.loads(SESSION_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("会话文件读取失败，按空会话启动：%s（%s）", SESSION_FILE, exc)
        return
    if isinstance(data, dict):
        _sessions.update(data)
        log.info("载入 %d 个历史会话：%s", len(data), SESSION_FILE)
    else:
        log.warning("会话文件结构异常（顶层应为对象），已忽略：%s", SESSION_FILE)


def _save_sessions() -> None:
    try:
        SESSION_FILE.write_text(
            json.dumps(_sessions, ensure_ascii=False, indent=2), "utf-8"
        )
        log.debug("会话已落盘：%d 条 → %s", len(_sessions), SESSION_FILE)
    except OSError as exc:
        log.error("会话落盘失败：%s（%s）", SESSION_FILE, exc)


_load_sessions()


def _get_client(token: str) -> JwxtClient:
    sess = _sessions.get(token)
    if not sess:
        log.debug("令牌无效或已过期：%s…", token[:8])
        raise HTTPException(401, "未登录或登录已失效")
    client = _clients.get(token)
    if client is None:
        client = JwxtClient(sess.get("cookies") or {}, label=_mask(sess.get("username")))
        _clients[token] = client
        log.debug("为会话 %s… 重建教务客户端（进程重启后的首次访问）", token[:8])
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


# ---------------- 请求日志 ----------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("请求处理异常：%s %s", request.method, request.url.path)
        raise
    cost_ms = (time.perf_counter() - started) * 1000
    path = request.url.path
    if path.startswith("/api"):
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        log.log(level, "%s %s → %s（%.0f ms）", request.method, path, response.status_code, cost_ms)
    elif response.status_code >= 400:
        log.debug("%s %s → %s（%.0f ms）", request.method, path, response.status_code, cost_ms)
    return response


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

    who = _mask(username)
    log.info("登录请求：user=%s mode=%s", who, mode)
    started = time.perf_counter()

    client = JwxtClient(label=who)
    try:
        if mode == "local":
            client.login_local(username, password)
        else:
            client.login_sso(username, password)
    except JwxtError as exc:
        client.close()
        log.warning("登录失败：user=%s mode=%s（%.1fs）%s", who, mode, time.perf_counter() - started, exc)
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
        log.error("登录时网络请求失败：user=%s %s: %s", who, type(exc).__name__, exc)
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
    log.info("登录成功：user=%s token=%s…（%.1fs，当前会话 %d 个）",
             who, token[:8], time.perf_counter() - started, len(_sessions))
    return JSONResponse({"ok": True, "token": token, "username": username})


@app.post("/api/login-cookie")
def login_cookie(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, "Cookie 不能为空")
    who = _mask(payload.get("username") or "cookie-user")
    log.info("Cookie 登录请求：user=%s cookie 长度=%d", who, len(cookie))
    client = JwxtClient(label=who)
    try:
        client.login_with_cookie(cookie)
    except JwxtError as exc:
        client.close()
        log.warning("Cookie 登录失败：user=%s %s", who, exc)
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
    log.info("Cookie 登录成功：user=%s token=%s…", who, token[:8])
    return JSONResponse({"ok": True, "token": token})


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    client = _get_client(token)
    try:
        info = client.student_info()
    except Exception as exc:  # noqa: BLE001
        log.warning("读取学生信息失败：token=%s… %s", token[:8], exc)
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
        log.warning("读取学年学期失败：token=%s… %s", token[:8], exc)
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
    log.debug("请求课表：xnm=%s xqm=%s token=%s…", xnm, xqm, token[:8])
    try:
        raw = client.timetable(xnm, xqm)
    except JwxtError as exc:
        log.warning("拉取课表失败：xnm=%s xqm=%s %s", xnm, xqm, exc)
        raise HTTPException(502, str(exc)) from exc
    _refresh_cookies(token, client)
    data = tt.normalize(raw)
    data["term"] = {"xnm": xnm, "xqm": xqm, "label": f"{xnm} 学年 · {tt_map(xqm)}"}
    log.debug("课表规整完成：%d 门课，最大周次 %s", data["meta"]["course_count"], data["meta"]["max_week"])
    return {"ok": True, **data}


def tt_map(xqm: str) -> str:
    labels = config.get("terms") or {}
    return str(labels.get(str(xqm), f"学期{xqm}"))


@app.get("/api/raw")
def raw_timetable(
    xnm: str,
    xqm: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """原始响应，方便排查字段差异。"""
    token = _bearer(authorization)
    client = _get_client(token)
    log.info("导出原始课表数据：xnm=%s xqm=%s token=%s…", xnm, xqm, token[:8])
    try:
        return {"ok": True, "raw": client.timetable(xnm, xqm)}
    except JwxtError as exc:
        log.warning("导出原始课表失败：xnm=%s xqm=%s %s", xnm, xqm, exc)
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
    log.info("退出登录：token=%s…（剩余会话 %d 个）", token[:8], len(_sessions))
    return {"ok": True}


# ---------------- 静态资源 ----------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="static")
else:
    log.warning("前端目录不存在，静态页面不可用：%s", FRONTEND)


if __name__ == "__main__":
    import uvicorn

    # 优先级：环境变量 > config.json。start.bat 会探测空闲端口并写进 KCB_PORT。
    host = (
        os.environ.get("KCB_HOST")
        or os.environ.get("HOST")
        or str(config.get("server.host", "127.0.0.1"))
    )
    port = int(
        os.environ.get("KCB_PORT")
        or os.environ.get("PORT")
        or config.get("server.port", 8000)
    )
    log.info("监听 %s:%s", host, port)
    # log_config=None：不让 uvicorn 另建一套 handler，全部走 logging_setup
    uvicorn.run(app, host=host, port=port, log_config=None)
