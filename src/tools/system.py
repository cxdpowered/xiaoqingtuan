"""系统工具：当前时间等轻量运行时信息查询。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from src.memory.search import query_memories
from src.tools.registry import ToolContext, context_tool

DEFAULT_TIMEZONE = "Asia/Shanghai"

_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_TIMEZONE_MEMORY_KEYWORDS = ("时区", "timezone", "time zone", "tz")
_IANA_RE = re.compile(r"\b[A-Za-z]+/[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)?\b")
_OFFSET_RE = re.compile(r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", re.IGNORECASE)
_CHINA_OFFSET_RE = re.compile(r"(东|西)\s*(\d{1,2})\s*区")

_ALIASES = {
    "北京时间": DEFAULT_TIMEZONE,
    "北京时区": DEFAULT_TIMEZONE,
    "中国时间": DEFAULT_TIMEZONE,
    "中国标准时间": DEFAULT_TIMEZONE,
    "上海时间": DEFAULT_TIMEZONE,
    "东八区": DEFAULT_TIMEZONE,
    "utc+8": DEFAULT_TIMEZONE,
    "utc+08": DEFAULT_TIMEZONE,
    "utc+08:00": DEFAULT_TIMEZONE,
    "gmt+8": DEFAULT_TIMEZONE,
    "gmt+08": DEFAULT_TIMEZONE,
    "gmt+08:00": DEFAULT_TIMEZONE,
    "美东时间": "America/New_York",
    "美国东部时间": "America/New_York",
    "纽约时间": "America/New_York",
    "美西时间": "America/Los_Angeles",
    "美国太平洋时间": "America/Los_Angeles",
    "洛杉矶时间": "America/Los_Angeles",
    "伦敦时间": "Europe/London",
    "英国时间": "Europe/London",
    "东京时间": "Asia/Tokyo",
    "日本时间": "Asia/Tokyo",
}


class CurrentTimeArgs(BaseModel):
    timezone: Optional[str] = Field(
        default=None,
        description=(
            "可选时区，如 Asia/Shanghai、America/New_York、UTC+8。"
            "不传则优先使用用户长期记忆中的时区，否则默认北京时间。"
        ),
    )


@dataclass(frozen=True)
class TimezoneChoice:
    name: str
    tz: tzinfo
    source: str
    memory_path: Optional[str] = None
    memory_value: Optional[str] = None


def _zoneinfo(name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _fixed_offset(sign: str, hours: int, minutes: int = 0) -> Optional[timezone]:
    if hours > 23 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    if sign == "-":
        delta = -delta
    total_minutes = int(delta.total_seconds() // 60)
    label = f"UTC{sign}{hours:02d}:{minutes:02d}"
    if total_minutes == 0:
        label = "UTC"
    return timezone(delta, label)


def _parse_timezone(raw: str, *, source: str) -> Optional[TimezoneChoice]:
    text = (raw or "").strip()
    if not text:
        return None

    folded = text.casefold()
    for alias, name in _ALIASES.items():
        if alias.casefold() in folded:
            tz = _zoneinfo(name)
            if tz:
                return TimezoneChoice(name=name, tz=tz, source=source)

    for match in _IANA_RE.finditer(text):
        name = match.group(0)
        tz = _zoneinfo(name)
        if tz:
            return TimezoneChoice(name=name, tz=tz, source=source)

    offset_match = _OFFSET_RE.search(text)
    if offset_match:
        sign, hour_text, minute_text = offset_match.groups()
        hours = int(hour_text)
        minutes = int(minute_text or "0")
        tz = _fixed_offset(sign, hours, minutes)
        if tz:
            return TimezoneChoice(name=tz.tzname(None) or text, tz=tz, source=source)

    china_offset_match = _CHINA_OFFSET_RE.search(text)
    if china_offset_match:
        direction, hour_text = china_offset_match.groups()
        sign = "+" if direction == "东" else "-"
        tz = _fixed_offset(sign, int(hour_text))
        if tz:
            return TimezoneChoice(name=tz.tzname(None) or text, tz=tz, source=source)

    tz = _zoneinfo(text)
    if tz:
        return TimezoneChoice(name=text, tz=tz, source=source)
    return None


def _timezone_from_memory(ctx: ToolContext) -> Optional[TimezoneChoice]:
    if not ctx.person_id:
        return None
    memories = query_memories(db=ctx.db, person_id=ctx.person_id, status="active", limit=100)
    for memory in memories:
        subject = str(memory.get("subject") or "")
        predicate = str(memory.get("predicate") or "")
        value = str(memory.get("value") or "")
        blob = f"{subject} {predicate} {value}"
        if not any(keyword in blob.casefold() for keyword in _TIMEZONE_MEMORY_KEYWORDS):
            continue
        choice = _parse_timezone(blob, source="memory")
        if choice:
            return TimezoneChoice(
                name=choice.name,
                tz=choice.tz,
                source="memory",
                memory_path=memory.get("path"),
                memory_value=value,
            )
    return None


def _resolve_timezone(raw_timezone: Optional[str], ctx: ToolContext) -> TimezoneChoice:
    if raw_timezone:
        choice = _parse_timezone(raw_timezone, source="argument")
        if not choice:
            raise ValueError(f"无法识别时区：{raw_timezone}")
        return choice

    memory_choice = _timezone_from_memory(ctx)
    if memory_choice:
        return memory_choice

    default_tz = _zoneinfo(DEFAULT_TIMEZONE)
    if default_tz:
        return TimezoneChoice(name=DEFAULT_TIMEZONE, tz=default_tz, source="default")
    return TimezoneChoice(
        name="UTC+08:00",
        tz=timezone(timedelta(hours=8), "UTC+08:00"),
        source="default",
    )


def _format_offset(dt: datetime) -> str:
    raw = dt.strftime("%z")
    if not raw:
        return ""
    return f"{raw[:3]}:{raw[3:]}"


async def current_time(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    choice = _resolve_timezone(args.get("timezone"), ctx)
    now = datetime.now(choice.tz)
    return {
        "datetime": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M:%S"),
        "weekday": _WEEKDAYS_ZH[now.weekday()],
        "timezone": choice.name,
        "utc_offset": _format_offset(now),
        "unix_timestamp": int(now.timestamp()),
        "timezone_source": choice.source,
        "memory_path": choice.memory_path,
        "memory_value": choice.memory_value,
    }


TOOLS = [
    context_tool(
        name="current_time",
        description="获取当前日期和时间；默认北京时间，若用户长期记忆中记录了时区则优先使用该时区。",
        args_schema=CurrentTimeArgs,
        func=current_time,
        high_risk=False,
    )
]
