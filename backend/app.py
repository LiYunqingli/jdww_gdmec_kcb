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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
    _start_persist()
    log.info(
        "服务已启动：%s v%s · 数据目录 %s · 会话 %d 个",
        app.title, app.version, DATA_DIR, len(_sessions),
    )
    if _CORS_ORIGINS:
        log.info("跨域已开放给：%s", ", ".join(_CORS_ORIGINS))
    else:
        log.debug("跨域未开放（同源部署不需要）；要跨域请在 server.cors_origins 里列出来源")
    if admin_api.enabled():
        log.info("管理后台已就绪：/admin（账号 %s）", admin_api.admin_name())
        if admin_api.is_default_password():
            log.warning("管理后台仍在使用默认密码，请在 config.json 的 admin.password 里改掉")
    yield
    # 关服务前把攒着的事件和会话都写掉，别丢最后一批
    _stop_persist()
    AUDIT.close()
    log.info("服务已停止（%d 个会话已落盘）", len(_sessions))


app = FastAPI(
    title=str(config.get("server.title", "课表代理")),
    version=str(config.get("server.version", "1.0")),
    lifespan=lifespan,
)

# 跨域默认关掉：前端和接口同源，不需要 CORS。开放 ["*"] 等于允许任何网页
# 借用户的浏览器调这个代理，配合 /api/login 就能拿去撞学校 SSO。
_CORS_ORIGINS = [str(o).strip() for o in (config.get("server.cors_origins") or []) if str(o).strip()]
if _CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_clients: dict[str, JwxtClient] = {}
# 被顶下线的令牌：留最近一批，好给旧标签页一句准确的提示，而不是含糊的"登录已失效"
_evicted: dict[str, float] = {}
_EVICTED_KEEP = 200

# 会话数上限：Cookie 通道的会话彼此不顶（见 COOKIE_PLACEHOLDER），
# 不封顶会随登录次数一直堆 client 和连接池。超出时按最后活动时间淘汰。
_SESSIONS_MAX_DEFAULT = 512
_SWEEP_INTERVAL = 60          # 多久扫一次空闲超时的会话
_last_sweep = 0.0

# Cookie 登录不强制带学号，这是它的占位账号；占位名之间不能互相顶，否则两个人都登不上
COOKIE_PLACEHOLDER = "cookie-user"


def _sessions_max() -> int:
    try:
        return max(10, int(config.get("session.max_sessions") or _SESSIONS_MAX_DEFAULT))
    except (TypeError, ValueError):
        return _SESSIONS_MAX_DEFAULT


def single_session() -> bool:
    return bool(config.get("session.single", True))


def _session_ttl_seconds() -> float:
    """空闲多久算过期（秒）。配 0 或负数表示永不过期。"""
    try:
        hours = float(config.get("session.idle_timeout_hours") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    return hours * 3600 if hours > 0 else 0.0


def _expired(sess: dict[str, Any]) -> bool:
    """ts 是最后一次活动时间（每次刷新 cookie 都会更新），所以这是空闲超时。"""
    ttl = _session_ttl_seconds()
    if not ttl:
        return False
    ts = float(sess.get("ts") or 0)
    if not ts:
        return True  # 没有活动时间的旧数据按过期处理，别让它永生
    return (time.time() - ts) > ttl


def _prune_sessions_locked() -> list[JwxtClient]:
    """清掉空闲超时的会话，返回待关闭的客户端（调用方在锁外 close）。

    只在持锁时调用。
    """
    stale = [token for token, sess in _sessions.items() if _expired(sess)]
    if not stale:
        return []
    dropped: list[JwxtClient] = []
    for token in stale:
        _sessions.pop(token, None)
        client = _clients.pop(token, None)
        if client:
            dropped.append(client)
    _save_sessions()
    log.info("清掉 %d 个空闲超时的会话（上限 %s 小时）",
             len(dropped), config.get("session.idle_timeout_hours"))
    return dropped


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


def _cap_sessions_locked() -> list[JwxtClient]:
    """会话数超过上限时，按最后活动时间淘汰最旧的，返回待关闭的客户端。

    只在持锁时调用。这里不写进 `_evicted`：那是"被同账号新登录顶掉"的语义，
    容量淘汰对用户来说只是"登录失效"，别让他以为被盗号了。
    """
    over = len(_sessions) - _sessions_max()
    if over <= 0:
        return []
    victims = sorted(_sessions.items(), key=lambda kv: float(kv[1].get("ts") or 0))[:over]
    dropped: list[JwxtClient] = []
    for token, _ in victims:
        _sessions.pop(token, None)
        client = _clients.pop(token, None)
        if client:
            dropped.append(client)
    _save_sessions()
    log.warning("会话数超过上限 %d，已淘汰最久未活动的 %d 个", _sessions_max(), len(victims))
    return dropped


def _sweep_sessions() -> None:
    """周期清理空闲超时的会话，由持久化线程每 _SWEEP_INTERVAL 秒调用一次。"""
    global _last_sweep
    if not _session_ttl_seconds():
        return  # 配置成永不过期，扫了也白扫
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    with _lock:
        dropped = _prune_sessions_locked()
    _close_clients(dropped)


# ---------------- 登录限流 ----------------
# 按 IP 做窗口计数，防止有人拿这个代理反复撞学校 SSO。
# 只覆盖账号密码登录：Cookie 通道没有可爆破的口令，算进来反而会连累正常用户。
_LOGIN_FAILS_MAX_IPS = 2000
_login_fails: dict[str, list[float]] = {}
_rl_lock = threading.Lock()


def _login_limit() -> tuple[int, int]:
    """(窗口内允许的失败次数, 窗口秒数)。配置写坏了回落到 8 次 / 10 分钟。"""
    try:
        limit = int(config.get("login.max_fails") or 8)
        window = int(config.get("login.fail_window_minutes") or 10) * 60
    except (TypeError, ValueError):
        return 8, 600
    return max(1, limit), max(60, window)


def _login_blocked_for(ip: str) -> int:
    """还要等多少秒才能再试，0 表示放行。"""
    limit, window = _login_limit()
    now = time.time()
    with _rl_lock:
        history = [t for t in _login_fails.get(ip, []) if now - t < window]
        if history:
            _login_fails[ip] = history
        else:
            _login_fails.pop(ip, None)  # 窗口过了就整条丢掉，别留空壳占位
        if len(history) >= limit:
            return int(window - (now - history[0])) + 1
    return 0


def _note_login_fail(ip: str) -> None:
    with _rl_lock:
        if ip not in _login_fails and len(_login_fails) >= _LOGIN_FAILS_MAX_IPS:
            # 顶掉最久没更新的那条，保证字典有界
            oldest = min(_login_fails, key=lambda k: _login_fails[k][-1] if _login_fails[k] else 0.0)
            _login_fails.pop(oldest, None)
        _login_fails.setdefault(ip, []).append(time.time())


def _clear_login_fails(ip: str) -> None:
    with _rl_lock:
        _login_fails.pop(ip, None)


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
        if _session_ttl_seconds():
            fresh = {t: s for t, s in sessions.items() if not _expired(s)}
            if len(fresh) != len(sessions):
                log.info("载入时清掉 %d 个空闲超时的历史会话", len(sessions) - len(fresh))
            sessions = fresh
        _sessions.update(sessions)
        log.info("载入 %d 个历史会话：%s", len(sessions), SESSION_FILE)
        if len(sessions) != len(data):
            _save_sessions()  # 把清理结果写回，别让旧会话下次又被载入
    else:
        log.warning("会话文件结构异常（顶层应为对象），已忽略：%s", SESSION_FILE)


_persist_dirty = threading.Event()
_persist_stop = threading.Event()
_persist_lock = threading.Lock()
_persist_thread: threading.Thread | None = None
_PERSIST_DELAY = 0.3          # 连续改动合并一下：开学季不要一次登录写一次盘
_TS_PERSIST_INTERVAL = 300    # 仅仅 ts 变了的话，5 分钟才落一次盘


def _save_sessions() -> None:
    """标记「会话需要落盘」，真正的 IO 交给后台线程，请求线程不碰磁盘。

    每次刷新 cookie 都重写整份 sessions.json 太贵，而绝大多数写的内容其实没变
    （cookie 集合一样，只有 ts 在动）。
    """
    _persist_dirty.set()


def _flush_sessions() -> None:
    """把当前会话快照原子写盘。不能在持 _lock 时调用。"""
    with _lock:
        # 浅拷贝就够：内层 dict 的键集合创建后不再变动，值都是整体替换，
        # json.dumps 期间不会撞上 "dictionary changed size"。
        snapshot = dict(_sessions)
    tmp = SESSION_FILE.with_name(SESSION_FILE.name + ".tmp")
    try:
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        with _persist_lock:
            tmp.write_text(payload, "utf-8")
            os.replace(tmp, SESSION_FILE)
            try:
                # 里面是明文的 SSO cookie / TGT，尽量只给当前用户读。
                # Windows 上 chmod 基本是空操作，不影响。
                os.chmod(SESSION_FILE, 0o600)
            except OSError:
                pass
        log.debug("会话已落盘：%d 条 → %s", len(snapshot), SESSION_FILE)
    except OSError as exc:
        log.error("会话落盘失败：%s（%s）", SESSION_FILE, exc)


def _persist_loop() -> None:
    while not _persist_stop.is_set():
        dirty = _persist_dirty.wait(1.0)
        _sweep_sessions()  # 顺手每 60 秒清一次空闲超时的会话
        if not dirty:
            continue
        _persist_stop.wait(_PERSIST_DELAY)  # 攒一下，期间的改动一起写
        _persist_dirty.clear()
        _flush_sessions()


def _start_persist() -> None:
    global _persist_thread
    if _persist_thread and _persist_thread.is_alive():
        return
    _persist_stop.clear()
    _persist_thread = threading.Thread(
        target=_persist_loop, name="kcb-session-persist", daemon=True
    )
    _persist_thread.start()


def _stop_persist() -> None:
    _persist_stop.set()
    if _persist_thread:
        _persist_thread.join(timeout=3)
    _flush_sessions()  # 关服务前保证最后一次落地


_load_sessions()
_start_persist()


def _get_client(token: str) -> JwxtClient:
    """取会话对应的教务客户端，没有就按快照重建。

    查表 + 建客户端都在锁里做：并发首访同一 token 不能建出两个客户端
    （否则有一个永远没人关，连接池白占着）。
    """
    stale: JwxtClient | None = None
    client: JwxtClient | None = None
    was_evicted = False
    was_expired = False

    with _lock:
        was_evicted = token in _evicted
        sess = _sessions.get(token)
        if sess is not None and _expired(sess):
            _sessions.pop(token, None)
            stale = _clients.pop(token, None)
            _save_sessions()
            sess = None
            was_expired = True

        if sess is not None:
            client = _clients.get(token)
            if client is None:
                label = str(sess.get("label") or sess.get("username") or "-")
                client = JwxtClient(sess.get("cookies") or {}, label=label)
                _clients[token] = client
                log.debug("为会话 %s… 重建教务客户端（进程重启后的首次访问）", token[:8])

    if stale is not None:
        try:
            stale.close()
        except Exception:  # noqa: BLE001
            log.debug("关闭过期会话的教务客户端失败", exc_info=True)

    if client is not None:
        return client
    if was_evicted:
        log.info("被顶下线的会话尝试访问：%s…（账号已在别处重新登录）", token[:8])
        raise HTTPException(401, "该账号已在另一处登录，此会话已失效")
    if was_expired:
        log.info("空闲超时的会话尝试访问：%s…", token[:8])
        raise HTTPException(401, "登录已过期，请重新登录")
    log.debug("令牌无效或已过期：%s…", token[:8])
    raise HTTPException(401, "未登录或登录已失效")


def _refresh_cookies(token: str, client: JwxtClient) -> None:
    """把客户端最新的 cookie 写回会话，并判断这次到底值不值得落盘。

    只有 cookie 集合真的变了、或者 ts 已经 5 分钟没存过，才标记脏。
    """
    dirty = False
    with _lock:
        sess = _sessions.get(token)
        if sess is None:
            # 请求期间被同名账号的新登录顶掉了，别把已失效的会话写回去
            log.debug("会话 %s… 已失效，跳过 cookie 刷新", token[:8])
            return
        fresh = client.cookie_list()
        if fresh != (sess.get("cookies") or []):
            sess["cookies"] = fresh
            dirty = True
        now = int(time.time())
        if now - int(sess.get("ts") or 0) >= _TS_PERSIST_INTERVAL:
            dirty = True
        sess["ts"] = now
    if dirty:
        _save_sessions()


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少访问令牌")
    return authorization.split(" ", 1)[1].strip()


# ---------------- 教务数据缓存 ----------------
# 刷新一次页面要打两次教务（学年学期 + 课表），来回切学期又会重复拉。
# 键里带 token：换个账号登录不会串数据；重新登录拿到新 token，缓存自然失效。
_CACHE_MAX = 512
_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cache_ttl() -> float:
    try:
        return float(config.get("cache.ttl_seconds") or 0)
    except (TypeError, ValueError):
        return 0.0


def _cache_get(key: tuple[Any, ...]) -> Any | None:
    if _cache_ttl() <= 0:
        return None
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        expire_at, value = item
        if expire_at <= time.time():
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[Any, ...], value: Any) -> None:
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    now = time.time()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            for k in [k for k, (exp, _v) in _cache.items() if exp <= now]:
                _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX:
                # 整锅清空会让所有会话同时回打教务，只淘汰最快到期的那批
                victims = sorted(_cache, key=lambda k: _cache[k][0])[: max(1, _CACHE_MAX // 4)]
                for k in victims:
                    _cache.pop(k, None)
        _cache[key] = (now + ttl, value)


def _cache_drop_token(token: str) -> None:
    """退出登录时把这个 token 的缓存摘掉，别让缓存跟着登录次数一起涨。"""
    with _cache_lock:
        for k in [k for k in _cache if len(k) > 1 and k[1] == token]:
            _cache.pop(k, None)


# ---------------- 并发去重（single-flight） ----------------
# 同一个 key 的并发首访只放一个人去打教务，其余人等它的结果。
# 键里带 token，所以不同账号互不影响。
_FLIGHT_WAIT_MAX = 30.0
_inflight: dict[tuple[Any, ...], threading.Event] = {}
_inflight_lock = threading.Lock()


def _claim_flight(key: tuple[Any, ...]) -> tuple[bool, threading.Event]:
    """抢占计算权。返回 (我是不是那个唯一的人, 事件)。"""
    with _inflight_lock:
        ev = _inflight.get(key)
        if ev is not None:
            return False, ev
        ev = threading.Event()
        _inflight[key] = ev
        return True, ev


def _release_flight(key: tuple[Any, ...], ev: threading.Event) -> None:
    with _inflight_lock:
        if _inflight.get(key) is ev:
            _inflight.pop(key, None)
    ev.set()  # 唤醒等待者（它们醒来会重新查缓存）


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
        # 管理后台的轮询不进统计，否则仪表盘自己的请求会盖住真实流量；
        # /api/health 同理，探活不是业务请求，记进去只会稀释错误率
        if not path.startswith("/api/admin") and path != "/api/health":
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
    wait = _login_blocked_for(meta["ip"])
    if wait:
        log.warning("登录被限流：user=%s ip=%s 需等待 %d 秒", username, meta["ip"], wait)
        AUDIT.record("login", username=username, ok=False, mode=mode,
                     reason="限流", **meta)
        raise HTTPException(429, f"尝试过于频繁，请 {wait} 秒后再试")

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
        _note_login_fail(meta["ip"])
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
    except Exception as exc:  # noqa: BLE001
        # 兜底：教务页面结构变了（KeyError/ValueError）、正则没匹配上之类，
        # 也要保证客户端被关掉、审计里有记录，而不是留个 500 + 泄漏一个连接池
        client.close()
        cost = time.perf_counter() - started
        log.exception("登录时出现未预期的错误：user=%s mode=%s", username, mode)
        AUDIT.record("login", username=username, ok=False, mode=mode,
                     ms=round(cost * 1000), reason=f"内部错误 {type(exc).__name__}", **meta)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": "登录过程出现内部错误，服务端日志里有完整堆栈",
                     "fallback": "cookie"},
        )

    cost = time.perf_counter() - started
    token = uuid.uuid4().hex
    _clear_login_fails(meta["ip"])
    with _lock:
        expired = _prune_sessions_locked()
        evicted = _evict_others(username)
        _sessions[token] = {
            "username": username,
            "cookies": client.cookie_list(),
            "ts": int(time.time()),
            "login_at": int(time.time()),
        }
        _clients[token] = client
        capped = _cap_sessions_locked()
        _save_sessions()
    _close_clients(expired + evicted + capped)
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
    # 身份固定是占位账号：客户端传来的名字只能当日志标签用。
    # 一旦拿它当身份，任何人填一个别人的学号都能顶掉对方的会话（同账号只留最新一条）。
    label = str(payload.get("username") or "").strip()[:64]
    username = COOKIE_PLACEHOLDER
    meta = client_meta(request)
    log.info("Cookie 登录请求：label=%s cookie 长度=%d ip=%s", label or "-", len(cookie), meta["ip"])
    started = time.perf_counter()
    client = JwxtClient(label=label or username)
    try:
        client.login_with_cookie(cookie)
    except JwxtError as exc:
        client.close()
        cost = time.perf_counter() - started
        log.warning("Cookie 登录失败：label=%s %s", label or "-", exc)
        AUDIT.record("login", username=username, ok=False, mode="cookie",
                     ms=round(cost * 1000), reason=str(exc)[:200],
                     label=label or None, **meta)
        return JSONResponse(status_code=401, content={"ok": False, "message": str(exc)})
    except httpx.HTTPError as exc:
        client.close()
        cost = time.perf_counter() - started
        log.error("Cookie 登录时网络请求失败：label=%s %s: %s", label or "-", type(exc).__name__, exc)
        AUDIT.record("login", username=username, ok=False, mode="cookie",
                     ms=round(cost * 1000), reason=f"网络错误 {type(exc).__name__}",
                     label=label or None, **meta)
        return JSONResponse(status_code=502, content={"ok": False, "message": f"网络请求失败：{exc}"})
    except Exception as exc:  # noqa: BLE001
        # 兜底：Cookie 串畸形等非预期异常，也要先关掉连接池，
        # 别留下一个没人认领的 socket
        client.close()
        cost = time.perf_counter() - started
        log.exception("Cookie 登录出现未预期的错误：label=%s", label or "-")
        AUDIT.record("login", username=username, ok=False, mode="cookie",
                     ms=round(cost * 1000), reason=f"内部错误 {type(exc).__name__}",
                     label=label or None, **meta)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": "登录过程出现内部错误，服务端日志里有完整堆栈"},
        )

    cost = time.perf_counter() - started
    token = uuid.uuid4().hex
    with _lock:
        # 占位账号之间不互相顶（_evict_others 对这些名字直接返回），
        # 所以多人用 Cookie 通道不会互相踢下线
        expired = _prune_sessions_locked()
        _sessions[token] = {
            "username": username,
            "label": label,
            "cookies": client.cookie_list(),
            "ts": int(time.time()),
            "login_at": int(time.time()),
        }
        _clients[token] = client
        capped = _cap_sessions_locked()
        _save_sessions()
    _close_clients(expired + capped)
    log.info("Cookie 登录成功：label=%s token=%s…", label or "-", token[:8])
    AUDIT.record("login", username=username, ok=True, mode="cookie",
                 ms=round(cost * 1000), label=label or None, **meta)
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
    key = ("terms", token)
    cached = _cache_get(key)
    if cached is not None:
        _refresh_cookies(token, client)
        log.debug("学年学期命中缓存：token=%s…", token[:8])
        return {"ok": True, **cached}

    owner, ev = _claim_flight(key)
    if not owner and ev.wait(_FLIGHT_WAIT_MAX):
        # 有人正在拉：等它的结果，省一次教务往返；等不到就自己打一次。
        cached = _cache_get(key)
        if cached is not None:
            _refresh_cookies(token, client)
            log.debug("学年学期沿用并发请求的结果：token=%s…", token[:8])
            return {"ok": True, **cached}
    try:
        try:
            data = client.terms()
        except JwxtError as exc:
            log.warning("读取学年学期失败：token=%s… %s", token[:8], exc)
            raise HTTPException(401, str(exc)) from exc
        except httpx.HTTPError as exc:
            log.error("读取学年学期时网络失败：token=%s… %s: %s", token[:8], type(exc).__name__, exc)
            raise HTTPException(502, f"教务接口网络错误：{type(exc).__name__}") from exc
        _cache_put(key, data)
    finally:
        if owner:
            _release_flight(key, ev)
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
    key = ("tt", token, xnm, xqm)
    cached = _cache_get(key)
    if cached is not None:
        _refresh_cookies(token, client)
        log.debug("课表命中缓存：xnm=%s xqm=%s token=%s…", xnm, xqm, token[:8])
        return {"ok": True, **cached}
    log.debug("请求课表：xnm=%s xqm=%s token=%s…", xnm, xqm, token[:8])

    owner, ev = _claim_flight(key)
    if not owner and ev.wait(_FLIGHT_WAIT_MAX):
        cached = _cache_get(key)
        if cached is not None:
            _refresh_cookies(token, client)
            log.debug("课表沿用并发请求的结果：xnm=%s xqm=%s token=%s…", xnm, xqm, token[:8])
            return {"ok": True, **cached}
    try:
        try:
            raw = client.timetable(xnm, xqm)
        except JwxtError as exc:
            log.warning("拉取课表失败：xnm=%s xqm=%s %s", xnm, xqm, exc)
            raise HTTPException(502, str(exc)) from exc
        except httpx.HTTPError as exc:
            log.error("拉取课表时网络失败：xnm=%s xqm=%s %s: %s", xnm, xqm, type(exc).__name__, exc)
            raise HTTPException(502, f"教务接口网络错误：{type(exc).__name__}") from exc
        data = tt.normalize(raw)
        data["term"] = {"xnm": xnm, "xqm": xqm, "label": f"{xnm} 学年 · {tt_map(xqm)}"}
        _cache_put(key, data)
    finally:
        if owner:
            _release_flight(key, ev)
    _refresh_cookies(token, client)
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
    except httpx.HTTPError as exc:
        log.error("导出原始课表时网络失败：xnm=%s xqm=%s %s: %s", xnm, xqm, type(exc).__name__, exc)
        raise HTTPException(502, f"教务接口网络错误：{type(exc).__name__}") from exc


@app.post("/api/logout")
def logout(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    with _lock:
        sess = _sessions.pop(token, None)
        client = _clients.pop(token, None)
        _save_sessions()
    _cache_drop_token(token)
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


@app.get("/admin.html", include_in_schema=False)
def admin_page_alias() -> RedirectResponse:
    """静态目录挂在根上，/admin.html 本来能直接打开。让它和 /admin 走同一道开关。"""
    if not admin_api.enabled():
        raise HTTPException(404, "管理后台未启用")
    return RedirectResponse("/admin", status_code=302)


# ---------------- 静态资源 ----------------
class FrontendFiles(StaticFiles):
    """前端目录里唯独 admin.html 不能直读：它得走 /admin，受 admin.enabled 和登录约束。

    正常访问会被上面的路由重定向走，这里兜的是 `/./admin.html` 这类绕开路由的写法。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        if path.replace("\\", "/").rsplit("/", 1)[-1].lower() == "admin.html":
            raise HTTPException(404, "管理后台请走 /admin")
        return await super().get_response(path, scope)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/", FrontendFiles(directory=str(FRONTEND), html=True), name="static")
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
