"""Year-qualified period keys and spoken labels."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd


def period_key_for_date(d: date, period: str) -> str:
    ts = pd.Timestamp(d)
    if period == "week":
        iso = ts.isocalendar()
        return f"{int(iso.year)}-W{int(iso.week):02d}"
    if period == "month":
        return f"{ts.year:04d}-{ts.month:02d}"
    return f"{ts.year:04d}"


def period_bounds(d: date, period: str) -> tuple[date, date]:
    ts = pd.Timestamp(d)
    if period == "week":
        iso = ts.isocalendar()
        start = date.fromisocalendar(int(iso.year), int(iso.week), 1)
        end = date.fromisocalendar(int(iso.year), int(iso.week), 7)
        return start, end
    if period == "month":
        start = date(ts.year, ts.month, 1)
        end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
        return start, end
    return date(ts.year, 1, 1), date(ts.year, 12, 31)


def period_key_series(series: pd.Series, period: str) -> pd.Series:
    if period == "week":
        iso = series.dt.isocalendar()
        return iso.year.astype(int).astype(str) + "-W" + iso.week.astype(int).astype(str).str.zfill(2)
    if period == "month":
        return series.dt.year.astype(int).astype(str).str.zfill(4) + "-" + series.dt.month.astype(int).astype(str).str.zfill(2)
    return series.dt.year.astype(int).astype(str)


def spoken_period(period_start, period_end, period: str) -> str:
    start = _as_date(period_start)
    if start is None:
        return str(period or "")
    if period == "week":
        return f"week of {format_date_label(start)}"
    if period == "month":
        return start.strftime("%B %Y")
    return str(start.year)


def backblast_archive_url(ao_id: str | None, timestamp: str | None) -> str | None:
    if not ao_id or not timestamp:
        return None
    ts = str(timestamp).replace(".", "")
    if not ts.isdigit():
        return None
    return f"https://slack.com/archives/{ao_id}/p{ts}"


def format_date_label(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except Exception:
        return None
