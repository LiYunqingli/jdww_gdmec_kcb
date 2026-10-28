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

# ---------------- 作息时间 ----------------
# 一节课 45 分钟；同一大节内两小节之间歇 10 分钟，
# 每上完两节歇 25 分钟（2→3、4→5、6→7、8→9）。
# 上午 / 下午 / 晚上各大节的开始时间是固定的，午饭、晚饭时长由锚点自然决定，
# 所以第 9 节从 19:00 开始这一点是靠 BLOCKS 里第三个锚点保证的，
# 第 8 节 17:55 下课后即晚饭时间。
PERIOD_MIN = 45
BREAK_BETWEEN_PERIODS = 10
BREAK_BETWEEN_PAIRS = 25

# (起始节次, 结束节次, 大节开始时间)
BLOCKS = [
    (1, 4, "08:30"),    # 上午
    (5, 8, "14:10"),    # 下午
    (9, 12, "19:00"),   # 晚上
]


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _build_slots() -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for first, last, anchor in BLOCKS:
        cur = _to_min(anchor)
        for idx in range(first, last + 1):
            slots.append({"idx": idx, "start": _fmt(cur), "end": _fmt(cur + PERIOD_MIN)})
            cur += PERIOD_MIN + (
                BREAK_BETWEEN_PERIODS if idx % 2 == 1 else BREAK_BETWEEN_PAIRS
            )
    return slots


SLOTS = _build_slots()

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
