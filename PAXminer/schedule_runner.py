"""Resolve destinations and run a scheduled report item."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from common.encryption import decrypt_field
from paxminer_db import connect_from_env
from scheduling import is_due_now, region_local_now
from slack_util import open_dm_channel, post_message, slack_client, upload_file

LOG = logging.getLogger(__name__)


def _parse_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except json.JSONDecodeError:
            return [s]
    return []


def resolve_destinations(
    regional_conn,
    schedule: dict[str, Any],
) -> list[dict[str, str]]:
    """
    Return list of {kind: channel|user, id: str} delivery targets.

    dm_* entries are resolved to DM channel IDs later by the caller that has a Slack client.
    """
    dest_type = schedule.get("destination_type") or ""
    if dest_type == "specific_channels":
        return [{"kind": "channel", "id": cid} for cid in _parse_json_list(schedule.get("destination_channels"))]
    if dest_type == "dm_specific_pax":
        return [{"kind": "user", "id": uid} for uid in _parse_json_list(schedule.get("destination_users"))]
    if dest_type == "all_ao_channels":
        with regional_conn.cursor() as cur:
            cur.execute(
                "SELECT channel_id FROM aos WHERE backblast = 1 AND archived = 0"
            )
            rows = cur.fetchall() or []
        return [{"kind": "channel", "id": r["channel_id"]} for r in rows if r.get("channel_id")]
    if dest_type == "dm_all_pax":
        with regional_conn.cursor() as cur:
            # Exclude app/bot users (app column) and the PAXminer placeholder.
            cur.execute(
                """
                SELECT user_id FROM users
                WHERE COALESCE(app, 0) != 1
                  AND user_id IS NOT NULL AND user_id != ''
                  AND COALESCE(user_name, '') NOT IN ('PAXminer', 'BackblastApp', 'APP')
                """
            )
            rows = cur.fetchall() or []
        return [{"kind": "user", "id": r["user_id"]} for r in rows if r.get("user_id")]
    return []


def mark_schedule_status(conn, pm_schema: str, schedule_id: int, local_date: date, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE `{pm_schema}`.`region_schedules`
            SET last_run_on=%s, last_run_status=%s
            WHERE id=%s
            """,
            (local_date.isoformat(), status, schedule_id),
        )
    conn.commit()


def use_schedule_dispatcher() -> bool:
    return os.environ.get("PM_USE_SCHEDULE_DISPATCHER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _load_definition(conn, pm_schema: str, definition_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM `{pm_schema}`.`region_report_definitions` WHERE id=%s",
            (definition_id,),
        )
        return cur.fetchone()


def _load_region(conn, pm_schema: str, schema_name: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM `{pm_schema}`.`regions` WHERE schema_name=%s LIMIT 1",
            (schema_name,),
        )
        return cur.fetchone()


def run_one_schedule_item(
    registry_conn,
    pm_schema: str,
    schedule: dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Execute a single schedule row. force=True skips due-now check (Run Now)."""
    started = time.time()
    schedule_id = int(schedule["id"])
    schema_name = schedule.get("schema_name") or ""
    region = _load_region(registry_conn, pm_schema, schema_name)
    if not region:
        return {"schedule_id": schedule_id, "ok": False, "error": "region not found"}

    tz_name = region.get("timezone") or "America/Chicago"
    local = region_local_now(tz_name)
    local_date = local.date()

    if not force and not is_due_now(schedule, timezone_name=tz_name):
        return {"schedule_id": schedule_id, "ok": True, "skipped": "not due"}

    definition = _load_definition(registry_conn, pm_schema, int(schedule["report_definition_id"]))
    if not definition:
        return {"schedule_id": schedule_id, "ok": False, "error": "definition not found"}

    report_type = definition.get("report_type") or ""
    LOG.info(
        "schedule run schema=%s schedule_id=%s definition_id=%s report_type=%s",
        schema_name,
        schedule_id,
        definition.get("id"),
        report_type,
    )

    if dry_run:
        return {
            "schedule_id": schedule_id,
            "ok": True,
            "dry_run": True,
            "report_type": report_type,
            "schema": schema_name,
        }

    mark_schedule_status(registry_conn, pm_schema, schedule_id, local_date, "running")
    try:
        result = _dispatch_report(
            registry_conn,
            pm_schema,
            region,
            schedule,
            definition,
        )
        mark_schedule_status(registry_conn, pm_schema, schedule_id, local_date, "success")
        return {
            "schedule_id": schedule_id,
            "ok": True,
            "report_type": report_type,
            "result": result,
            "duration_s": round(time.time() - started, 2),
        }
    except Exception as e:
        LOG.exception(
            "schedule failed schema=%s schedule_id=%s report_type=%s",
            schema_name,
            schedule_id,
            report_type,
        )
        mark_schedule_status(registry_conn, pm_schema, schedule_id, local_date, "error")
        return {
            "schedule_id": schedule_id,
            "ok": False,
            "report_type": report_type,
            "error": str(e),
            "duration_s": round(time.time() - started, 2),
        }


def _dispatch_report(
    registry_conn,
    pm_schema: str,
    region: dict,
    schedule: dict,
    definition: dict,
) -> dict:
    from schedule_reports import run_custom_report  # local import keeps light tests lean

    schema_name = region["schema_name"]
    token_enc = region.get("slack_token")
    if not token_enc:
        raise RuntimeError("missing slack_token")
    token = decrypt_field(token_enc)
    report_type = definition["report_type"]
    plot_dir = os.environ.get("CHART_PLOT_DIR", "/tmp/paxminer_plots")

    regional = connect_from_env(schema_name)
    try:
        targets = resolve_destinations(regional, schedule)
        channel_ids = [t["id"] for t in targets if t["kind"] == "channel"]
        user_ids = [t["id"] for t in targets if t["kind"] == "user"]

        if report_type == "pax_charts":
            from monthly_charts.PAXcharter import run_pax_charter

            return run_pax_charter(
                regional,
                token,
                schema_name,
                plot_dir=plot_dir,
                user_ids=user_ids or None,
            )
        if report_type == "q_charts":
            from monthly_charts.Qcharter import run_q_charter

            return run_q_charter(
                regional,
                token,
                schema_name,
                region.get("region") or schema_name,
                channel_ids[0] if channel_ids else (region.get("firstf_channel") or ""),
                plot_dir=plot_dir,
                destinations=channel_ids or None,
                post_per_ao=(schedule.get("destination_type") == "all_ao_channels"),
            )
        if report_type == "region_leaderboard":
            from monthly_charts.Leaderboard_Charter import run_region_leaderboard

            dest = channel_ids[0] if channel_ids else (region.get("firstf_channel") or "")
            return run_region_leaderboard(
                regional,
                token,
                schema_name,
                region.get("region") or schema_name,
                dest,
                plot_dir=plot_dir,
                destinations=channel_ids or None,
            )
        if report_type == "ao_leaderboard":
            from monthly_charts.LeaderboardByAO_Charter import run_ao_leaderboard

            return run_ao_leaderboard(
                regional,
                token,
                schema_name,
                region.get("region") or schema_name,
                region.get("firstf_channel") or "",
                plot_dir=plot_dir,
                destinations=channel_ids or None,
                post_per_ao=(schedule.get("destination_type") == "all_ao_channels"),
            )
        if report_type == "achievement_leaderboard":
            from achievements.leaderboard import run_leaderboard_for_region

            # Temporarily override achievement_channel for multi-dest delivery.
            if channel_ids:
                region = dict(region)
                region["achievement_channel"] = channel_ids[0]
            result = run_leaderboard_for_region(registry_conn, pm_schema, region)
            client = slack_client(token)
            if len(channel_ids) > 1 and result.get("text"):
                for cid in channel_ids[1:]:
                    try:
                        post_message(client, cid, result["text"], blocks=result.get("blocks"))
                    except Exception:
                        LOG.exception("extra achievement_leaderboard post failed channel=%s", cid)
            return result
        if report_type == "kotter":
            from kotter.kotter_report import run_kotter_for_region

            region = dict(region)
            region["send_aoq_reports"] = 1
            if channel_ids:
                region["kotter_channel"] = channel_ids[0]
            result = run_kotter_for_region(registry_conn, pm_schema, region, dry_run=False)
            client = slack_client(token)
            if len(channel_ids) > 1 and result.get("text"):
                for cid in channel_ids[1:]:
                    try:
                        post_message(client, cid, result["text"], blocks=result.get("blocks"))
                    except Exception:
                        LOG.exception("extra kotter post failed channel=%s", cid)
            return result
        if report_type == "custom_report":
            return run_custom_report(
                regional,
                token,
                schema_name,
                definition,
                channel_ids=channel_ids,
                user_ids=user_ids,
                timezone_name=region.get("timezone"),
                plot_dir=plot_dir,
            )
        raise RuntimeError(f"unknown report_type={report_type}")
    finally:
        regional.close()


def list_due_schedules(conn, pm_schema: str) -> list[dict]:
    """Return enabled schedules that are due now (timezone-aware)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.*, r.timezone AS region_timezone
            FROM `{pm_schema}`.`region_schedules` s
            JOIN `{pm_schema}`.`regions` r ON r.schema_name = s.schema_name
            WHERE s.enabled = 1 AND r.active = 1
            """
        )
        rows = list(cur.fetchall() or [])
    due: list[dict] = []
    for row in rows:
        tz = row.get("region_timezone") or "America/Chicago"
        if is_due_now(row, timezone_name=tz):
            due.append(row)
    return due


def async_invoke_schedule_item(schedule_id: int, *, force: bool = False) -> None:
    """Fan-out: async-invoke ScheduleFunction for one item."""
    import boto3

    fn = os.environ.get("SCHEDULE_FUNCTION_NAME") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not fn:
        raise RuntimeError("SCHEDULE_FUNCTION_NAME not set")
    boto3.client("lambda").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "source": "schedule_fanout",
                "schedule_id": schedule_id,
                "force": force,
            }
        ).encode("utf-8"),
    )
