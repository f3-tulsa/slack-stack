"""Data-driven achievement rule evaluation."""

from __future__ import annotations

from datetime import date

import pandas as pd

from achievements.attendance import filter_activity, period_key
from achievements.period import period_bounds, period_key_for_date

EMPTY_COLS = [
    "pax_id",
    "achievement_id",
    "date_awarded",
    "period_bucket",
    "period_key",
    "period_start",
    "period_end",
    "qualifying_count",
    "ao_id",
    "timestamp",
    "version_id",
]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=EMPTY_COLS)


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    try:
        return pd.Timestamp(value).date()
    except Exception:
        return None


def period_in_effective_range(period_start, period_end, rule: dict) -> bool:
    start = _as_date(period_start)
    end = _as_date(period_end)
    if start is None or end is None:
        return True
    effective_from = _as_date(rule.get("effective_from"))
    effective_to = _as_date(rule.get("effective_to"))
    if effective_from and end < effective_from:
        return False
    if effective_to and start > effective_to:
        return False
    return True


def evaluate_rule(
    nation_df: pd.DataFrame,
    rule: dict,
    *,
    schema: str,
    pax_filter: set[str] | None = None,
) -> pd.DataFrame:
    """Return qualifying awards with year-qualified period_key and threshold-crossing date."""
    df = nation_df[nation_df["region"] == schema].copy()
    if pax_filter is not None:
        df = df[df["user_id"].isin(pax_filter)]
    if df.empty:
        return _empty()

    df = filter_activity(df, rule.get("activity", "beatdown"))
    metric = rule.get("metric", "posts")
    period = rule.get("period", "year")
    threshold = int(rule.get("threshold", 1))
    achievement_id = int(rule.get("id") or rule.get("achievement_id"))
    version_id = rule.get("version_id")

    if metric == "qs":
        df = df[df["q_flag"] == 1].copy()
    elif metric in ("posts", "distinct_aos", "posts_at_single_ao"):
        pass
    else:
        return _empty()

    if df.empty:
        return _empty()

    if "timestamp" not in df.columns:
        df["timestamp"] = None
    df["period_bucket"] = period_key(df["date"], period)
    df = df.sort_values(
        ["user_id", "period_bucket", "date", "timestamp"],
        kind="mergesort",
    )

    if metric == "distinct_aos":
        first_ao = df.drop_duplicates(["user_id", "period_bucket", "ao_id"], keep="first")
        first_ao = first_ao.copy()
        first_ao["rn"] = first_ao.groupby(["user_id", "period_bucket"]).cumcount() + 1
        counts = first_ao.groupby(["user_id", "period_bucket"], as_index=False).agg(
            qualifying_count=("ao_id", "nunique")
        )
        crossing = first_ao[first_ao["rn"] == threshold]
    elif metric == "posts_at_single_ao":
        by_ao = df.groupby(["user_id", "period_bucket", "ao_id"], as_index=False).agg(
            ao_count=("ao_id", "count")
        )
        top = by_ao.sort_values("ao_count", ascending=False).drop_duplicates(
            ["user_id", "period_bucket"], keep="first"
        )
        top = top.rename(columns={"ao_id": "top_ao_id", "ao_count": "qualifying_count"})
        at_top = df.merge(
            top[["user_id", "period_bucket", "top_ao_id", "qualifying_count"]],
            on=["user_id", "period_bucket"],
            how="inner",
        )
        at_top = at_top[at_top["ao_id"] == at_top["top_ao_id"]].copy()
        at_top["rn"] = at_top.groupby(["user_id", "period_bucket"]).cumcount() + 1
        counts = top[["user_id", "period_bucket", "qualifying_count"]]
        crossing = at_top[at_top["rn"] == threshold]
    else:
        df = df.copy()
        df["rn"] = df.groupby(["user_id", "period_bucket"]).cumcount() + 1
        counts = df.groupby(["user_id", "period_bucket"], as_index=False).agg(
            qualifying_count=("ao_id", "count")
        )
        crossing = df[df["rn"] == threshold]

    if crossing.empty:
        return _empty()

    # posts_at_single_ao already carries qualifying_count on `crossing`; a second
    # merge would suffix it to qualifying_count_x/_y (pandas 3) and KeyError.
    count_cols = counts[["user_id", "period_bucket", "qualifying_count"]].drop_duplicates()
    grouped = crossing.drop(columns=["qualifying_count"], errors="ignore").merge(
        count_cols,
        on=["user_id", "period_bucket"],
        how="left",
    )
    grouped = grouped[grouped["qualifying_count"] >= threshold]
    if grouped.empty:
        return _empty()

    grouped = grouped.rename(columns={"user_id": "pax_id"})
    grouped["achievement_id"] = achievement_id
    grouped["version_id"] = version_id
    grouped["date_awarded"] = pd.to_datetime(grouped["date"]).dt.date
    bounds = grouped["date_awarded"].map(lambda d: period_bounds(d, period))
    grouped["period_start"] = bounds.map(lambda b: b[0])
    grouped["period_end"] = bounds.map(lambda b: b[1])
    grouped["period_key"] = grouped["period_bucket"]
    in_range = grouped.apply(
        lambda r: period_in_effective_range(r["period_start"], r["period_end"], rule),
        axis=1,
    )
    grouped = grouped[in_range]
    if grouped.empty:
        return _empty()

    cols = [c for c in EMPTY_COLS if c in grouped.columns]
    return grouped[cols].reset_index(drop=True)


def period_bucket_for_date(d: date, period: str) -> str:
    return period_key_for_date(d, period)


def awarded_period_bucket(date_awarded, period: str) -> str:
    if isinstance(date_awarded, str):
        date_awarded = pd.to_datetime(date_awarded).date()
    elif hasattr(date_awarded, "date") and callable(date_awarded.date):
        date_awarded = date_awarded.date()
    return period_bucket_for_date(date_awarded, period)
