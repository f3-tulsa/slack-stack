"""Grant, revoke, and post achievement awards."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

import pandas as pd

from achievements.activity import classify_null_activity_types
from achievements.announcements import (
    channel_grant_messages,
    channel_revoke_message,
    dm_grant_messages,
    dm_revoke_messages,
    grant_log_line,
    reconcile_channel_line,
    revoke_log_line,
    run_summary_line,
)
from achievements.attendance import attach_home_regions, load_nation_attendance
from achievements.engine import awarded_period_bucket, evaluate_rule, period_in_effective_range
from achievements.period import period_bounds
from common.encryption import decrypt_field
from slack_util import (
    format_log_message,
    is_slack_user_id,
    open_dm_channel,
    post_log,
    post_message,
    resolve_display_name,
    slack_client,
    workspace_user_ids,
)

LOG = logging.getLogger(__name__)

RULES_SQL = """
SELECT a.id AS id, a.id AS achievement_id, a.code, a.name, a.description, a.verb,
       a.enabled, v.id AS version_id, v.version_key, v.metric, v.activity,
       v.period, v.threshold, v.effective_from, v.effective_to
FROM `{schema}`.`achievements_list` a
JOIN `{schema}`.`achievement_versions` v
  ON v.achievement_id = a.id AND v.superseded_at IS NULL
WHERE a.enabled = 1
ORDER BY a.id
"""

RULES_ONE_SQL = """
SELECT a.id AS id, a.id AS achievement_id, a.code, a.name, a.description, a.verb,
       a.enabled, v.id AS version_id, v.version_key, v.metric, v.activity,
       v.period, v.threshold, v.effective_from, v.effective_to
FROM `{schema}`.`achievements_list` a
JOIN `{schema}`.`achievement_versions` v
  ON v.achievement_id = a.id AND v.superseded_at IS NULL
WHERE a.id = %s
"""


def _load_rules(cur, schema: str, *, achievement_id: int | None = None) -> list[dict]:
    if achievement_id is not None:
        cur.execute(RULES_ONE_SQL.format(schema=schema), (int(achievement_id),))
    else:
        cur.execute(RULES_SQL.format(schema=schema))
    return list(cur.fetchall() or [])


def _load_awarded(
    cur,
    schema: str,
    *,
    start: date,
    end: date,
) -> pd.DataFrame:
    cur.execute(
        f"""
        SELECT aa.*, COALESCE(aa.period, al.period) AS period, al.code
        FROM `{schema}`.`achievements_awarded` aa
        JOIN `{schema}`.`achievements_list` al ON aa.achievement_id = al.id
        WHERE (
            (aa.period_end IS NOT NULL AND aa.period_end >= %s AND aa.period_start <= %s)
            OR (aa.period_end IS NULL AND YEAR(aa.date_awarded) >= YEAR(%s)
                AND YEAR(aa.date_awarded) <= YEAR(%s))
        )
        """,
        (start, end, start, end),
    )
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["id", "achievement_id", "pax_id", "date_awarded", "period", "period_key"]
        )
    return pd.DataFrame(rows)


def _norm_key(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def _row_period_key(row, period: str) -> str:
    stored = row.get("period_key") if hasattr(row, "get") else None
    if stored is not None and not (isinstance(stored, float) and pd.isna(stored)):
        text = str(stored).strip()
        if text and text.lower() not in ("none", "nan"):
            return text
    return awarded_period_bucket(row["date_awarded"], period)


def _existing_keys(awarded: pd.DataFrame, rules_by_id: dict[int, dict]) -> set[tuple]:
    keys: set[tuple] = set()
    if awarded.empty:
        return keys
    for _, row in awarded.iterrows():
        aid = int(row["achievement_id"])
        period = rules_by_id.get(aid, {}).get("period") or row.get("period") or "year"
        keys.add((str(row["pax_id"]), aid, _row_period_key(row, period)))
    return keys


def _name_map_from_nation(nation: pd.DataFrame) -> dict[str, str]:
    if nation.empty or "user_id" not in nation.columns:
        return {}
    cols = ["user_id"]
    if "user_name" in nation.columns:
        cols.append("user_name")
    subset = nation[cols].drop_duplicates(subset=["user_id"])
    if "user_name" not in subset.columns:
        return {str(r.user_id): str(r.user_id) for r in subset.itertuples(index=False)}
    return {
        str(r.user_id): (str(r.user_name) if pd.notna(r.user_name) else str(r.user_id))
        for r in subset.itertuples(index=False)
    }


def _first_channel(raw) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                return text
        else:
            return text
    if isinstance(raw, list) and raw:
        return str(raw[0]).strip() or None
    return None


def resolve_achievement_channel(
    conn,
    pm_schema: str,
    regional_schema: str,
    region_row: dict,
    *,
    channel_override: str | None = None,
) -> str | None:
    """Prefer the enabled award_achievements schedule, then achievement_channel."""
    if channel_override:
        return channel_override
    if pm_schema:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.destination_channels
                FROM `{pm_schema}`.`region_schedules` s
                JOIN `{pm_schema}`.`region_report_definitions` d
                  ON d.id = s.report_definition_id
                WHERE s.schema_name=%s
                  AND d.code='award_achievements'
                  AND COALESCE(s.enabled, 0) = 1
                LIMIT 1
                """,
                (regional_schema,),
            )
            row = cur.fetchone()
        scheduled = _first_channel((row or {}).get("destination_channels"))
        if scheduled:
            return scheduled
    return (region_row.get("achievement_channel") or "").strip() or None


def _grant_payload(row, rule: dict) -> dict:
    period = rule.get("period", "year")
    awarded = row["date_awarded"]
    if hasattr(awarded, "date") and callable(awarded.date) and not isinstance(awarded, date):
        awarded = awarded.date()
    key = _norm_key(row.get("period_key") or row.get("period_bucket")) or awarded_period_bucket(
        awarded, period
    )
    start = row.get("period_start")
    end = row.get("period_end")
    if start is None or end is None or (isinstance(start, float) and pd.isna(start)):
        start, end = period_bounds(awarded, period)
    return {
        "pax_id": str(row["pax_id"]),
        "achievement_id": int(rule.get("id") or rule.get("achievement_id")),
        "date_awarded": awarded,
        "period": period,
        "period_key": key,
        "period_start": start,
        "period_end": end,
        "qualifying_count": row.get("qualifying_count"),
        "ao_id": row.get("ao_id"),
        "timestamp": row.get("timestamp"),
        "version_id": rule.get("version_id") or row.get("version_id"),
        "rule": rule,
    }


def _award_in_range(row, start: date, end: date, period: str) -> bool:
    p_start = row.get("period_start")
    p_end = row.get("period_end")
    if p_start is None or p_end is None or (isinstance(p_start, float) and pd.isna(p_start)):
        d = row["date_awarded"]
        p_start, p_end = period_bounds(d, period)
    if hasattr(p_start, "date") and callable(p_start.date):
        p_start = p_start.date()
    if hasattr(p_end, "date") and callable(p_end.date):
        p_end = p_end.date()
    return p_end >= start and p_start <= end


def _current_version_id(rule: dict) -> int | None:
    vid = rule.get("version_id")
    if vid is None or (isinstance(vid, float) and pd.isna(vid)):
        return None
    return int(vid)


def iter_year_windows(start: date, end: date, overlap_days: int = 7) -> list[tuple[int, date, date]]:
    windows: list[tuple[int, date, date]] = []
    year = start.year
    while date(year, 1, 1) <= end:
        chunk_start = date(year, 1, 1) - timedelta(days=overlap_days)
        chunk_end = date(year, 12, 31) + timedelta(days=overlap_days)
        windows.append((year, chunk_start, chunk_end))
        year += 1
    return windows


def _filter_period_year(qualified: pd.DataFrame, year: int) -> pd.DataFrame:
    if qualified.empty:
        return qualified
    prefix = f"{year}-"
    year_s = str(year)
    keys = qualified["period_key"].astype(str) if "period_key" in qualified.columns else qualified["period_bucket"].astype(str)
    return qualified[keys.eq(year_s) | keys.str.startswith(prefix)]


def run_achievements_for_region(
    conn,
    *,
    pm_schema: str,
    regional_schema: str,
    region_row: dict,
    pax_user_ids: set[str] | None = None,
    post_to_ao: bool = False,
    ao_channel_id: str | None = None,
    dry_run: bool = False,
    channel_override: str | None = None,
    post_channels: list[str] | None = None,
    announce: bool = True,
    start: date | None = None,
    end: date | None = None,
    log_mode: str = "scheduled",
    trigger_ao_id: str | None = None,
    trigger_timestamp: str | None = None,
    trigger_date: date | None = None,
    achievement_id: int | None = None,
    actor: str | None = None,
    allow_revoke: bool | None = None,
    period_year: int | None = None,
    emit_logs: bool = True,
) -> dict:
    started = time.time()
    today = date.today()
    year = today.year
    if start is None:
        start = date(year, 1, 1)
    if end is None:
        end = today
    if allow_revoke is None:
        allow_revoke = pax_user_ids is not None
    channels = [c for c in (post_channels or []) if c]
    if channel_override and channel_override not in channels:
        channels.insert(0, channel_override)
    if not channels:
        resolved = resolve_achievement_channel(
            conn,
            pm_schema,
            regional_schema,
            region_row,
            channel_override=channel_override,
        )
        if resolved:
            channels = [resolved]
    channel = channels[0] if channels else None
    if announce and not channel_override and not channel:
        if not region_row.get("send_achievements"):
            return {"skipped": "send_achievements off"}
        channel = region_row.get("achievement_channel")
        if channel:
            channels = [channel]
    token_enc = region_row.get("slack_token")
    need_client = announce or log_mode in ("scheduled", "webhook", "reconcile", "backfill")
    if announce and (not channel or not token_enc):
        return {"skipped": "missing channel or token"}

    client = None
    if need_client and token_enc:
        token = decrypt_field(token_enc)
        client = slack_client(token)

    with conn.cursor() as cur:
        classify_null_activity_types(cur, regional_schema)
        rules = _load_rules(cur, regional_schema, achievement_id=achievement_id)
        if not rules:
            return {"skipped": "no rules"}
        rules_by_id = {int(r["id"]): r for r in rules}
        awarded = _load_awarded(cur, regional_schema, start=start, end=end)
        existing = _existing_keys(awarded, rules_by_id)

    schemas = [regional_schema]
    nation = load_nation_attendance(conn, schemas, start=start, end=end)
    if nation.empty:
        LOG.warning(
            "achievements skipped region=%s: no attendance data (would not revoke)",
            regional_schema,
        )
        return {"skipped": "no attendance data"}
    nation = attach_home_regions(conn, nation, schemas)

    scope = pax_user_ids
    grants: list[dict] = []
    revokes: list[dict] = []
    held = 0
    held_grandfathered = 0
    held_older_version = 0
    held_out_of_range = 0

    for rule in rules:
        if not rule.get("enabled", 1):
            continue
        qualified = evaluate_rule(nation, rule, schema=regional_schema, pax_filter=scope)
        if period_year is not None:
            qualified = _filter_period_year(qualified, period_year)
        period = rule.get("period") or "year"
        aid = int(rule["id"])
        current_vid = _current_version_id(rule)
        qual_keys = set()
        if not qualified.empty:
            for r in qualified.itertuples(index=False):
                key = _norm_key(getattr(r, "period_key", None) or getattr(r, "period_bucket", None))
                qual_keys.add((str(r.pax_id), aid, key))

        for _, row in qualified.iterrows():
            key = (
                str(row["pax_id"]),
                aid,
                _norm_key(row.get("period_key") or row.get("period_bucket")),
            )
            if key in existing:
                continue
            grants.append(_grant_payload(row, rule))
            existing.add(key)

        if allow_revoke:
            subset = awarded[awarded["achievement_id"] == aid] if not awarded.empty else awarded
            for _, row in subset.iterrows():
                if scope is not None and str(row["pax_id"]) not in scope:
                    continue
                if not _award_in_range(row, start, end, period):
                    held += 1
                    held_out_of_range += 1
                    continue
                award_vid = row.get("achievement_version_id")
                if award_vid is None or (isinstance(award_vid, float) and pd.isna(award_vid)):
                    held += 1
                    held_grandfathered += 1
                    continue
                if current_vid is None or int(award_vid) != current_vid:
                    held += 1
                    held_older_version += 1
                    continue
                bucket = _row_period_key(row, period)
                if (str(row["pax_id"]), aid, bucket) in qual_keys:
                    held += 1
                    continue
                if not period_in_effective_range(row.get("period_start"), row.get("period_end"), rule):
                    held += 1
                    held_out_of_range += 1
                    continue
                revokes.append(
                    {
                        "id": row["id"],
                        "pax_id": str(row["pax_id"]),
                        "rule": rule,
                        "period": period,
                        "period_key": bucket,
                        "period_start": row.get("period_start"),
                        "period_end": row.get("period_end"),
                        "ao_id": row.get("ao_id"),
                        "timestamp": row.get("timestamp"),
                        "date_awarded": row.get("date_awarded"),
                        "trigger_ao_id": trigger_ao_id,
                        "trigger_timestamp": trigger_timestamp,
                        "trigger_date": trigger_date,
                    }
                )

    counts: dict[str, Counter] = defaultdict(Counter)
    if not awarded.empty:
        for _, row in awarded.iterrows():
            counts[str(row["pax_id"])][int(row["achievement_id"])] += 1

    if dry_run:
        return {
            "grants": len(grants),
            "revokes": len(revokes),
            "held": held,
            "dry_run": True,
        }

    with conn.cursor() as cur:
        for g in revokes:
            cur.execute(
                f"DELETE FROM `{regional_schema}`.`achievements_awarded` WHERE id=%s",
                (g["id"],),
            )
        inserted = []
        for g in grants:
            cur.execute(
                f"""
                INSERT IGNORE INTO `{regional_schema}`.`achievements_awarded`
                (achievement_id, pax_id, date_awarded, achievement_version_id, period,
                 period_key, period_start, period_end, qualifying_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    g["achievement_id"],
                    g["pax_id"],
                    g["date_awarded"],
                    g.get("version_id"),
                    g.get("period"),
                    g.get("period_key"),
                    g.get("period_start"),
                    g.get("period_end"),
                    g.get("qualifying_count"),
                ),
            )
            # Unique (achievement, pax, period) is the authority across overlapping
            # Lambdas; _existing_keys is only a cheap in-process skip.
            if cur.rowcount:
                inserted.append(g)
                counts[g["pax_id"]][g["achievement_id"]] += 1
        conn.commit()
    grants = inserted

    duration_s = round(time.time() - started, 2)
    names = _name_map_from_nation(nation)
    known_ids = workspace_user_ids(client) if client is not None else None
    dm_failed = 0
    dms_sent = 0
    webhook = log_mode == "webhook"

    ytd_totals = {pax: int(sum(c.values())) for pax, c in counts.items()}
    ytd_family = {(pax, aid): int(cnt) for pax, c in counts.items() for aid, cnt in c.items()}

    if announce and client is not None:
        grant_msgs = channel_grant_messages(
            grants,
            year=year,
            names=names,
            known_ids=known_ids,
            ytd_totals=ytd_totals,
            ytd_family=ytd_family,
        )
        for text, blocks, react in grant_msgs:
            for cid in channels:
                post_message(client, cid, text, blocks=blocks, add_reaction=react)
            if post_to_ao and ao_channel_id:
                post_message(client, ao_channel_id, text, blocks=blocks, add_reaction=react)
        for g in revokes:
            text, blocks = channel_revoke_message(
                g, names=names, known_ids=known_ids, webhook=webhook
            )
            for cid in channels:
                post_message(client, cid, text, blocks=blocks)
            if post_to_ao and ao_channel_id:
                post_message(client, ao_channel_id, text, blocks=blocks)
        dm_map = dm_grant_messages(
            grants,
            year=year,
            names=names,
            known_ids=known_ids,
            ytd_totals=ytd_totals,
            ytd_family=ytd_family,
        )
        revoke_dms = dm_revoke_messages(
            revokes, names=names, known_ids=known_ids, webhook=webhook
        )
        for pax_id, (text, blocks) in {**dm_map, **revoke_dms}.items():
            if pax_id in dm_map and pax_id in revoke_dms:
                # Send grant and revoke DMs separately when both happen.
                pass
        for pax_id, (text, blocks) in dm_map.items():
            if not is_slack_user_id(pax_id) or (known_ids is not None and pax_id not in known_ids):
                LOG.info("Skip achievement DM for non-Slack user_id=%s", pax_id)
                continue
            try:
                dm = open_dm_channel(client, pax_id)
                post_message(client, dm, text, blocks=blocks)
                dms_sent += 1
            except Exception:
                dm_failed += 1
                LOG.exception("DM failed pax=%s", pax_id)
        for pax_id, (text, blocks) in revoke_dms.items():
            if not is_slack_user_id(pax_id) or (known_ids is not None and pax_id not in known_ids):
                continue
            try:
                dm = open_dm_channel(client, pax_id)
                post_message(client, dm, text, blocks=blocks)
                dms_sent += 1
            except Exception:
                dm_failed += 1
                LOG.exception("revoke DM failed pax=%s", pax_id)

    if client is not None and emit_logs:
        log_lines: list[str] = []
        for g in grants:
            log_lines.append(grant_log_line(g, names.get(g["pax_id"])))
        for g in revokes:
            log_lines.append(revoke_log_line(g, names.get(g["pax_id"]), webhook=webhook))
        changed = bool(grants or revokes or dm_failed)
        if log_mode == "webhook":
            if changed:
                for line in log_lines:
                    post_log(client, line, region=region_row)
        else:
            for line in log_lines:
                post_log(client, line, region=region_row)
            # Scheduled / Run Now: one outcome line from schedule_runner, not a second dashed summary.
            if log_mode != "scheduled":
                dest_channel = f"<#{channels[0]}>" if channels and str(channels[0]).startswith("C") else (channels[0] if channels else None)
                post_log(
                    client,
                    run_summary_line(
                        kind="backfill" if log_mode == "backfill" else log_mode,
                        granted=len(grants),
                        revoked=len(revokes),
                        held=held,
                        held_grandfathered=held_grandfathered,
                        held_older_version=held_older_version,
                        held_out_of_range=held_out_of_range,
                        rules=len(rules),
                        start=start,
                        end=end,
                        channel=dest_channel,
                        dms=dms_sent,
                        dm_failed=dm_failed,
                        duration_s=duration_s,
                        actor=resolve_display_name(client, actor),
                        achievement_name=(rules[0].get("name") if achievement_id and rules else None),
                    ),
                    region=region_row,
                )

    return {
        "grants": len(grants),
        "revokes": len(revokes),
        "held": held,
        "dm_failed": dm_failed,
        "duration_s": duration_s,
        "rules": len(rules),
        "results_line": (
            f"{len(rules)} rules, {len(grants)} granted, {len(revokes)} revoked, {held} held"
        ),
        "period_start": start.isoformat() if start else None,
        "period_end": end.isoformat() if end else None,
    }


def reconcile_rule_awards(
    conn,
    *,
    pm_schema: str,
    regional_schema: str,
    region_row: dict,
    achievement_id: int,
    actor: str | None = None,
    start: date | None = None,
    end: date | None = None,
    automatic: bool = False,
) -> dict:
    """Re-evaluate one family across its range; silent on T-claps/DMs, one channel summary."""
    from achievements.range import clear_reeval_lock

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT a.name, v.effective_from, v.effective_to
                FROM `{regional_schema}`.`achievements_list` a
                JOIN `{regional_schema}`.`achievement_versions` v
                  ON v.achievement_id = a.id AND v.superseded_at IS NULL
                WHERE a.id=%s
                """,
                (achievement_id,),
            )
            meta = cur.fetchone() or {}
            if start is None:
                start = meta.get("effective_from")
            if start is None:
                cur.execute(f"SELECT MIN(bd_date) AS d FROM `{regional_schema}`.`beatdowns`")
                row = cur.fetchone() or {}
                start = row.get("d")
            if end is None:
                end = meta.get("effective_to") or date.today()
        if start is None:
            start = date(date.today().year, 1, 1)
        if hasattr(start, "date") and callable(start.date):
            start = start.date()
        if hasattr(end, "date") and callable(end.date):
            end = end.date()

        totals = {"grants": 0, "revokes": 0, "held": 0}
        last_result: dict = {}
        for year, chunk_start, chunk_end in iter_year_windows(start, end):
            result = run_achievements_for_region(
                conn,
                pm_schema=pm_schema,
                regional_schema=regional_schema,
                region_row=region_row,
                pax_user_ids=None,
                dry_run=False,
                announce=False,
                start=chunk_start,
                end=chunk_end,
                log_mode="backfill",
                achievement_id=achievement_id,
                actor=actor,
                allow_revoke=True,
                period_year=year,
                emit_logs=False,
            )
            last_result = result
            if result.get("skipped"):
                continue
            totals["grants"] += int(result.get("grants") or 0)
            totals["revokes"] += int(result.get("revokes") or 0)
            totals["held"] += int(result.get("held") or 0)

        token_enc = region_row.get("slack_token")
        channel = resolve_achievement_channel(conn, pm_schema, regional_schema, region_row)
        name = meta.get("name") or "achievement"
        if token_enc and channel:
            try:
                client = slack_client(decrypt_field(token_enc))
                # Public "was corrected" is false when nothing was granted or revoked.
                if totals["grants"] or totals["revokes"]:
                    post_message(
                        client,
                        channel,
                        reconcile_channel_line(
                            name, totals["grants"], totals["revokes"], totals["held"]
                        ),
                    )
                post_log(
                    client,
                    run_summary_line(
                        kind="backfill",
                        granted=totals["grants"],
                        revoked=totals["revokes"],
                        held=totals["held"],
                        held_grandfathered=0,
                        held_older_version=0,
                        held_out_of_range=0,
                        rules=1,
                        start=start,
                        end=end,
                        channel=None,
                        dms=0,
                        dm_failed=0,
                        duration_s=None,
                        actor=resolve_display_name(client, actor),
                        achievement_name=name,
                        automatic=automatic,
                    ),
                    region=region_row,
                )
            except Exception:
                LOG.exception("reconcile channel summary failed schema=%s", regional_schema)
        return {
            "grants": totals["grants"],
            "revokes": totals["revokes"],
            "held": totals["held"],
            "reconcile": True,
            "achievement_id": achievement_id,
            "skipped": last_result.get("skipped"),
        }

    finally:
        try:
            with conn.cursor() as cur:
                clear_reeval_lock(cur, regional_schema, int(achievement_id))
            conn.commit()
        except Exception:
            LOG.debug("clear reeval lock skipped schema=%s id=%s", regional_schema, achievement_id)

def run_daily(conn, pm_schema: str, *, dry_run: bool = False, announce: bool = True) -> list[dict]:
    results = []
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{pm_schema}`.`regions` WHERE active=1")
        regions = cur.fetchall()
    for row in regions:
        schema = row.get("schema_name")
        if not schema:
            continue
        try:
            r = run_achievements_for_region(
                conn,
                pm_schema=pm_schema,
                regional_schema=schema,
                region_row=row,
                dry_run=dry_run,
                announce=announce,
                log_mode="scheduled",
            )
            results.append({"region": row["region"], **r})
        except Exception as e:
            LOG.exception("achievements region=%s", row.get("region"))
            results.append({"region": row.get("region"), "error": str(e)})
            _post_achievement_failure_log(row, e)
    return results


def _post_achievement_failure_log(region_row: dict, exc: Exception) -> None:
    """Best-effort failure line to paxminer_logs. Never raises."""
    token_enc = region_row.get("slack_token")
    if not token_enc:
        return
    try:
        token = decrypt_field(token_enc)
        client = slack_client(token)
        post_log(
            client,
            format_log_message(
                "The *Achievements* job was run as scheduled",
                status="failed",
                detail=str(exc)[:500],
            ),
            region=region_row,
        )
    except Exception:
        LOG.debug("achievement failure log skipped", exc_info=True)
