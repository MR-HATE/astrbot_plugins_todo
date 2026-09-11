"""把「模型给出的草稿」变成「可以安全写入的待办」。

职责：
1. **时间归一化**：绝对日期直接用；「明天/下周三/3 天后」这类相对说法也兜底解析，
   避免模型不知道今天几号时整条待办丢失日期。
2. **校验**：标题长度、优先级枚举、提醒时间必须合法且在未来、子步骤数量上限。
3. **去重**：同一批次内去重 + 与历史导入记录（source_key）比对。
4. **渲染**：生成给用户看的预览卡片与导入结果。

这一层是纯函数，不碰网络、不碰 KV，方便单测（M6）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import (
    IMPORTANCE_ALIASES,
    NOTE_MAX,
    STEP_COUNT_MAX,
    STEP_MAX,
    TITLE_MAX,
    TaskDraft,
    clean_text,
)

#: 暂存计划的有效期（秒）。超过后 confirm 会被拒绝，避免"隔天误确认"。
PENDING_TTL = 1800

_WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEEKDAY_CHARS = {
    "一": 0, "1": 0,
    "二": 1, "2": 1,
    "三": 2, "3": 2,
    "四": 3, "4": 3,
    "五": 4, "5": 4,
    "六": 5, "6": 5,
    "日": 6, "天": 6, "7": 6, "0": 6,
}

_RE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_RE_MD = re.compile(r"^(\d{1,2})[月/\-](\d{1,2})[日号]?$")
_RE_N_DAYS = re.compile(r"^(\d{1,3})\s*天\s*(?:后|之后|以后)$")
_RE_WEEK = re.compile(r"^(下|下个|下一|本|这|这个)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天0-7])$")
_RE_CLOCK_ARABIC = re.compile(r"^(\d{1,2})\s*[:：点时]\s*(\d{1,2})?")
_RE_CLOCK_CN = re.compile(r"^([零〇一二两三四五六七八九十]+)\s*[点时]")
#: 结尾处的时刻，用于把「9月18日 20:00」「明天 晚上8点」拆开
_RE_TRAILING_TIME = re.compile(
    r"(?:上午|早上|早晨|凌晨|中午|下午|傍晚|晚上|夜里|晚)?\s*"
    r"(?:\d{1,2}\s*[:：]\s*\d{2}"
    r"|\d{1,2}\s*[点时](?:\d{1,2}|半|一刻|三刻|[零〇一二两三四五六七八九十]+分?)?"
    r"|[零〇一二两三四五六七八九十]+\s*[点时](?:半|一刻|三刻|[零〇一二两三四五六七八九十]+分?)?"
    r")\s*$"
)

#: 汉字数字（「十」不在此表内，由 _cn_number 单独处理）
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_RE_DATE_TIME = re.compile(r"^(\d{4}-\d{1,2}-\d{1,2})[ T](\d{1,2}:\d{2})")

_SIMPLE_OFFSETS = {
    "今天": 0, "今日": 0, "当天": 0, "本日": 0,
    "明天": 1, "明日": 1,
    "后天": 2,
    "大后天": 3,
    "昨天": -1, "昨日": -1,
}


# ---------------------------------------------------------------- 时区

def _try_iso_datetime(text: str) -> datetime | None:
    """尽力把字符串解析成 datetime（支持 ``Z`` 后缀；3.10 不认 ``Z``，手动替换）。"""
    candidate = str(text).strip()
    if not candidate:
        return None
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    # 把 "2026-09-15 20:00" 这种空格分隔也交给 fromisoformat
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        try:
            return datetime.fromisoformat(candidate.replace(" ", "T", 1))
        except ValueError:
            return None


def get_tz(tz_name: str | None) -> ZoneInfo:
    """取时区，非法名称回退到系统本地时区。"""
    try:
        return ZoneInfo(str(tz_name or "").strip() or "Asia/Shanghai")
    except Exception:
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")  # type: ignore[return-value]


def local_today(tz_name: str | None) -> date:
    return datetime.now(get_tz(tz_name)).date()


def local_now(tz_name: str | None) -> datetime:
    return datetime.now(get_tz(tz_name))


# ---------------------------------------------------------------- 解析

def weekday_cn(value: date) -> str:
    return _WEEKDAYS_CN[value.weekday()]


def resolve_date(raw: str | None, today: date) -> tuple[str | None, str | None]:
    """把各种日期写法解析成 ``YYYY-MM-DD``。

    Returns:
        (绝对日期, 警告文案)。解析不出来时返回 (None, 警告)。
    """
    if not raw:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None

    # 1) 常规绝对日期（允许带时间，时间部分交给 resolve_time）
    m = _RE_ISO.match(text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y, mo, d).isoformat(), None
        except ValueError:
            return None, f"日期「{text}」不是有效日期，已忽略截止时间"

    # 2) 相对说法
    if text in _SIMPLE_OFFSETS:
        return (today + timedelta(days=_SIMPLE_OFFSETS[text])).isoformat(), None

    m = _RE_N_DAYS.match(text)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat(), None

    m = _RE_WEEK.match(text)
    if m:
        prefix, char = m.group(1) or "", m.group(2)
        target = _WEEKDAY_CHARS.get(char)
        if target is not None:
            if prefix.startswith("下"):
                # 「下周三」= 下一个自然周的周三
                next_monday = today + timedelta(days=7 - today.weekday())
                return (next_monday + timedelta(days=target)).isoformat(), None
            # 「周三」= 最近的周三（含今天）
            return (today + timedelta(days=(target - today.weekday()) % 7)).isoformat(), None

    # 3) 月日（没写年份时：今年；若已过去则顺延到明年）
    m = _RE_MD.match(text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(today.year, mo, d)
        except ValueError:
            return None, f"日期「{text}」不是有效日期，已忽略截止时间"
        if candidate < today:
            try:
                candidate = date(today.year + 1, mo, d)
            except ValueError:
                return None, None
        return candidate.isoformat(), None

    return None, f"无法识别日期「{text}」，已忽略该项的截止时间"


def _cn_number(text: str) -> int | None:
    """汉字数字 → int（支持 0-99：``三`` / ``十`` / ``十一`` / ``二十三``）。"""
    value = (text or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CN_DIGITS.get(left) if left else 1
        units = _CN_DIGITS.get(right) if right else 0
        if tens is None or units is None:
            return None
        return tens * 10 + units
    return _CN_DIGITS.get(value)


def _minute_word(rest: str) -> int | None:
    """解析「点」之后的分钟说法：``半`` / ``一刻`` / ``三刻`` / ``30`` / ``三十``。"""
    text = (rest or "").strip()
    if not text:
        return None
    if text.startswith("半"):
        return 30
    m = re.match(r"^([一二两三四])?\s*刻", text)
    if m:
        count = _cn_number(m.group(1)) if m.group(1) else 1
        return {1: 15, 2: 30, 3: 45}.get(count or 0)
    m = re.match(r"^(\d{1,2})\s*分?", text)
    if m:
        return int(m.group(1))
    m = re.match(r"^([零〇一二两三四五六七八九十]+)\s*分?", text)
    if m:
        return _cn_number(m.group(1))
    return None


def resolve_time(raw: str | None) -> tuple[str | None, str | None]:
    """把时刻说法解析成 ``HH:MM``。

    支持：``9:00``、``9点``、``9点30``、``9点半``、``9点一刻``、``9点三刻``、
    ``下午3点``、``晚上8点``，以及汉字数字 ``九点``、``九点半``、``下午三点``、``十二点``。
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None

    # 先剥离"上午/下午/晚上"这类前缀，统一按 24 小时制处理
    meridiem = ""
    for prefix in ("上午", "早上", "早晨", "凌晨", "中午", "下午", "傍晚", "晚上", "夜里", "晚"):
        if text.startswith(prefix):
            meridiem = prefix
            text = text[len(prefix):].strip()
            break

    hh: int | None = None
    mm: int | None = None

    m = _RE_CLOCK_ARABIC.match(text)
    if m:
        # 9:30 / 9点30 / 9点
        hh = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else _minute_word(text[m.end():])
    else:
        m = _RE_CLOCK_CN.match(text)
        if m:
            # 九点 / 九点半 / 下午三点一刻
            hh = _cn_number(m.group(1))
            mm = _minute_word(text[m.end():])
        else:
            m = re.match(
                r"^([零〇一二两三四五六七八九十]+)\s*[:：]\s*([零〇一二两三四五六七八九十]+)$",
                text,
            )
            if m:
                # 九:三十
                hh = _cn_number(m.group(1))
                mm = _cn_number(m.group(2))

    if hh is None:
        return None, f"无法识别时间「{raw}」，已忽略具体时刻"
    if mm is None:
        mm = 0

    if meridiem in ("下午", "傍晚", "晚上", "夜里", "晚") and hh < 12:
        hh += 12
    elif meridiem == "中午" and hh < 11:
        hh += 12
    elif meridiem in ("上午", "早上", "早晨", "凌晨") and hh == 12:
        hh = 0

    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None, f"时间「{raw}」超出范围，已忽略具体时刻"
    return f"{hh:02d}:{mm:02d}", None


def split_date_time(text: str) -> tuple[str, str | None]:
    """把「9月18日 20:00」「明天 晚上8点」拆成 (日期部分, 时刻部分)。

    只剥离**结尾**的时刻，拆不出来时原样返回 ``(text, None)``，
    避免把日期里本身的数字误当成时刻。
    """
    raw = (text or "").strip()
    if not raw:
        return "", None
    match = _RE_TRAILING_TIME.search(raw)
    if not match:
        return raw, None
    time_part = match.group(0).strip()
    date_part = raw[: match.start()].strip(" ，,、;；:：")
    if not date_part:
        # 整串就是一个时刻（例如「晚上8点」），交给只解析时刻的分支
        return raw, None
    return date_part, time_part


def parse_due(
    raw_date: str | None,
    raw_time: str | None,
    today: date,
    tz_name: str | None = None,
) -> tuple[str | None, str | None, list[str]]:
    """综合解析截止时间，返回 (date, time, warnings)。

    ``raw_date`` 里如果内嵌了时刻（甚至带时区偏移，如 ``2026-09-18T09:00:00+08:00``），
    会先换算到配置时区，再拆成 date + time。
    """
    warnings: list[str] = []

    text = str(raw_date).strip() if raw_date is not None else ""
    if text and re.match(r"^\d{4}-\d{1,2}-\d{1,2}[ T]", text):
        iso = _try_iso_datetime(text)
        if iso is not None:
            if iso.tzinfo is not None and tz_name:
                aware_local = iso.astimezone(get_tz(tz_name))
                local = aware_local.replace(tzinfo=None)
                if iso.utcoffset() != aware_local.utcoffset():
                    warnings.append("截止时间带时区，已按配置时区换算")
            else:
                local = iso.replace(tzinfo=None)
            has_clock = bool(re.search(r"\d{1,2}:\d{2}", text))
            return local.date().isoformat(), (local.strftime("%H:%M") if has_clock else None), warnings

    # 中文日期与时刻混在一起的情况（「9月18日 20:00」「明天 晚上8点」）
    if text and (raw_time is None or str(raw_time).strip() == ""):
        date_part, time_part = split_date_time(text)
        if time_part and resolve_date(date_part, today)[0]:
            raw_date, raw_time = date_part, time_part

    due_date, warn = resolve_date(raw_date, today)
    if warn:
        warnings.append(warn)
    due_time, warn_t = resolve_time(raw_time)
    if warn_t:
        warnings.append(warn_t)
    # 日期里内嵌了时间的情况（2026-09-18T09:00）
    if raw_date:
        m = _RE_DATE_TIME.match(str(raw_date).strip())
        if m and not due_time:
            due_time, _ = resolve_time(m.group(2))
    return due_date, due_time, warnings


def normalize_important(raw: object) -> tuple[str, str | None]:
    if raw is None or str(raw).strip() == "":
        return "normal", None
    text = str(raw).strip()
    value = IMPORTANCE_ALIASES.get(text.lower()) or IMPORTANCE_ALIASES.get(text)
    if value:
        return value, None

    # 子串兜底：模型可能输出「很低」「非常重要」这类没进别名词典的说法。
    # 注意先判「不重要」，否则会被「重要」误判成 high。
    if any(key in text for key in ("不重要", "不急", "可以缓", "次要", "低")):
        return "low", None
    if any(key in text for key in ("高", "急", "重要")):
        return "high", None
    if any(key in text for key in ("中", "普通", "一般", "正常")):
        return "normal", None
    return "normal", f"优先级「{raw}」无法识别，已按「普通」处理"


def normalize_tasks(raw_tasks, tz_name: str, *, now: datetime | None = None) -> list[TaskDraft]:
    """把模型给的原始数组整成 TaskDraft 列表（不抛异常，问题写进 warnings）。"""
    today = (now or local_now(tz_name)).date()
    drafts: list[TaskDraft] = []

    for item in raw_tasks or []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue

        title = clean_text(item.get("title"), TITLE_MAX)
        if not title:
            continue

        warnings: list[str] = []
        if len(clean_text(item.get("title"), 10_000)) > TITLE_MAX:
            warnings.append("标题过长已截断")

        due_date, due_time, due_warnings = parse_due(
            item.get("due_date"), item.get("due_time"), today, tz_name
        )
        warnings.extend(due_warnings)
        if due_time and not due_date:
            warnings.append("只有时刻没有日期，已忽略该时刻")
            due_time = None
        if due_date and date.fromisoformat(due_date) < today:
            warnings.append("截止日期早于今天")

        importance, imp_warning = normalize_important(item.get("importance"))
        if imp_warning:
            warnings.append(imp_warning)

        steps: list[str] = []
        for step in item.get("steps") or []:
            text = clean_text(step, STEP_MAX)
            if text and text not in steps:
                steps.append(text)
        if len(steps) > STEP_COUNT_MAX:
            steps = steps[:STEP_COUNT_MAX]
            warnings.append(f"子步骤超过 {STEP_COUNT_MAX} 条，已截断")

        remind_at, remind_warning = parse_remind(
            item.get("remind_at"), tz_name, now, fallback_date=due_date
        )
        if remind_warning:
            warnings.append(remind_warning)

        note = clean_text(item.get("note"), NOTE_MAX) or None
        source_key = clean_text(item.get("source_key"), 64) or None

        # 注意：source_key 留空时**不在这里**补默认值——默认指纹要带上目标列表名，
        # 而列表是在导入阶段才解析出来的，所以由调用方（main.py）补齐。
        drafts.append(
            TaskDraft(
                title=title,
                due_date=due_date,
                due_time=due_time,
                importance=importance,
                note=note,
                remind_at=remind_at,
                steps=steps,
                source_key=source_key,
                warnings=warnings,
            )
        )

    return drafts


def parse_remind(
    raw: object,
    tz_name: str,
    now: datetime | None = None,
    *,
    fallback_date: str | None = None,
) -> tuple[str | None, str | None]:
    """解析提醒时间，返回 (``YYYY-MM-DDTHH:MM`` 本地时间, 警告)。

    兼容三种输入：
    - **带时区偏移**的 ISO 串（``2026-09-15T20:00:00+08:00``、``...Z``）→ 换算到配置时区；
    - 本地 ISO 串 / 中文写法（``2026-09-15T20:00``、``9月18日 20:00``、``下午三点``）；
    - 只给日期或相对说法（``明天``）→ 默认当天 09:00。

    只给时刻（``下午三点``）时，优先落在 ``fallback_date``（通常是该待办的截止日期）当天，
    没有截止日期才按今天算、已过则顺延明天。

    注意：内部一律用**带时区**的时间做比较，否则 naive 与 aware 相减会抛 TypeError。
    """
    # 模型有时会照抄 Graph 的 dateTimeTimeZone 结构
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date_time") or raw.get("datetime")
    if raw is None or str(raw).strip() == "":
        return None, None

    tz = get_tz(tz_name)
    current = now or local_now(tz_name)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    current = current.astimezone(tz)
    current_naive = current.replace(tzinfo=None)

    text = str(raw).strip()
    has_clock = bool(re.search(r"\d{1,2}:\d{2}", text))

    # A) 能按 ISO 解析（含带偏移量的情况）
    iso = _try_iso_datetime(text)
    if iso is not None and re.match(r"^\d{4}-\d{1,2}-\d{1,2}", text):
        if iso.tzinfo is None:
            moment, note = iso, None
        else:
            aware_local = iso.astimezone(tz)
            moment = aware_local.replace(tzinfo=None)
            note = (
                "提醒时间带时区，已按配置时区换算"
                if iso.utcoffset() != aware_local.utcoffset()
                else None
            )
        if not has_clock:
            moment = moment.replace(hour=9, minute=0, second=0, microsecond=0)
            return _finish_remind(moment, current_naive, "提醒未指定具体时刻，已按 09:00 处理")
        return _finish_remind(moment, current_naive, note)

    # B) 相对说法 / 中文日期（可能带空格分隔的时刻：「9月18日 20:00」「明天 晚上8点」）
    day = None
    clock_from_split = None
    date_part, time_part = split_date_time(text)
    if time_part:
        day, _w = resolve_date(date_part, current.date())
        if day:
            clock_from_split, _tw = resolve_time(time_part)
    if day is None:
        day, _time, _warn = parse_due(text, None, current.date(), tz_name)
    if day:
        clock = clock_from_split or "09:00"
        note = None if clock_from_split else "提醒未指定具体时刻，已按 09:00 处理"
        return _finish_remind(datetime.fromisoformat(f"{day}T{clock}"), current_naive, note)

    # C) 只给了时刻（例如「晚上8点」「下午三点」）
    clock, _warn_t = resolve_time(text)
    if clock:
        if fallback_date:
            moment = datetime.fromisoformat(f"{fallback_date}T{clock}")
            return _finish_remind(moment, current_naive, None)
        moment = datetime.fromisoformat(f"{current_naive.date().isoformat()}T{clock}")
        if moment < current_naive:
            return _finish_remind(
                moment + timedelta(days=1), current_naive, "只给了时刻且今天已过，已顺延到明天"
            )
        return _finish_remind(moment, current_naive, "只给了时刻，按今天处理")

    return None, f"无法识别提醒时间「{raw}」，已忽略提醒"


def _finish_remind(
    moment: datetime, current_naive: datetime, note: str | None
) -> tuple[str | None, str | None]:
    if moment < current_naive:
        return None, "提醒时间已过去，已忽略提醒"
    return moment.strftime("%Y-%m-%dT%H:%M"), note


# ---------------------------------------------------------------- 去重

def dedupe_batch(tasks: list[TaskDraft], list_name: str) -> tuple[list[TaskDraft], list[TaskDraft]]:
    """同一次导入内部的重复（同列表 + 同日期 + 同标题）。"""
    seen: set[str] = set()
    unique: list[TaskDraft] = []
    duplicated: list[TaskDraft] = []
    for task in tasks:
        key = task.fingerprint(list_name)
        if key in seen:
            duplicated.append(task)
            continue
        seen.add(key)
        unique.append(task)
    return unique, duplicated


def apply_seen(tasks: list[TaskDraft], list_name: str, seen: dict) -> tuple[list[TaskDraft], list[TaskDraft]]:
    """与历史导入记录比对，返回 (待写入, 已存在而跳过)。"""
    fresh: list[TaskDraft] = []
    skipped: list[TaskDraft] = []
    for task in tasks:
        key = task.source_key or task.fingerprint(list_name)
        if key in seen:
            skipped.append(task)
        else:
            fresh.append(task)
    return fresh, skipped


# ---------------------------------------------------------------- 渲染

def format_due(due_date: str | None, due_time: str | None, today: date) -> str:
    if not due_date:
        return "未设截止"
    try:
        value = date.fromisoformat(due_date)
    except ValueError:
        return due_date
    delta = (value - today).days
    relative = {
        0: "今天",
        1: "明天",
        2: "后天",
    }.get(delta)
    if relative is None and delta < 0:
        relative = f"已逾期 {abs(delta)} 天"
    label = f"{due_date}（{weekday_cn(value)}）"
    if relative:
        label = f"{relative} · {label}"
    if due_time:
        label += f" {due_time}"
    return label


def render_preview(list_name: str, tasks: list[TaskDraft], plan_id: str, tz_name: str, *, max_show: int = 30) -> str:
    """生成给用户确认的预览卡片。"""
    today = local_today(tz_name)
    lines = [f"📋 待办预览 · 目标列表「{list_name}」 · 共 {len(tasks)} 项", ""]

    for index, task in enumerate(tasks[:max_show], start=1):
        flag = {"high": "🔴 ", "low": "🔽 "}.get(task.importance, "")
        lines.append(f"{index}. {flag}{task.title}")
        detail = [f"   截止：{format_due(task.due_date, task.due_time, today)}"]
        if task.remind_at:
            detail.append(f"   提醒：{task.remind_at.replace('T', ' ')}")
        if task.note:
            detail.append(f"   备注：{task.note}")
        if task.steps:
            detail.append(f"   子步骤（{len(task.steps)}）：{' / '.join(task.steps)}")
        lines.extend(detail)

    if len(tasks) > max_show:
        lines.append(f"…（其余 {len(tasks) - max_show} 项省略）")

    warnings = [(i, w) for i, t in enumerate(tasks, start=1) for w in t.warnings]
    if warnings:
        lines.append("")
        lines.append("⚠️ 需要留意")
        for index, warning in warnings[:8]:
            lines.append(f"- 第 {index} 项：{warning}")
        if len(warnings) > 8:
            lines.append(f"- …另有 {len(warnings) - 8} 条提示")

    lines.append("")
    lines.append(f"回复「确认」即可导入；回复「取消」放弃这次清单。")
    lines.append(f"（plan_id: {plan_id}）")
    return "\n".join(lines)


def render_import_result(
    list_name: str,
    created: list[tuple[TaskDraft, str | None]],
    skipped: list[TaskDraft],
    failed: list[tuple[TaskDraft, str]],
    *,
    tz_name: str,
) -> str:
    """生成导入完成后的回执。"""
    today = local_today(tz_name)
    if created:
        lines = [f"✅ 已写入 Microsoft To Do · 列表「{list_name}」（成功 {len(created)} 项）", ""]
        for index, (task, _task_id) in enumerate(created, start=1):
            due = f" —— {format_due(task.due_date, task.due_time, today)}" if task.due_date else ""
            lines.append(f"{index}. {task.title}{due}")
            if task.steps:
                lines.append(f"   子步骤 {len(task.steps)} 条")
    else:
        lines = [f"ℹ️ 没有新增待办 · 列表「{list_name}」"]

    if skipped:
        lines.append("")
        lines.append(
            f"⏭ 跳过 {len(skipped)} 项（之前已导入过）："
            + "、".join(t.title for t in skipped[:5])
            + ("…" if len(skipped) > 5 else "")
        )
    if failed:
        lines.append("")
        lines.append(f"❌ 失败 {len(failed)} 项：")
        for task, reason in failed[:5]:
            lines.append(f"- {task.title}：{reason}")
    if not created and not skipped and not failed:
        lines.append("（没有需要写入的条目）")
    return "\n".join(lines)


def plan_expired(created_at: float, *, now: float, ttl: int = PENDING_TTL) -> bool:
    return (now - float(created_at or 0)) > ttl


__all__ = [
    "PENDING_TTL",
    "apply_seen",
    "dedupe_batch",
    "format_due",
    "get_tz",
    "local_now",
    "local_today",
    "normalize_tasks",
    "parse_due",
    "plan_expired",
    "render_import_result",
    "render_preview",
    "resolve_date",
    "resolve_time",
    "weekday_cn",
]
