"""Monthly achievement leaderboard and almost-there text posts."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from achievements.attendance import attach_home_regions, filter_activity, load_nation_attendance, period_key
from achievements.engine import awarded_period_bucket
from achievements.period import period_key_for_date
from achievements.runner import resolve_achievement_channel
from common.encryption import decrypt_field
from slack_blocks import chunk_messages, chunk_sections, fallback_text, header, section
from slack_util import mention, post_message, slack_client, workspace_user_ids

LOG = logging.getLogger(__name__)
CAP = 10
GAP_SIZES = (1, 2)


def _progress_for_rule(nation_df: pd.DataFrame, rule: dict, schema: str) -> pd.DataFrame:
    df = filter_activity(
        nation_df[nation_df["region"] == schema].copy(),
        rule.get("activity", "beatdown"),
    )
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


def period_bucket_for_today(period: str) -> str:
    return period_key_for_date(date.today(), period)


def build_leaderboard_message(
    awarded: pd.DataFrame,
    users: pd.DataFrame,
    *,
    known_ids: set[str] | None = None,
    title: str | None = None,
    empty: str | None = None,
    top_n: int | None = None,
) -> tuple[str, list[dict]]:
    heading = (title or "Achievement leaderboard").strip() or "Achievement leaderboard"
    empty_text = empty or "No awards yet."
    limit = int(top_n or CAP)
    if awarded.empty:
        text = f"*{heading}*\n\n{empty_text}"
        return text, [header(heading[:150]), section(empty_text)]
    counts = awarded.groupby("pax_id", as_index=False).agg(cnt=("id", "count"))
    if not users.empty:
        users_df = users.rename(columns={"user_name": "display_name", "user_id": "pax_id"})
        counts = counts.merge(users_df[["pax_id", "display_name"]], on="pax_id", how="left")
        counts["display_name"] = counts["display_name"].fillna(counts["pax_id"])
    else:
        counts["display_name"] = counts["pax_id"]
    counts = counts.sort_values(["cnt", "display_name", "pax_id"], ascending=[False, True, True]).head(max(limit, 1))
    body_lines = [
        f"\n{mention(row['pax_id'], row['display_name'], known_ids=known_ids)}: "
        f"{int(row['cnt'])} awards"
        for _, row in counts.iterrows()
    ]
    text = f"*{heading}*\n" + "".join(body_lines)
    blocks = [header(heading[:150])]
    blocks.extend(chunk_sections(["".join(body_lines).lstrip("\n")]))
    return text, blocks


def build_almost_there_message(
    nation_df: pd.DataFrame,
    rules: list[dict],
    awarded: pd.DataFrame,
    schema: str,
    users: pd.DataFrame,
    *,
    known_ids: set[str] | None = None,
    title: str | None = None,
    top_n: int | None = None,
) -> tuple[str, list[dict]]:
    candidates: list[tuple[int, str, str]] = []
    awarded_keys = set()
    rules_by_id = {int(r["id"]): r for r in rules}
    name_by_id: dict[str, str] = {}
    if not users.empty and "user_id" in users.columns:
        for _, urow in users.iterrows():
            name_by_id[str(urow["user_id"])] = str(
                urow.get("user_name") or urow["user_id"]
            )
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
            uid = row["user_id"]
            tag = mention(uid, name_by_id.get(str(uid)), known_ids=known_ids)
            candidates.append(
                (gap, str(uid), f"{tag} is {gap} {unit} away from *{rule['name']}*")
            )

    candidates.sort(key=lambda x: (x[0], x[1]))
    candidates = candidates[: max(int(top_n or CAP), 1)]
    heading = (title or "Almost there").strip() or "Almost there"
    if not candidates:
        return "", []
    body_lines = [f"\n{line}" for _, _, line in candidates]
    text = f"\n\n*{heading}*\n" + "".join(body_lines)
    blocks = [section(f"*{heading}*")]
    blocks.extend(chunk_sections(["".join(body_lines).lstrip("\n")]))
    return text, blocks


def _load_region_award_inputs(
    conn,
    pm_schema: str,
    region_row: dict,
    *,
    window: tuple[date, date] | None = None,
    load_nation: bool = False,
) -> dict:
    schema = region_row.get("schema_name")
    channel = resolve_achievement_channel(conn, pm_schema, schema or "", region_row)
    token_enc = region_row.get("slack_token")
    if not schema or not channel or not token_enc:
        return {"skipped": "missing schema, channel, or token"}

    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{schema}`.`achievements_list` ORDER BY id")
        rules = cur.fetchall()
        if window is not None:
            start, end = window
            cur.execute(
                f"SELECT * FROM `{schema}`.`achievements_awarded` "
                f"WHERE (period_end IS NOT NULL AND period_end >= %s AND period_start <= %s) "
                f"OR (period_end IS NULL AND date_awarded BETWEEN %s AND %s)",
                (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
            )
        else:
            year = date.today().year
            year_start = date(year, 1, 1).isoformat()
            year_end = date(year, 12, 31).isoformat()
            cur.execute(
                f"SELECT * FROM `{schema}`.`achievements_awarded` "
                f"WHERE (period_end IS NOT NULL AND period_end >= %s AND period_start <= %s) "
                f"OR (period_end IS NULL AND YEAR(date_awarded)=%s)",
                (year_start, year_end, year),
            )
        awarded_rows = cur.fetchall()
        cur.execute(f"SELECT user_id, user_name FROM `{schema}`.`users`")
        users = pd.DataFrame(cur.fetchall())

    awarded = (
        pd.DataFrame(awarded_rows)
        if awarded_rows
        else pd.DataFrame(columns=["pax_id", "id", "achievement_id"])
    )
    nation = None
    if load_nation:
        # Single-region only; cross-region / down-range attendance needs the F3 Nation API.
        nation = attach_home_regions(conn, load_nation_attendance(conn, [schema]), [schema])
    return {
        "schema": schema,
        "channel": channel,
        "token_enc": token_enc,
        "rules": rules,
        "awarded": awarded,
        "users": users,
        "nation": nation,
    }


def _post_or_preview(
    *,
    token_enc: str,
    channel: str,
    text: str,
    blocks: list[dict],
    dry_run: bool,
    extra: dict,
) -> dict:
    if dry_run:
        return {**extra, "chars": len(text), "dry_run": True, "text": text, "blocks": list(blocks)}
    token = decrypt_field(token_enc)
    client = slack_client(token)
    for chunk in chunk_messages(list(blocks)):
        post_message(client, channel, fallback_text(chunk), blocks=chunk)
    return {**extra, "posted": True, "text": text, "blocks": list(blocks)}


def run_leaderboard_for_region(
    conn,
    pm_schema: str,
    region_row: dict,
    *,
    dry_run: bool = False,
    window: tuple[date, date] | None = None,
    title: str | None = None,
    top_n: int | None = None,
) -> dict:
    loaded = _load_region_award_inputs(conn, pm_schema, region_row, window=window)
    if loaded.get("skipped"):
        return loaded

    known_ids = None
    if not dry_run and loaded["token_enc"]:
        known_ids = workspace_user_ids(slack_client(decrypt_field(loaded["token_enc"])))

    empty = "No awards yet."
    if window is not None:
        from scheduling import format_window_label

        empty = f"No awards in {format_window_label(*window)}."
    text, blocks = build_leaderboard_message(
        loaded["awarded"],
        loaded["users"],
        known_ids=known_ids,
        title=title,
        empty=empty,
        top_n=top_n,
    )
    extra: dict = {}
    if window is not None:
        extra["window_start"] = window[0].isoformat()
        extra["window_end"] = window[1].isoformat()
    return _post_or_preview(
        token_enc=loaded["token_enc"],
        channel=loaded["channel"],
        text=text,
        blocks=blocks,
        dry_run=dry_run,
        extra=extra,
    )


def run_almost_there_for_region(
    conn,
    pm_schema: str,
    region_row: dict,
    *,
    dry_run: bool = False,
    title: str | None = None,
    top_n: int | None = None,
) -> dict:
    loaded = _load_region_award_inputs(
        conn, pm_schema, region_row, load_nation=True
    )
    if loaded.get("skipped"):
        return loaded

    known_ids = None
    if not dry_run and loaded["token_enc"]:
        known_ids = workspace_user_ids(slack_client(decrypt_field(loaded["token_enc"])))

    text, blocks = build_almost_there_message(
        loaded["nation"],
        loaded["rules"],
        loaded["awarded"],
        loaded["schema"],
        loaded["users"],
        known_ids=known_ids,
        title=title,
        top_n=top_n,
    )
    if not text:
        text = f"*{title or 'Almost there'}*\n\nNobody is within striking distance."
        blocks = [section(text)]
    return _post_or_preview(
        token_enc=loaded["token_enc"],
        channel=loaded["channel"],
        text=text,
        blocks=blocks,
        dry_run=dry_run,
        extra={},
    )


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
