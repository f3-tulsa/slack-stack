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
from slack_util import open_dm_channel, post_log, post_message, slack_client, upload_file

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


def _result_status(dispatch_result: dict | None) -> str:
    """Map a producer return value to last_run_status."""
    if not isinstance(dispatch_result, dict):
        return "success"
    if dispatch_result.get("skipped"):
        return "skipped"
    if dispatch_result.get("ok") is False or dispatch_result.get("error"):
        return "error"
    if "delivered" in dispatch_result and int(dispatch_result.get("delivered") or 0) == 0:
        return "skipped"
    return "success"


def format_run_result(result: dict) -> tuple[str, list[dict] | None]:
    """Human-readable summary for Run Now result DMs."""
    sid = result.get("schedule_id")
    report_type = result.get("report_type") or "?"
    duration = result.get("duration_s")
    dur = f" ({duration}s)" if duration is not None else ""
    if result.get("dry_run"):
        text = f"Schedule #{sid} ({report_type}): dry run — would run{dur}."
        return text, None
    if result.get("error") and not result.get("ok", True):
        text = f"Schedule #{sid} ({report_type}): *failed*{dur}\n`{str(result.get('error'))[:500]}`"
        return text, None
    skipped = result.get("skipped") or (result.get("result") or {}).get("skipped")
    if skipped:
        text = f"Schedule #{sid} ({report_type}): *skipped* — {skipped}{dur}"
        return text, None
    channels = result.get("channel_count")
    users = result.get("user_count")
    dest_bits = []
    if channels is not None:
        dest_bits.append(f"{channels} channel(s)")
    if users is not None:
        dest_bits.append(f"{users} user DM(s)")
    dest = (", ".join(dest_bits) if dest_bits else "destinations resolved")
    text = f"Schedule #{sid} ({report_type}): *success* — posted to {dest}{dur}."
    return text, None


def format_schedule_log_line(region_name: str, result: dict) -> str:
    """Channel-styled bullet for automatic schedule runs posted to paxminer_logs."""
    sid = result.get("schedule_id")
    report_type = result.get("report_type") or "?"
    duration = result.get("duration_s")
    dur = f" ({duration}s)" if duration is not None else ""
    label = f"- Schedule ({region_name}) #{sid} ({report_type})"
    if result.get("error") and not result.get("ok", True):
        return f"{label}: FAILED - {str(result.get('error'))[:500]}{dur}"
    skipped = result.get("skipped") or (result.get("result") or {}).get("skipped")
    if skipped:
        return f"{label}: skipped - {skipped}{dur}"
    channels = result.get("channel_count")
    users = result.get("user_count")
    dest_bits = []
    if channels is not None:
        dest_bits.append(f"{channels} channel(s)")
    if users is not None:
        dest_bits.append(f"{users} user DM(s)")
    dest = ", ".join(dest_bits) if dest_bits else "destinations resolved"
    return f"{label}: success - posted to {dest}{dur}"


def _post_schedule_outcome_log(region: dict | None, result: dict) -> None:
    """Best-effort paxminer_logs line for an automatic schedule run. Never raises."""
    region_name = "?"
    token_enc = None
    if region:
        region_name = region.get("region") or region.get("schema_name") or "?"
        token_enc = region.get("slack_token")
    if not token_enc:
        token = (os.environ.get("PM_SLACK_TOKEN") or "").strip() or None
        if not token:
            return
    else:
        try:
            token = decrypt_field(token_enc)
        except Exception:
            LOG.debug("schedule log decrypt failed region=%s", region_name, exc_info=True)
            return
    try:
        client = slack_client(token)
        post_log(client, format_schedule_log_line(region_name, result))
    except Exception:
        LOG.debug("schedule outcome log failed region=%s", region_name, exc_info=True)


def notify_run_result(
    region: dict | None,
    user_id: str,
    result: dict,
    *,
    token: str | None = None,
) -> None:
    """DM the requesting admin with the Run Now outcome. Never raises."""
    if not user_id:
        return
    try:
        tok = token
        if not tok and region and region.get("slack_token"):
            tok = decrypt_field(region["slack_token"])
        if not tok:
            tok = (os.environ.get("PM_SLACK_TOKEN") or "").strip() or None
        if not tok:
            LOG.warning("notify_run_result: no Slack token available")
            return
        text, blocks = format_run_result(result)
        client = slack_client(tok)
        dm = open_dm_channel(client, user_id)
        post_message(client, dm, text, blocks=blocks)
    except Exception:
        LOG.exception("notify_run_result failed user_id=%s", user_id)


def run_one_schedule_item(
    registry_conn,
    pm_schema: str,
    schedule: dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
    manual: bool = False,
) -> dict:
    """Execute a single schedule row. force=True skips due-now check (Run Now).

    manual=True (Run Now) skips paxminer_logs; the admin is DMed instead.
    Automatic tick/fan-out runs post an outcome line to paxminer_logs.
    """
    started = time.time()
    schedule_id = int(schedule["id"])
    schema_name = schedule.get("schema_name") or ""
    region = _load_region(registry_conn, pm_schema, schema_name)
    if not region:
        try:
            tz_guess = "America/Chicago"
            mark_schedule_status(
                registry_conn, pm_schema, schedule_id, region_local_now(tz_guess).date(), "error"
            )
        except Exception:
            LOG.exception("mark error status failed schedule_id=%s", schedule_id)
        out = {"schedule_id": schedule_id, "ok": False, "error": "region not found"}
        if not manual and not dry_run:
            _post_schedule_outcome_log(None, out)
        return out

    tz_name = region.get("timezone") or "America/Chicago"
    local = region_local_now(tz_name)
    local_date = local.date()

    if not force and not is_due_now(schedule, timezone_name=tz_name):
        return {"schedule_id": schedule_id, "ok": True, "skipped": "not due"}

    definition = _load_definition(registry_conn, pm_schema, int(schedule["report_definition_id"]))
    if not definition:
        mark_schedule_status(registry_conn, pm_schema, schedule_id, local_date, "error")
        out = {
            "schedule_id": schedule_id,
            "ok": False,
            "error": "definition not found",
        }
        if not manual and not dry_run:
            _post_schedule_outcome_log(region, out)
        return out

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
        status = _result_status(result)
        mark_schedule_status(registry_conn, pm_schema, schedule_id, local_date, status)
        out = {
            "schedule_id": schedule_id,
            "ok": status != "error",
            "report_type": report_type,
            "result": result,
            "status": status,
            "duration_s": round(time.time() - started, 2),
            "channel_count": result.get("channel_count") if isinstance(result, dict) else None,
            "user_count": result.get("user_count") if isinstance(result, dict) else None,
        }
        if status == "skipped":
            out["skipped"] = (result or {}).get("skipped") or "no delivery"
            out["ok"] = True
        if not manual:
            _post_schedule_outcome_log(region, out)
        return out
    except Exception as e:
        LOG.exception(
            "schedule failed schema=%s schedule_id=%s report_type=%s",
            schema_name,
            schedule_id,
            report_type,
        )
        mark_schedule_status(registry_conn, pm_schema, schedule_id, local_date, "error")
        out = {
            "schedule_id": schedule_id,
            "ok": False,
            "report_type": report_type,
            "error": str(e),
            "status": "error",
            "duration_s": round(time.time() - started, 2),
        }
        if not manual:
            _post_schedule_outcome_log(region, out)
        return out


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
    dest_type = schedule.get("destination_type") or ""

    regional = connect_from_env(schema_name)
    try:
        targets = resolve_destinations(regional, schedule)
        channel_ids = [t["id"] for t in targets if t["kind"] == "channel"]
        user_ids = [t["id"] for t in targets if t["kind"] == "user"]

        # Empty configured destinations = skip (do not fall back to "all users" / legacy).
        if dest_type in ("specific_channels", "dm_specific_pax") and not targets:
            return {
                "skipped": "no destinations configured",
                "channel_count": 0,
                "user_count": 0,
            }
        if dest_type == "all_ao_channels" and not channel_ids:
            return {"skipped": "no AO channels found", "channel_count": 0, "user_count": 0}
        if dest_type == "dm_all_pax" and not user_ids:
            return {"skipped": "no PAX users found", "channel_count": 0, "user_count": 0}
        if not targets and dest_type:
            return {
                "skipped": "no destinations configured",
                "channel_count": 0,
                "user_count": 0,
            }

        if report_type == "pax_charts":
            from monthly_charts.PAXcharter import run_pax_charter

            # Pass the list as-is (including empty). Never coerce [] → None (all users).
            result = run_pax_charter(
                regional,
                token,
                schema_name,
                plot_dir=plot_dir,
                user_ids=user_ids,
            )
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("user_count", len(user_ids))
                result.setdefault("channel_count", 0)
            return result
        if report_type == "q_charts":
            from monthly_charts.Qcharter import run_q_charter

            result = run_q_charter(
                regional,
                token,
                schema_name,
                region.get("region") or schema_name,
                channel_ids[0] if channel_ids else "",
                plot_dir=plot_dir,
                destinations=channel_ids,
                post_per_ao=(dest_type == "all_ao_channels"),
            )
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("channel_count", len(channel_ids))
            return result
        if report_type == "region_leaderboard":
            from monthly_charts.Leaderboard_Charter import run_region_leaderboard

            dest = channel_ids[0] if channel_ids else ""
            result = run_region_leaderboard(
                regional,
                token,
                schema_name,
                region.get("region") or schema_name,
                dest,
                plot_dir=plot_dir,
                destinations=channel_ids,
            )
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("channel_count", len(channel_ids))
            return result
        if report_type == "ao_leaderboard":
            from monthly_charts.LeaderboardByAO_Charter import run_ao_leaderboard

            result = run_ao_leaderboard(
                regional,
                token,
                schema_name,
                region.get("region") or schema_name,
                channel_ids[0] if channel_ids else "",
                plot_dir=plot_dir,
                destinations=channel_ids,
                post_per_ao=(dest_type == "all_ao_channels"),
            )
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("channel_count", len(channel_ids))
            return result
        if report_type == "achievement_leaderboard":
            from achievements.leaderboard import run_leaderboard_for_region

            region = dict(region)
            region["send_achievement_leaderboard"] = 1
            if channel_ids:
                region["achievement_channel"] = channel_ids[0]
            result = run_leaderboard_for_region(registry_conn, pm_schema, region)
            client = slack_client(token)
            if len(channel_ids) > 1 and result.get("text"):
                for cid in channel_ids[1:]:
                    try:
                        post_message(client, cid, result["text"], blocks=result.get("blocks"))
                    except Exception:
                        LOG.exception("extra achievement_leaderboard post failed channel=%s", cid)
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("channel_count", len(channel_ids))
            return result
        if report_type == "kotter":
            from kotter.kotter_report import run_kotter_for_region

            region = dict(region)
            region["send_aoq_reports"] = 1
            if channel_ids:
                region["kotter_channel"] = channel_ids[0]
            elif not region.get("kotter_channel"):
                return {
                    "skipped": "no destinations configured",
                    "channel_count": 0,
                    "user_count": 0,
                }
            result = run_kotter_for_region(
                registry_conn,
                pm_schema,
                region,
                dry_run=False,
                emit_paxminer_log=False,
            )
            client = slack_client(token)
            if len(channel_ids) > 1 and result.get("text"):
                for cid in channel_ids[1:]:
                    try:
                        post_message(client, cid, result["text"], blocks=result.get("blocks"))
                    except Exception:
                        LOG.exception("extra kotter post failed channel=%s", cid)
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("channel_count", len(channel_ids) or (1 if region.get("kotter_channel") else 0))
            return result
        if report_type == "custom_report":
            result = run_custom_report(
                regional,
                token,
                schema_name,
                definition,
                channel_ids=channel_ids,
                user_ids=user_ids,
                timezone_name=region.get("timezone"),
                plot_dir=plot_dir,
            )
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("channel_count", len(channel_ids))
                result.setdefault("user_count", len(user_ids))
            return result
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
