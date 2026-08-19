"""Pure schedule evaluation helpers (no DB / pandas / matplotlib).

Used by ScheduleFunction and unit-tested in the light CI env.
"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from pathlib import Path
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
    "achievement_almost_there",
    "award_achievements",
    "kotter",
    "custom_report",
)

DESTINATION_TYPES = (
    "all_ao_channels",
    "specific_channels",
    "dm_all_pax",
    "dm_specific_pax",
)

FREQUENCY_TYPES = ("hourly", "daily", "weekly", "monthly", "custom")
MONTH_DAY_MODES = ("first", "last", "specific")
TIME_WINDOW_TYPES = ("relative_days", "last_month", "this_month", "ytd", "custom")
REPORT_KINDS = ("chart", "table")
ALLOWED_SOURCES = ("bd_attendance", "beatdowns", "attendance_view")

# Valid destination types per report_type (UI constraint).
VALID_DESTINATIONS: dict[str, tuple[str, ...]] = {
    "pax_charts": ("dm_all_pax", "dm_specific_pax"),
    "q_charts": ("all_ao_channels", "specific_channels"),
    "region_leaderboard": ("specific_channels", "all_ao_channels"),
    "ao_leaderboard": ("all_ao_channels", "specific_channels"),
    "achievement_leaderboard": ("specific_channels", "all_ao_channels"),
    "achievement_almost_there": ("specific_channels", "all_ao_channels"),
    "award_achievements": ("specific_channels",),
    "kotter": ("specific_channels",),
    "custom_report": DESTINATION_TYPES,
}

# Template id == report_type (existing rows keep working). Declares output and
# which definition columns the Add/Edit form and dispatcher should honor.
REPORT_TEMPLATES: dict[str, dict[str, Any]] = {
    "achievement_leaderboard": {
        "label": "Achievement leaderboard",
        "output": "blocks",
        "fields": ("name", "window", "top_n"),
        "default_top_n": 10,
    },
    "achievement_almost_there": {
        "label": "Almost there",
        "output": "blocks",
        "fields": ("name", "top_n"),
        "default_top_n": 10,
    },
    "custom_report": {
        "label": "Custom report",
        "output": "custom",
        "fields": ("name", "code", "kind", "source", "fields", "metric", "group_by", "window", "top_n"),
        "default_top_n": 20,
    },
    "pax_charts": {
        "label": "PAX charts",
        "output": "png",
        "fields": ("name", "window"),
    },
    "q_charts": {
        "label": "Q charts",
        "output": "png",
        "fields": ("name", "window"),
    },
    "region_leaderboard": {
        "label": "Region leaderboard",
        "output": "png",
        "fields": ("name", "window", "top_n"),
        "default_top_n": 20,
    },
    "ao_leaderboard": {
        "label": "AO leaderboard",
        "output": "png",
        "fields": ("name", "window", "top_n"),
        "default_top_n": 20,
    },
    "kotter": {
        "label": "Kotter",
        "output": "blocks",
        "fields": ("name",),
    },
    "award_achievements": {
        "label": "Award achievements",
        "output": "blocks",
        "fields": ("name",),
    },
}

# Human labels + expansion for logs, the Schedule picker, and list summary.
# Unknown future types default to computed so the logger never dumps a roster.
DESTINATION_META: dict[str, dict[str, str]] = {
    "all_ao_channels": {
        "label": "All AO channels",
        "expansion": "computed",
        "kind": "channel",
    },
    "specific_channels": {
        "label": "Specific channels",
        "expansion": "specific",
        "kind": "channel",
    },
    "dm_all_pax": {
        "label": "DM to all PAX",
        "expansion": "computed",
        "kind": "dm",
    },
    "dm_specific_pax": {
        "label": "DM to specific PAX",
        "expansion": "specific",
        "kind": "dm",
    },
}


def destination_descriptor(destination_type: str | None) -> dict[str, str]:
    """Label / expansion / kind for a destination_type. Never switches on report_type."""
    code = (destination_type or "").strip()
    meta = DESTINATION_META.get(code)
    if meta:
        return {"destination_type": code, **meta}
    if not code:
        return {
            "destination_type": "",
            "label": "unknown",
            "expansion": "specific",
            "kind": "channel",
        }
    kind = "dm" if "dm" in code else "channel"
    return {
        "destination_type": code,
        "label": code.replace("_", " "),
        "expansion": "computed",
        "kind": kind,
    }


def destination_label(destination_type: str | None) -> str:
    return destination_descriptor(destination_type)["label"]


def template_for(report_type: str | None) -> dict[str, Any]:
    return REPORT_TEMPLATES.get(report_type or "") or REPORT_TEMPLATES["custom_report"]


def template_has(report_type: str | None, field: str) -> bool:
    return field in template_for(report_type).get("fields", ())


def format_report_title(name: str | None, window: tuple[date, date] | None = None) -> str:
    """Slack header / PNG title: definition name plus window label. Never a literal YTD."""
    base = (name or "").strip() or "Report"
    if window:
        return f"{base} ({format_window_label(*window)})"
    return base


def caption_with_window(title: str | None, label: str | None, fallback: str) -> str:
    """Slack caption noun: ``{title} for {label}`` without repeating the window.

    Schedule jobs pass ``format_report_title`` (already ``Name (July 2026)``). Charters
    also know the window label, so naive ``title for label`` becomes
    ``PAX Charts (July 2026) for July 2026``.
    """
    heading = (title or "").strip() or fallback
    period = (label or "").strip()
    if not period or period in heading:
        return heading
    return f"{heading} for {period}"


def destination_expansion(destination_type: str | None) -> str:
    return destination_descriptor(destination_type)["expansion"]


def format_iso_range(start: Any, end: Any) -> str | None:
    """Slack-facing inclusive range: ``YYYY-MM-DD to YYYY-MM-DD``. None if either bound is missing."""

    def _iso(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        if not text:
            return None
        return text[:10]

    lo, hi = _iso(start), _iso(end)
    if not lo or not hi:
        return None
    return f"{lo} to {hi}"


def _load_report_defaults() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "report_defaults.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


_REPORT_DEFAULTS = _load_report_defaults()
BUILTIN_DEFINITIONS: tuple[dict[str, Any], ...] = tuple(_REPORT_DEFAULTS["definitions"])
DEFAULT_SCHEDULES: tuple[dict[str, Any], ...] = tuple(_REPORT_DEFAULTS["default_schedules"])


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
    """Parse TIME / 'HH:MM' / 'HH:MM:SS' / datetime.time / timedelta into time.

    Never raises — falls back to 07:00 on unparseable values (PyMySQL TIME may
    arrive as timedelta, bytes, or fractional seconds).
    """
    try:
        if isinstance(value, time):
            return value.replace(tzinfo=None)
        if isinstance(value, datetime):
            return value.time().replace(tzinfo=None)
        if isinstance(value, timedelta):
            total = int(value.total_seconds()) % (24 * 3600)
            hour, rem = divmod(total, 3600)
            minute, second = divmod(rem, 60)
            return time(hour, minute, second)
        if value is None:
            return time(7, 0)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        s = str(value).strip()
        if not s:
            return time(7, 0)
        # Strip fractional seconds: "7:00:00.500000" → "7:00:00"
        if "." in s:
            s = s.split(".", 1)[0]
        parts = s.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = int(float(parts[2])) if len(parts) > 2 else 0
        return time(hour % 24, minute % 60, second % 60)
    except Exception:
        return time(7, 0)


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
    if freq == "hourly":
        return True
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


def already_ran_this_hour(schedule: dict[str, Any], local_dt: datetime) -> bool:
    """Skip when last_run_at falls in the same local hour and status is terminal.

    ``success`` and ``skipped`` are both terminal for the hour so a no-op run
    (no rules / no attendance / no destinations) does not re-fire every tick.
    ``last_run_at`` is region-local wall time, matching ``mark_schedule_status``.
    """
    last_at = schedule.get("last_run_at")
    if last_at is None:
        return False
    if isinstance(last_at, str):
        last_at = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
        if last_at.tzinfo is not None:
            last_at = last_at.replace(tzinfo=None)
    elif isinstance(last_at, datetime) and last_at.tzinfo is not None:
        last_at = last_at.replace(tzinfo=None)
    local_naive = local_dt.replace(tzinfo=None) if local_dt.tzinfo else local_dt
    if (last_at.year, last_at.month, last_at.day, last_at.hour) != (
        local_naive.year,
        local_naive.month,
        local_naive.day,
        local_naive.hour,
    ):
        return False
    status = (schedule.get("last_run_status") or "").strip().lower()
    return status in ("success", "skipped")


def is_due_now(
    schedule: dict[str, Any],
    *,
    timezone_name: str | None,
    utc_now: datetime | None = None,
) -> bool:
    """Due today + region-local now >= time_of_day + not already run successfully.

    Hourly schedules use ``last_run_at`` for intra-day idempotency and fire when
    the local minute is past the configured minute-of-hour from ``time_of_day``
    (snapped to the 15-minute tick so a stored ``:50`` still fires at ``:45``).
    """
    local = region_local_now(timezone_name, utc_now=utc_now)
    local_date = local.date()
    freq = (schedule.get("frequency_type") or "monthly").strip()
    tod = parse_time_of_day(schedule.get("time_of_day"))

    if freq == "hourly":
        if already_ran_this_hour(schedule, local):
            return False
        tod = snap_time_to_tick(tod)
        return local.minute >= tod.minute

    if already_ran_successfully(schedule, local_date):
        return False
    if not is_due_today(schedule, local_date):
        return False
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
    if wtype == "this_month":
        return date(today.year, today.month, 1), today
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


def is_calendar_month(start: date, end: date) -> bool:
    """True when [start, end] is exactly one calendar month."""
    if start.day != 1 or start.year != end.year or start.month != end.month:
        return False
    import calendar

    return end.day == calendar.monthrange(start.year, start.month)[1]


def format_window_label(start: date, end: date) -> str:
    """Human label for chart titles: 'July 2026' or 'Jun 01 to Jul 27, 2026'."""
    if is_calendar_month(start, end):
        return start.strftime("%B %Y")
    if start == end:
        return start.strftime("%b %d, %Y")
    if start.year == end.year:
        return f"{start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y')} to {end.strftime('%b %d, %Y')}"


def window_file_tag(start: date, end: date) -> str:
    """Short slug for chart filenames."""
    if is_calendar_month(start, end):
        return start.strftime("%b%Y")
    return f"{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"


def default_chart_window() -> tuple[date, date]:
    """Legacy CHART_PERIOD_OFFSET_DAYS → prior calendar month containing that day."""
    import os

    off = int(os.environ.get("CHART_PERIOD_OFFSET_DAYS", "7"))
    d = (datetime.now() - timedelta(days=off)).date()
    first = date(d.year, d.month, 1)
    import calendar

    last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return first, last


def destination_valid_for_report(report_type: str, destination_type: str) -> bool:
    allowed = VALID_DESTINATIONS.get(report_type, DESTINATION_TYPES)
    return destination_type in allowed


def format_schedule_summary(schedule: dict[str, Any], definition: dict[str, Any] | None = None) -> str:
    """Short human-readable line for the schedule list modal."""
    name = (definition or {}).get("name") or schedule.get("name") or f"#{schedule.get('id')}"
    dest_code = schedule.get("destination_type") or "?"
    dest = destination_label(dest_code) if dest_code in DESTINATION_META else dest_code
    freq = schedule.get("frequency_type") or "?"
    tod = parse_time_of_day(schedule.get("time_of_day"))
    enabled = "on" if schedule.get("enabled") else "off"
    if freq == "hourly":
        time_label = f":{tod.minute:02d}"
    else:
        time_label = tod.strftime("%H:%M")
    line = f"*{name}* — {dest} / {freq} @ {time_label} ({enabled})"
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
