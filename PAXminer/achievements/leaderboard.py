"""Monthly achievement leaderboard and almost-there text posts."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from achievements.attendance import attach_home_regions, filter_activity, load_nation_attendance, period_key
from achievements.engine import awarded_period_bucket, evaluate_rule
from common.encryption import decrypt_field
from slack_util import post_message, slack_client

LOG = logging.getLogger(__name__)
CAP = 10
GAP_SIZES = (1, 2)


def _progress_for_rule(nation_df: pd.DataFrame, rule: dict, schema: str) -> pd.DataFrame:
    df = filter_activity(nation_df[nation_df["region"] == schema], rule.get("activity", "beatdown"))
    metric = rule.get("metric", "posts")
    period = rule.get("period", "year")
    threshold = int(rule.get("threshold", 1))
    if metric == "qs":
        df = df[df["q_flag"] == 1]
    df["period_bucket"] = period_key(df["date"], period)
    current_bucket = period_bucket_for_today(period)

    if metric == "distinct_aos":
        prog = df.groupby(["user_id", "period_bucket"], as_index=False).agg(count=("ao_id", "nunique"))
    elif metric == "posts_at_single_ao":
        by_ao = df.groupby(["user_id", "period_bucket", "ao_id"], as_index=False).agg(count=("ao_id", "count"))
        prog = by_ao.groupby(["user_id", "period_bucket"], as_index=False).agg(count=("count", "max"))
    else:
        prog = df.groupby(["user_id", "period_bucket"], as_index=False).agg(count=("ao_id", "count"))

    prog = prog[prog["period_bucket"] == current_bucket]
    prog["gap"] = threshold - prog["count"]
    prog["achievement_id"] = int(rule["id"])
    prog["name"] = rule["name"]
    prog["threshold"] = threshold
    return prog


def period_bucket_for_today(period: str) -> int:
    today = date.today()
    if period == "week":
        return today.isocalendar().week
    if period == "month":
        return today.month
    return today.year


def build_leaderboard_message(awarded: pd.DataFrame, users: pd.DataFrame) -> str:
    if awarded.empty:
        return "*Achievement leaderboard (YTD)*\n\nNo awards yet this year."
    counts = awarded.groupby("pax_id", as_index=False).agg(cnt=("id", "count"))
    if not users.empty:
        users_df = users.rename(columns={"user_name": "display_name", "user_id": "pax_id"})
        counts = counts.merge(users_df[["pax_id", "display_name"]], on="pax_id", how="left")
        counts["display_name"] = counts["display_name"].fillna(counts["pax_id"])
    else:
        counts["display_name"] = counts["pax_id"]
    counts = counts.sort_values(["cnt", "display_name", "pax_id"], ascending=[False, True, True]).head(CAP)
    lines = ["*Achievement leaderboard (YTD)*\n"]
    for _, row in counts.iterrows():
        lines.append(f"\n- <@{row['pax_id']}>: {int(row['cnt'])} awards")
    return "".join(lines)


def build_almost_there_message(
    nation_df: pd.DataFrame,
    rules: list[dict],
    awarded: pd.DataFrame,
    schema: str,
    users: pd.DataFrame,
) -> str:
    candidates: list[tuple[int, str, str]] = []
    awarded_keys = set()
    rules_by_id = {int(r["id"]): r for r in rules}
    for _, row in awarded.iterrows():
        period = rules_by_id.get(int(row["achievement_id"]), {}).get("period", "year")
        bucket = awarded_period_bucket(row["date_awarded"], period)
        awarded_keys.add((row["pax_id"], int(row["achievement_id"]), bucket))

    for rule in rules:
        prog = _progress_for_rule(nation_df, rule, schema)
        aid = int(rule["id"])
        period = rule["period"]
        bucket = period_bucket_for_today(period)
        for _, row in prog.iterrows():
            gap = int(row["gap"])
            if gap not in GAP_SIZES:
                continue
            if (row["user_id"], aid, bucket) in awarded_keys:
                continue
            unit = "post" if rule["metric"] in ("posts", "posts_at_single_ao") else "Q"
            if gap != 1:
                unit += "s"
            candidates.append((gap, row["user_id"], f"<@{row['user_id']}> is {gap} {unit} away from *{rule['name']}*"))

    candidates.sort(key=lambda x: (x[0], x[1]))
    candidates = candidates[:CAP]
    if not candidates:
        return ""
    lines = ["\n\n*Almost there*\n"]
    for _, _, text in candidates:
        lines.append(f"\n- {text}")
    return "".join(lines)


def run_leaderboard_for_region(conn, pm_schema: str, region_row: dict, *, dry_run: bool = False) -> dict:
    if not region_row.get("send_achievement_leaderboard"):
        return {"skipped": "send_achievement_leaderboard off"}
    schema = region_row.get("schema_name")
    channel = region_row.get("achievement_channel")
    token_enc = region_row.get("slack_token")
    if not schema or not channel or not token_enc:
        return {"skipped": "missing schema, channel, or token"}

    year = date.today().year
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{schema}`.`achievements_list` ORDER BY id")
        rules = cur.fetchall()
        cur.execute(
            f"SELECT * FROM `{schema}`.`achievements_awarded` WHERE YEAR(date_awarded)=%s",
            (year,),
        )
        awarded_rows = cur.fetchall()
        cur.execute(f"SELECT user_id, user_name FROM `{schema}`.`users`")
        users = pd.DataFrame(cur.fetchall())
        cur.execute(f"SELECT schema_name FROM `{pm_schema}`.`regions` WHERE active=1 AND schema_name LIKE 'f3%%'")
        schemas = [r["schema_name"] for r in cur.fetchall() if r.get("schema_name")]

    awarded = pd.DataFrame(awarded_rows) if awarded_rows else pd.DataFrame(columns=["pax_id", "id", "achievement_id"])
    nation = load_nation_attendance(conn, schemas)
    nation = attach_home_regions(conn, nation, schemas)

    msg = build_leaderboard_message(awarded, users)
    almost = build_almost_there_message(nation, rules, awarded, schema, users)

    if dry_run:
        full = msg + almost
        return {"chars": len(full), "dry_run": True}

    token = decrypt_field(token_enc)
    client = slack_client(token)
    if almost and len(msg) + len(almost) <= 3900:
        post_message(client, channel, msg + almost)
    else:
        post_message(client, channel, msg)
        if almost:
            post_message(client, channel, almost.strip())
    return {"posted": True}


def run_leaderboard(conn, pm_schema: str, *, dry_run: bool = False) -> list[dict]:
    results = []
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{pm_schema}`.`regions` WHERE active=1")
        regions = cur.fetchall()
    for row in regions:
        try:
            r = run_leaderboard_for_region(conn, pm_schema, row, dry_run=dry_run)
            results.append({"region": row["region"], **r})
        except Exception as e:
            LOG.exception("leaderboard region=%s", row.get("region"))
            results.append({"region": row.get("region"), "error": str(e)})
    return results
