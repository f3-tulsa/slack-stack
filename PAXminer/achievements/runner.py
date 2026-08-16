"""Grant, revoke, and post achievement awards."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import date

import pandas as pd

from achievements.engine import awarded_period_bucket, evaluate_rule
from achievements.attendance import attach_home_regions, load_nation_attendance
from common.encryption import decrypt_field
from slack_blocks import section
from slack_util import (
    is_slack_user_id,
    mention,
    open_dm_channel,
    ordinal_suffix,
    post_log,
    post_message,
    slack_client,
    workspace_user_ids,
)

LOG = logging.getLogger(__name__)


def _load_rules(cur, schema: str) -> list[dict]:
    cur.execute(f"SELECT * FROM `{schema}`.`achievements_list` ORDER BY id")
    return cur.fetchall()


def _load_awarded_ytd(cur, schema: str, year: int) -> pd.DataFrame:
    cur.execute(
        f"""
        SELECT aa.*, al.period, al.code
        FROM `{schema}`.`achievements_awarded` aa
        JOIN `{schema}`.`achievements_list` al ON aa.achievement_id = al.id
        WHERE YEAR(aa.date_awarded) = %s
        """,
        (year,),
    )
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["id", "achievement_id", "pax_id", "date_awarded", "period"])
    df = pd.DataFrame(rows)
    df["period_bucket"] = df.apply(
        lambda r: awarded_period_bucket(r["date_awarded"], r["period"]), axis=1
    )
    return df


def _existing_keys(awarded: pd.DataFrame, rules_by_id: dict[int, dict]) -> set[tuple]:
    keys: set[tuple] = set()
    for _, row in awarded.iterrows():
        aid = int(row["achievement_id"])
        period = rules_by_id.get(aid, {}).get("period", "year")
        bucket = awarded_period_bucket(row["date_awarded"], period)
        keys.add((row["pax_id"], aid, bucket))
    return keys


def _format_grant_message(
    pax_id: str,
    name: str,
    verb: str,
    awarded_on: date,
    total: int,
    idx_count: int,
    *,
    display_name: str | None = None,
    known_ids: set[str] | None = None,
) -> tuple[str, list[dict]]:
    ending = ordinal_suffix(idx_count)
    tag = mention(pax_id, display_name, known_ids=known_ids)
    text = (
        f"Congrats to our man {tag}! "
        f"He just unlocked the achievement *{name}* for {verb} "
        f"which he earned on {awarded_on.strftime('%B %d, %Y')}. "
        f"This is achievement #{total} for {tag} and the {idx_count}{ending} "
        f"time this year he's earned this award. Keep up the good work!"
    )
    return text, [section(text)]


def _format_revoke_message(
    pax_id: str,
    name: str,
    *,
    display_name: str | None = None,
    known_ids: set[str] | None = None,
) -> tuple[str, list[dict]]:
    tag = mention(pax_id, display_name, known_ids=known_ids)
    text = f"Correction: {tag}'s achievement *{name}* was revoked after attendance was updated."
    return text, [section(text)]


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
) -> dict:
    year = date.today().year
    # Label logs with schema_name (e.g. f3ttown_test), not display region.
    region_name = regional_schema
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
    # Schedule path passes channel_override and uses schedule.enabled as the gate.
    # Webhook / legacy daily path still honors send_achievements when no schedule channel.
    if announce and not channel_override and not channel:
        if not region_row.get("send_achievements"):
            return {"skipped": "send_achievements off"}
        channel = region_row.get("achievement_channel")
        if channel:
            channels = [channel]
    token_enc = region_row.get("slack_token")
    if announce and (not channel or not token_enc):
        return {"skipped": "missing channel or token"}

    client = None
    if announce:
        token = decrypt_field(token_enc)
        client = slack_client(token)

    with conn.cursor() as cur:
        rules = _load_rules(cur, regional_schema)
        if not rules:
            return {"skipped": "no rules"}
        rules_by_id = {int(r["id"]): r for r in rules}
        awarded = _load_awarded_ytd(cur, regional_schema, year)
        existing = _existing_keys(awarded, rules_by_id)

    # Single-region only; cross-region / down-range attendance needs the F3 Nation API.
    schemas = [regional_schema]
    nation = load_nation_attendance(conn, schemas)
    if nation.empty:
        # Never mass-revoke awards when attendance data is missing/empty.
        LOG.warning(
            "achievements skipped region=%s: no attendance data (would not revoke)",
            regional_schema,
        )
        return {"skipped": "no attendance data"}
    nation = attach_home_regions(conn, nation, schemas)

    scope = pax_user_ids
    grants: list[dict] = []
    revokes: list[dict] = []

    for rule in rules:
        qualified = evaluate_rule(nation, rule, schema=regional_schema, pax_filter=scope)
        period = rule["period"]
        aid = int(rule["id"])
        qual_keys = {
            (r.pax_id, aid, int(r.period_bucket))
            for r in qualified.itertuples(index=False)
        }

        for _, row in qualified.iterrows():
            key = (row["pax_id"], aid, int(row["period_bucket"]))
            if key in existing:
                continue
            grants.append(
                {
                    "pax_id": row["pax_id"],
                    "achievement_id": aid,
                    "date_awarded": row["date_awarded"],
                    "rule": rule,
                }
            )
            existing.add(key)

        if allow_revoke and announce:
            for _, row in awarded[awarded["achievement_id"] == aid].iterrows():
                if scope is not None and row["pax_id"] not in scope:
                    continue
                bucket = awarded_period_bucket(row["date_awarded"], period)
                if (row["pax_id"], aid, bucket) not in qual_keys:
                    revokes.append({"id": row["id"], "pax_id": row["pax_id"], "rule": rule})

    counts: dict[str, Counter] = defaultdict(Counter)
    for _, row in awarded.iterrows():
        counts[row["pax_id"]][int(row["achievement_id"])] += 1

    if dry_run:
        return {"grants": len(grants), "revokes": len(revokes), "dry_run": True}

    names = _name_map_from_nation(nation) if announce else {}
    known_ids = workspace_user_ids(client) if announce and client is not None else None

    with conn.cursor() as cur:
        for g in revokes:
            rule = g["rule"]
            cur.execute(f"DELETE FROM `{regional_schema}`.`achievements_awarded` WHERE id=%s", (g["id"],))
            if announce and client is not None:
                text, blocks = _format_revoke_message(
                    g["pax_id"],
                    rule["name"],
                    display_name=names.get(g["pax_id"]),
                    known_ids=known_ids,
                )
                for cid in channels:
                    post_message(client, cid, text, blocks=blocks)
                if post_to_ao and ao_channel_id:
                    post_message(client, ao_channel_id, text, blocks=blocks)
                tag = mention(g["pax_id"], names.get(g["pax_id"]), known_ids=known_ids)
                post_log(
                    client,
                    f"- Achievement ({region_name}): revoked '{rule['name']}' from {tag}",
                )
            conn.commit()

        for g in grants:
            rule = g["rule"]
            cur.execute(
                f"""
                INSERT INTO `{regional_schema}`.`achievements_awarded`
                (achievement_id, pax_id, date_awarded) VALUES (%s, %s, %s)
                """,
                (g["achievement_id"], g["pax_id"], g["date_awarded"]),
            )
            counts[g["pax_id"]][g["achievement_id"]] += 1
            if announce and client is not None:
                total = sum(counts[g["pax_id"]].values())
                idx_count = counts[g["pax_id"]][g["achievement_id"]]
                text, blocks = _format_grant_message(
                    g["pax_id"],
                    rule["name"],
                    rule["verb"],
                    g["date_awarded"],
                    total,
                    idx_count,
                    display_name=names.get(g["pax_id"]),
                    known_ids=known_ids,
                )
                for cid in channels:
                    post_message(client, cid, text, blocks=blocks, add_reaction=True)
                if is_slack_user_id(g["pax_id"]) and (
                    known_ids is None or g["pax_id"] in known_ids
                ):
                    try:
                        dm = open_dm_channel(client, g["pax_id"])
                        post_message(client, dm, text, blocks=blocks)
                    except Exception:
                        LOG.exception("DM failed pax=%s", g["pax_id"])
                else:
                    LOG.info("Skip achievement DM for non-Slack user_id=%s", g["pax_id"])
                if post_to_ao and ao_channel_id:
                    post_message(client, ao_channel_id, text, blocks=blocks, add_reaction=True)
                tag = mention(g["pax_id"], names.get(g["pax_id"]), known_ids=known_ids)
                post_log(
                    client,
                    f"- Achievement ({region_name}): granted '{rule['name']}' to {tag}",
                )
            conn.commit()

    return {"grants": len(grants), "revokes": len(revokes)}


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
            )
            results.append({"region": row["region"], **r})
        except Exception as e:
            LOG.exception("achievements region=%s", row.get("region"))
            results.append({"region": row.get("region"), "error": str(e)})
            _post_achievement_failure_log(row, e)
    return results


def _post_achievement_failure_log(region_row: dict, exc: Exception) -> None:
    """Best-effort failure line to paxminer_logs. Never raises."""
    region_name = region_row.get("schema_name") or "?"
    token_enc = region_row.get("slack_token")
    if not token_enc:
        return
    try:
        token = decrypt_field(token_enc)
        client = slack_client(token)
        post_log(client, f"- Achievement ({region_name}): FAILED - {exc}")
    except Exception:
        LOG.debug("achievement failure log skipped region=%s", region_name, exc_info=True)
