"""日志初始化。

一套 handler 挂在 root 上，所有 `logging.getLogger("kcb.xxx")` 自动继承：

    console  默认 INFO，带颜色（只在真终端上色，重定向到文件时自动关掉）
    file     logs/app.log，默认 DEBUG，按大小轮转

两层级别：
    logging.level          总闸，比它轻的记录根本不产生（root logger 的 level）
    console.level / file.level   每一路自己再收窄，只能比总闸更严，不能更松

所以"想看全部细节"只改 `logging.level` 一个值就够；环境变量 `KCB_LOG_LEVEL` 优先级更高，
方便临时排查而不动文件。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

from . import config

_HANDLER_TAG = "_kcb_handler"

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


def setup_logging(force: bool = False) -> logging.Logger:
    """建 handler，幂等。重复调用不会叠加 handler。"""
    root = logging.getLogger()
    if not force and any(getattr(h, _HANDLER_TAG, False) for h in root.handlers):
        return logging.getLogger("kcb")

    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()

    fmt = str(config.get("logging.format") or "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    datefmt = str(config.get("logging.date_format") or "%Y-%m-%d %H:%M:%S")
    env_level = os.environ.get("KCB_LOG_LEVEL", "").strip()
    global_level = parse_level(env_level or config.get("logging.level"), logging.INFO)

    console_cfg = config.get("logging.console") or {}
    if _enabled(console_cfg.get("enabled")):
        console_level = parse_level(console_cfg.get("level"), global_level)
        stream = sys.stdout if sys.stdout.isatty() else sys.stderr
        use_color = _enabled(config.get("logging.color"), True) and stream.isatty()
        formatter = _ColorFormatter(fmt, datefmt) if use_color else logging.Formatter(fmt, datefmt)
        handler = logging.StreamHandler(stream)
        handler.setLevel(console_level)
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)

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
            setattr(handler, _HANDLER_TAG, True)
            root.addHandler(handler)
        except OSError as exc:
            logging.getLogger("kcb.log").warning("日志文件不可用（%s）：%s", path, exc)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    # 总闸。handler 上的 level 只能更严，想放宽必须动这里
    root.setLevel(global_level)
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
