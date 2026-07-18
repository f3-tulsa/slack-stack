"""Generic custom report runner (chart PNG or Block Kit table)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from scheduling import ALLOWED_SOURCES, resolve_time_window
from slack_blocks import chunk_messages, chunk_sections, fallback_text, header, section
from slack_util import open_dm_channel, post_message, slack_client, upload_file

LOG = logging.getLogger(__name__)

_ALLOWED_FIELDS = {
    "Date",
    "AO",
    "PAX",
    "Q",
    "CoQ",
    "pax_count",
    "fng_count",
    "posts",
    "distinct_aos",
}

_SOURCE_SQL = {
    "bd_attendance": """
        SELECT
            bd.date AS Date,
            ao.ao AS AO,
            u.user_name AS PAX,
            u.user_id AS user_id,
            bd.ao_id AS ao_id
        FROM bd_attendance bd
        LEFT JOIN aos ao ON bd.ao_id = ao.channel_id
        LEFT JOIN users u ON bd.user_id = u.user_id
        WHERE COALESCE(u.app, 0) != 1
          AND bd.date BETWEEN %s AND %s
    """,
    "beatdowns": """
        SELECT
            B.bd_date AS Date,
            a.ao AS AO,
            U1.user_name AS Q,
            U2.user_name AS CoQ,
            B.pax_count AS pax_count,
            B.fng_count AS fng_count,
            B.ao_id AS ao_id
        FROM beatdowns B
        LEFT JOIN users U1 ON U1.user_id = B.q_user_id
        LEFT JOIN users U2 ON U2.user_id = B.coq_user_id
        LEFT JOIN aos a ON a.channel_id = B.ao_id
        WHERE B.bd_date BETWEEN %s AND %s
          AND COALESCE(U1.app, 0) != 1
    """,
    "attendance_view": """
        SELECT Date, AO, PAX
        FROM attendance_view
        WHERE Date BETWEEN %s AND %s
    """,
}


def _parse_fields(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x) in _ALLOWED_FIELDS]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if str(x) in _ALLOWED_FIELDS]
        except json.JSONDecodeError:
            return [x.strip() for x in raw.split(",") if x.strip() in _ALLOWED_FIELDS]
    return []


def _aggregate(df: pd.DataFrame, definition: dict) -> pd.DataFrame:
    metric = (definition.get("metric") or "posts").strip()
    group_by = (definition.get("group_by") or "PAX").strip()
    top_n = int(definition.get("top_n") or 20)
    if group_by not in df.columns:
        # Fallbacks
        for candidate in ("PAX", "Q", "AO"):
            if candidate in df.columns:
                group_by = candidate
                break
        else:
            return df.head(top_n)

    if metric == "distinct_aos" and "AO" in df.columns:
        out = (
            df.groupby(group_by, as_index=False)
            .agg(value=("AO", "nunique"))
            .rename(columns={"value": "distinct_aos"})
            .sort_values("distinct_aos", ascending=False)
        )
    elif metric in df.columns and pd.api.types.is_numeric_dtype(df[metric]):
        out = (
            df.groupby(group_by, as_index=False)
            .agg(value=(metric, "sum"))
            .rename(columns={"value": metric})
            .sort_values(metric, ascending=False)
        )
    else:
        out = (
            df.groupby(group_by, as_index=False)
            .size()
            .rename(columns={"size": "posts"})
            .sort_values("posts", ascending=False)
        )
    return out.head(max(top_n, 1))


def _table_blocks(title: str, frame: pd.DataFrame) -> tuple[str, list[dict]]:
    if frame.empty:
        text = f"{title}\n_No data for this window._"
        return text, [header(title[:150]), section("_No data for this window._")]
    lines = [" | ".join(str(c) for c in frame.columns)]
    for _, row in frame.iterrows():
        lines.append(" | ".join(str(row[c]) for c in frame.columns))
    body = "\n".join(lines)
    blocks = [header(title[:150])]
    blocks.extend(chunk_sections([f"```{body}```"]))
    return fallback_text(blocks) or title, blocks


def run_custom_report(
    regional_conn,
    slack_token: str,
    schema: str,
    definition: dict,
    *,
    channel_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    timezone_name: str | None = None,
    plot_dir: str | Path = "/tmp/paxminer_plots",
) -> dict:
    source = (definition.get("source") or "bd_attendance").strip()
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"source not allowed: {source}")
    sql = _SOURCE_SQL[source]
    start, end = resolve_time_window(definition, timezone_name=timezone_name)
    df = pd.read_sql(sql, regional_conn, params=(start.isoformat(), end.isoformat()))
    fields = _parse_fields(definition.get("fields"))
    if fields:
        keep = [c for c in fields if c in df.columns]
        if keep:
            df = df[keep]

    agg = _aggregate(df, definition)
    kind = (definition.get("kind") or "table").strip()
    title = definition.get("name") or "Custom report"
    client = slack_client(slack_token)
    delivered = 0

    delivery_channels: list[str] = list(channel_ids or [])
    for uid in user_ids or []:
        try:
            delivery_channels.append(open_dm_channel(client, uid))
        except Exception:
            LOG.exception("custom report DM open failed user=%s", uid)

    if kind == "chart":
        plot_base = Path(plot_dir) / schema
        plot_base.mkdir(parents=True, exist_ok=True)
        if agg.empty:
            text, blocks = _table_blocks(title, agg)
            for ch in delivery_channels:
                try:
                    post_message(client, ch, text, blocks=blocks)
                    delivered += 1
                except Exception:
                    LOG.exception("custom report empty post failed channel=%s", ch)
            return {"kind": "chart", "rows": 0, "delivered": delivered}

        value_col = [c for c in agg.columns if c != agg.columns[0]][-1]
        label_col = agg.columns[0]
        ax = agg.plot.bar(x=label_col, y=value_col, legend=False)
        ax.set_title(title)
        out = plot_base / f"custom_{definition.get('id')}_{start}_{end}.jpg"
        plt.savefig(str(out), bbox_inches="tight")
        plt.close("all")
        comment = f"{title} ({start} → {end})"
        for ch in delivery_channels:
            try:
                upload_file(client, ch, str(out), initial_comment=comment)
                delivered += 1
            except Exception:
                LOG.exception("custom report chart upload failed channel=%s", ch)
        return {"kind": "chart", "rows": len(agg), "delivered": delivered, "file": str(out)}

    text, blocks = _table_blocks(f"{title} ({start} → {end})", agg)
    for ch in delivery_channels:
        try:
            for chunk in chunk_messages(blocks) or [[]]:
                post_message(
                    client,
                    ch,
                    fallback_text(chunk) if chunk else text,
                    blocks=chunk or None,
                )
            delivered += 1
        except Exception:
            LOG.exception("custom report table post failed channel=%s", ch)
    return {"kind": "table", "rows": len(agg), "delivered": delivered}
