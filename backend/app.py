"""课表代理后端：登录教务系统并把课表数据转发给前端。

启动（在项目根目录执行）：
    双击 start.bat                          ← 推荐，自动找系统 Python、挑空闲端口
    python -m backend.app                   ← 等价，host/port 读 config.json
    python -m uvicorn backend.app:app --port 8000   ← 手动指定端口时用
注意：别用 python backend/app.py，包内相对导入会失败。

页面：
    /        课表
    /admin   管理后台（账号密码在 config.json 的 admin 段）

所有可调参数都在项目根目录的 config.json 里，见 backend/config.py。
"""
from __future__ import annotations

import json
import logging
import os
import platform
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

from . import admin as admin_api
from . import audit, config
from . import timetable as tt
from .jwxt import JwxtClient, JwxtError
from .logging_setup import setup_logging
from .requestinfo import client_ip, client_meta

setup_logging()
log = logging.getLogger("kcb.app")

DATA_DIR = config.resolve(str(config.get("paths.data_dir", "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = DATA_DIR / str(config.get("paths.session_file", "sessions.json"))
FRONTEND = config.resolve(str(config.get("paths.frontend_dir", "frontend")))
ADMIN_PAGE = FRONTEND / "admin.html"
LOG_FILE = config.resolve(str(config.get("logging.file.path") or "logs/app.log"))
STARTED_AT = time.time()

AUDIT = audit.store()


def _live_state() -> dict[str, Any]:
    """给仪表盘用的实时状态，audit 通过回调拿到，避免循环导入。"""
    with _lock:
        sessions = [
            {
                "username": str(s.get("username") or "-"),
                # 登录时间单独存 login_at；ts 是最后活动时间，刷新 cookie 会改
                "created": int(s.get("login_at") or s.get("ts") or 0),
                "cookies": len(s.get("cookies") or []),
            }
            for s in _sessions.values()
        ]
    sessions.sort(key=lambda x: x["created"], reverse=True)
    return {
        "sessions": len(sessions),
        "sessions_detail": sessions,
        "started_at": STARTED_AT,
        "version": str(config.get("server.version", "1.0")),
        "admin_user": admin_api.admin_name() if admin_api.enabled() else "-",
        "log_level": logging.getLevelName(logging.getLogger().level),
        "log_file": str(LOG_FILE),
        "python": platform.python_version(),
    }


def _token_user(request: Request) -> str:
    """从 Bearer 令牌反查当前请求是谁发的，查不到就是 '-'。"""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return "-"
    sess = _sessions.get(auth.split(" ", 1)[1].strip())
    return str(sess.get("username") or "-") if sess else "-"


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "服务已启动：%s v%s · 数据目录 %s · 会话 %d 个",
        app.title, app.version, DATA_DIR, len(_sessions),
    )
    if admin_api.enabled():
        log.info("管理后台已就绪：/admin（账号 %s）", admin_api.admin_name())
        if admin_api.is_default_password():
            log.warning("管理后台仍在使用默认密码，请在 config.json 的 admin.password 里改掉")
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
# 被顶下线的令牌：只留最近一批，用来给旧标签页一句准确的话，而不是含糊的"登录已失效"
_evicted: dict[str, float] = {}
_EVICTED_KEEP = 200

# Cookie 登录不强制带学号，这是它的占位账号；占位名之间不能互相顶，否则两个人都登不上
COOKIE_PLACEHOLDER = "cookie-user"


def single_session() -> bool:
    return bool(config.get("session.single", True))


def _evict_others(username: str) -> list[JwxtClient]:
    """同一账号只留最新会话：弹掉该学号的其它令牌，返回待关闭的客户端。

    只在持锁时调用。返回的客户端要在释放锁之后再 close，避免关连接池时卡住别人。
    """
    if not single_session() or not username or username == COOKIE_PLACEHOLDER:
        return []
    stale = [
        token for token, sess in _sessions.items()
        if str(sess.get("username") or "") == username
    ]
    dropped: list[JwxtClient] = []
    for token in stale:
        _sessions.pop(token, None)
        client = _clients.pop(token, None)
        if client:
            dropped.append(client)
        _evicted[token] = time.time()
    if len(_evicted) > _EVICTED_KEEP:
        for token in sorted(_evicted, key=_evicted.get)[: len(_evicted) - _EVICTED_KEEP]:
            _evicted.pop(token, None)
    return dropped


def _close_clients(clients: list[JwxtClient]) -> None:
    for client in clients:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            log.debug("关闭旧会话的教务客户端失败", exc_info=True)


AUDIT.bind_live(_live_state)


# ---------------- 会话持久化 ----------------
def _dedupe_sessions(data: dict[str, Any]) -> dict[str, Any]:
    """同一学号只留 ts 最新的那条，历史文件里堆积的旧会话一并清掉。"""
    valid = {t: s for t, s in data.items() if isinstance(s, dict)}
    if not single_session():
        return valid
    latest: dict[str, tuple[str, int]] = {}
    keep: set[str] = set()
    for token, sess in valid.items():
        name = str(sess.get("username") or "")
        if not name:
            keep.add(token)  # 没有账号信息就无从判断是不是同一个人，原样保留
            continue
        ts = int(sess.get("login_at") or sess.get("ts") or 0)
        if name not in latest or ts >= latest[name][1]:
            latest[name] = (token, ts)
    keep.update(token for token, _ in latest.values())
    dropped = len(valid) - len(keep)
    if dropped:
        log.info("会话文件里同一账号的多余会话已清理：%d 条", dropped)
    return {t: s for t, s in valid.items() if t in keep}


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
        sessions = _dedupe_sessions(data)
        _sessions.update(sessions)
        log.info("载入 %d 个历史会话：%s", len(sessions), SESSION_FILE)
        if len(sessions) != len(data):
            _save_sessions()  # 把清理结果写回，别让旧会话下次又被载入
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
        if token in _evicted:
            log.info("被顶下线的会话尝试访问：%s…（账号已在别处重新登录）", token[:8])
            raise HTTPException(401, "该账号已在另一处登录，此会话已失效")
        log.debug("令牌无效或已过期：%s…", token[:8])
        raise HTTPException(401, "未登录或登录已失效")
    client = _clients.get(token)
    if client is None:
        client = JwxtClient(sess.get("cookies") or {}, label=str(sess.get("username") or "-"))
        _clients[token] = client
        log.debug("为会话 %s… 重建教务客户端（进程重启后的首次访问）", token[:8])
    return client


def _refresh_cookies(token: str, client: JwxtClient) -> None:
    with _lock:
        sess = _sessions.get(token)
        if sess is None:
            # 请求期间被同名账号的新登录顶掉了，别把已失效的会话写回去
            log.debug("会话 %s… 已失效，跳过 cookie 刷新", token[:8])
            return
        sess["cookies"] = client.cookie_list()
        sess["ts"] = int(time.time())
        _save_sessions()


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少访问令牌")
    return authorization.split(" ", 1)[1].strip()


# ---------------- 请求日志 ----------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        cost_ms = (time.perf_counter() - started) * 1000
        log.exception("请求处理异常：%s %s", request.method, path)
        AUDIT.record(
            "request", method=request.method, path=path, status=500,
            ms=round(cost_ms, 1), username=_token_user(request), ip=client_ip(request),
        )
        raise

    cost_ms = (time.perf_counter() - started) * 1000
    if path.startswith("/api"):
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        log.log(level, "%s %s → %s（%.0f ms）", request.method, path, response.status_code, cost_ms)
        # 管理后台的轮询不进统计，否则仪表盘自己的请求会盖住真实流量
        if not path.startswith("/api/admin"):
            meta = client_meta(request)
            AUDIT.record(
                "request", method=request.method, path=path,
                status=response.status_code, ms=round(cost_ms, 1),
                username=_token_user(request),
                ip=meta["ip"], browser=meta["browser"], os=meta["os"], device=meta["device"],
            )
    elif response.status_code >= 400:
        log.debug("%s %s → %s（%.0f ms）", request.method, path, response.status_code, cost_ms)
    return response


# ---------------- 接口 ----------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "time": int(time.time()),
        "sessions": len(_sessions),
        "uptime": int(time.time() - STARTED_AT),
        "version": str(config.get("server.version", "1.0")),
    }


@app.post("/api/login")
def login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """用统一身份认证（auth.gdmec.edu.cn）的账号密码登录，再单点进教务系统。"""
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    mode = str(payload.get("mode") or "sso")
    if not username or not password:
        raise HTTPException(400, "账号和密码不能为空")

    meta = client_meta(request)
    log.info("登录请求：user=%s mode=%s ip=%s device=%s", username, mode, meta["ip"], meta["device"])
    started = time.perf_counter()

    client = JwxtClient(label=username)
    try:
        if mode == "local":
            client.login_local(username, password)
        else:
            client.login_sso(username, password)
    except JwxtError as exc:
        client.close()
        cost = time.perf_counter() - started
        log.warning("登录失败：user=%s mode=%s（%.1fs）%s", username, mode, cost, exc)
        AUDIT.record("login", username=username, ok=False, mode=mode,
                     ms=round(cost * 1000), reason=str(exc)[:200], **meta)
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
        cost = time.perf_counter() - started
        log.error("登录时网络请求失败：user=%s %s: %s", username, type(exc).__name__, exc)
        AUDIT.record("login", username=username, ok=False, mode=mode,
                     ms=round(cost * 1000), reason=f"网络错误 {type(exc).__name__}", **meta)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "message": f"网络请求失败：{exc}", "fallback": "cookie"},
        )

    cost = time.perf_counter() - started
    token = uuid.uuid4().hex
    with _lock:
        evicted = _evict_others(username)
        _sessions[token] = {
            "username": username,
            "cookies": client.cookie_list(),
            "ts": int(time.time()),
            "login_at": int(time.time()),
        }
        _clients[token] = client
        _save_sessions()
    _close_clients(evicted)
    if evicted:
        log.info("账号 %s 重新登录，已顶掉 %d 个旧会话", username, len(evicted))
        AUDIT.record("session_evicted", username=username, count=len(evicted),
                     reason="同账号在新设备登录", **meta)
    log.info("登录成功：user=%s token=%s…（%.1fs，当前会话 %d 个）",
             username, token[:8], cost, len(_sessions))
    AUDIT.record("login", username=username, ok=True, mode=mode,
                 ms=round(cost * 1000), **meta)
    return JSONResponse({"ok": True, "token": token, "username": username})


@app.post("/api/login-cookie")
def login_cookie(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, "Cookie 不能为空")
    username = str(payload.get("username") or "cookie-user").strip()
    meta = client_meta(request)
    log.info("Cookie 登录请求：user=%s cookie 长度=%d ip=%s", username, len(cookie), meta["ip"])
    started = time.perf_counter()
    client = JwxtClient(label=username)
    try:
        client.login_with_cookie(cookie)
    except JwxtError as exc:
        client.close()
        cost = time.perf_counter() - started
        log.warning("Cookie 登录失败：user=%s %s", username, exc)
        AUDIT.record("login", username=username, ok=False, mode="cookie",
                     ms=round(cost * 1000), reason=str(exc)[:200], **meta)
        return JSONResponse(status_code=401, content={"ok": False, "message": str(exc)})

    cost = time.perf_counter() - started
    token = uuid.uuid4().hex
    with _lock:
        evicted = _evict_others(username)
        _sessions[token] = {
            "username": username,
            "cookies": client.cookie_list(),
            "ts": int(time.time()),
            "login_at": int(time.time()),
        }
        _clients[token] = client
        _save_sessions()
    _close_clients(evicted)
    if evicted:
        log.info("Cookie 登录：账号 %s 的 %d 个旧会话已失效", username, len(evicted))
        AUDIT.record("session_evicted", username=username, count=len(evicted),
                     reason="同账号以 Cookie 方式重新登录", **meta)
    log.info("Cookie 登录成功：user=%s token=%s…", username, token[:8])
    AUDIT.record("login", username=username, ok=True, mode="cookie",
                 ms=round(cost * 1000), **meta)
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
    sess = _sessions.get(token) or {}
    return {"ok": True, "username": sess.get("username"), **info}


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
def logout(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    with _lock:
        sess = _sessions.pop(token, None)
        client = _clients.pop(token, None)
        _save_sessions()
    if client:
        client.close()
    if sess:
        AUDIT.record("logout", username=str(sess.get("username") or "-"), **client_meta(request))
    log.info("退出登录：user=%s token=%s…（剩余会话 %d 个）",
             (sess or {}).get("username", "-"), token[:8], len(_sessions))
    return {"ok": True}


# ---------------- 管理后台 ----------------
app.include_router(admin_api.router)


@app.get("/admin")
def admin_page() -> FileResponse:
    if not admin_api.enabled():
        raise HTTPException(404, "管理后台未启用")
    if not ADMIN_PAGE.exists():
        raise HTTPException(500, "缺少 frontend/admin.html")
    return FileResponse(ADMIN_PAGE)


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
    if admin_api.enabled():
        log.info("管理后台：http://%s:%s/admin", "127.0.0.1" if host == "0.0.0.0" else host, port)
    # log_config=None：不让 uvicorn 另建一套 handler，全部走 logging_setup
    uvicorn.run(app, host=host, port=port, log_config=None)
