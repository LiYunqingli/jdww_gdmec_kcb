"""运行数据采集：把有分析价值的事件写成 JSONL，并给出管理后台要的聚合结果。

为什么单独一套而不去解析 app.log：
- 日志是给人看的（带时间戳、对齐、可读），机器再解析一遍既脆又慢
- 事件是给程序看的，字段固定，随便聚合，写坏了也不影响业务

写入位置默认 `logs/events.jsonl`，一行一个 JSON，按大小轮转。同时在内存里留最近
`memory_events` 条，仪表盘直接从内存聚合，不回读磁盘。

事件类型：
    request     每个 /api 请求（方法、路径、状态码、耗时、账号、IP、设备）
    login       登录尝试（账号、成功与否、模式、耗时、失败原因、IP、设备）
    logout      退出登录
    admin_login 管理后台登录
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import queue
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("kcb.audit")

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


class AuditStore:
    """事件库：请求线程只做入队，磁盘 IO 全在后台线程批量完成。

    record() 只 append + 入队（约 15 µs），后台线程按 flush_interval 攒一批写，
    一次 open 写几百行，避免同步写盘卡住请求线程。
    """

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 3,
        memory_events: int = 5000,
        flush_interval: float = 0.4,
        queue_max: int = 20000,
    ):
        self.path = path
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.flush_interval = max(0.02, float(flush_interval))
        self._events: deque[dict[str, Any]] = deque(maxlen=memory_events)
        self._lock = threading.Lock()
        self._write_error_logged = False
        self._size = 0
        self._live: Callable[[], dict[str, Any]] | None = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._wlock = threading.Lock()
        self._fh: Any = None
        self._writer: threading.Thread | None = None
        self._dropped = 0
        # 聚合结果缓存：仪表盘轮询一次要算十几毫秒，没必要每次都重算
        self._snap: tuple[float, dict[str, Any]] | None = None
        self._snap_lock = threading.Lock()
        if enabled:
            self._load()
            self._start_writer()

    # ---------------- 落盘 ----------------
    def _load(self) -> None:
        if not self.path.exists():
            log.debug("事件文件不存在，从空数据开始：%s", self.path)
            return
        try:
            self._size = self.path.stat().st_size
            # 文件大了只读尾部，避免启动就吃掉几百 MB 内存
            limit = 4 * 1024 * 1024
            with self.path.open("rb") as fh:
                if self._size > limit:
                    fh.seek(self._size - limit)
                    fh.readline()  # 丢掉被截断的半行
                raw = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            log.warning("事件文件读取失败：%s（%s）", self.path, exc)
            return

        loaded = skipped = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(event, dict) and event.get("kind"):
                self._events.append(event)
                loaded += 1
        log.info("载入 %d 条历史事件（跳过 %d 行坏数据）：%s", loaded, skipped, self.path)

    def _rotate(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        try:
            for i in range(self.backup_count - 1, 0, -1):
                src = self.path.parent / f"{self.path.name}.{i}"
                dst = self.path.parent / f"{self.path.name}.{i + 1}"
                if src.exists():
                    os.replace(src, dst)
            os.replace(self.path, self.path.parent / f"{self.path.name}.1")
            log.info("事件文件已轮转：%s → %s.1", self.path.name, self.path.name)
        except OSError as exc:
            log.warning("事件文件轮转失败：%s", exc)

    # ---------------- 后台写线程 ----------------
    def _start_writer(self) -> None:
        with self._wlock:
            if self._writer and self._writer.is_alive():
                return
            self._stop.clear()
            self._writer = threading.Thread(
                target=self._writer_loop, name="kcb-audit-writer", daemon=True
            )
            self._writer.start()

    def _open_handle(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("ab")

    def _close_handle(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _writer_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            batch = [first]
            # 攒一批一起写，但别攒太大：一次写太多会在轮转阈值上冲过头
            pending = len(first)
            while len(batch) < 512 and pending < 64 * 1024:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                batch.append(item)
                pending += len(item)
            self._write_batch(batch)
            if self._stop.is_set() and self._queue.empty():
                break
        self._close_handle()

    def _write_batch(self, batch: list[bytes]) -> None:
        payload = b"".join(batch)
        try:
            # 文件不存在或刚轮转完时 _size 为 0，按真实大小判断要不要轮转
            if self._size + len(payload) > self.max_bytes:
                self._close_handle()  # Windows 上文件被占着换不了名
                self._rotate()
                self._size = self.path.stat().st_size if self.path.exists() else 0
            if self._fh is None:
                self._open_handle()
            self._fh.write(payload)
            self._fh.flush()
            self._size += len(payload)
            self._write_error_logged = False
        except (OSError, ValueError) as exc:
            self._close_handle()
            if not self._write_error_logged:
                self._write_error_logged = True
                log.error("事件写入失败（后续同类错误不再重复）：%s（%s）", self.path, exc)

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        """记一条事件。任何失败都只记日志，绝不影响接口本身。"""
        event: dict[str, Any] = {"ts": time.time(), "kind": kind}
        event.update({k: v for k, v in fields.items() if v is not None})
        if not self.enabled:
            return event  # 不入内存，也不落盘
        with self._lock:
            self._events.append(event)
        try:
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        except (TypeError, ValueError) as exc:
            log.warning("事件里有无法序列化的字段，已跳过：%s", exc)
            return event
        try:
            self._queue.put_nowait(line.encode("utf-8"))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 500 == 0:
                log.warning("事件队列已满，累计丢弃 %d 条（磁盘可能卡住了）", self._dropped)
        # 关服务之后就别再自愈了，否则退出阶段又把文件句柄打开
        if not self._stop.is_set() and (self._writer is None or not self._writer.is_alive()):
            self._start_writer()  # 写线程意外退出后自愈
        return event

    def flush(self, timeout: float = 3.0) -> None:
        """等队列写完（测试与关服务时用）。"""
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        if self._fh is not None:
            try:
                self._fh.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        """停写线程并把剩下的写完。可重复调用。"""
        if self._writer and self._writer.is_alive():
            self._stop.set()
            self._writer.join(timeout=5)
        self._close_handle()

    # ---------------- 实时信息注入 ----------------
    def bind_live(self, provider: Callable[[], dict[str, Any]]) -> None:
        """由 app 注册：返回会话数、启动时间等内存状态，避免循环导入。"""
        self._live = provider

    def snapshot(self, max_age: float = 2.0) -> dict[str, Any]:
        """返回仪表盘要的全部聚合结果，`max_age` 秒内复用上一次的。

        聚合一次要十几毫秒，而后台每 15 秒轮询一次、开几个标签页就翻几倍。
        `max_age=0` 关掉缓存（测试用）。

        注意：返回的是共享对象，**调用方要加字段请先 copy**。
        """
        now = time.monotonic()
        with self._snap_lock:
            cached = self._snap
            if cached is not None and now - cached[0] < max_age:
                return cached[1]

        live = {}
        if self._live:
            try:
                live = self._live() or {}
            except Exception:  # noqa: BLE001
                log.exception("实时状态提供者异常，仪表盘部分数据将缺失")
        with self._lock:
            events = list(self._events)
        stats = build_stats(events, live=live, path=self.path, size=self._size)
        with self._snap_lock:
            self._snap = (now, stats)
        return stats

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:][::-1]


# ---------------- 聚合 ----------------
def build_stats(
    events: list[dict[str, Any]],
    *,
    live: dict[str, Any] | None = None,
    path: Path | None = None,
    size: int = 0,
) -> dict[str, Any]:
    live = live or {}
    now = time.time()
    today = _day_of(now)

    # 一次遍历把「日期 / 小时 / 状态码 / 耗时」都算好，
    # 行结构是 (事件, 日期, 小时, 状态码, 耗时ms, 类型)，后面的聚合只读行。
    rows: list[tuple[dict[str, Any], str, int, int, float, str]] = []
    for e in events:
        ts = _f(e.get("ts"))
        tm = time.localtime(ts) if ts > 0 else None
        day = f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d}" if tm else "-"
        hour = tm.tm_hour if tm else 0
        rows.append((
            e, day, hour,
            int(e.get("status") or 0),
            _f(e.get("ms")),
            str(e.get("kind") or ""),
        ))

    requests = [r for r in rows if r[5] == "request"]
    logins = [r for r in rows if r[5] == "login"]
    admin_logins = [r for r in rows if r[5] == "admin_login"]

    req_today = [r for r in requests if r[1] == today]
    ok_logins = [r for r in logins if r[0].get("ok")]
    ok_today = [r for r in ok_logins if r[1] == today]
    fail_today = [r for r in logins if not r[0].get("ok") and r[1] == today]

    durations = [r[4] for r in req_today]
    err_today = [r for r in req_today if r[3] >= 400]
    total_attempts_today = len(ok_today) + len(fail_today)

    # 近 7 天走势
    days: list[str] = []
    day_labels: list[str] = []
    for offset in range(6, -1, -1):
        d = datetime.fromtimestamp(now) - timedelta(days=offset)
        days.append(d.strftime("%Y-%m-%d"))
        day_labels.append(f"{d.month}/{d.day} {_WEEKDAYS[d.weekday()]}")

    day_req = Counter(r[1] for r in requests)
    day_err = Counter(r[1] for r in requests if r[3] >= 400)
    day_login = Counter(r[1] for r in ok_logins)
    day_fail = Counter(r[1] for r in logins if not r[0].get("ok"))

    # 今日 24 小时
    hour_req = Counter(r[2] for r in req_today)
    hour_login = Counter(r[2] for r in ok_today)

    # 接口维度
    per_endpoint: dict[str, dict[str, Any]] = {}
    for e, _day, _hour, status, ms, _kind in requests:
        key = f"{str(e.get('method') or 'GET')} {e.get('path') or '-'}"
        slot = per_endpoint.get(key)
        if slot is None:
            slot = per_endpoint[key] = {"path": key, "count": 0, "ms": [], "errors": 0}
        slot["count"] += 1
        slot["ms"].append(ms)
        if status >= 400:
            slot["errors"] += 1
    endpoints = [
        {
            "path": v["path"],
            "count": v["count"],
            "avg_ms": round(sum(v["ms"]) / len(v["ms"]), 1) if v["ms"] else 0,
            "p95_ms": round(_pct(v["ms"], 0.95), 1),
            "max_ms": round(max(v["ms"]), 1) if v["ms"] else 0,
            "errors": v["errors"],
        }
        for v in per_endpoint.values()
    ]
    endpoints.sort(key=lambda x: x["count"], reverse=True)

    # 用户维度（以登录事件为准，含失败次数）
    users: dict[str, dict[str, Any]] = {}
    for e, _day, _hour, _status, _ms, _kind in logins:
        name = str(e.get("username") or "-")
        slot = users.get(name)
        if slot is None:
            slot = users[name] = {
                "username": name, "logins": 0, "fails": 0, "last_login": 0,
                "last_ip": "-", "device": "-", "browser": "-", "os": "-",
            }
        if e.get("ok"):
            slot["logins"] += 1
            slot["last_login"] = max(slot["last_login"], _f(e.get("ts")))
            slot["last_ip"] = e.get("ip") or slot["last_ip"]
            for key in ("device", "browser", "os"):
                if e.get(key):
                    slot[key] = e[key]
        else:
            slot["fails"] += 1
    user_rows = sorted(users.values(), key=lambda u: (u["logins"], u["last_login"]), reverse=True)
    for row in user_rows:
        row["last_login_text"] = _fmt_ts(row["last_login"])

    # 设备 / 浏览器 / 系统分布：优先用登录事件（一个人一次），没有就看全部
    device_source = [r[0] for r in logins] or [r[0] for r in rows]
    devices = Counter(str(e.get("device") or "未知") for e in device_source)
    browsers = Counter(str(e.get("browser") or "未知") for e in device_source)
    systems = Counter(str(e.get("os") or "未知") for e in device_source)

    status_codes = Counter(str(r[3]) if r[3] else "-" for r in requests)

    recent_logins = []
    for e, _day, _hour, _status, ms, _kind in heapq.nlargest(
        60, logins, key=lambda r: _f(r[0].get("ts"))
    ):
        recent_logins.append({
            "ts": _f(e.get("ts")),
            "time": _fmt_ts(_f(e.get("ts"))),
            "username": e.get("username") or "-",
            "ok": bool(e.get("ok")),
            "mode": e.get("mode") or "sso",
            "ms": round(ms),
            "ip": e.get("ip") or "-",
            "device": e.get("device") or "-",
            "browser": e.get("browser") or "-",
            "os": e.get("os") or "-",
            "reason": e.get("reason") or "",
        })

    recent_requests = []
    for e, _day, _hour, status, ms, _kind in heapq.nlargest(
        80, requests, key=lambda r: _f(r[0].get("ts"))
    ):
        recent_requests.append({
            "ts": _f(e.get("ts")),
            "time": _fmt_ts(_f(e.get("ts"))),
            "username": e.get("username") or "-",
            "method": e.get("method") or "GET",
            "path": e.get("path") or "-",
            "status": status,
            "ms": round(ms),
        })

    slowest = [
        {
            "time": _fmt_ts(_f(e.get("ts"))),
            "path": f"{e.get('method') or 'GET'} {e.get('path') or '-'}",
            "status": status,
            "ms": round(ms),
        }
        for e, _day, _hour, status, ms, _kind in heapq.nlargest(
            10, requests, key=lambda r: r[4]
        )
    ]

    peak_hour, peak_count = ("-", 0)
    if hour_req:
        peak_hour = f"{max(hour_req, key=hour_req.get):02d}:00"
        peak_count = max(hour_req.values())

    return {
        "meta": {
            "now": _fmt_ts(now),
            "today": today,
            "events_loaded": len(events),
            "events_file": str(path) if path else "-",
            "events_size_kb": round(size / 1024, 1),
            "admin_user": live.get("admin_user") or "-",
            "version": live.get("version") or "-",
            "log_level": live.get("log_level") or "-",
            "log_file": live.get("log_file") or "-",
            "started_at": _fmt_ts(live.get("started_at")),
            "uptime_seconds": int(now - float(live.get("started_at") or now)),
            "python": live.get("python") or "-",
        },
        "cards": {
            "sessions": int(live.get("sessions") or 0),
            "users_total": len([u for u in user_rows if u["logins"]]),
            "users_today": len({str(e.get("username") or "-") for e in (r[0] for r in ok_today)}),
            "logins_today": len(ok_today),
            "fails_today": len(fail_today),
            "success_rate": round(100 * len(ok_today) / total_attempts_today, 1) if total_attempts_today else 0.0,
            "requests_today": len(req_today),
            "errors_today": len(err_today),
            "error_rate": round(100 * len(err_today) / len(req_today), 1) if req_today else 0.0,
            "avg_ms": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "p95_ms": round(_pct(durations, 0.95), 1),
            "peak_hour": peak_hour,
            "peak_hour_count": peak_count,
            "admin_logins": len(admin_logins),
        },
        "timeline": {
            "labels": day_labels,
            "requests": [day_req.get(d, 0) for d in days],
            "errors": [day_err.get(d, 0) for d in days],
            "logins": [day_login.get(d, 0) for d in days],
            "fails": [day_fail.get(d, 0) for d in days],
        },
        "hourly": {
            "labels": [f"{h:02d}" for h in range(24)],
            "requests": [hour_req.get(h, 0) for h in range(24)],
            "logins": [hour_login.get(h, 0) for h in range(24)],
        },
        "endpoints": endpoints,
        "sessions_detail": live.get("sessions_detail") or [],
        "devices": [{"name": k, "value": v} for k, v in devices.most_common()],
        "browsers": [{"name": k, "value": v} for k, v in browsers.most_common(8)],
        "systems": [{"name": k, "value": v} for k, v in systems.most_common(8)],
        "status_codes": [{"name": k, "value": v} for k, v in sorted(status_codes.items())],
        "users": user_rows[:80],
        "recent_logins": recent_logins,
        "recent_requests": recent_requests,
        "slowest": slowest,
    }


def _f(value: Any) -> float:
    """把任意值收敛成 float，坏数据当 0 —— 聚合不能因为一条脏事件整个失败。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _day_of(ts: float) -> str:
    tm = time.localtime(ts)
    return f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d}"


# ---------------- 单例 ----------------
_store: AuditStore | None = None


def store() -> AuditStore:
    """全局唯一的事件库，路径与容量来自 config.json 的 audit 段。"""
    global _store
    if _store is None:
        from . import config

        cfg = config.get("audit") or {}
        _store = AuditStore(
            config.resolve(str(cfg.get("path") or "logs/events.jsonl")),
            enabled=bool(cfg.get("enabled", True)),
            max_bytes=int(cfg.get("max_bytes") or 5 * 1024 * 1024),
            backup_count=int(cfg.get("backup_count") or 3),
            memory_events=int(cfg.get("memory_events") or 5000),
            flush_interval=float(cfg.get("flush_interval_ms") or 400) / 1000,
        )
    return _store


def reset_store() -> None:
    """测试用：丢掉单例，下次调用重新构建。"""
    global _store
    if _store is not None:
        _store.close()
    _store = None

