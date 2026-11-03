"""把正方 kbList 的原始数据规整成前端好用的结构。

真实数据要点（2026-2027-1 实测）：
- `jcs` 是整段跨度（如 "1-4"），`jc` 是分段单元格（如 "1-2节"），同一门课会拆成多行，
  因此必须按 (星期, 起止, 课程, 地点, 周次) 去重，否则课表会重叠。
- `jcma`/`jcmb` 是节次位掩码（jcma=整段, jcmb=当前单元格），可作为 jcs 缺失时的兜底。
- `zcd` 的"单/双"按逗号分段生效：`11-13周(单),14-17周` 表示前半单周、后半全周。
"""
from __future__ import annotations

import functools
import logging
import re
from typing import Any

from . import config

log = logging.getLogger("kcb.timetable")

# ---------------- 作息时间 ----------------
# 一节课多久、课间歇多久、上午/下午/晚上各大节几点打铃，全部在 config.json 的
# timetable 段里。默认值（学校现行作息）：
#   period_minutes=40，同大节内两小节课间 10 分钟，每上完两节歇 25 分钟，
#   上午 4 节 08:30 起、下午 4 节 14:10 起、晚上 4 节 19:00 起。
# 大节之间的午饭、晚饭时长不写死，由下一个大节的锚点时间自然决定。
_DEFAULT_SCHEDULE = {
    "period_minutes": 40,
    "break_between_periods": 10,
    "break_between_pairs": 25,
    "blocks": [[1, 4, "08:30"], [5, 8, "14:10"], [9, 12, "19:00"]],
}


def _to_min(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@functools.lru_cache(maxsize=1)
def get_slots() -> list[dict[str, Any]]:
    """按配置算出全部节次的起止时间，首次调用时算一次。"""
    cfg = dict(_DEFAULT_SCHEDULE)
    cfg.update({k: v for k, v in (config.get("timetable") or {}).items() if k in cfg})
    period = int(cfg["period_minutes"])
    break_inner = int(cfg["break_between_periods"])
    break_pair = int(cfg["break_between_pairs"])

    slots: list[dict[str, Any]] = []
    for item in cfg["blocks"]:
        try:
            first, last, anchor = int(item[0]), int(item[1]), str(item[2])
        except (TypeError, ValueError, IndexError):
            log.warning("timetable.blocks 里有非法项，已跳过：%r", item)
            continue
        cur = _to_min(anchor)
        for idx in range(first, last + 1):
            slots.append({"idx": idx, "start": _fmt(cur), "end": _fmt(cur + period)})
            cur += period + (break_inner if idx % 2 == 1 else break_pair)
    log.debug("作息已按配置展开：%d 节，从 %s 到 %s", len(slots),
              slots[0]["start"] if slots else "-", slots[-1]["end"] if slots else "-")
    if not slots:
        log.warning("timetable.blocks 配置为空或全部非法，课表节次将不可用，请检查 config.json")
    return slots

WEEKDAY_CN = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def parse_weeks(zcd: str) -> list[int]:
    """解析周次串，单/双按段生效。

    支持：'1-16周'、'第19周'、'1-8周,10-16周'、'11-13周(单),14-17周'、'1-16周(双)'。
    """
    s = (zcd or "").replace(" ", "").replace("\u00a0", "")
    if not s:
        return []
    weeks: set[int] = set()
    for part in re.split(r"[,，;；]", s):
        part = part.strip()
        if not part:
            continue
        parity = None
        if "单" in part:
            parity = 1
        elif "双" in part:
            parity = 2
        core = re.sub(r"[（(][^)）]*[)）]", "", part).replace("周", "").replace("第", "")
        seg_weeks: set[int] = set()
        for seg in re.split(r"[,，;；、]", core):
            seg = seg.strip()
            if not seg:
                continue
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", seg)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if a > b:
                    a, b = b, a
                seg_weeks.update(range(a, b + 1))
            elif seg.isdigit():
                seg_weeks.add(int(seg))
        if parity == 1:
            seg_weeks = {w for w in seg_weeks if w % 2 == 1}
        elif parity == 2:
            seg_weeks = {w for w in seg_weeks if w % 2 == 0}
        weeks |= seg_weeks
    return sorted(weeks)


def _mask_span(mask: Any) -> tuple[int, int] | None:
    """位掩码 → (起, 止)。jcma=15 -> (1,4)；240 -> (5,8)。"""
    try:
        m = int(mask)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    bits = [i + 1 for i in range(24) if m & (1 << i)]
    if not bits:
        return None
    return (bits[0], bits[-1])


def parse_span(item: dict[str, Any]) -> tuple[int, int]:
    """取课程的节次跨度：优先 jcs 文本，其次 jcma 掩码，最后 jc 文本。"""
    for key in ("jcs", "jc"):
        txt = str(item.get(key) or "")
        m = re.search(r"(\d+)\s*-\s*(\d+)", txt)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            return (min(a, b), max(a, b))
        m = re.search(r"(\d+)", txt)
        if m:
            v = int(m.group(1))
            return (v, v)
    span = _mask_span(item.get("jcma"))
    if span:
        return span
    return (0, 0)


def parse_sections(jcs: str) -> tuple[int, int]:
    """兼容旧接口：'1-2' -> (1,2)。"""
    m = re.search(r"(\d+)\s*-\s*(\d+)", str(jcs or ""))
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    m = re.search(r"\d+", str(jcs or ""))
    if m:
        v = int(m.group(0))
        return (v, v)
    return (0, 0)


def _pick(d: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    kb = raw.get("kbList") or []
    sjk = raw.get("sjkList") or []
    xsxx = raw.get("xsxx") or {}

    courses: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    skipped_bad_weekday = 0
    skipped_no_span = 0
    duplicated = 0

    for i, item in enumerate(kb):
        if not isinstance(item, dict):
            continue
        try:
            weekday = int(_pick(item, "xqj", default="0") or 0)
        except ValueError:
            weekday = 0
        if not (1 <= weekday <= 7):
            skipped_bad_weekday += 1
            log.debug("跳过第 %d 条：星期值非法 xqj=%r 课程=%r", i, item.get("xqj"), item.get("kcmc"))
            continue

        start, end = parse_span(item)
        if start < 1:
            skipped_no_span += 1
            log.debug("跳过第 %d 条：节次无法解析 jcs=%r jcma=%r 课程=%r",
                      i, item.get("jcs"), item.get("jcma"), item.get("kcmc"))
            continue

        name = _pick(item, "kcmc", default="未命名课程")
        room = _pick(item, "cdmc")
        weeks_text = _pick(item, "zcd")
        teacher = _pick(item, "xm")
        weeks = parse_weeks(weeks_text)
        if not weeks:
            log.debug("课程无有效周次，周次串=%r 课程=%r", weeks_text, name)

        key = (weekday, start, end, name, room, weeks_text)
        if key in seen:
            duplicated += 1
            continue
        seen.add(key)

        courses.append({
            "id": f"{_pick(item, 'jxb_id', default=str(i))}-{i}",
            "name": name,
            "teacher": teacher,
            "room": room,
            "campus": _pick(item, "xqmc"),
            "weekday": weekday,
            "weekday_cn": WEEKDAY_CN[weekday],
            "start": start,
            "end": end,
            "weeks": weeks,
            "weeks_text": weeks_text,
            "course_no": _pick(item, "kch", "kch_id"),
            "class_name": _pick(item, "jxbmc"),
            "credit": _pick(item, "xf"),
            "note": _pick(item, "xkbz"),
            "category": _pick(item, "kclb", "kclbmc"),
        })

    courses.sort(key=lambda c: (c["weekday"], c["start"], c["name"]))

    practices = []
    for item in sjk:
        if isinstance(item, dict):
            practices.append({
                "name": _pick(item, "sjkcgs", "kcmc", default="实践环节"),
                "weeks_text": _pick(item, "zcd", "qsjs"),
            })

    student = {
        "name": _pick(xsxx, "XM"),
        "id": _pick(xsxx, "XH_ID", "XH"),
        "class_name": _pick(xsxx, "BJMC"),
        "major": _pick(xsxx, "ZYMC"),
        "grade": _pick(xsxx, "NJDM_ID"),
        "term_name": _pick(xsxx, "XNMC"),
    }

    log.debug(
        "规整完成：原始 %d 条 → 有效 %d 门（去重 %d 条，星期非法 %d 条，无节次 %d 条），实践环节 %d 项",
        len(kb), len(courses), duplicated, skipped_bad_weekday, skipped_no_span, len(practices),
    )

    return {
        "student": student,
        "courses": courses,
        "practices": practices,
        "slots": get_slots(),
        "meta": {
            "course_count": len(courses),
            "max_week": max((max(c["weeks"]) for c in courses if c["weeks"]), default=0),
            "raw_keys": sorted(raw.keys()),
        },
    }
