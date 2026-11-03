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

import json
import logging
import os
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
    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 3,
        memory_events: int = 5000,
    ):
        self.path = path
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._events: deque[dict[str, Any]] = deque(maxlen=memory_events)
        self._lock = threading.Lock()
        self._write_error_logged = False
        self._size = 0
        self._live: Callable[[], dict[str, Any]] | None = None
        if enabled:
            self._load()

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

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        """记一条事件。任何失败都只记日志，绝不影响接口本身。"""
        event: dict[str, Any] = {"ts": time.time(), "kind": kind}
        event.update({k: v for k, v in fields.items() if v is not None})
        with self._lock:
            self._events.append(event)
            if not self.enabled:
                return event
            try:
                if self._size >= self.max_bytes:
                    self._rotate()
                    self._size = 0
                self.path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                self._size += len(line.encode("utf-8"))
            except OSError as exc:
                if not self._write_error_logged:
                    self._write_error_logged = True
                    log.error("事件写入失败（后续同类错误不再重复）：%s（%s）", self.path, exc)
        return event

    # ---------------- 实时信息注入 ----------------
    def bind_live(self, provider: Callable[[], dict[str, Any]]) -> None:
        """由 app 注册：返回会话数、启动时间等内存状态，避免循环导入。"""
        self._live = provider

    def snapshot(self) -> dict[str, Any]:
        live = {}
        if self._live:
            try:
                live = self._live() or {}
            except Exception:  # noqa: BLE001
                log.exception("实时状态提供者异常，仪表盘部分数据将缺失")
        with self._lock:
            events = list(self._events)
        return build_stats(events, live=live, path=self.path, size=self._size)

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
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    requests = [e for e in events if e.get("kind") == "request"]
    logins = [e for e in events if e.get("kind") == "login"]
    admin_logins = [e for e in events if e.get("kind") == "admin_login"]

    req_today = [e for e in requests if _day(e) == today]
    ok_logins = [e for e in logins if e.get("ok")]
    ok_today = [e for e in ok_logins if _day(e) == today]
    fail_today = [e for e in logins if not e.get("ok") and _day(e) == today]

    durations = [float(e.get("ms") or 0) for e in requests]
    err_today = [e for e in req_today if int(e.get("status") or 0) >= 400]
    total_attempts_today = len(ok_today) + len(fail_today)

    # 近 7 天走势
    days: list[str] = []
    day_labels: list[str] = []
    for offset in range(6, -1, -1):
        d = datetime.fromtimestamp(now) - timedelta(days=offset)
        days.append(d.strftime("%Y-%m-%d"))
        day_labels.append(f"{d.month}/{d.day} {_WEEKDAYS[d.weekday()]}")

    day_req = Counter(_day(e) for e in requests)
    day_err = Counter(_day(e) for e in requests if int(e.get("status") or 0) >= 400)
    day_login = Counter(_day(e) for e in ok_logins)
    day_fail = Counter(_day(e) for e in logins if not e.get("ok"))

    # 今日 24 小时
    hour_req = Counter(_hour(e) for e in req_today)
    hour_login = Counter(_hour(e) for e in ok_today)

    # 接口维度
    per_endpoint: dict[str, dict[str, Any]] = {}
    for e in requests:
        key = f"{str(e.get('method') or 'GET')} {e.get('path') or '-'}"
        slot = per_endpoint.setdefault(key, {"path": key, "count": 0, "ms": [], "errors": 0})
        slot["count"] += 1
        slot["ms"].append(float(e.get("ms") or 0))
        if int(e.get("status") or 0) >= 400:
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
    for e in logins:
        name = str(e.get("username") or "-")
        slot = users.setdefault(name, {
            "username": name, "logins": 0, "fails": 0, "last_login": 0,
            "last_ip": "-", "device": "-", "browser": "-", "os": "-",
        })
        if e.get("ok"):
            slot["logins"] += 1
            slot["last_login"] = max(slot["last_login"], float(e.get("ts") or 0))
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
    device_source = logins or events
    devices = Counter(str(e.get("device") or "未知") for e in device_source)
    browsers = Counter(str(e.get("browser") or "未知") for e in device_source)
    systems = Counter(str(e.get("os") or "未知") for e in device_source)

    status_codes = Counter(str(e.get("status") or "-") for e in requests)

    recent_logins = []
    for e in sorted(logins, key=lambda x: float(x.get("ts") or 0), reverse=True)[:60]:
        recent_logins.append({
            "ts": float(e.get("ts") or 0),
            "time": _fmt_ts(float(e.get("ts") or 0)),
            "username": e.get("username") or "-",
            "ok": bool(e.get("ok")),
            "mode": e.get("mode") or "sso",
            "ms": round(float(e.get("ms") or 0)),
            "ip": e.get("ip") or "-",
            "device": e.get("device") or "-",
            "browser": e.get("browser") or "-",
            "os": e.get("os") or "-",
            "reason": e.get("reason") or "",
        })

    recent_requests = []
    for e in sorted(requests, key=lambda x: float(x.get("ts") or 0), reverse=True)[:80]:
        recent_requests.append({
            "ts": float(e.get("ts") or 0),
            "time": _fmt_ts(float(e.get("ts") or 0)),
            "username": e.get("username") or "-",
            "method": e.get("method") or "GET",
            "path": e.get("path") or "-",
            "status": int(e.get("status") or 0),
            "ms": round(float(e.get("ms") or 0)),
        })

    slowest = [
        {
            "time": _fmt_ts(float(e.get("ts") or 0)),
            "path": f"{e.get('method') or 'GET'} {e.get('path') or '-'}",
            "status": int(e.get("status") or 0),
            "ms": round(float(e.get("ms") or 0)),
        }
        for e in sorted(requests, key=lambda x: float(x.get("ms") or 0), reverse=True)[:10]
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
            "users_today": len({str(e.get("username") or "-") for e in ok_today}),
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


def _day(event: dict[str, Any]) -> str:
    return datetime.fromtimestamp(float(event.get("ts") or 0)).strftime("%Y-%m-%d")


def _hour(event: dict[str, Any]) -> int:
    return datetime.fromtimestamp(float(event.get("ts") or 0)).hour


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
        )
    return _store


def reset_store() -> None:
    """测试用：丢掉单例，下次调用重新构建。"""
    global _store
    _store = None

