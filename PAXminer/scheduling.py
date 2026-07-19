"""Pure schedule evaluation helpers (no DB / pandas / matplotlib).

Used by ScheduleFunction and unit-tested in the light CI env.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Chicago"
TICK_MINUTES = 15

REPORT_TYPES = (
    "pax_charts",
    "q_charts",
    "region_leaderboard",
    "ao_leaderboard",
    "achievement_leaderboard",
    "kotter",
    "custom_report",
)

DESTINATION_TYPES = (
    "all_ao_channels",
    "specific_channels",
    "dm_all_pax",
    "dm_specific_pax",
)

FREQUENCY_TYPES = ("daily", "weekly", "monthly", "custom")
MONTH_DAY_MODES = ("first", "last", "specific")
TIME_WINDOW_TYPES = ("relative_days", "last_month", "ytd", "custom")
REPORT_KINDS = ("chart", "table")
ALLOWED_SOURCES = ("bd_attendance", "beatdowns", "attendance_view")

# Valid destination types per report_type (UI constraint).
VALID_DESTINATIONS: dict[str, tuple[str, ...]] = {
    "pax_charts": ("dm_all_pax", "dm_specific_pax"),
    "q_charts": ("all_ao_channels", "specific_channels"),
    "region_leaderboard": ("specific_channels", "all_ao_channels"),
    "ao_leaderboard": ("all_ao_channels", "specific_channels"),
    "achievement_leaderboard": ("specific_channels", "all_ao_channels"),
    "kotter": ("specific_channels",),
    "custom_report": DESTINATION_TYPES,
}

BUILTIN_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "code": "pax_charts",
        "name": "PAX charts (DM)",
        "report_type": "pax_charts",
        "is_builtin": 1,
        "time_window_type": "last_month",
    },
    {
        "code": "q_charts",
        "name": "Q charts",
        "report_type": "q_charts",
        "is_builtin": 1,
        "time_window_type": "last_month",
    },
    {
        "code": "region_leaderboard",
        "name": "Region leaderboard",
        "report_type": "region_leaderboard",
        "is_builtin": 1,
        "time_window_type": "last_month",
    },
    {
        "code": "ao_leaderboard",
        "name": "AO leaderboard",
        "report_type": "ao_leaderboard",
        "is_builtin": 1,
        "time_window_type": "last_month",
    },
    {
        "code": "achievement_leaderboard",
        "name": "Achievement leaderboard",
        "report_type": "achievement_leaderboard",
        "is_builtin": 1,
        "time_window_type": "ytd",
    },
    {
        "code": "kotter",
        "name": "Kotter report",
        "report_type": "kotter",
        "is_builtin": 1,
        "time_window_type": None,
    },
)

# Legacy send_* flag -> builtin definition code + preferred destination channel column.
LEGACY_FLAG_MAP: tuple[dict[str, Any], ...] = (
    {
        "code": "pax_charts",
        "flag": "send_pax_charts",
        "channel_col": "firstf_channel",
        "destination_type": "dm_all_pax",
    },
    {
        "code": "q_charts",
        "flag": "send_q_charts",
        "channel_col": "firstf_channel",
        "destination_type": "all_ao_channels",
    },
    {
        "code": "region_leaderboard",
        "flag": "send_region_leaderboard",
        "channel_col": "firstf_channel",
        "destination_type": "specific_channels",
    },
    {
        "code": "ao_leaderboard",
        "flag": "send_ao_leaderboard",
        "channel_col": "firstf_channel",
        "destination_type": "all_ao_channels",
    },
    {
        "code": "achievement_leaderboard",
        "flag": "send_achievement_leaderboard",
        "channel_col": "achievement_channel",
        "destination_type": "specific_channels",
    },
    {
        "code": "kotter",
        "flag": "send_aoq_reports",
        "channel_col": "kotter_channel",
        "destination_type": "specific_channels",
    },
)


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return ZoneInfo for name; fall back to DEFAULT_TIMEZONE on unknown."""
    tz_name = (name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def region_local_now(timezone_name: str | None, *, utc_now: datetime | None = None) -> datetime:
    """Current datetime in the region's timezone (aware)."""
    utc = utc_now or datetime.now(tz=ZoneInfo("UTC"))
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=ZoneInfo("UTC"))
    return utc.astimezone(resolve_timezone(timezone_name))


def parse_time_of_day(value: Any) -> time:
    """Parse TIME / 'HH:MM' / 'HH:MM:SS' / datetime.time into time."""
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    if value is None:
        return time(7, 0)
    s = str(value).strip()
    if not s:
        return time(7, 0)
    parts = s.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(parts[2]) if len(parts) > 2 else 0
    return time(hour, minute, second)


def snap_time_to_tick(t: time, tick_minutes: int = TICK_MINUTES) -> time:
    """Snap minutes down to the nearest tick boundary."""
    minute = (t.minute // tick_minutes) * tick_minutes
    return time(t.hour, minute, 0)


def time_of_day_options(tick_minutes: int = TICK_MINUTES) -> list[dict[str, Any]]:
    """96 Slack static_select options for 15-minute steps (under 100-option cap)."""
    options: list[dict[str, Any]] = []
    for hour in range(24):
        for minute in range(0, 60, tick_minutes):
            value = f"{hour:02d}:{minute:02d}"
            display_h = hour % 12 or 12
            ampm = "AM" if hour < 12 else "PM"
            label = f"{display_h}:{minute:02d} {ampm}"
            options.append(
                {
                    "text": {"type": "plain_text", "text": label},
                    "value": value,
                }
            )
    return options


def _last_day_of_month(d: date) -> int:
    return monthrange(d.year, d.month)[1]


def is_due_today(schedule: dict[str, Any], local_date: date) -> bool:
    """True when the schedule's calendar rule matches local_date."""
    freq = (schedule.get("frequency_type") or "monthly").strip()
    if freq == "daily":
        return True
    if freq == "weekly":
        # Python: Monday=0 … Sunday=6. Stored the same way.
        dow = schedule.get("day_of_week")
        if dow is None:
            return False
        return int(dow) == local_date.weekday()
    if freq == "monthly":
        mode = (schedule.get("month_day_mode") or "first").strip()
        if mode == "first":
            return local_date.day == 1
        if mode == "last":
            return local_date.day == _last_day_of_month(local_date)
        # specific
        target = schedule.get("day_of_month")
        if target is None:
            return False
        target = int(target)
        last = _last_day_of_month(local_date)
        # Clamp: day 31 in February fires on last day.
        effective = min(target, last)
        return local_date.day == effective
    if freq == "custom":
        spec = schedule.get("custom_spec") or {}
        if isinstance(spec, str):
            import json

            try:
                spec = json.loads(spec)
            except json.JSONDecodeError:
                return False
        interval = int(spec.get("interval_days") or 0)
        if interval < 1:
            return False
        last_run = schedule.get("last_run_on")
        if last_run is None:
            return True
        if isinstance(last_run, str):
            last_run = date.fromisoformat(last_run[:10])
        elif isinstance(last_run, datetime):
            last_run = last_run.date()
        return (local_date - last_run).days >= interval
    return False


def already_ran_successfully(schedule: dict[str, Any], local_date: date) -> bool:
    """Skip when last_run_on is today and status is success.

    ``running``, ``skipped``, ``error``, or empty status do not count — a crashed
    Run Now must not block the scheduled tick for the rest of the day.
    """
    last_run = schedule.get("last_run_on")
    if last_run is None:
        return False
    if isinstance(last_run, str):
        last_run = date.fromisoformat(last_run[:10])
    elif isinstance(last_run, datetime):
        last_run = last_run.date()
    if last_run != local_date:
        return False
    status = (schedule.get("last_run_status") or "").strip().lower()
    return status == "success"


def is_due_now(
    schedule: dict[str, Any],
    *,
    timezone_name: str | None,
    utc_now: datetime | None = None,
) -> bool:
    """Due today + region-local now >= time_of_day + not already run successfully today."""
    local = region_local_now(timezone_name, utc_now=utc_now)
    local_date = local.date()
    if already_ran_successfully(schedule, local_date):
        return False
    if not is_due_today(schedule, local_date):
        return False
    tod = parse_time_of_day(schedule.get("time_of_day"))
    return local.time().replace(tzinfo=None) >= tod


def resolve_time_window(
    definition: dict[str, Any],
    *,
    timezone_name: str | None = None,
    utc_now: datetime | None = None,
) -> tuple[date, date]:
    """Return (start_inclusive, end_inclusive) for a report definition's window."""
    local = region_local_now(timezone_name, utc_now=utc_now)
    today = local.date()
    wtype = (definition.get("time_window_type") or "last_month").strip()
    if wtype == "relative_days":
        days = int(definition.get("window_days") or 30)
        return today - timedelta(days=max(days, 1) - 1), today
    if wtype == "ytd":
        return date(today.year, 1, 1), today
    if wtype == "custom":
        start = definition.get("window_start")
        end = definition.get("window_end")
        if isinstance(start, str):
            start = date.fromisoformat(start[:10])
        if isinstance(end, str):
            end = date.fromisoformat(end[:10])
        if start is None or end is None:
            return today - timedelta(days=29), today
        return start, end
    # last_month (default) — calendar prior month
    first_this = date(today.year, today.month, 1)
    last_prev = first_this - timedelta(days=1)
    first_prev = date(last_prev.year, last_prev.month, 1)
    return first_prev, last_prev


def destination_valid_for_report(report_type: str, destination_type: str) -> bool:
    allowed = VALID_DESTINATIONS.get(report_type, DESTINATION_TYPES)
    return destination_type in allowed


def format_schedule_summary(schedule: dict[str, Any], definition: dict[str, Any] | None = None) -> str:
    """Short human-readable line for the schedule list modal."""
    name = (definition or {}).get("name") or schedule.get("name") or f"#{schedule.get('id')}"
    dest = schedule.get("destination_type") or "?"
    freq = schedule.get("frequency_type") or "?"
    tod = parse_time_of_day(schedule.get("time_of_day"))
    enabled = "on" if schedule.get("enabled") else "off"
    line = f"*{name}* — {dest} / {freq} @ {tod.strftime('%H:%M')} ({enabled})"
    status = (schedule.get("last_run_status") or "").strip()
    last_on = schedule.get("last_run_on")
    if status or last_on:
        if isinstance(last_on, datetime):
            last_s = last_on.date().isoformat()
        elif isinstance(last_on, date):
            last_s = last_on.isoformat()
        elif last_on:
            last_s = str(last_on)[:10]
        else:
            last_s = "?"
        line += f" — last run: {status or '?'} ({last_s})"
    return line
