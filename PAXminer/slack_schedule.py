"""Bolt listeners for Schedule / PAX Reports / Kotter config modals."""

from __future__ import annotations

import json
import logging
import os

from config_paxminer import _config_modal, _parse_metadata
from config_schedule import (
    ADD_REPORT_ACTION_ID,
    ADD_SCHEDULE_ACTION_ID,
    DELETE_ALL_REPORTS_ACTION_ID,
    DELETE_ALL_SCHEDULES_ACTION_ID,
    DELETE_REPORT_ACTION_ID,
    DELETE_SCHEDULE_ACTION_ID,
    DUPLICATE_REPORT_ACTION_ID,
    DUPLICATE_SCHEDULE_ACTION_ID,
    EDIT_REPORT_ACTION_ID,
    EDIT_SCHEDULE_ACTION_ID,
    KOTTER_CONFIG_CALLBACK_ID,
    OPEN_ACHIEVEMENTS_ACTION_ID,
    OPEN_KOTTER_CONFIG_ACTION_ID,
    OPEN_REPORTS_ACTION_ID,
    OPEN_SCHEDULE_ACTION_ID,
    MORE_REPORT_ACTION_ID,
    MORE_SCHEDULE_ACTION_ID,
    REPORT_DELETE_CONFIRM_CALLBACK_ID,
    REPORT_EDIT_CALLBACK_ID,
    REPORT_TEMPLATE_ACTION_ID,
    REPORT_WINDOW_ACTION_ID,
    REPORTS_LIST_CALLBACK_ID,
    REPORTS_PAGE_NEXT_ACTION_ID,
    REPORTS_PAGE_PREV_ACTION_ID,
    RESTORE_DEFAULTS_ACTION_ID,
    RESTORE_REPORTS_ACTION_ID,
    RUN_NOW_SCHEDULE_ACTION_ID,
    SCHEDULE_DELETE_CONFIRM_CALLBACK_ID,
    SCHEDULE_DEST_TYPE_ACTION_ID,
    SCHEDULE_EDIT_CALLBACK_ID,
    SCHEDULE_FREQ_ACTION_ID,
    SCHEDULE_LIST_CALLBACK_ID,
    SCHEDULE_PAGE_NEXT_ACTION_ID,
    SCHEDULE_PAGE_PREV_ACTION_ID,
    SCHEDULE_REPORT_ACTION_ID,
    TOGGLE_SCHEDULE_ACTION_ID,
    _kotter_config_modal,
    _report_edit_modal,
    _reports_list_modal,
    _schedule_edit_modal,
    _schedules_list_modal,
    duplicate_report_draft,
    draft_from_report_state,
    draft_from_schedule_state,
    is_code_rendered,
    load_definition,
    load_definitions,
    load_schedule,
    load_schedules,
    parse_kotter_form,
    parse_report_form,
    parse_schedule_form,
    report_delete_warning,
    schedule_as_new_draft,
    schedule_delete_warning,
    selected_report_id,
    selected_schedule_id,
    validate_kotter_form,
    validate_report_form,
    validate_schedule_form,
    _metadata,
)
from config_paxminer import (
    _achievements_list_modal,
    _load_achievements,
)
from paxminer_db import connect_from_env, paxminer_schema_from_env
from schedule_schema import (
    count_customized_builtins,
    count_schedules_for_definition,
    delete_all_definitions_and_schedules,
    delete_all_schedules,
    delete_definition_and_schedules,
    ensure_report_enabled_column,
    restore_defaults,
)
from slack_blocks import (
    OVERFLOW_DELETE,
    OVERFLOW_DISABLE,
    OVERFLOW_DUPLICATE,
    OVERFLOW_EDIT,
    OVERFLOW_ENABLE,
    delete_confirm_modal,
    parse_overflow_action,
)
from slack_http import is_slack_admin, notify_admin_required

LOG = logging.getLogger(__name__)


def _json_date(value) -> str | None:
    """ISO date string for Lambda payloads; accepts date/datetime/str."""
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def queue_run_now(schedule_id: int, user_id: str) -> None:
    """Async-invoke ScheduleFunction for a forced Run Now."""
    import boto3

    fn = os.environ.get("SCHEDULE_FUNCTION_NAME", "").strip()
    if not fn:
        raise RuntimeError("SCHEDULE_FUNCTION_NAME not configured")
    boto3.client("lambda").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "source": "run_now",
                "schedule_id": int(schedule_id),
                "force": True,
                "notify_user": user_id or "",
            }
        ).encode("utf-8"),
    )


def queue_achievement_backfill(
    *,
    schema: str,
    achievement_id: int,
    actor: str,
    start: str | None = None,
    end: str | None = None,
    automatic: bool = False,
    action: str = "re-evaluated",
) -> None:
    """Ack-friendly async invoke of reconcile_rule_awards via ScheduleFunction."""
    import boto3

    fn = os.environ.get("SCHEDULE_FUNCTION_NAME", "").strip()
    if not fn:
        raise RuntimeError("SCHEDULE_FUNCTION_NAME not configured")
    payload = {
        "source": "achievement_rule_backfill",
        "schema": schema,
        "achievement_id": int(achievement_id),
        "actor": actor or "",
        "action": action or "re-evaluated",
    }
    start_s = _json_date(start)
    end_s = _json_date(end)
    if start_s:
        payload["start"] = start_s
    if end_s:
        payload["end"] = end_s
    if automatic:
        payload["automatic"] = True
    boto3.client("lambda").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def register_schedule_listeners(app) -> None:
    """Attach schedule/report listeners to a Bolt App."""

    def _ctx(body):
        from slack_app import _region_context_from_body

        return _region_context_from_body(body)

    def _admin_ack(ack, body, client):
        """Ack first, then check admin (keeps Slack's 3s window for the ack)."""
        ack()
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            notify_admin_required(client, body)
            return False
        return True

    def _noop_ack(*_a, **_k):
        return None

    def _refresh_schedule_list(
        client,
        body,
        team_id,
        regional_schema,
        region,
        notice=None,
        page=0,
        selected_id=None,
    ):
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
            with conn.cursor() as cur:
                schedules = load_schedules(cur, pm, regional_schema)
            client.views_update(
                view_id=meta.get("list_view_id") or body["view"]["id"],
                view=_schedules_list_modal(
                    team_id,
                    regional_schema,
                    schedules,
                    timezone_name=region.get("timezone") or "America/Chicago",
                    page=page,
                    notice=notice,
                    selected_schedule_id=selected_id,
                ),
            )
        finally:
            conn.close()

    def _refresh_reports_list(client, body, team_id, regional_schema, notice=None, page=None):
        meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
        if page is None:
            try:
                page = int(meta.get("page") or 0)
            except (TypeError, ValueError):
                page = 0
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                ensure_report_enabled_column(cur, pm)
                defs = load_definitions(cur, pm, regional_schema)
            conn.commit()
            client.views_update(
                view_id=meta.get("list_view_id") or body["view"]["id"],
                view=_reports_list_modal(
                    team_id, regional_schema, defs, notice=notice, page=page
                ),
            )
        finally:
            conn.close()

    @app.action(OPEN_SCHEDULE_ACTION_ID)
    def open_schedule(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                schedules = load_schedules(cur, pm, regional_schema)
            client.views_push(
                trigger_id=body["trigger_id"],
                view=_schedules_list_modal(
                    team_id,
                    regional_schema,
                    schedules,
                    timezone_name=region.get("timezone") or "America/Chicago",
                ),
            )
        finally:
            conn.close()

    @app.action(OPEN_REPORTS_ACTION_ID)
    def open_reports(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                ensure_report_enabled_column(cur, pm)
                defs = load_definitions(cur, pm, regional_schema)
            conn.commit()
            client.views_push(
                trigger_id=body["trigger_id"],
                view=_reports_list_modal(team_id, regional_schema, defs),
            )
        finally:
            conn.close()

    @app.action(OPEN_KOTTER_CONFIG_ACTION_ID)
    def open_kotter(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        client.views_push(
            trigger_id=body["trigger_id"],
            view=_kotter_config_modal(team_id, regional_schema, region),
        )

    @app.action(OPEN_ACHIEVEMENTS_ACTION_ID)
    def open_achievements_hub(ack, body, client, logger):
        # Reuse existing manage-achievements push.
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                from achievements.range import ensure_achievement_range_columns

                ensure_achievement_range_columns(cur, regional_schema)
                achievements = _load_achievements(cur, regional_schema)
            client.views_push(
                trigger_id=body["trigger_id"],
                view=_achievements_list_modal(team_id, regional_schema, achievements),
            )
        finally:
            conn.close()

    @app.action(ADD_SCHEDULE_ACTION_ID)
    def add_schedule(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                defs = load_definitions(cur, pm, regional_schema)
            client.views_update(
                view_id=body["view"]["id"],
                view=_schedule_edit_modal(
                    team_id,
                    regional_schema,
                    defs,
                    timezone_name=region.get("timezone") or "America/Chicago",
                ),
            )
        finally:
            conn.close()

    @app.action(EDIT_SCHEDULE_ACTION_ID)
    def edit_schedule(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        sid = selected_schedule_id(body)
        if not region or not regional_schema or not sid:
            _refresh_schedule_list(
                client, body, team_id, regional_schema, region or {}, notice="Select a schedule item first."
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                sched = load_schedule(cur, pm, sid)
                defs = load_definitions(cur, pm, regional_schema)
                if not sched:
                    _refresh_schedule_list(
                        client, body, team_id, regional_schema, region, notice="Schedule not found."
                    )
                    return
                view = _schedule_edit_modal(
                    team_id,
                    regional_schema,
                    defs,
                    schedule=sched,
                    timezone_name=region.get("timezone") or "America/Chicago",
                )
            client.views_update(view_id=body["view"]["id"], view=view)
        except Exception:
            logger.exception("edit_schedule failed sid=%s", sid)
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region,
                notice="Could not open edit form — try again.",
                selected_id=sid,
            )
        finally:
            conn.close()

    def _conditional_schedule_update(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
        state = body.get("view", {}).get("state", {}).get("values", {})
        draft = draft_from_schedule_state(state, meta.get("draft"))
        # Cap metadata size: large channel lists can exceed Slack's 3000-char private_metadata.
        channels = draft.get("destination_channels") or []
        if isinstance(channels, list) and len(channels) > 40:
            draft = dict(draft)
            draft["destination_channels"] = channels[:40]
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                defs = load_definitions(cur, pm, regional_schema)
                sched = None
                sid_raw = meta.get("schedule_id")
                if sid_raw is not None and str(sid_raw).isdigit():
                    sched = load_schedule(cur, pm, int(sid_raw))
            client.views_update(
                view_id=body["view"]["id"],
                view=_schedule_edit_modal(
                    team_id,
                    regional_schema,
                    defs,
                    schedule=sched,
                    timezone_name=(region or {}).get("timezone") or "America/Chicago",
                    draft=draft,
                ),
            )
        finally:
            conn.close()

    app.action(SCHEDULE_DEST_TYPE_ACTION_ID)(_conditional_schedule_update)
    app.action(SCHEDULE_FREQ_ACTION_ID)(_conditional_schedule_update)
    app.action(SCHEDULE_REPORT_ACTION_ID)(_conditional_schedule_update)

    @app.action(DELETE_SCHEDULE_ACTION_ID)
    def delete_schedule(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        sid = selected_schedule_id(body)
        if not sid:
            _refresh_schedule_list(
                client, body, team_id, regional_schema, region or {}, notice="Select a schedule item first."
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM `{pm}`.`region_schedules` WHERE id=%s", (sid,))
                conn.commit()
            _refresh_schedule_list(
                client, body, team_id, regional_schema, region, notice="Deleted schedule item."
            )
        finally:
            conn.close()

    @app.action(TOGGLE_SCHEDULE_ACTION_ID)
    def toggle_schedule(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        sid = selected_schedule_id(body)
        if not sid:
            _refresh_schedule_list(
                client, body, team_id, regional_schema, region or {}, notice="Select a schedule item first."
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE `{pm}`.`region_schedules` SET enabled = 1 - COALESCE(enabled,0) WHERE id=%s",
                    (sid,),
                )
                conn.commit()
            _refresh_schedule_list(
                client, body, team_id, regional_schema, region, notice="Toggled schedule enabled flag."
            )
        finally:
            conn.close()

    @app.action(DELETE_ALL_SCHEDULES_ACTION_ID)
    def delete_all(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                n = delete_all_schedules(cur, pm, regional_schema)
                conn.commit()
                schedules = load_schedules(cur, pm, regional_schema)
            selected = schedules[0]["id"] if schedules else None
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region,
                notice=f"Deleted {n} schedule item(s).",
                selected_id=selected,
            )
        finally:
            conn.close()

    @app.action(RESTORE_DEFAULTS_ACTION_ID)
    def restore(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                customized = count_customized_builtins(cur, pm, regional_schema)
                n = restore_defaults(cur, pm, region)
                conn.commit()
                schedules = load_schedules(cur, pm, regional_schema)
            selected = schedules[0]["id"] if schedules else None
            notice = f"Added missing defaults ({n} schedule row(s) added)."
            if customized:
                notice += (
                    f" Kept {customized} customized builtin report(s); "
                    "missing builtins were re-added."
                )
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region,
                notice=notice,
                selected_id=selected,
            )
        finally:
            conn.close()

    @app.action(RUN_NOW_SCHEDULE_ACTION_ID)
    def run_now(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        sid = selected_schedule_id(body)
        if not sid:
            _refresh_schedule_list(
                client, body, team_id, regional_schema, region or {}, notice="Select a schedule item first."
            )
            return
        user_id = (body.get("user") or {}).get("id", "")
        try:
            queue_run_now(sid, user_id)
            notice = f"Running schedule #{sid} now — I'll DM you the result."
        except Exception as exc:
            logger.exception("Run Now failed")
            notice = f"Run Now failed: {str(exc)[:200]}"
        _refresh_schedule_list(
            client,
            body,
            team_id,
            regional_schema,
            region,
            notice=notice,
            selected_id=sid,
        )

    @app.action(SCHEDULE_PAGE_PREV_ACTION_ID)
    @app.action(SCHEDULE_PAGE_NEXT_ACTION_ID)
    def schedule_page(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        action = (body.get("actions") or [{}])[0]
        try:
            page = int(action.get("value") or 0)
        except ValueError:
            page = 0
        _refresh_schedule_list(client, body, team_id, regional_schema, region or {}, page=page)

    @app.view(SCHEDULE_LIST_CALLBACK_ID)
    def schedule_list_submit(ack, body, client, logger):
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            ack(response_action="clear")
            return
        team_id, regional_schema, region = _ctx(body)
        if not region:
            ack(response_action="clear")
            return
        region = dict(region)
        region["team_id"] = team_id
        if regional_schema:
            region["schema_name"] = regional_schema
        ack(response_action="update", view=_config_modal(region))

    @app.view(SCHEDULE_EDIT_CALLBACK_ID)
    def schedule_edit_submit(ack, body, client, logger):
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            ack(response_action="errors", errors={"report_definition_id": "Admin required"})
            return
        team_id, regional_schema, region = _ctx(body)
        values = parse_schedule_form(body)
        pm = paxminer_schema_from_env()
        try:
            conn = connect_from_env(
                os.environ.get("PAXMINER_REGISTRY_DATABASE")
                or os.environ.get("PAXMINER_SCHEMA")
                or "paxminer"
            )
        except Exception as exc:
            logger.exception("schedule edit connect failed")
            ack(
                response_action="errors",
                errors={"report_definition_id": f"Save failed: {str(exc)[:120]}"},
            )
            return
        try:
            with conn.cursor() as cur:
                definition = (
                    load_definition(cur, pm, values["report_definition_id"])
                    if values.get("report_definition_id")
                    else None
                )
                errors = validate_schedule_form(
                    values,
                    (definition or {}).get("report_type"),
                    definition_exists=(
                        None
                        if not values.get("report_definition_id")
                        else definition is not None
                    ),
                )
                if errors:
                    ack(response_action="errors", errors=errors)
                    return
                channels = json.dumps(values.get("destination_channels") or []) or None
                users = json.dumps(values.get("destination_users") or []) or None
                custom = (
                    json.dumps(values["custom_spec"]) if values.get("custom_spec") else None
                )
                if values.get("schedule_id"):
                    cur.execute(
                        f"""
                        UPDATE `{pm}`.`region_schedules`
                        SET report_definition_id=%s, destination_type=%s,
                            destination_channels=%s, destination_users=%s,
                            frequency_type=%s, day_of_week=%s, month_day_mode=%s,
                            day_of_month=%s, time_of_day=%s, custom_spec=%s
                        WHERE id=%s
                        """,
                        (
                            values["report_definition_id"],
                            values["destination_type"],
                            channels,
                            users,
                            values["frequency_type"],
                            values.get("day_of_week"),
                            values.get("month_day_mode"),
                            values.get("day_of_month"),
                            values["time_of_day"] + ":00"
                            if len(values["time_of_day"]) == 5
                            else values["time_of_day"],
                            custom,
                            values["schedule_id"],
                        ),
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO `{pm}`.`region_schedules`
                        (schema_name, report_definition_id, destination_type,
                         destination_channels, destination_users, frequency_type,
                         day_of_week, month_day_mode, day_of_month, time_of_day,
                         custom_spec, enabled)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            regional_schema,
                            values["report_definition_id"],
                            values["destination_type"],
                            channels,
                            users,
                            values["frequency_type"],
                            values.get("day_of_week"),
                            values.get("month_day_mode"),
                            values.get("day_of_month"),
                            values["time_of_day"] + ":00"
                            if len(values["time_of_day"]) == 5
                            else values["time_of_day"],
                            custom,
                            1,
                        ),
                    )
                conn.commit()
                schedules = load_schedules(cur, pm, regional_schema)
            ack(
                response_action="update",
                view=_schedules_list_modal(
                    team_id,
                    regional_schema,
                    schedules,
                    timezone_name=(region or {}).get("timezone") or "America/Chicago",
                    notice="Schedule saved.",
                ),
            )
        except Exception as exc:
            logger.exception("schedule edit submit failed")
            ack(
                response_action="errors",
                errors={"report_definition_id": f"Save failed: {str(exc)[:120]}"},
            )
        finally:
            conn.close()

    @app.action(ADD_REPORT_ACTION_ID)
    def add_report(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        client.views_update(
            view_id=body["view"]["id"],
            view=_report_edit_modal(team_id, regional_schema, None),
        )

    @app.action(EDIT_REPORT_ACTION_ID)
    def edit_report(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        rid = selected_report_id(body)
        if not rid:
            _refresh_reports_list(
                client, body, team_id, regional_schema, notice="Select a report first."
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                row = load_definition(cur, pm, rid)
            if not row:
                _refresh_reports_list(
                    client, body, team_id, regional_schema, notice="Report not found."
                )
                return
            client.views_update(
                view_id=body["view"]["id"],
                view=_report_edit_modal(team_id, regional_schema, row),
            )
        finally:
            conn.close()

    @app.action(DUPLICATE_REPORT_ACTION_ID)
    def duplicate_report(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        rid = selected_report_id(body)
        if not rid:
            _refresh_reports_list(
                client, body, team_id, regional_schema, notice="Open a report from More first."
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                row = load_definition(cur, pm, rid)
                if not row:
                    _refresh_reports_list(
                        client, body, team_id, regional_schema, notice="Report not found."
                    )
                    return
                draft = duplicate_report_draft(cur, pm, regional_schema, row)
            client.views_update(
                view_id=body["view"]["id"],
                view=_report_edit_modal(team_id, regional_schema, None, draft=draft),
            )
        finally:
            conn.close()

    @app.action(DUPLICATE_SCHEDULE_ACTION_ID)
    def duplicate_schedule(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        sid = selected_schedule_id(body)
        if not sid:
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region or {},
                notice="Open a schedule from More first.",
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                sched = load_schedule(cur, pm, sid)
                defs = load_definitions(cur, pm, regional_schema)
            if not sched:
                _refresh_schedule_list(
                    client,
                    body,
                    team_id,
                    regional_schema,
                    region or {},
                    notice="Schedule not found.",
                )
                return
            client.views_update(
                view_id=body["view"]["id"],
                view=_schedule_edit_modal(
                    team_id,
                    regional_schema,
                    defs,
                    timezone_name=(region or {}).get("timezone") or "America/Chicago",
                    draft=schedule_as_new_draft(sched),
                ),
            )
        finally:
            conn.close()

    @app.action(DELETE_ALL_REPORTS_ACTION_ID)
    def delete_all_reports(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                counts = delete_all_definitions_and_schedules(cur, pm, regional_schema)
                conn.commit()
            notice = (
                f"Deleted {counts['definitions']} report(s) and "
                f"{counts['schedules']} schedule(s)."
            )
            _refresh_reports_list(client, body, team_id, regional_schema, notice=notice, page=0)
        finally:
            conn.close()

    @app.action(RESTORE_REPORTS_ACTION_ID)
    def restore_reports(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                customized = count_customized_builtins(cur, pm, regional_schema)
                n = restore_defaults(cur, pm, region)
                conn.commit()
                defs = load_definitions(cur, pm, regional_schema)
            notice = (
                f"Added missing defaults ({n} schedule row(s) added, {len(defs)} report(s) now)."
            )
            if customized:
                notice += (
                    f" Kept {customized} customized builtin report(s); "
                    "missing builtins were re-added."
                )
            _refresh_reports_list(client, body, team_id, regional_schema, notice=notice, page=0)
        finally:
            conn.close()

    @app.action(REPORTS_PAGE_PREV_ACTION_ID)
    @app.action(REPORTS_PAGE_NEXT_ACTION_ID)
    def reports_page(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        action = (body.get("actions") or [{}])[0]
        try:
            page = int(action.get("value") or 0)
        except ValueError:
            page = 0
        _refresh_reports_list(client, body, team_id, regional_schema, page=page)

    @app.action(DELETE_REPORT_ACTION_ID)
    def delete_report(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        rid = selected_report_id(body)
        if not rid:
            _refresh_reports_list(
                client, body, team_id, regional_schema, notice="Select a report first."
            )
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                row = load_definition(cur, pm, rid)
                if not row:
                    notice = "Report not found."
                else:
                    n_sched = count_schedules_for_definition(cur, pm, rid)
                    counts = delete_definition_and_schedules(
                        cur, pm, rid, regional_schema
                    )
                    conn.commit()
                    notice = (
                        f"Deleted `{row.get('code')}` "
                        f"({counts['definitions']} report, {counts['schedules']} schedule(s))."
                    )
                    if n_sched and counts["schedules"] == 0:
                        notice = f"Deleted report; schedule cleanup unexpected (had {n_sched})."
            _refresh_reports_list(client, body, team_id, regional_schema, notice=notice)
        finally:
            conn.close()

    def _push_schedule_delete_confirm(body, client, logger, schedule_id: int) -> None:
        team_id, regional_schema, region = _ctx(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                row = load_schedule(cur, pm, schedule_id)
            if not row:
                _refresh_schedule_list(
                    client,
                    body,
                    team_id,
                    regional_schema,
                    region or {},
                    notice="Schedule not found.",
                )
                return
            name = row.get("definition_name") or f"#{schedule_id}"
            client.views_push(
                trigger_id=body["trigger_id"],
                view=delete_confirm_modal(
                    callback_id=SCHEDULE_DELETE_CONFIRM_CALLBACK_ID,
                    title="Delete schedule?",
                    warning=schedule_delete_warning(name),
                    metadata=_metadata(
                        team_id,
                        regional_schema,
                        schedule_id=schedule_id,
                        list_view_id=body["view"]["id"],
                    ),
                ),
            )
        except Exception:
            logger.exception("schedule delete confirm failed id=%s", schedule_id)
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region or {},
                notice="Could not open delete confirmation.",
            )
        finally:
            conn.close()

    def _toggle_report_enabled(body, client, logger, definition_id: int) -> None:
        team_id, regional_schema, region = _ctx(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                ensure_report_enabled_column(cur, pm)
                cur.execute(
                    f"UPDATE `{pm}`.`region_report_definitions` "
                    "SET enabled = 1 - COALESCE(enabled, 0) WHERE id=%s",
                    (definition_id,),
                )
                row = load_definition(cur, pm, definition_id)
                conn.commit()
            enabled = int((row or {}).get("enabled") or 0) == 1
            code = (row or {}).get("code") or definition_id
            notice = f"Enabled `{code}`." if enabled else f"Disabled `{code}`."
            _refresh_reports_list(client, body, team_id, regional_schema, notice=notice)
        except Exception:
            logger.exception("report toggle failed id=%s", definition_id)
            _refresh_reports_list(
                client,
                body,
                team_id,
                regional_schema,
                notice="Could not update enabled flag — try again.",
            )
        finally:
            conn.close()

    def _push_report_delete_confirm(body, client, logger, definition_id: int) -> None:
        team_id, regional_schema, region = _ctx(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                row = load_definition(cur, pm, definition_id)
            if not row:
                _refresh_reports_list(
                    client, body, team_id, regional_schema, notice="Report not found."
                )
                return
            code = row.get("code") or str(definition_id)
            client.views_push(
                trigger_id=body["trigger_id"],
                view=delete_confirm_modal(
                    callback_id=REPORT_DELETE_CONFIRM_CALLBACK_ID,
                    title="Delete report?",
                    warning=report_delete_warning(code),
                    metadata=_metadata(
                        team_id,
                        regional_schema,
                        definition_id=definition_id,
                        list_view_id=body["view"]["id"],
                    ),
                ),
            )
        except Exception:
            logger.exception("report delete confirm failed id=%s", definition_id)
            _refresh_reports_list(
                client,
                body,
                team_id,
                regional_schema,
                notice="Could not open delete confirmation.",
            )
        finally:
            conn.close()

    @app.action(MORE_SCHEDULE_ACTION_ID)
    def handle_schedule_more(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        action = (body.get("actions") or [{}])[0]
        verb, row_id = parse_overflow_action(action)
        team_id, regional_schema, region = _ctx(body)
        if not verb or not row_id:
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region or {},
                notice="Could not read that menu action.",
            )
            return
        inner = dict(body)
        inner["actions"] = [{**action, "value": str(row_id), "action_id": action.get("action_id")}]
        if verb == OVERFLOW_EDIT:
            edit_schedule(_noop_ack, inner, client, logger)
        elif verb == OVERFLOW_DUPLICATE:
            duplicate_schedule(_noop_ack, inner, client, logger)
        elif verb in (OVERFLOW_DISABLE, OVERFLOW_ENABLE):
            toggle_schedule(_noop_ack, inner, client, logger)
        elif verb == OVERFLOW_DELETE:
            _push_schedule_delete_confirm(body, client, logger, row_id)

    @app.action(MORE_REPORT_ACTION_ID)
    def handle_report_more(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        action = (body.get("actions") or [{}])[0]
        verb, row_id = parse_overflow_action(action)
        team_id, regional_schema, _region = _ctx(body)
        if not verb or not row_id:
            _refresh_reports_list(
                client, body, team_id, regional_schema, notice="Could not read that menu action."
            )
            return
        inner = dict(body)
        inner["actions"] = [{**action, "value": str(row_id), "action_id": action.get("action_id")}]
        if verb == OVERFLOW_EDIT:
            edit_report(_noop_ack, inner, client, logger)
        elif verb == OVERFLOW_DUPLICATE:
            duplicate_report(_noop_ack, inner, client, logger)
        elif verb in (OVERFLOW_DISABLE, OVERFLOW_ENABLE):
            _toggle_report_enabled(body, client, logger, row_id)
        elif verb == OVERFLOW_DELETE:
            _push_report_delete_confirm(body, client, logger, row_id)

    app.view(SCHEDULE_DELETE_CONFIRM_CALLBACK_ID)(delete_schedule)
    app.view(REPORT_DELETE_CONFIRM_CALLBACK_ID)(delete_report)

    @app.action(REPORT_WINDOW_ACTION_ID)
    def report_window_change(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
        state = body.get("view", {}).get("state", {}).get("values", {})
        draft = draft_from_report_state(state, meta.get("draft"))
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            row = None
            if meta.get("definition_id"):
                with conn.cursor() as cur:
                    row = load_definition(cur, pm, int(meta["definition_id"]))
            client.views_update(
                view_id=body["view"]["id"],
                view=_report_edit_modal(team_id, regional_schema, row, draft=draft),
            )
        finally:
            conn.close()

    @app.action(REPORT_TEMPLATE_ACTION_ID)
    def report_template_change(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
        state = body.get("view", {}).get("state", {}).get("values", {})
        draft = draft_from_report_state(state, meta.get("draft"))
        client.views_update(
            view_id=body["view"]["id"],
            view=_report_edit_modal(team_id, regional_schema, None, draft=draft),
        )

    @app.view(REPORTS_LIST_CALLBACK_ID)
    def reports_list_submit(ack, body, client, logger):
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            ack(response_action="clear")
            return
        team_id, regional_schema, region = _ctx(body)
        if not region:
            ack(response_action="clear")
            return
        region = dict(region)
        region["team_id"] = team_id
        if regional_schema:
            region["schema_name"] = regional_schema
        ack(response_action="update", view=_config_modal(region))

    @app.view(REPORT_EDIT_CALLBACK_ID)
    def report_edit_submit(ack, body, client, logger):
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            ack(response_action="errors", errors={"name": "Admin required"})
            return
        team_id, regional_schema, region = _ctx(body)
        values = parse_report_form(body)
        errors = validate_report_form(values)
        if errors:
            ack(response_action="errors", errors=errors)
            return
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                fields_json = json.dumps(values.get("fields") or [])
                if values.get("definition_id"):
                    existing = load_definition(cur, pm, int(values["definition_id"]))
                    if not existing or existing.get("schema_name") != regional_schema:
                        ack(
                            response_action="errors",
                            errors={"name": "Report not found"},
                        )
                        return
                    code_rendered = is_code_rendered(existing.get("report_type"))
                    if code_rendered:
                        # Name + window only; mark customized when originally builtin.
                        is_customized = 1 if existing.get("is_builtin") else int(
                            existing.get("is_customized") or 0
                        )
                        cur.execute(
                            f"""
                            UPDATE `{pm}`.`region_report_definitions`
                            SET name=%s, time_window_type=%s, window_days=%s,
                                window_start=%s, window_end=%s, top_n=%s, is_customized=%s
                            WHERE id=%s AND schema_name=%s
                            """,
                            (
                                values["name"],
                                values.get("time_window_type"),
                                values.get("window_days"),
                                values.get("window_start"),
                                values.get("window_end"),
                                values.get("top_n"),
                                is_customized,
                                values["definition_id"],
                                regional_schema,
                            ),
                        )
                    else:
                        cur.execute(
                            f"""
                            UPDATE `{pm}`.`region_report_definitions`
                            SET name=%s, code=%s, kind=%s, source=%s, fields=%s,
                                metric=%s, group_by=%s, top_n=%s, time_window_type=%s,
                                window_days=%s, window_start=%s, window_end=%s
                            WHERE id=%s AND schema_name=%s
                            """,
                            (
                                values["name"],
                                values["code"],
                                values["kind"],
                                values["source"],
                                fields_json,
                                values["metric"],
                                values["group_by"],
                                values["top_n"],
                                values["time_window_type"],
                                values["window_days"],
                                values.get("window_start"),
                                values.get("window_end"),
                                values["definition_id"],
                                regional_schema,
                            ),
                        )
                else:
                    try:
                        cur.execute(
                            f"""
                            INSERT INTO `{pm}`.`region_report_definitions`
                            (schema_name, code, name, report_type, is_builtin, is_customized,
                             kind, source, fields, metric, group_by, top_n, time_window_type,
                             window_days, window_start, window_end)
                            VALUES (%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                regional_schema,
                                values["code"],
                                values["name"],
                                values.get("report_type") or "custom_report",
                                values["kind"],
                                values["source"],
                                fields_json,
                                values["metric"],
                                values["group_by"],
                                values["top_n"],
                                values["time_window_type"],
                                values["window_days"],
                                values.get("window_start"),
                                values.get("window_end"),
                            ),
                        )
                    except Exception:
                        ack(
                            response_action="errors",
                            errors={"code": "Code already in use for this region"},
                        )
                        return
                conn.commit()
                defs = load_definitions(cur, pm, regional_schema)
            ack(
                response_action="update",
                view=_reports_list_modal(
                    team_id, regional_schema, defs, notice="Report saved."
                ),
            )
        finally:
            conn.close()

    @app.view(KOTTER_CONFIG_CALLBACK_ID)
    def kotter_config_submit(ack, body, client, logger):
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            ack(response_action="errors", errors={"NO_POST_THRESHOLD": "Admin required"})
            return
        team_id, regional_schema, region = _ctx(body)
        if not region:
            ack(response_action="clear")
            return
        region_key = region.get("region")
        if not region_key:
            ack(response_action="errors", errors={"NO_POST_THRESHOLD": "Region key missing"})
            return
        values = parse_kotter_form(body)
        errors = validate_kotter_form(values)
        if errors:
            ack(response_action="errors", errors=errors)
            return
        pm = paxminer_schema_from_env()
        try:
            conn = connect_from_env(
                os.environ.get("PAXMINER_REGISTRY_DATABASE")
                or os.environ.get("PAXMINER_SCHEMA")
                or "paxminer"
            )
        except Exception as exc:
            logger.exception("kotter config connect failed")
            ack(
                response_action="errors",
                errors={"NO_POST_THRESHOLD": f"Save failed: {str(exc)[:120]}"},
            )
            return
        try:
            with conn.cursor() as cur:
                sets = ", ".join(f"`{k}`=%s" for k in values)
                cur.execute(
                    f"UPDATE `{pm}`.`regions` SET {sets} WHERE region=%s",
                    (*values.values(), region_key),
                )
                conn.commit()
            region = dict(region)
            region.update(values)
            region["team_id"] = team_id
            if regional_schema:
                region["schema_name"] = regional_schema
            ack(response_action="update", view=_config_modal(region))
        except Exception as exc:
            logger.exception("kotter config submit failed")
            ack(
                response_action="errors",
                errors={"NO_POST_THRESHOLD": f"Save failed: {str(exc)[:120]}"},
            )
        finally:
            conn.close()
