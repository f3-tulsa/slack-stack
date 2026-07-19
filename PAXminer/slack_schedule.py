"""Bolt listeners for Schedule / PAX Reports / Kotter config modals."""

from __future__ import annotations

import json
import logging
import os

from config_paxminer import _config_modal, _parse_metadata
from config_schedule import (
    ADD_REPORT_ACTION_ID,
    ADD_SCHEDULE_ACTION_ID,
    DELETE_ALL_SCHEDULES_ACTION_ID,
    DELETE_REPORT_ACTION_ID,
    DELETE_SCHEDULE_ACTION_ID,
    EDIT_REPORT_ACTION_ID,
    EDIT_SCHEDULE_ACTION_ID,
    KOTTER_CONFIG_CALLBACK_ID,
    OPEN_ACHIEVEMENTS_ACTION_ID,
    OPEN_KOTTER_CONFIG_ACTION_ID,
    OPEN_REPORTS_ACTION_ID,
    OPEN_SCHEDULE_ACTION_ID,
    REPORT_EDIT_CALLBACK_ID,
    REPORT_WINDOW_ACTION_ID,
    REPORTS_LIST_CALLBACK_ID,
    RESTORE_DEFAULTS_ACTION_ID,
    RUN_NOW_SCHEDULE_ACTION_ID,
    SCHEDULE_DEST_TYPE_ACTION_ID,
    SCHEDULE_EDIT_CALLBACK_ID,
    SCHEDULE_FREQ_ACTION_ID,
    SCHEDULE_LIST_CALLBACK_ID,
    SCHEDULE_REPORT_ACTION_ID,
    TOGGLE_SCHEDULE_ACTION_ID,
    _kotter_config_modal,
    _report_edit_modal,
    _reports_list_modal,
    _schedule_edit_modal,
    _schedules_list_modal,
    draft_from_report_state,
    draft_from_schedule_state,
    load_definition,
    load_definitions,
    load_schedule,
    load_schedules,
    parse_kotter_form,
    parse_report_form,
    parse_schedule_form,
    selected_report_id,
    selected_schedule_id,
    validate_report_form,
    validate_schedule_form,
)
from config_paxminer import (
    _achievements_list_modal,
    _load_achievements,
)
from paxminer_db import connect_from_env, paxminer_schema_from_env
from schedule_schema import (
    count_schedules_for_definition,
    delete_all_schedules,
    restore_defaults,
)
from slack_http import is_slack_admin

LOG = logging.getLogger(__name__)


def queue_run_now(schedule_id: int, user_id: str) -> None:
    """Async-invoke ScheduleFunction for a forced Run Now (DMs ``user_id`` on completion)."""
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


def register_schedule_listeners(app) -> None:
    """Attach schedule/report listeners to a Bolt App."""

    def _ctx(body):
        from slack_app import _region_context_from_body

        return _region_context_from_body(body)

    def _admin_ack(ack, body, client):
        user_id = (body.get("user") or {}).get("id", "")
        if not is_slack_admin(user_id, client=client):
            ack()
            return False
        ack()
        return True

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
            with conn.cursor() as cur:
                schedules = load_schedules(cur, pm, regional_schema)
            client.views_update(
                view_id=body["view"]["id"],
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

    def _refresh_reports_list(client, body, team_id, regional_schema, notice=None):
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
                view=_reports_list_modal(team_id, regional_schema, defs, notice=notice),
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
                defs = load_definitions(cur, pm, regional_schema)
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
            client.views_push(
                trigger_id=body["trigger_id"],
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
            client.views_push(
                trigger_id=body["trigger_id"],
                view=_schedule_edit_modal(
                    team_id,
                    regional_schema,
                    defs,
                    schedule=sched,
                    timezone_name=region.get("timezone") or "America/Chicago",
                ),
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
                if meta.get("schedule_id"):
                    sched = load_schedule(cur, pm, int(meta["schedule_id"]))
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
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region,
                notice=f"Deleted {n} schedule item(s).",
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
                n = restore_defaults(cur, pm, region)
                conn.commit()
            _refresh_schedule_list(
                client,
                body,
                team_id,
                regional_schema,
                region,
                notice=f"Restored defaults ({n} schedule row(s) added).",
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

    @app.action("paxminer_schedule_page_prev")
    @app.action("paxminer_schedule_page_next")
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
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                definition = (
                    load_definition(cur, pm, values["report_definition_id"])
                    if values.get("report_definition_id")
                    else None
                )
                errors = validate_schedule_form(
                    values, (definition or {}).get("report_type")
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
                            day_of_month=%s, time_of_day=%s, custom_spec=%s, enabled=%s
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
                            values["enabled"],
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
                            values["enabled"],
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
        finally:
            conn.close()

    @app.action(ADD_REPORT_ACTION_ID)
    def add_report(ack, body, client, logger):
        if not _admin_ack(ack, body, client):
            return
        team_id, regional_schema, region = _ctx(body)
        if not region or not regional_schema:
            return
        client.views_push(
            trigger_id=body["trigger_id"],
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
            if row.get("is_builtin") and row.get("report_type") != "custom_report":
                _refresh_reports_list(
                    client,
                    body,
                    team_id,
                    regional_schema,
                    notice="Builtin reports are not builder-editable. Schedule them, or add a custom report.",
                )
                return
            client.views_push(
                trigger_id=body["trigger_id"],
                view=_report_edit_modal(team_id, regional_schema, row),
            )
        finally:
            conn.close()

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
                elif row.get("is_builtin"):
                    notice = "Cannot delete a builtin report definition."
                else:
                    n = count_schedules_for_definition(cur, pm, rid)
                    if n:
                        notice = f"Cannot delete: {n} schedule(s) still reference it."
                    else:
                        cur.execute(
                            f"DELETE FROM `{pm}`.`region_report_definitions` WHERE id=%s",
                            (rid,),
                        )
                        conn.commit()
                        notice = "Deleted custom report."
            _refresh_reports_list(client, body, team_id, regional_schema, notice=notice)
        finally:
            conn.close()

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
                    cur.execute(
                        f"""
                        UPDATE `{pm}`.`region_report_definitions`
                        SET name=%s, code=%s, kind=%s, source=%s, fields=%s,
                            metric=%s, group_by=%s, top_n=%s, time_window_type=%s,
                            window_days=%s, window_start=%s, window_end=%s
                        WHERE id=%s AND schema_name=%s AND is_builtin=0
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
                            (schema_name, code, name, report_type, is_builtin, kind, source,
                             fields, metric, group_by, top_n, time_window_type, window_days,
                             window_start, window_end)
                            VALUES (%s,%s,%s,'custom_report',0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                regional_schema,
                                values["code"],
                                values["name"],
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
        values = parse_kotter_form(body)
        pm = paxminer_schema_from_env()
        conn = connect_from_env(
            os.environ.get("PAXMINER_REGISTRY_DATABASE")
            or os.environ.get("PAXMINER_SCHEMA")
            or "paxminer"
        )
        try:
            with conn.cursor() as cur:
                sets = ", ".join(f"`{k}`=%s" for k in values)
                cur.execute(
                    f"UPDATE `{pm}`.`regions` SET {sets} WHERE region=%s",
                    (*values.values(), region["region"]),
                )
                conn.commit()
            region = dict(region)
            region.update(values)
            region["team_id"] = team_id
            if regional_schema:
                region["schema_name"] = regional_schema
            ack(response_action="update", view=_config_modal(region))
        finally:
            conn.close()
