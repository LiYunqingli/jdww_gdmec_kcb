"""日志初始化。

一套 handler 挂在 root 上，所有 `logging.getLogger("kcb.xxx")` 自动继承：

    console  默认 INFO，带颜色（只在真终端上色，重定向到文件时自动关掉）
    file     logs/app.log，默认 DEBUG，按大小轮转

两层级别：
    logging.level          总闸，比它轻的记录根本不产生（root logger 的 level）
    console.level / file.level   每一路自己再收窄，只能比总闸更严，不能更松

所以"想看全部细节"只改 `logging.level` 一个值就够；环境变量 `KCB_LOG_LEVEL` 优先级更高，
方便临时排查而不动文件。

**写盘是异步的**：root 上挂的是 QueueHandler，真正的 console / 文件 handler 跑在
QueueListener 的线程里。原来 RotatingFileHandler 是同步写，DEBUG 级别下一个请求
要往磁盘写好几条，这条 IO 直接卡在请求线程上（机械盘 / 同步目录会明显变慢）。
审计事件（audit.py）早就这么做了，日志这条链路补上。
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from typing import Any

from . import config

_HANDLER_TAG = "_kcb_handler"

# 队列上限：磁盘真卡住时宁可丢日志，也不能把内存吃掉。
# 超了静默丢弃并偶尔提示一句，见 _DropQueueHandler。
_QUEUE_MAX = 20000

_listener: QueueListener | None = None
_atexit_done = False

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

_COLORS = {
    logging.DEBUG: "\x1b[38;5;245m",     # 灰
    logging.INFO: "\x1b[38;5;39m",       # 蓝
    logging.WARNING: "\x1b[38;5;214m",   # 橙
    logging.ERROR: "\x1b[38;5;203m",     # 红
    logging.CRITICAL: "\x1b[1;38;5;196m",
}
_RESET = "\x1b[0m"


def parse_level(value: Any, fallback: int = logging.INFO) -> int:
    """字符串 / 整数都接受，认不出来就回落。"""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    name = str(value or "").strip().upper()
    return _LEVELS.get(name, fallback)


class _ColorFormatter(logging.Formatter):
    """只给级别名上色，其余保持原格式。"""

    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelno, "")
        if not color:
            return super().format(record)
        original = record.levelname
        record.levelname = f"{color}{original}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def _enabled(flag: Any, default: bool = True) -> bool:
    return default if not isinstance(flag, bool) else flag


class _DropQueueHandler(QueueHandler):
    """队列满时静默丢一条，别像默认那样往 stderr 刷 traceback（会滚成雪崩）。"""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__(q)
        self._dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                # 这里只能直接写 stderr：再走 logging 会自己套自己
                print(f"[kcb] 日志队列已满，累计丢弃 {self._dropped} 条", file=sys.stderr)


def _stop_listener() -> None:
    """把队列里剩下的记录写完再退出（QueueListener.stop 会先清空队列）。"""
    global _listener
    listener = _listener
    if listener is None:
        return
    _listener = None
    try:
        listener.stop()
    except Exception:  # noqa: BLE001
        pass
    # 真实 handler 只被 listener 引用，root 上找不到它们，得自己关
    for handler in listener.handlers:
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass


def _install_async(handlers: list[logging.Handler]) -> None:
    """把真实 handler 交给后台线程，root 上只留一个入队的 handler。"""
    global _listener, _atexit_done
    root = logging.getLogger()
    if not handlers:
        root.addHandler(logging.NullHandler())
        return
    q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
    listener = QueueListener(q, *handlers, respect_handler_level=True)
    listener.start()
    handler = _DropQueueHandler(q)
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    _listener = listener
    if not _atexit_done:
        _atexit_done = True
        atexit.register(_stop_listener)


def setup_logging(force: bool = False) -> logging.Logger:
    """建 handler，幂等。重复调用不会叠加 handler。"""
    root = logging.getLogger()
    if not force and any(getattr(h, _HANDLER_TAG, False) for h in root.handlers):
        return logging.getLogger("kcb")

    _stop_listener()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()

    fmt = str(config.get("logging.format") or "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    datefmt = str(config.get("logging.date_format") or "%Y-%m-%d %H:%M:%S")
    env_level = os.environ.get("KCB_LOG_LEVEL", "").strip()
    global_level = parse_level(env_level or config.get("logging.level"), logging.INFO)

    built: list[logging.Handler] = []

    console_cfg = config.get("logging.console") or {}
    if _enabled(console_cfg.get("enabled")):
        console_level = parse_level(console_cfg.get("level"), global_level)
        stream = sys.stdout if sys.stdout.isatty() else sys.stderr
        use_color = _enabled(config.get("logging.color"), True) and stream.isatty()
        formatter = _ColorFormatter(fmt, datefmt) if use_color else logging.Formatter(fmt, datefmt)
        handler = logging.StreamHandler(stream)
        handler.setLevel(console_level)
        handler.setFormatter(formatter)
        built.append(handler)

    file_cfg = config.get("logging.file") or {}
    if _enabled(file_cfg.get("enabled")):
        path = config.resolve(str(file_cfg.get("path") or "logs/app.log"))
        file_level = parse_level(file_cfg.get("level"), logging.DEBUG)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=int(file_cfg.get("max_bytes") or 2 * 1024 * 1024),
                backupCount=int(file_cfg.get("backup_count") or 5),
                encoding=str(file_cfg.get("encoding") or "utf-8"),
                delay=True,
            )
            handler.setLevel(file_level)
            handler.setFormatter(logging.Formatter(fmt, datefmt))
            built.append(handler)
        except OSError as exc:
            logging.getLogger("kcb.log").warning("日志文件不可用（%s）：%s", path, exc)

    # 总闸。handler 上的 level 只能更严，想放宽必须动这里
    root.setLevel(global_level)

    for handler in built:
        setattr(handler, _HANDLER_TAG, True)
    _install_async(built)

    logging.captureWarnings(True)

    # 第三方库降噪，别让 httpx 的每条请求都刷屏
    for name, level in (config.get("logging.quiet") or {}).items():
        logging.getLogger(str(name)).setLevel(parse_level(level, logging.WARNING))

    log = logging.getLogger("kcb.log")
    log.info(
        "日志就绪：级别 %s · 输出 %s · 配置文件 %s",
        logging.getLevelName(global_level), _targets(), config.CONFIG_PATH,
    )
    for note in config.LOAD_NOTES:
        log.warning(note)
    return logging.getLogger("kcb")


def _targets() -> str:
    """一句话说明日志往哪写。"""
    parts: list[str] = []
    console_cfg = config.get("logging.console") or {}
    if _enabled(console_cfg.get("enabled")):
        parts.append(f"控制台（{console_cfg.get('level', 'INFO')}）")
    file_cfg = config.get("logging.file") or {}
    if _enabled(file_cfg.get("enabled")):
        parts.append(f"{config.resolve(str(file_cfg.get('path') or 'logs/app.log'))}（{file_cfg.get('level', 'DEBUG')}）")
    return " + ".join(parts) or "无输出（请在 config.json 里至少开一路）"
