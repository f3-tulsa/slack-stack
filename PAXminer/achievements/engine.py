"""Data-driven achievement rule evaluation."""

from __future__ import annotations

from datetime import date

import pandas as pd

from achievements.attendance import filter_activity, period_key


def evaluate_rule(
    nation_df: pd.DataFrame,
    rule: dict,
    *,
    schema: str,
    pax_filter: set[str] | None = None,
) -> pd.DataFrame:
    """Return rows of newly qualifying awards: pax_id, achievement_id, date_awarded, period_bucket."""
    df = nation_df[nation_df["region"] == schema].copy()
    if pax_filter is not None:
        df = df[df["user_id"].isin(pax_filter)]
    if df.empty:
        return pd.DataFrame(columns=["pax_id", "achievement_id", "date_awarded", "period_bucket"])

    df = filter_activity(df, rule.get("activity", "beatdown"))
    metric = rule.get("metric", "posts")
    period = rule.get("period", "year")
    threshold = int(rule.get("threshold", 1))
    achievement_id = int(rule["id"])

    if metric == "qs":
        df = df[df["q_flag"] == 1]
    elif metric in ("posts", "distinct_aos", "posts_at_single_ao"):
        pass
    else:
        return pd.DataFrame(columns=["pax_id", "achievement_id", "date_awarded", "period_bucket"])

    df["period_bucket"] = period_key(df["date"], period)

    if metric == "distinct_aos":
        grouped = (
            df.groupby(["period_bucket", "email", "user_id", "region"], as_index=False)
            .agg(ao_count=("ao_id", "nunique"), date_awarded=("date", "max"))
            .rename(columns={"user_id": "pax_id"})
        )
        grouped = grouped[grouped["ao_count"] >= threshold]
    elif metric == "posts_at_single_ao":
        grouped = (
            df.groupby(["period_bucket", "email", "user_id", "region", "ao_id"], as_index=False)
            .agg(post_count=("ao_id", "count"), date_awarded=("date", "max"))
            .rename(columns={"user_id": "pax_id"})
        )
        grouped = grouped[grouped["post_count"] >= threshold]
        grouped = grouped.groupby(["period_bucket", "email", "pax_id", "region"], as_index=False).agg(
            date_awarded=("date_awarded", "max")
        )
    else:
        grouped = (
            df.groupby(["period_bucket", "email", "user_id", "region"], as_index=False)
            .agg(cnt=("ao_id", "count"), date_awarded=("date", "max"))
            .rename(columns={"user_id": "pax_id"})
        )
        grouped = grouped[grouped["cnt"] >= threshold]

    grouped["achievement_id"] = achievement_id
    grouped["date_awarded"] = grouped["date_awarded"].dt.date
    return grouped[["pax_id", "achievement_id", "date_awarded", "period_bucket"]]


def period_bucket_for_date(d: date, period: str) -> int:
    ts = pd.Timestamp(d)
    if period == "week":
        return int(ts.isocalendar().week)
    if period == "month":
        return ts.month
    return ts.year


def awarded_period_bucket(date_awarded, period: str) -> int:
    if isinstance(date_awarded, str):
        date_awarded = pd.to_datetime(date_awarded).date()
    elif hasattr(date_awarded, "date") and callable(date_awarded.date):
        date_awarded = date_awarded.date()
    return period_bucket_for_date(date_awarded, period)
