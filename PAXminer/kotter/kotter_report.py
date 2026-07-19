"""Monthly Kotter report — channel delivery only."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from achievements.attendance import attach_home_regions, load_nation_attendance
from common.encryption import decrypt_field
from slack_blocks import chunk_messages, chunk_sections, fallback_text, header, section
from slack_util import post_log, post_message, slack_client

LOG = logging.getLogger(__name__)


def _kotter_nation_sql(schema: str) -> str:
    """Load Kotter attendance for one regional schema.

    TODO: union external_attendance from brother regions when Nation API sync exists.
    """
    return f"""
    SELECT u.email, u.user_id, a.ao_id, ao.ao, b.bd_date AS date,
           CASE WHEN (a.user_id = b.q_user_id OR a.user_id = b.coq_user_id) THEN 1 ELSE 0 END AS q_flag
    FROM `{schema}`.users u
    JOIN `{schema}`.bd_attendance a ON a.user_id = u.user_id
    JOIN `{schema}`.beatdowns b ON (
        (a.q_user_id = b.q_user_id OR a.q_user_id = b.coq_user_id)
        AND a.ao_id = b.ao_id AND a.date = b.bd_date
    )
    JOIN `{schema}`.aos ao ON b.ao_id = ao.channel_id
    WHERE b.bd_date > 0 AND b.bd_date <= CURDATE()
      AND u.email != 'none' AND u.user_name != 'PAXminer'
      AND b.q_user_id IS NOT NULL
    """


def build_kotter_message(
    df_mia: pd.DataFrame, df_lowq: pd.DataFrame, df_noq: pd.DataFrame
) -> tuple[str, list[dict]]:
    intro = "Howdy! This is your monthly PAXMiner Kotter report. According to my records..."
    body_lines: list[str] = []
    if not df_mia.empty:
        body_lines.append("\n\nThe following men haven't posted in a while.")
        for _, row in df_mia.iterrows():
            body_lines.append(f"\n- <@{row['user_id']}> last posted {row['date']}")
    if not df_lowq.empty:
        body_lines.append("\n\nThese guys haven't Q'd in a while. Here's how many days it's been:")
        today = date.today()
        for _, row in df_lowq.iterrows():
            days = (today - pd.to_datetime(row["date"]).date()).days
            body_lines.append(f"\n- <@{row['user_id']}>: {days} days!")
    if not df_noq.empty:
        body_lines.append("\n\nThese guys have never been Q:")
        for _, row in df_noq.iterrows():
            body_lines.append(f"\n- <@{row['user_id']}>")
    text = intro + "".join(body_lines)
    blocks: list[dict] = [header("Monthly Kotter Report"), section(intro)]
    if body_lines:
        blocks.extend(chunk_sections(["".join(body_lines).lstrip("\n")]))
    return text, blocks


def run_kotter_for_region(
    conn,
    pm_schema: str,
    region_row: dict,
    *,
    dry_run: bool = False,
    emit_paxminer_log: bool = True,
) -> dict:
    if not region_row.get("send_aoq_reports"):
        return {"skipped": "send_aoq_reports off"}
    schema = region_row.get("schema_name")
    channel = region_row.get("kotter_channel")
    token_enc = region_row.get("slack_token")
    if not schema or not channel or not token_enc:
        return {"skipped": "missing schema, kotter_channel, or token"}
    region_name = region_row.get("region") or schema

    no_post = int(region_row.get("NO_POST_THRESHOLD") or 2)
    reminder = int(region_row.get("REMINDER_WEEKS") or 2)
    home_ao_capture = int(region_row.get("HOME_AO_CAPTURE") or 8)
    no_q_weeks = int(region_row.get("NO_Q_THRESHOLD_WEEKS") or 4)
    no_q_posts = int(region_row.get("NO_Q_THRESHOLD_POSTS") or 4)

    with conn.cursor() as cur:
        cur.execute(f"SELECT schema_name FROM `{pm_schema}`.`regions` WHERE active=1 AND schema_name LIKE 'f3%%'")
        schemas = [r["schema_name"] for r in cur.fetchall() if r.get("schema_name")]

    nation_parts = []
    for s in schemas:
        try:
            df = pd.read_sql(_kotter_nation_sql(s), conn)
            df["region"] = s
            nation_parts.append(df)
        except Exception as e:
            LOG.error("kotter nation schema=%s: %s", s, e)
    if not nation_parts:
        return {"skipped": "no nation data"}
    nation = pd.concat(nation_parts, ignore_index=True)
    nation["date"] = pd.to_datetime(nation["date"])

    home = attach_home_regions(conn, nation.copy(), schemas)
    if "user_id_y" in home.columns:
        home = home.rename(columns={"user_id_y": "user_id"}).drop(columns=["user_id_x"], errors="ignore")
    df = home[home["region"] == schema].copy()

    recent = df[df["date"] > pd.Timestamp(date.today() - timedelta(weeks=home_ao_capture))]
    home_ao = (
        recent.groupby("email")
        .agg(ao_count=("ao_id", "count"), home_ao=("ao_id", "last"))
        .reset_index()
    )
    df = df.merge(home_ao[["email", "home_ao"]], on="email", how="left")

    today = date.today()
    mia = (
        df.groupby(["email", "user_id", "home_ao"], as_index=False)["date"]
        .max()
        .assign(date=lambda x: x["date"].dt.date)
    )
    mia = mia[
        mia["date"].between(
            today - timedelta(weeks=reminder),
            today - timedelta(weeks=no_post),
        )
    ].sort_values("date", ascending=False)
    mia["date"] = pd.to_datetime(mia["date"]).dt.strftime("%B %d, %Y")

    lowq = (
        df[df["q_flag"] == 1]
        .groupby(["email", "user_id", "home_ao"], as_index=False)["date"]
        .max()
    )
    lowq = lowq[
        lowq["date"].dt.date.between(
            today - timedelta(weeks=reminder),
            today - timedelta(weeks=no_q_posts),
        )
    ]
    lowq = lowq[~lowq["user_id"].isin(mia["user_id"])].sort_values("date", ascending=False)

    posted = df.groupby(["email", "user_id"], as_index=False).agg(q_sum=("q_flag", "sum"))
    never_q = posted[posted["q_sum"] == 0]["email"]
    noq = df[df["email"].isin(never_q)]
    noq = noq[
        noq["date"].dt.date.between(
            today - timedelta(weeks=reminder),
            today - timedelta(weeks=no_q_weeks),
        )
    ][["user_id"]].drop_duplicates()
    noq = noq[~noq["user_id"].isin(mia["user_id"]) & ~noq["user_id"].isin(lowq["user_id"])]

    text, blocks = build_kotter_message(mia, lowq, noq)
    if mia.empty and lowq.empty and noq.empty:
        active = "Everyone looks active this month!"
        text = f"{text}\n\n{active}"
        blocks = list(blocks) + [section(active)]
    if dry_run:
        return {"chars": len(text), "dry_run": True, "text": text, "blocks": blocks}

    token = decrypt_field(token_enc)
    client = slack_client(token)
    for chunk in chunk_messages(blocks) or [[]]:
        post_message(
            client,
            channel,
            fallback_text(chunk) if chunk else text,
            blocks=chunk or None,
        )
    if emit_paxminer_log:
        post_log(
            client,
            (
                f"- Kotter report ({region_name}): posted to <#{channel}> "
                f"({len(mia)} MIA, {len(lowq)} low-Q, {len(noq)} no-Q)"
            ),
        )
    return {
        "posted": True,
        "channel": channel,
        "text": text,
        "blocks": blocks,
        "mia_count": len(mia),
        "lowq_count": len(lowq),
        "noq_count": len(noq),
    }


def run_kotter(conn, pm_schema: str, *, region_filter: str | None = None, dry_run: bool = False) -> list[dict]:
    results = []
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{pm_schema}`.`regions` WHERE active=1")
        regions = cur.fetchall()
    for row in regions:
        if region_filter and row.get("region") != region_filter and row.get("schema_name") != region_filter:
            continue
        try:
            r = run_kotter_for_region(conn, pm_schema, row, dry_run=dry_run)
            results.append({"region": row["region"], **r})
        except Exception as e:
            LOG.exception("kotter region=%s", row.get("region"))
            results.append({"region": row.get("region"), "error": str(e)})
            _post_kotter_failure_log(row, e)
    return results


def _post_kotter_failure_log(region_row: dict, exc: Exception) -> None:
    """Best-effort failure line to paxminer_logs. Never raises."""
    region_name = region_row.get("region") or region_row.get("schema_name") or "?"
    token_enc = region_row.get("slack_token")
    if not token_enc:
        return
    try:
        token = decrypt_field(token_enc)
        client = slack_client(token)
        post_log(client, f"- Kotter report ({region_name}): FAILED - {exc}")
    except Exception:
        LOG.debug("kotter failure log skipped region=%s", region_name, exc_info=True)
