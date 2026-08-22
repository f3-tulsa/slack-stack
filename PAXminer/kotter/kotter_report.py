"""Monthly Kotter report — channel delivery only."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from achievements.attendance import attach_home_regions
from common.encryption import decrypt_field
from slack_blocks import chunk_messages, chunk_sections, fallback_text, header, section
from slack_util import mention, post_message, slack_client, workspace_user_ids

LOG = logging.getLogger(__name__)


def _kotter_nation_sql(schema: str) -> str:
    """Load Kotter attendance for one regional schema.

    TODO: union external_attendance from brother regions when Nation API sync exists.
    """
    return f"""
    SELECT u.email, u.user_id, u.user_name, a.ao_id, ao.ao, b.bd_date AS date,
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


def _row_mention(row, known_ids: set[str] | None = None) -> str:
    name = row["user_name"] if "user_name" in row.index else None
    return mention(row["user_id"], name, known_ids=known_ids)


def build_kotter_message(
    df_mia: pd.DataFrame,
    df_lowq: pd.DataFrame,
    df_noq: pd.DataFrame,
    *,
    known_ids: set[str] | None = None,
) -> tuple[str, list[dict]]:
    intro = "Howdy! This is your monthly PAXMiner Kotter report. According to my records..."
    body_lines: list[str] = []
    if not df_mia.empty:
        body_lines.append("\n\nThe following men haven't posted in a while.")
        for _, row in df_mia.iterrows():
            body_lines.append(f"\n{_row_mention(row, known_ids)} last posted {row['date']}")
    if not df_lowq.empty:
        body_lines.append("\n\nThese guys haven't Q'd in a while. Here's how many days it's been:")
        today = date.today()
        for _, row in df_lowq.iterrows():
            days = (today - pd.to_datetime(row["date"]).date()).days
            body_lines.append(f"\n{_row_mention(row, known_ids)}: {days} days!")
    if not df_noq.empty:
        body_lines.append("\n\nThese guys have never been Q:")
        for _, row in df_noq.iterrows():
            body_lines.append(f"\n{_row_mention(row, known_ids)}")
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
) -> dict:
    del pm_schema  # retained for call-site compatibility; attendance is single-region now
    schema = region_row.get("schema_name")
    channel = region_row.get("kotter_channel")
    token_enc = region_row.get("slack_token")
    if not schema or not channel or not token_enc:
        return {"skipped": "missing schema, kotter_channel, or token"}

    no_post = int(region_row.get("NO_POST_THRESHOLD") or 2)
    reminder = int(region_row.get("REMINDER_WEEKS") or 2)
    home_ao_capture = int(region_row.get("HOME_AO_CAPTURE") or 8)
    no_q_weeks = int(region_row.get("NO_Q_THRESHOLD_WEEKS") or 4)
    no_q_posts = int(region_row.get("NO_Q_THRESHOLD_POSTS") or 4)

    # Single-region only; cross-region / down-range attendance needs the F3 Nation API.
    schemas = [schema]
    nation_parts = []
    first_error: Exception | None = None
    for s in schemas:
        try:
            from paxminer_db import read_sql_df

            df = read_sql_df(conn, _kotter_nation_sql(s))
            LOG.info("kotter attendance schema=%s rows=%s", s, len(df))
            if df.empty:
                continue
            df["region"] = s
            nation_parts.append(df)
        except Exception as e:
            LOG.error("kotter nation schema=%s: %s", s, e)
            if first_error is None:
                first_error = e
    if not nation_parts:
        if first_error is not None:
            return {"error": f"attendance query failed: {first_error}"}
        return {"skipped": f"no attendance rows for {schema}"}
    nation = pd.concat(nation_parts, ignore_index=True)
    if "date" not in nation.columns or nation.empty:
        if first_error is not None:
            return {"error": f"attendance query failed: {first_error}"}
        return {"skipped": f"no attendance rows for {schema}"}
    raw_dates = nation["date"].copy()
    nation["date"] = pd.to_datetime(nation["date"], errors="coerce")
    bad = int(nation["date"].isna().sum())
    if bad:
        sample = raw_dates.loc[nation["date"].isna()].head(5).tolist()
        LOG.warning(
            "Dropping %s kotter rows with unparseable bd_date (sample=%r)",
            bad,
            sample,
        )
        nation = nation[nation["date"].notna()].copy()
    if nation.empty:
        if first_error is not None:
            return {"error": f"attendance query failed: {first_error}"}
        return {"skipped": f"no attendance rows for {schema}"}

    home = attach_home_regions(conn, nation.copy(), schemas)
    if "user_id_y" in home.columns:
        home = home.rename(columns={"user_id_y": "user_id"}).drop(columns=["user_id_x"], errors="ignore")
    if "user_name_y" in home.columns:
        home = home.rename(columns={"user_name_y": "user_name"}).drop(
            columns=["user_name_x"], errors="ignore"
        )
    df = home[home["region"] == schema].copy()

    recent = df[df["date"] > pd.Timestamp(date.today() - timedelta(weeks=home_ao_capture))]
    home_ao = (
        recent.groupby("email")
        .agg(ao_count=("ao_id", "count"), home_ao=("ao_id", "last"))
        .reset_index()
    )
    df = df.merge(home_ao[["email", "home_ao"]], on="email", how="left")

    today = date.today()
    group_cols = ["email", "user_id", "home_ao"]
    if "user_name" in df.columns:
        group_cols = ["email", "user_id", "user_name", "home_ao"]
    mia = (
        df.groupby(group_cols, as_index=False, dropna=False)["date"]
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
        .groupby(group_cols, as_index=False, dropna=False)["date"]
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
    noq_cols = ["user_id"] + (["user_name"] if "user_name" in df.columns else [])
    noq = noq[
        noq["date"].dt.date.between(
            today - timedelta(weeks=reminder),
            today - timedelta(weeks=no_q_weeks),
        )
    ][noq_cols].drop_duplicates()
    noq = noq[~noq["user_id"].isin(mia["user_id"]) & ~noq["user_id"].isin(lowq["user_id"])]

    known_ids = None
    if not dry_run:
        token = decrypt_field(token_enc)
        client = slack_client(token)
        known_ids = workspace_user_ids(client)
    else:
        client = None
        token = None

    text, blocks = build_kotter_message(mia, lowq, noq, known_ids=known_ids)
    if mia.empty and lowq.empty and noq.empty:
        active = "Everyone looks active this month!"
        text = f"{text}\n\n{active}"
        blocks = list(blocks) + [section(active)]
    if dry_run:
        return {"chars": len(text), "dry_run": True, "text": text, "blocks": blocks}

    assert client is not None
    for chunk in chunk_messages(blocks) or [[]]:
        post_message(
            client,
            channel,
            fallback_text(chunk) if chunk else text,
            blocks=chunk or None,
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
