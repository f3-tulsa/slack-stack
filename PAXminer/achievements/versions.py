"""Create and supersede achievement_versions; mirror current params onto achievements_list."""

from __future__ import annotations

import json
from datetime import date, datetime

from achievements.activity import activity_legacy_mirror, activity_list_from_rule


def version_key_for(code: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.utcnow()).strftime("%Y%m%d%H%M")
    return f"{code}_{stamp}"


def insert_version(
    cur,
    schema: str,
    *,
    achievement_id: int,
    code: str,
    metric: str,
    activity_list: list[str],
    period: str,
    threshold: int,
    effective_from: date | None,
    effective_to: date | None,
    created_by: str | None,
    version: int = 1,
    range_mode: str | None = None,
) -> int:
    key = version_key_for(code)
    cur.execute(
        f"""
        INSERT INTO `{schema}`.`achievement_versions`
        (achievement_id, version, version_key, metric, activity, period, threshold,
         effective_from, effective_to, range_mode, superseded_at, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
        """,
        (
            achievement_id,
            version,
            key,
            metric,
            json.dumps(activity_list),
            period,
            threshold,
            effective_from,
            effective_to,
            range_mode,
            created_by,
        ),
    )
    return int(cur.lastrowid)


def current_version(cur, schema: str, achievement_id: int) -> dict | None:
    cur.execute(
        f"""
        SELECT * FROM `{schema}`.`achievement_versions`
        WHERE achievement_id=%s AND superseded_at IS NULL
        ORDER BY version DESC LIMIT 1
        """,
        (achievement_id,),
    )
    return cur.fetchone()


def next_version_number(cur, schema: str, achievement_id: int) -> int:
    cur.execute(
        f"SELECT COALESCE(MAX(version), 0) AS v FROM `{schema}`.`achievement_versions` WHERE achievement_id=%s",
        (achievement_id,),
    )
    row = cur.fetchone() or {}
    return int(row.get("v") or 0) + 1


def mirror_list_params(
    cur,
    schema: str,
    achievement_id: int,
    *,
    metric: str,
    activity_list: list[str],
    period: str,
    threshold: int,
) -> None:
    cur.execute(
        f"""
        UPDATE `{schema}`.`achievements_list`
        SET metric=%s, activity=%s, period=%s, threshold=%s
        WHERE id=%s
        """,
        (metric, activity_legacy_mirror(activity_list)[:32], period, threshold, achievement_id),
    )


def supersede_and_insert(
    cur,
    schema: str,
    *,
    achievement_id: int,
    code: str,
    metric: str,
    activity_list: list[str],
    period: str,
    threshold: int,
    effective_from: date | None,
    effective_to: date | None,
    created_by: str | None,
    range_mode: str | None = None,
) -> int:
    cur.execute(
        f"""
        UPDATE `{schema}`.`achievement_versions`
        SET superseded_at=NOW()
        WHERE achievement_id=%s AND superseded_at IS NULL
        """,
        (achievement_id,),
    )
    version = next_version_number(cur, schema, achievement_id)
    vid = insert_version(
        cur,
        schema,
        achievement_id=achievement_id,
        code=code,
        metric=metric,
        activity_list=activity_list,
        period=period,
        threshold=threshold,
        effective_from=effective_from,
        effective_to=effective_to,
        created_by=created_by,
        version=version,
        range_mode=range_mode,
    )
    mirror_list_params(
        cur,
        schema,
        achievement_id,
        metric=metric,
        activity_list=activity_list,
        period=period,
        threshold=threshold,
    )
    return vid


def params_changed(existing: dict, values: dict) -> bool:
    old_activity = {a.lower() for a in activity_list_from_rule(existing)}
    new_activity = {a.lower() for a in (values.get("activity_list") or [])}
    return (
        (existing.get("metric") or "posts") != (values.get("metric") or "posts")
        or old_activity != new_activity
        or (existing.get("period") or "year") != (values.get("period") or "year")
        or int(existing.get("threshold") or 1) != int(values.get("threshold") or 1)
    )


def update_current_range(
    cur,
    schema: str,
    achievement_id: int,
    *,
    effective_from: date | None,
    effective_to: date | None,
    range_mode: str | None,
) -> None:
    cur.execute(
        f"""
        UPDATE `{schema}`.`achievement_versions`
        SET effective_from=%s, effective_to=%s, range_mode=%s
        WHERE achievement_id=%s AND superseded_at IS NULL
        """,
        (effective_from, effective_to, range_mode, achievement_id),
    )
