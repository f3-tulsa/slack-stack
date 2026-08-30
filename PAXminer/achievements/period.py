"""Year-qualified period keys and spoken labels.

Stdlib-only at import time so the migration CLI can use these helpers without pandas.
"""

from __future__ import annotations

import math
from calendar import monthrange
from datetime import date, datetime


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def period_key_for_date(d: date, period: str) -> str:
    d = _to_date(d)
    if period == "week":
        iso = d.isocalendar()
        return f"{int(iso.year)}-W{int(iso.week):02d}"
    if period == "month":
        return f"{d.year:04d}-{d.month:02d}"
    return f"{d.year:04d}"


def period_bounds(d: date, period: str) -> tuple[date, date]:
    d = _to_date(d)
    if period == "week":
        iso = d.isocalendar()
        start = date.fromisocalendar(int(iso.year), int(iso.week), 1)
        end = date.fromisocalendar(int(iso.year), int(iso.week), 7)
        return start, end
    if period == "month":
        start = date(d.year, d.month, 1)
        end = date(d.year, d.month, monthrange(d.year, d.month)[1])
        return start, end
    return date(d.year, 1, 1), date(d.year, 12, 31)


def period_key_series(series, period: str):
    if period == "week":
        iso = series.dt.isocalendar()
        return (
            iso.year.astype(int).astype(str)
            + "-W"
            + iso.week.astype(int).astype(str).str.zfill(2)
        )
    if period == "month":
        return (
            series.dt.year.astype(int).astype(str).str.zfill(4)
            + "-"
            + series.dt.month.astype(int).astype(str).str.zfill(2)
        )
    return series.dt.year.astype(int).astype(str)


def spoken_period(period_start, period_end, period: str) -> str:
    start = _as_date(period_start)
    if start is None:
        return str(period or "")
    if period == "week":
        # ISO weeks are keyed by the year holding their Thursday, so 2026-W01
        # begins Dec 29, 2025. Naming only the Monday reads like a 2025 award;
        # spelling out both ends leaves no doubt which week is meant.
        end = _as_date(period_end)
        if end is None:
            return f"week of {format_date_label(start)}"
        return f"week of {format_date_label(start)} - {format_date_label(end)}"
    if period == "month":
        return start.strftime("%B %Y")
    return str(start.year)


def backblast_archive_url(
    ao_id: str | None,
    timestamp: str | None,
    *,
    archive_base: str | None = None,
) -> str | None:
    """Permalink to a Backblast message.

    ``archive_base`` must be the workspace host (``https://team.slack.com``) for
    the link to open in the Slack mobile app; the bare ``slack.com`` fallback
    only works in a browser.
    """
    if not ao_id or not timestamp:
        return None
    ts = str(timestamp).replace(".", "")
    if not ts.isdigit():
        return None
    base = (archive_base or "https://slack.com").rstrip("/")
    return f"{base}/archives/{ao_id}/p{ts}"


def format_date_label(d: date) -> str:
    """The one date format for anything an operator or PAX reads: August 21, 2026."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, float):
        try:
            if math.isnan(value):
                return None
        except (TypeError, ValueError):
            pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
