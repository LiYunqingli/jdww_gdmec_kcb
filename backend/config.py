"""配置加载：`config.json` 是唯一的设置入口，代码里不再散落常量。

设计要点：
- `DEFAULTS` 与 `config.json` 同构，缺项 / 写错类型都不会让服务起不来，只是回落默认值。
- 配置文件缺失、JSON 语法错误等不抛异常，记进 `LOAD_NOTES`，由日志模块在启动时打出来。
- 读取走点号路径：`config.get("logging.level", "INFO")`。
- 重启生效。运行期不改内存里的配置，避免"改了一半"的状态。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = "config.json"


DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "title": "课表代理",
        "version": "1.0",
        "cors_origins": ["*"],
    },
    "logging": {
        "level": "DEBUG",
        "format": "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s",
        "date_format": "%Y-%m-%d %H:%M:%S",
        "color": True,
        "console": {"enabled": True, "level": "INFO"},
        "file": {
            "enabled": True,
            "path": "logs/app.log",
            "level": "DEBUG",
            "max_bytes": 2097152,
            "backup_count": 5,
            "encoding": "utf-8",
        },
        "quiet": {
            "httpx": "WARNING",
            "httpcore": "WARNING",
            "uvicorn.access": "WARNING",
            "asyncio": "WARNING",
        },
    },
    "paths": {
        "data_dir": "data",
        "session_file": "sessions.json",
        "frontend_dir": "frontend",
    },
    "http": {
        "timeout": 25,
        "max_redirects": 14,
        "max_redirects_login": 16,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    },
    "jwxt": {
        "base": "https://jwxt.gdmec.edu.cn",
        "portal": "https://home.gdmec.edu.cn",
        "sso": "https://auth.gdmec.edu.cn",
    },
    "terms": {"3": "第一学期", "12": "第二学期", "16": "第三学期"},
    "timetable": {
        "period_minutes": 40,
        "break_between_periods": 10,
        "break_between_pairs": 25,
        "blocks": [[1, 4, "08:30"], [5, 8, "14:10"], [9, 12, "19:00"]],
    },
}

# 启动时发现的问题（文件缺失 / 语法错误 / 类型不对），交给 logging_setup 打印
LOAD_NOTES: list[str] = []


def _merge(base: dict[str, Any], override: Any, trail: str = "") -> dict[str, Any]:
    """深合并：override 覆盖 base，但只覆盖同类型的东西。

    类型不匹配（比如把 logging.level 写成 123）时保留默认值并记一条 note，
    这样"配置写错"永远不会变成"服务起不来"。
    """
    if not isinstance(override, dict):
        return base
    out = dict(base)
    for key, val in override.items():
        default = base.get(key)
        where = f"{trail}.{key}" if trail else key
        if isinstance(default, dict):
            if isinstance(val, dict):
                out[key] = _merge(default, val, where)
            else:
                LOAD_NOTES.append(f"配置项 {where} 应为对象，已忽略（保持默认值）")
        elif isinstance(default, list):
            if isinstance(val, list):
                out[key] = val
            else:
                LOAD_NOTES.append(f"配置项 {where} 应为数组，已忽略（保持默认值）")
        elif isinstance(default, bool):
            if isinstance(val, bool):
                out[key] = val
            else:
                LOAD_NOTES.append(f"配置项 {where} 应为 true/false，已忽略")
        elif isinstance(default, int):
            if isinstance(val, int) and not isinstance(val, bool):
                out[key] = val
            else:
                LOAD_NOTES.append(f"配置项 {where} 应为整数，已忽略")
        elif isinstance(default, str):
            if isinstance(val, str):
                out[key] = val
            else:
                LOAD_NOTES.append(f"配置项 {where} 应为字符串，已忽略")
        else:
            out[key] = val
    return out


def _config_path() -> Path:
    """`KCB_CONFIG` 环境变量可指向另一份配置，默认项目根目录的 config.json。"""
    env = os.environ.get("KCB_CONFIG", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else ROOT / p
    return ROOT / DEFAULT_CONFIG_FILE


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or _config_path()
    if not path.exists():
        LOAD_NOTES.append(f"未找到配置文件 {path}，全部使用内置默认值")
        return _merge({}, DEFAULTS)
    try:
        raw = json.loads(path.read_text("utf-8-sig"))
    except json.JSONDecodeError as exc:
        LOAD_NOTES.append(f"配置文件 {path} 语法错误（第 {exc.lineno} 行：{exc.msg}），全部使用内置默认值")
        return _merge({}, DEFAULTS)
    except OSError as exc:
        LOAD_NOTES.append(f"读取配置文件 {path} 失败：{exc}，全部使用内置默认值")
        return _merge({}, DEFAULTS)
    if not isinstance(raw, dict):
        LOAD_NOTES.append(f"配置文件 {path} 顶层必须是对象，已忽略")
        return _merge({}, DEFAULTS)
    return _merge(DEFAULTS, raw)


CONFIG: dict[str, Any] = load()
CONFIG_PATH: Path = _config_path()


def get(dotted: str, default: Any = None) -> Any:
    """按点号路径取值：`config.get("logging.file.enabled", True)`。"""
    cur: Any = CONFIG
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve(rel: str | Path) -> Path:
    """相对路径一律相对项目根目录，避免受启动时 cwd 影响。"""
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p)


def reload() -> dict[str, Any]:
    """重新读盘，仅测试 / 运维脚本用。"""
    global CONFIG
    LOAD_NOTES.clear()
    CONFIG = load()
    return CONFIG
