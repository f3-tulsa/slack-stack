"""Create and supersede achievement_versions; mirror current params onto achievements_list."""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from achievements.activity import (
    activity_filter_from_rule,
    activity_json_for_version,
    activity_legacy_mirror,
    coerce_activity_filter,
    resolve_activity_filter_for_save,
)


def version_key_for(code: str, version: int = 1, when: datetime | None = None) -> str:
    """Globally unique per row. Minute-only stamps collided on Slack retries / same-minute saves."""
    stamp = (when or datetime.utcnow()).strftime("%Y%m%d%H%M%S")
    slug = (code or "achievement").strip() or "achievement"
    return f"{slug}_v{int(version)}_{stamp}_{uuid4().hex[:8]}"


def _spec_from_write_args(activity_filter=None) -> dict:
    return coerce_activity_filter(activity_filter)


def insert_version(
    cur,
    schema: str,
    *,
    achievement_id: int,
    code: str,
    metric: str,
    period: str,
    threshold: int,
    effective_from: date | None,
    effective_to: date | None,
    created_by: str | None,
    version: int = 1,
    range_mode: str | None = None,
    activity_filter=None,
) -> int:
    spec = _spec_from_write_args(activity_filter)
    key = version_key_for(code, version)
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
            activity_json_for_version(spec),
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
    period: str,
    threshold: int,
    version: int,
    activity_filter=None,
) -> None:
    spec = _spec_from_write_args(activity_filter)
    cur.execute(
        f"""
        UPDATE `{schema}`.`achievements_list`
        SET metric=%s, activity=%s, period=%s, threshold=%s
        WHERE id=%s
        """,
        (
            metric,
            activity_legacy_mirror(spec, version=version)[:32],
            period,
            threshold,
            achievement_id,
        ),
    )


def supersede_and_insert(
    cur,
    schema: str,
    *,
    achievement_id: int,
    code: str,
    metric: str,
    period: str,
    threshold: int,
    effective_from: date | None,
    effective_to: date | None,
    created_by: str | None,
    range_mode: str | None = None,
    activity_filter=None,
) -> int:
    spec = _spec_from_write_args(activity_filter)
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
        activity_filter=spec,
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
        activity_filter=spec,
        period=period,
        threshold=threshold,
        version=version,
    )
    return vid


def params_changed(existing: dict, values: dict) -> bool:
    old = activity_filter_from_rule(existing)
    new = resolve_activity_filter_for_save(values, existing)
    old_inc = {a.lower() for a in old["include"]}
    new_inc = {a.lower() for a in new["include"]}
    old_exc = {a.lower() for a in old["exclude"]}
    new_exc = {a.lower() for a in new["exclude"]}
    return (
        (existing.get("metric") or "posts") != (values.get("metric") or "posts")
        or old_inc != new_inc
        or old_exc != new_exc
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
