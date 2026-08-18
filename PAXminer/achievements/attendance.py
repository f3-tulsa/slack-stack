"""Load attendance data for achievements and Kotter (pandas + PyMySQL)."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from achievements.activity import activity_list_from_rule, filter_by_activity_types
from achievements.period import period_key_series
from slack_util import home_region_date_tiers

LOG = logging.getLogger(__name__)


def _nation_sql_for_schema(schema: str) -> str:
    return f"""
    SELECT
        u.email,
        u.user_name,
        u.user_id,
        a.ao_id,
        ao.ao,
        b.bd_date AS date,
        CASE WHEN (a.user_id = b.q_user_id OR a.user_id = b.coq_user_id) THEN 1 ELSE 0 END AS q_flag,
        b.activity_type,
        b.timestamp,
        b.json
    FROM `{schema}`.users u
    JOIN `{schema}`.bd_attendance a ON a.user_id = u.user_id
    JOIN `{schema}`.beatdowns b ON (
        (a.q_user_id = b.q_user_id OR a.q_user_id = b.coq_user_id)
        AND a.ao_id = b.ao_id AND a.date = b.bd_date
    )
    JOIN `{schema}`.aos ao ON b.ao_id = ao.channel_id
    WHERE b.bd_date >= %s
      AND b.bd_date <= %s
      AND u.email != 'none'
      AND u.user_name != 'PAXminer'
      AND b.q_user_id IS NOT NULL
    """


def _home_region_sql_for_schema(schema: str, tiers: tuple[int, int, int, int]) -> str:
    d1, d2, d3, d4 = tiers
    return f"""
    SELECT '{schema}' AS region, u.email, u.user_id,
           COALESCE(
             MAX(s1.attendance), MAX(s2.attendance), MAX(s3.attendance), MAX(s4.attendance),
             COUNT(a.user_id)
           ) AS attendance
    FROM `{schema}`.users u
    JOIN `{schema}`.bd_attendance a ON a.user_id = u.user_id
    JOIN `{schema}`.beatdowns b ON (
        a.q_user_id = b.q_user_id AND a.ao_id = b.ao_id AND a.date = b.bd_date
    )
    JOIN `{schema}`.aos ao ON b.ao_id = ao.channel_id
    LEFT JOIN (
        SELECT u2.email, COUNT(a2.user_id) AS attendance
        FROM `{schema}`.users u2
        JOIN `{schema}`.bd_attendance a2 ON a2.user_id = u2.user_id
        JOIN `{schema}`.beatdowns b2 ON (
            a2.q_user_id = b2.q_user_id AND a2.ao_id = b2.ao_id AND a2.date = b2.bd_date
        )
        JOIN `{schema}`.aos ao2 ON b2.ao_id = ao2.channel_id
        WHERE DATEDIFF(CURDATE(), b2.bd_date) < {d1}
        GROUP BY u2.email
    ) s1 ON u.email = s1.email
    LEFT JOIN (
        SELECT u2.email, COUNT(a2.user_id) AS attendance
        FROM `{schema}`.users u2
        JOIN `{schema}`.bd_attendance a2 ON a2.user_id = u2.user_id
        JOIN `{schema}`.beatdowns b2 ON (
            a2.q_user_id = b2.q_user_id AND a2.ao_id = b2.ao_id AND a2.date = b2.bd_date
        )
        JOIN `{schema}`.aos ao2 ON b2.ao_id = ao2.channel_id
        WHERE DATEDIFF(CURDATE(), b2.bd_date) < {d2}
        GROUP BY u2.email
    ) s2 ON u.email = s2.email
    LEFT JOIN (
        SELECT u2.email, COUNT(a2.user_id) AS attendance
        FROM `{schema}`.users u2
        JOIN `{schema}`.bd_attendance a2 ON a2.user_id = u2.user_id
        JOIN `{schema}`.beatdowns b2 ON (
            a2.q_user_id = b2.q_user_id AND a2.ao_id = b2.ao_id AND a2.date = b2.bd_date
        )
        JOIN `{schema}`.aos ao2 ON b2.ao_id = ao2.channel_id
        WHERE DATEDIFF(CURDATE(), b2.bd_date) < {d3}
        GROUP BY u2.email
    ) s3 ON u.email = s3.email
    LEFT JOIN (
        SELECT u2.email, COUNT(a2.user_id) AS attendance
        FROM `{schema}`.users u2
        JOIN `{schema}`.bd_attendance a2 ON a2.user_id = u2.user_id
        JOIN `{schema}`.beatdowns b2 ON (
            a2.q_user_id = b2.q_user_id AND a2.ao_id = b2.ao_id AND a2.date = b2.bd_date
        )
        JOIN `{schema}`.aos ao2 ON b2.ao_id = ao2.channel_id
        WHERE DATEDIFF(CURDATE(), b2.bd_date) < {d4}
        GROUP BY u2.email
    ) s4 ON u.email = s4.email
    WHERE YEAR(b.bd_date) = YEAR(CURDATE())
    GROUP BY u.email, u.user_id
    """


def _default_attendance_window() -> tuple[date, date]:
    today = date.today()
    return date(today.year, 1, 1), today


def load_nation_attendance(
    conn,
    schemas: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Union attendance across regional schemas for [start, end] (inclusive).

    Defaults to year-to-date through today. TODO: union external_attendance from
    brother regions when Nation API sync exists.
    """
    from paxminer_db import read_sql_df

    if start is None or end is None:
        default_start, default_end = _default_attendance_window()
        start = start or default_start
        end = end or default_end

    frames: list[pd.DataFrame] = []
    for schema in schemas:
        try:
            df = read_sql_df(
                conn,
                _nation_sql_for_schema(schema),
                (start.isoformat(), end.isoformat()),
            )
            LOG.info("nation attendance schema=%s rows=%s", schema, len(df))
            df["region"] = schema
            frames.append(df)
        except Exception as e:
            LOG.error("nation attendance schema=%s: %s", schema, e)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    raw_dates = out["date"].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    bad = int(out["date"].isna().sum())
    if bad:
        sample = raw_dates.loc[out["date"].isna()].head(5).tolist()
        LOG.warning(
            "Dropping %s attendance rows with unparseable bd_date (sample=%r)",
            bad,
            sample,
        )
        out = out[out["date"].notna()].copy()
    if out.empty:
        return out
    if "ao" in out.columns:
        out["ao"] = out["ao"].astype(str)
    if "activity_type" in out.columns:
        out["activity_type"] = out["activity_type"].fillna("").astype(str)
        empty = out["activity_type"].str.strip() == ""
        out.loc[empty, "activity_type"] = "beatdown"
    if "timestamp" in out.columns:
        out["timestamp"] = out["timestamp"].where(out["timestamp"].notna(), None)
    return out


def attach_home_regions(conn, nation_df: pd.DataFrame, schemas: list[str]) -> pd.DataFrame:
    if nation_df.empty:
        return nation_df
    if len(schemas) == 1:
        nation_df = nation_df.copy()
        nation_df["region"] = schemas[0]
        return nation_df
    from paxminer_db import read_sql_df

    tiers = home_region_date_tiers()
    frames: list[pd.DataFrame] = []
    for schema in schemas:
        try:
            frames.append(read_sql_df(conn, _home_region_sql_for_schema(schema, tiers)))
        except Exception as e:
            LOG.error("home region schema=%s: %s", schema, e)
    if not frames:
        return nation_df
    home = pd.concat(frames, ignore_index=True)
    home = home.sort_values("attendance").groupby("email", as_index=False).last()
    return nation_df.merge(home.drop(columns=["attendance"], errors="ignore"), on="email", how="left")


def period_key(series: pd.Series, period: str) -> pd.Series:
    return period_key_series(series, period)


def filter_activity(df: pd.DataFrame, activity) -> pd.DataFrame:
    return filter_by_activity_types(df, activity_list_from_rule({"activity": activity}))
