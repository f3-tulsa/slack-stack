"""Grant, revoke, and post achievement awards."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date

import pandas as pd

from achievements.engine import awarded_period_bucket, evaluate_rule, period_bucket_for_date
from achievements.attendance import attach_home_regions, load_nation_attendance
from common.encryption import decrypt_field
from slack_util import open_dm_channel, ordinal_suffix, post_message, slack_client

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


def _format_grant_message(pax_id: str, name: str, verb: str, awarded_on: date, total: int, idx_count: int) -> str:
    ending = ordinal_suffix(idx_count)
    return (
        f"Congrats to our man <@{pax_id}>! "
        f"He just unlocked the achievement *{name}* for {verb} "
        f"which he earned on {awarded_on.strftime('%B %d, %Y')}. "
        f"This is achievement #{total} for <@{pax_id}> and the {idx_count}{ending} "
        f"time this year he's earned this award. Keep up the good work!"
    )


def _format_revoke_message(pax_id: str, name: str) -> str:
    return f"Correction: <@{pax_id}>'s achievement *{name}* was revoked after attendance was updated."


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
) -> dict:
    year = date.today().year
    if not region_row.get("send_achievements"):
        return {"skipped": "send_achievements off"}
    channel = region_row.get("achievement_channel")
    token_enc = region_row.get("slack_token")
    if not channel or not token_enc:
        return {"skipped": "missing channel or token"}

    token = decrypt_field(token_enc)
    client = slack_client(token)

    with conn.cursor() as cur:
        rules = _load_rules(cur, regional_schema)
        if not rules:
            return {"skipped": "no rules"}
        rules_by_id = {int(r["id"]): r for r in rules}
        awarded = _load_awarded_ytd(cur, regional_schema, year)
        existing = _existing_keys(awarded, rules_by_id)

        cur.execute(f"SELECT schema_name FROM `{pm_schema}`.`regions` WHERE active=1 AND schema_name LIKE 'f3%%'")
        schemas = [r["schema_name"] for r in cur.fetchall() if r.get("schema_name")]
        if regional_schema not in schemas:
            schemas.append(regional_schema)

    nation = load_nation_attendance(conn, schemas)
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

    with conn.cursor() as cur:
        for g in revokes:
            rule = g["rule"]
            cur.execute(f"DELETE FROM `{regional_schema}`.`achievements_awarded` WHERE id=%s", (g["id"],))
            msg = _format_revoke_message(g["pax_id"], rule["name"])
            post_message(client, channel, msg)
            if post_to_ao and ao_channel_id:
                post_message(client, ao_channel_id, msg)

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
            total = sum(counts[g["pax_id"]].values())
            idx_count = counts[g["pax_id"]][g["achievement_id"]]
            msg = _format_grant_message(
                g["pax_id"], rule["name"], rule["verb"], g["date_awarded"], total, idx_count
            )
            post_message(client, channel, msg, add_reaction=True)
            try:
                dm = open_dm_channel(client, g["pax_id"])
                post_message(client, dm, msg)
            except Exception:
                LOG.exception("DM failed pax=%s", g["pax_id"])
            if post_to_ao and ao_channel_id:
                post_message(client, ao_channel_id, msg, add_reaction=True)

        conn.commit()

    return {"grants": len(grants), "revokes": len(revokes)}


def run_daily(conn, pm_schema: str) -> list[dict]:
    results = []
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{pm_schema}`.`regions` WHERE active=1")
        regions = cur.fetchall()
    for row in regions:
        schema = row.get("schema_name")
        if not schema:
            continue
        try:
            r = run_achievements_for_region(conn, pm_schema=pm_schema, regional_schema=schema, region_row=row)
            results.append({"region": row["region"], **r})
        except Exception as e:
            LOG.exception("achievements region=%s", row.get("region"))
            results.append({"region": row.get("region"), "error": str(e)})
    return results
