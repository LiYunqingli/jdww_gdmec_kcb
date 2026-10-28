"""把正方 kbList 的原始数据规整成前端好用的结构。

真实数据要点（2026-2027-1 实测）：
- `jcs` 是整段跨度（如 "1-4"），`jc` 是分段单元格（如 "1-2节"），同一门课会拆成多行，
  因此必须按 (星期, 起止, 课程, 地点, 周次) 去重，否则课表会重叠。
- `jcma`/`jcmb` 是节次位掩码（jcma=整段, jcmb=当前单元格），可作为 jcs 缺失时的兜底。
- `zcd` 的"单/双"按逗号分段生效：`11-13周(单),14-17周` 表示前半单周、后半全周。
"""
from __future__ import annotations

import re
from typing import Any

SLOTS = [
    {"idx": 1, "start": "08:00", "end": "08:45"},
    {"idx": 2, "start": "08:55", "end": "09:40"},
    {"idx": 3, "start": "10:00", "end": "10:45"},
    {"idx": 4, "start": "10:55", "end": "11:40"},
    {"idx": 5, "start": "14:00", "end": "14:45"},
    {"idx": 6, "start": "14:55", "end": "15:40"},
    {"idx": 7, "start": "16:00", "end": "16:45"},
    {"idx": 8, "start": "16:55", "end": "17:40"},
    {"idx": 9, "start": "19:00", "end": "19:45"},
    {"idx": 10, "start": "19:55", "end": "20:40"},
    {"idx": 11, "start": "20:50", "end": "21:35"},
    {"idx": 12, "start": "21:45", "end": "22:30"},
]

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

    for i, item in enumerate(kb):
        if not isinstance(item, dict):
            continue
        try:
            weekday = int(_pick(item, "xqj", default="0") or 0)
        except ValueError:
            weekday = 0
        if not (1 <= weekday <= 7):
            continue

        start, end = parse_span(item)
        if start < 1:
            continue

        name = _pick(item, "kcmc", default="未命名课程")
        room = _pick(item, "cdmc")
        weeks_text = _pick(item, "zcd")
        teacher = _pick(item, "xm")
        weeks = parse_weeks(weeks_text)

        key = (weekday, start, end, name, room, weeks_text)
        if key in seen:
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

    return {
        "student": student,
        "courses": courses,
        "practices": practices,
        "slots": SLOTS,
        "meta": {
            "course_count": len(courses),
            "max_week": max((max(c["weeks"]) for c in courses if c["weeks"]), default=0),
            "raw_keys": sorted(raw.keys()),
        },
    }
