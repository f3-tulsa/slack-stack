"""Resolve destinations and run a scheduled report item."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from common.encryption import decrypt_field
from paxminer_db import connect_from_env
from scheduling import (
    destination_descriptor,
    format_iso_range,
    format_report_title,
    is_due_now,
    region_local_now,
    resolve_time_window,
    template_for,
    template_has,
)
from slack_util import (
    format_log_message,
    is_slack_user_id,
    open_dm_channel,
    post_log,
    post_message,
    slack_client,
    slack_display_name,
    ticked_display_name,
    upload_file,
)

LOG = logging.getLogger(__name__)

# Cached per-process: None = unknown, True/False after the first write attempt.
_HAS_LAST_RUN_AT: bool | None = None


def reset_last_run_at_probe() -> None:
    """Clear the last_run_at column cache (tests)."""
    global _HAS_LAST_RUN_AT
    _HAS_LAST_RUN_AT = None


def _is_unknown_last_run_at(exc: BaseException) -> bool:
    code = exc.args[0] if exc.args else None
    return code == 1054 or "unknown column" in str(exc).lower()


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
        out: list[dict[str, str]] = []
        for uid in _parse_json_list(schedule.get("destination_users")):
            if not is_slack_user_id(uid):
                LOG.info("Skip dm_specific_pax non-Slack user_id=%s", uid)
                continue
            out.append({"kind": "user", "id": uid})
        return out
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
        out: list[dict[str, str]] = []
        for r in rows:
            uid = r.get("user_id")
            if not uid:
                continue
            if not is_slack_user_id(uid):
                LOG.info("Skip dm_all_pax target non-Slack user_id=%s", uid)
                continue
            out.append({"kind": "user", "id": uid})
        return out
    return []


def mark_schedule_status(
    conn,
    pm_schema: str,
    schedule_id: int,
    local_date: date,
    status: str,
    *,
    local_dt: datetime | None = None,
) -> None:
    """Record run outcome. ``local_dt`` (region wall time) populates last_run_at for hourly.

    Tolerates a missing ``last_run_at`` column (MySQL 1054) so a deploy-before-migration
    tick still records ``last_run_on`` / ``last_run_status`` instead of 500ing the region.
    """
    global _HAS_LAST_RUN_AT
    run_at = local_dt.replace(tzinfo=None) if local_dt is not None else None
    write_at = run_at is not None and _HAS_LAST_RUN_AT is not False
    with conn.cursor() as cur:
        if write_at:
            try:
                cur.execute(
                    f"""
                    UPDATE `{pm_schema}`.`region_schedules`
                    SET last_run_on=%s, last_run_at=%s, last_run_status=%s
                    WHERE id=%s
                    """,
                    (
                        local_date.isoformat(),
                        run_at.strftime("%Y-%m-%d %H:%M:%S"),
                        status,
                        schedule_id,
                    ),
                )
                _HAS_LAST_RUN_AT = True
            except Exception as exc:
                if not _is_unknown_last_run_at(exc):
                    raise
                _HAS_LAST_RUN_AT = False
                LOG.warning("region_schedules.last_run_at missing; writing without it")
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.execute(
                    f"""
                    UPDATE `{pm_schema}`.`region_schedules`
                    SET last_run_on=%s, last_run_status=%s
                    WHERE id=%s
                    """,
                    (local_date.isoformat(), status, schedule_id),
                )
        else:
            cur.execute(
                f"""
                UPDATE `{pm_schema}`.`region_schedules`
                SET last_run_on=%s, last_run_status=%s
                WHERE id=%s
                """,
                (local_date.isoformat(), status, schedule_id),
            )
    conn.commit()


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


def _format_dest_lines(
    posted: list | None,
    failed: list | None,
    *,
    max_chars: int = 1200,
    id_key: str = "channel_id",
    name_key: str = "ao",
) -> list[str]:
    """Compact posted/failed destination lines; cap length for Slack section limits."""
    lines: list[str] = []

    def _fmt_items(items: list, *, with_reason: bool) -> str:
        parts: list[str] = []
        shown = 0
        for item in items:
            name = item.get(name_key) or item.get("pax") or "?"
            cid = item.get(id_key) or item.get("user_id") or "?"
            if with_reason:
                reason = str(item.get("reason") or "?")[:80]
                part = f"{name} ({cid}) - {reason}"
            else:
                part = f"{name} ({cid})"
            parts.append(part)
            shown += 1
            joined = ", ".join(parts)
            if len(joined) > max_chars:
                overflow = len(items) - shown + 1
                parts = parts[:-1]
                if overflow > 0:
                    parts.append(f"+{overflow} more")
                break
        return ", ".join(parts)

    if posted:
        lines.append(f"posted: {_fmt_items(posted, with_reason=False)}")
    if failed:
        lines.append(f"failed: {_fmt_items(failed, with_reason=True)}")
    return lines


def _apply_delivery_result(
    result: dict,
    *,
    attempted_channels: int = 0,
    attempted_users: int = 0,
) -> dict:
    """Prefer producer posted counts over attempted destination counts."""
    out = dict(result)
    posted = out.get("posted_channels") or []
    failed = out.get("failed_channels") or []
    posted_users = out.get("posted_users") or []
    failed_users = out.get("failed_users") or []

    if "posted_channels" in out or "failed_channels" in out:
        out["channel_count"] = len(posted)
        out["attempted_count"] = attempted_channels or (len(posted) + len(failed))
        if not posted and failed:
            out["ok"] = False
            reasons = "; ".join(
                f"{f.get('ao') or f.get('channel_id')}: {f.get('reason')}" for f in failed[:5]
            )
            out.setdefault("error", f"all channel uploads failed: {reasons}"[:500])
    elif "channel_count" not in out:
        out["channel_count"] = attempted_channels

    if "posted_users" in out or "failed_users" in out:
        out["user_count"] = len(posted_users)
        if not posted_users and failed_users and not posted:
            out["ok"] = False
            reasons = "; ".join(
                f"{f.get('pax') or f.get('user_id')}: {f.get('reason')}" for f in failed_users[:5]
            )
            out.setdefault("error", f"all user DMs failed: {reasons}"[:500])
    elif "user_count" not in out:
        out["user_count"] = attempted_users

    return out


def format_run_result(result: dict) -> tuple[str, list[dict] | None]:
    """Alias of the paxminer_logs formatter (Run Now no longer DMs the admin)."""
    return format_schedule_log_line("", result), None


def _report_title(name: str) -> str:
    """Strip a trailing ' report' so builtin names don't read 'The *Kotter report* report'."""
    title = (name or "report").strip() or "report"
    if title.lower().endswith(" report"):
        stripped = title[: -len(" report")].rstrip()
        if stripped:
            return stripped
    return title


def _channel_link(channel_id: Any) -> str | None:
    cid = str(channel_id or "").strip()
    if not cid:
        return None
    return f"<#{cid}>" if cid.startswith("C") else cid


def _posted_channel_links(result: dict, nested: dict) -> list[str]:
    posted = result.get("posted_channels") or nested.get("posted_channels") or []
    links: list[str] = []
    extra = 0
    for item in posted:
        cid = item.get("channel_id") if isinstance(item, dict) else item
        tag = _channel_link(cid)
        if not tag:
            continue
        candidate = " ".join(links + [tag])
        if len(candidate) > 2800:
            extra += 1
        else:
            links.append(tag)
    if not links:
        for cid in result.get("specified_channels") or []:
            tag = _channel_link(cid)
            if not tag:
                continue
            candidate = " ".join(links + [tag])
            if len(candidate) > 2800:
                extra += 1
            else:
                links.append(tag)
    if extra:
        links.append(f"+{extra} more")
    return links


def _posted_user_ticks(result: dict, nested: dict) -> list[str]:
    posted = result.get("posted_users") or nested.get("posted_users") or []
    ticks: list[str] = []
    extra = 0
    for item in posted:
        if isinstance(item, dict):
            label = (item.get("pax") or item.get("user_id") or "").strip()
        else:
            label = str(item).strip()
        if not label:
            continue
        tag = ticked_display_name(label, fallback="PAX")
        candidate = " ".join(ticks + [tag])
        if len(candidate) > 2800:
            extra += 1
        else:
            ticks.append(tag)
    if extra:
        ticks.append(f"+{extra} more")
    return ticks


def _ensure_outcome_fields(result: dict, report_type: str) -> dict:
    """Fill optional envelope keys producers may omit. Does not switch the logger."""
    out = dict(result)
    if not out.get("results_line"):
        if "grants" in out or "revokes" in out:
            rules = out.get("rules")
            if rules is None:
                rules = out.get("rule_count")
            grants = int(out.get("grants") or 0)
            revokes = int(out.get("revokes") or 0)
            held = int(out.get("held") or 0)
            if rules is not None:
                out["results_line"] = (
                    f"{int(rules)} rules, {grants} granted, {revokes} revoked, {held} held"
                )
        elif "mia_count" in out or "lowq_count" in out or "noq_count" in out:
            out["results_line"] = (
                f"{int(out.get('mia_count') or 0)} MIA, "
                f"{int(out.get('lowq_count') or 0)} low-Q, "
                f"{int(out.get('noq_count') or 0)} never-Q"
            )
        elif out.get("kind") == "chart":
            delivered = int(out.get("delivered") or 0)
            out["results_line"] = (
                f"{delivered} charts posted" if delivered else f"{int(out.get('rows') or 0)} rows"
            )
        elif out.get("kind") == "table":
            out["results_line"] = f"{int(out.get('rows') or 0)} rows"
        else:
            n = out.get("graphs") or out.get("ao_charts") or out.get("channel_count") or out.get("user_count")
            if n:
                noun = "chart" if int(n) == 1 else "charts"
                out["results_line"] = f"{int(n)} {noun} posted"
    if not out.get("period_start"):
        out["period_start"] = out.get("window_start")
    if not out.get("period_end"):
        out["period_end"] = out.get("window_end")
    return out


def format_schedule_log_line(region_name: str, result: dict) -> str:
    """Reusable schedule-run outcome for paxminer_logs. Independent of report_type."""
    nested = result.get("result") if isinstance(result.get("result"), dict) else {}
    name = result.get("definition_name") or nested.get("definition_name") or result.get("report_type") or "report"
    notify_user = (result.get("notify_user") or nested.get("notify_user") or "").strip()
    operator = (result.get("operator_name") or "").strip() or notify_user
    if result.get("manual") or notify_user:
        trigger = (
            f"was run manually by {ticked_display_name(operator)}"
            if operator
            else "was run manually"
        )
    else:
        trigger = "was run as scheduled"
    header = f"The *{_report_title(str(name))}* report {trigger}"

    skipped = result.get("skipped") or nested.get("skipped")
    error = result.get("error") or nested.get("error")
    ok = result.get("ok", True)
    status_name = result.get("status")
    if skipped and ok:
        status = "skipped"
        detail = str(skipped)
    elif error or not ok or status_name in ("error", "failed"):
        status = "failed"
        detail = str(error or "failed")[:500]
    else:
        status = "success"
        detail = ""

    duration = result.get("duration_s")
    if duration is None:
        duration = nested.get("duration_s")
    try:
        dur_s = float(duration)
    except (TypeError, ValueError):
        dur_s = 0.0

    inner = nested if nested else result
    messages = int(inner.get("channel_count") or 0) + int(inner.get("user_count") or 0)
    if "message_count" in result and result["message_count"] is not None:
        messages = int(result["message_count"])

    dest_type = result.get("destination_type") or nested.get("destination_type")
    meta = destination_descriptor(dest_type)
    label = result.get("destination_label") or nested.get("destination_label") or meta["label"]
    expansion = (
        result.get("destination_expansion")
        or nested.get("destination_expansion")
        or meta["expansion"]
    )
    kind = result.get("destination_kind") or nested.get("destination_kind") or meta["kind"]
    if not dest_type:
        specified = result.get("specified_channels") or []
        posted_ch = result.get("posted_channels") or nested.get("posted_channels") or []
        posted_u = result.get("posted_users") or nested.get("posted_users") or []
        if specified or posted_ch:
            expansion, kind, label = "specific", "channel", label if dest_type else "Specific channels"
        elif posted_u:
            expansion, kind, label = "specific", "dm", label if dest_type else "DM to specific PAX"

    if messages <= 0:
        dest = "none"
    elif expansion == "computed":
        dest = label
    elif kind == "dm":
        ticks = _posted_user_ticks(result, nested)
        dest = f"{label} ({' '.join(ticks)})" if ticks else label
    else:
        links = _posted_channel_links(result, nested)
        dest = " ".join(links) if links else label

    results_line = (result.get("results_line") or nested.get("results_line") or "").strip()
    period = format_iso_range(
        result.get("period_start") or nested.get("period_start") or result.get("window_start") or nested.get("window_start"),
        result.get("period_end") or nested.get("period_end") or result.get("window_end") or nested.get("window_end"),
    )

    fields: list[tuple[str, str]] = []
    if results_line:
        fields.append(("Results", results_line))
    if period:
        fields.append(("Period", period))
    return format_log_message(
        header,
        status=status,
        duration_s=dur_s,
        detail=detail if status != "success" else None,
        fields=fields,
        message_count=messages,
        destinations=dest,
    )


def _post_schedule_outcome_log(region: dict | None, result: dict) -> None:
    """Best-effort paxminer_logs line for an automatic schedule run. Never raises."""
    region_name = "?"
    token_enc = None
    if region:
        region_name = region.get("schema_name") or "?"
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
        payload = dict(result)
        notify = (payload.get("notify_user") or "").strip()
        if notify and not payload.get("operator_name"):
            payload["operator_name"] = slack_display_name(client, notify)
        post_log(client, format_schedule_log_line(region_name, payload), region=region)
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
    notify_user: str = "",
) -> dict:
    """Execute a single schedule row. force=True skips due-now check (Run Now).

    Scheduled ticks and Run Now both post an outcome line to paxminer_logs.
    ``manual`` / ``notify_user`` only change the first sentence of that line.
    """
    started = time.time()
    schedule_id = int(schedule["id"])
    schema_name = schedule.get("schema_name") or ""
    region = _load_region(registry_conn, pm_schema, schema_name)
    if not region:
        try:
            tz_guess = "America/Chicago"
            local_guess = region_local_now(tz_guess)
            mark_schedule_status(
                registry_conn,
                pm_schema,
                schedule_id,
                local_guess.date(),
                "error",
                local_dt=local_guess,
            )
        except Exception:
            LOG.exception("mark error status failed schedule_id=%s", schedule_id)
        out = {
            "schedule_id": schedule_id,
            "ok": False,
            "error": "region not found",
            "manual": bool(manual or notify_user),
            "notify_user": notify_user or "",
        }
        if not dry_run:
            _post_schedule_outcome_log(None, out)
        return out

    tz_name = region.get("timezone") or "America/Chicago"
    local = region_local_now(tz_name)
    local_date = local.date()

    if not force and not is_due_now(schedule, timezone_name=tz_name):
        return {"schedule_id": schedule_id, "ok": True, "skipped": "not due"}

    definition = _load_definition(registry_conn, pm_schema, int(schedule["report_definition_id"]))
    if not definition:
        mark_schedule_status(
            registry_conn, pm_schema, schedule_id, local_date, "error", local_dt=local
        )
        out = {
            "schedule_id": schedule_id,
            "ok": False,
            "error": "definition not found",
            "manual": bool(manual or notify_user),
            "notify_user": notify_user or "",
        }
        if not dry_run:
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

    dest_meta = destination_descriptor(schedule.get("destination_type"))
    mark_schedule_status(
        registry_conn, pm_schema, schedule_id, local_date, "running", local_dt=local
    )
    try:
        result = _dispatch_report(
            registry_conn,
            pm_schema,
            region,
            schedule,
            definition,
        )
        if isinstance(result, dict):
            result = _ensure_outcome_fields(result, report_type)
        status = _result_status(result)
        mark_schedule_status(
            registry_conn, pm_schema, schedule_id, local_date, status, local_dt=local
        )
        out = {
            "schedule_id": schedule_id,
            "ok": status != "error",
            "report_type": report_type,
            "definition_name": definition.get("name") or report_type,
            "result": result,
            "status": status,
            "duration_s": round(time.time() - started, 2),
            "channel_count": result.get("channel_count") if isinstance(result, dict) else None,
            "user_count": result.get("user_count") if isinstance(result, dict) else None,
            "manual": bool(manual or notify_user),
            "notify_user": notify_user or "",
            "destination_type": schedule.get("destination_type"),
            "destination_label": dest_meta["label"],
            "destination_expansion": dest_meta["expansion"],
            "destination_kind": dest_meta["kind"],
            "specified_channels": _parse_json_list(schedule.get("destination_channels")),
        }
        if isinstance(result, dict):
            out["posted_channels"] = result.get("posted_channels")
            out["failed_channels"] = result.get("failed_channels")
            out["posted_users"] = result.get("posted_users")
            out["failed_users"] = result.get("failed_users")
            out["results_line"] = result.get("results_line")
            out["period_start"] = result.get("period_start")
            out["period_end"] = result.get("period_end")
        ch = int(out.get("channel_count") or 0)
        us = int(out.get("user_count") or 0)
        out["message_count"] = ch + us
        if status == "skipped":
            out["skipped"] = (result or {}).get("skipped") or "no delivery"
            out["ok"] = True
        if not dry_run:
            _post_schedule_outcome_log(region, out)
        return out
    except Exception as e:
        LOG.exception(
            "schedule failed schema=%s schedule_id=%s report_type=%s",
            schema_name,
            schedule_id,
            report_type,
        )
        mark_schedule_status(
            registry_conn, pm_schema, schedule_id, local_date, "error", local_dt=local
        )
        out = {
            "schedule_id": schedule_id,
            "ok": False,
            "report_type": report_type,
            "definition_name": definition.get("name") or report_type,
            "error": str(e),
            "status": "error",
            "duration_s": round(time.time() - started, 2),
            "manual": bool(manual or notify_user),
            "notify_user": notify_user or "",
            "destination_type": schedule.get("destination_type"),
            "destination_label": dest_meta["label"],
            "destination_expansion": dest_meta["expansion"],
            "destination_kind": dest_meta["kind"],
            "specified_channels": _parse_json_list(schedule.get("destination_channels")),
            "message_count": 0,
        }
        if not dry_run:
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

        window = None
        if template_has(report_type, "window"):
            window = resolve_time_window(
                definition, timezone_name=region.get("timezone")
            )
        title = format_report_title(
            definition.get("name"),
            window if template_has(report_type, "window") else None,
        )
        default_n = int(template_for(report_type).get("default_top_n") or 20)
        try:
            top_n = int(definition.get("top_n") or default_n)
        except (TypeError, ValueError):
            top_n = default_n
        top_n = max(1, top_n)

        if report_type == "pax_charts":
            from monthly_charts.PAXcharter import run_pax_charter

            # Pass the list as-is (including empty). Never coerce [] → None (all users).
            result = run_pax_charter(
                regional,
                token,
                schema_name,
                plot_dir=plot_dir,
                user_ids=user_ids,
                window=window,
                title=title,
            )
            if isinstance(result, dict):
                return _apply_delivery_result(result, attempted_users=len(user_ids))
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
                window=window,
                title=title,
            )
            if isinstance(result, dict):
                return _apply_delivery_result(result, attempted_channels=len(channel_ids))
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
                window=window,
                title=title,
                top_n=top_n,
            )
            if isinstance(result, dict):
                return _apply_delivery_result(result, attempted_channels=len(channel_ids))
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
                window=window,
                title=title,
                top_n=top_n,
            )
            if isinstance(result, dict):
                return _apply_delivery_result(result, attempted_channels=len(channel_ids))
            return result
        if report_type == "achievement_leaderboard":
            from achievements.leaderboard import run_leaderboard_for_region

            region = dict(region)
            if channel_ids:
                region["achievement_channel"] = channel_ids[0]
            result = run_leaderboard_for_region(
                registry_conn,
                pm_schema,
                region,
                window=window,
                title=title,
                top_n=top_n,
            )
            client = slack_client(token)
            posted_channels: list[dict] = []
            failed_channels: list[dict] = []
            if result.get("text") and channel_ids:
                for cid in channel_ids:
                    try:
                        if cid != channel_ids[0]:
                            post_message(client, cid, result["text"], blocks=result.get("blocks"))
                        posted_channels.append({"ao": "achievement-lb", "channel_id": cid})
                    except Exception as exc:
                        LOG.exception("extra achievement_leaderboard post failed channel=%s", cid)
                        failed_channels.append(
                            {"ao": "achievement-lb", "channel_id": cid, "reason": str(exc)[:200]}
                        )
            if isinstance(result, dict):
                result = dict(result)
                if posted_channels or failed_channels:
                    result["posted_channels"] = posted_channels
                    result["failed_channels"] = failed_channels
                return _apply_delivery_result(result, attempted_channels=len(channel_ids))
            return result
        if report_type == "achievement_almost_there":
            from achievements.leaderboard import run_almost_there_for_region

            region = dict(region)
            if channel_ids:
                region["achievement_channel"] = channel_ids[0]
            result = run_almost_there_for_region(
                registry_conn,
                pm_schema,
                region,
                title=title,
                top_n=top_n,
            )
            client = slack_client(token)
            posted_channels = []
            failed_channels = []
            if result.get("text") and channel_ids:
                for cid in channel_ids:
                    try:
                        if cid != channel_ids[0]:
                            post_message(client, cid, result["text"], blocks=result.get("blocks"))
                        posted_channels.append({"ao": "almost-there", "channel_id": cid})
                    except Exception as exc:
                        LOG.exception("extra achievement_almost_there post failed channel=%s", cid)
                        failed_channels.append(
                            {"ao": "almost-there", "channel_id": cid, "reason": str(exc)[:200]}
                        )
            if isinstance(result, dict):
                result = dict(result)
                if posted_channels or failed_channels:
                    result["posted_channels"] = posted_channels
                    result["failed_channels"] = failed_channels
                return _apply_delivery_result(result, attempted_channels=len(channel_ids))
            return result
        if report_type == "award_achievements":
            from achievements.runner import run_achievements_for_region

            if not channel_ids:
                return {
                    "skipped": "no destinations configured",
                    "channel_count": 0,
                    "user_count": 0,
                }
            result = run_achievements_for_region(
                registry_conn,
                pm_schema=pm_schema,
                regional_schema=schema_name,
                region_row=region,
                channel_override=channel_ids[0],
                post_channels=channel_ids,
            )
            if isinstance(result, dict):
                result = dict(result)
                if result.get("skipped"):
                    return _apply_delivery_result(result, attempted_channels=0)
                posted_any = bool(result.get("grants") or result.get("revokes"))
                if posted_any:
                    result["posted_channels"] = [
                        {"ao": "awards", "channel_id": cid} for cid in channel_ids
                    ]
                    return _apply_delivery_result(
                        result, attempted_channels=len(channel_ids)
                    )
                return _apply_delivery_result(result, attempted_channels=0)
            return result
        if report_type == "kotter":
            from kotter.kotter_report import run_kotter_for_region

            region = dict(region)
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
            )
            client = slack_client(token)
            posted_channels = []
            failed_channels = []
            primary = channel_ids[0] if channel_ids else region.get("kotter_channel")
            if result.get("posted") and primary:
                posted_channels.append({"ao": "kotter", "channel_id": primary})
            if result.get("error"):
                failed_channels.append(
                    {
                        "ao": "kotter",
                        "channel_id": primary or "?",
                        "reason": str(result.get("error"))[:200],
                    }
                )
            if len(channel_ids) > 1 and result.get("text"):
                for cid in channel_ids[1:]:
                    try:
                        post_message(client, cid, result["text"], blocks=result.get("blocks"))
                        posted_channels.append({"ao": "kotter", "channel_id": cid})
                    except Exception as exc:
                        LOG.exception("extra kotter post failed channel=%s", cid)
                        failed_channels.append(
                            {"ao": "kotter", "channel_id": cid, "reason": str(exc)[:200]}
                        )
            if isinstance(result, dict):
                result = dict(result)
                if posted_channels or failed_channels:
                    result["posted_channels"] = posted_channels
                    result["failed_channels"] = failed_channels
                return _apply_delivery_result(
                    result,
                    attempted_channels=len(channel_ids) or (1 if region.get("kotter_channel") else 0),
                )
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
                return _apply_delivery_result(
                    result,
                    attempted_channels=len(channel_ids),
                    attempted_users=len(user_ids),
                )
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
