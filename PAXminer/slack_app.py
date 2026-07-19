"""
Lightweight Slack Bolt front door for PAXMiner.

Acks interactive requests quickly; heavy work (Schedule Run Now) is
async-invoked on ScheduleFunction. Keep-warm EventBridge pings
short-circuit before Bolt.
"""

from __future__ import annotations

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk.errors import SlackApiError

from config_paxminer import (
    ACHIEVEMENT_EDIT_CALLBACK_ID,
    ACHIEVEMENTS_LIST_CALLBACK_ID,
    ADD_ACHIEVEMENT_ACTION_ID,
    CALLBACK_ID,
    DELETE_ACHIEVEMENT_ACTION_ID,
    EDIT_ACHIEVEMENT_ACTION_ID,
    _achievement_edit_modal,
    _achievements_list_modal,
    _config_modal,
    _load_achievement,
    _load_achievements,
    _parse_achievement_form,
    _parse_metadata,
    _parse_modal_values,
    _region_for_team,
    _registry_db,
    _selected_achievement_id,
    _validate_achievement,
)
from paxminer_db import connect_from_env, paxminer_schema_from_env
from slack_http import is_http_request, is_slack_admin

LOCAL_DEVELOPMENT = not os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

SlackRequestHandler.clear_all_log_handlers()
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if LOCAL_DEVELOPMENT:
    logger.addHandler(logging.StreamHandler())

app = App(
    process_before_response=not LOCAL_DEVELOPMENT,
    token=os.environ.get("PM_SLACK_TOKEN", ""),
    signing_secret=os.environ.get("PM_SLACK_SIGNING_SECRET", ""),
    # Skip auth.test locally so unit tests can import without a real bot token.
    token_verification_enabled=not LOCAL_DEVELOPMENT,
)


@app.middleware
def log_request(logger, body, next):
    team_id = body.get("team_id") or (body.get("team") or {}).get("id")
    user_id = body.get("user_id") or (body.get("user") or {}).get("id")
    request_type = body.get("type") or ("command" if body.get("command") else "unknown")
    callback_or_action = (
        body.get("command")
        or ((body.get("view") or {}).get("callback_id"))
        or (((body.get("actions") or [{}])[0]).get("action_id"))
        or ""
    )
    logger.info(
        "slack request team_id=%s user_id=%s type=%s callback_or_action=%s",
        team_id,
        user_id,
        request_type,
        callback_or_action,
    )
    return next()


@app.error
def handle_error(error, body, logger, client):
    logger.exception("Unhandled Slack Bolt error: %s", error)
    user_id = body.get("user_id") or (body.get("user") or {}).get("id")
    channel_id = body.get("channel_id") or (body.get("channel") or {}).get("id")
    if user_id and channel_id:
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Something went wrong: {str(error)[:500]}",
            )
        except Exception:
            logger.exception("Failed to send error ephemeral")


def _strip_channel_initials(view: dict) -> dict:
    """Return a copy of the modal view without channels_select initial_channel."""
    import copy

    cleaned = copy.deepcopy(view)
    for block in cleaned.get("blocks") or []:
        element = block.get("element") or {}
        if element.get("type") == "channels_select":
            element.pop("initial_channel", None)
    return cleaned


def _open_config_modal(client, trigger_id: str, region: dict, logger) -> None:
    view = _config_modal(region)
    try:
        client.views_open(trigger_id=trigger_id, view=view)
    except SlackApiError as exc:
        logger.warning("views_open failed (%s); retrying without channel initials", exc)
        client.views_open(trigger_id=trigger_id, view=_strip_channel_initials(view))


def handle_config_command(ack, body, client, logger, respond):
    """Named listener for /config-paxminer — importable for unit tests."""
    user_id = body.get("user_id", "")
    team_id = body.get("team_id", "")
    trigger_id = body.get("trigger_id", "")

    if not is_slack_admin(user_id, client=client):
        ack(text="Workspace admin required.", response_type="ephemeral")
        return

    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            region = _region_for_team(cur, pm, team_id)
        if not region:
            ack(
                text="No PAXMiner region linked to this workspace.",
                response_type="ephemeral",
            )
            return
        ack()
        region = dict(region)
        region["team_id"] = team_id
        try:
            _open_config_modal(client, trigger_id, region, logger)
        except Exception as exc:
            logger.exception("Failed to open config modal: %s", exc)
            try:
                respond(text=f"Could not open settings: {str(exc)[:300]}")
            except Exception:
                pass
    finally:
        conn.close()


app.command("/config-paxminer")(handle_config_command)


def _region_context_from_body(body: dict) -> tuple[str, str, dict | None]:
    meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
    team_id = meta.get("team_id") or (body.get("team") or {}).get("id", "")
    regional_schema = meta.get("regional_schema", "")
    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            region = _region_for_team(cur, pm, team_id) if team_id else None
        return team_id, regional_schema or (region or {}).get("schema_name", ""), region
    finally:
        conn.close()


def handle_add_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack()
        return
    ack()
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    view = _achievement_edit_modal(team_id, regional_schema, None)
    client.views_push(trigger_id=body["trigger_id"], view=view)


app.action(ADD_ACHIEVEMENT_ACTION_ID)(handle_add_achievement)


def _refresh_achievements_list(client, body, team_id, regional_schema, notice: str) -> None:
    """Re-render the list modal with an inline notice (modal actions have no response_url)."""
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            achievements = _load_achievements(cur, regional_schema)
        client.views_update(
            view_id=body["view"]["id"],
            view=_achievements_list_modal(team_id, regional_schema, achievements, notice=notice),
        )
    finally:
        conn.close()


def handle_edit_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack()
        return
    ack()
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    selected_id = _selected_achievement_id(body)
    if not selected_id:
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Select an achievement to edit."
        )
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            row = _load_achievement(cur, regional_schema, selected_id)
        if not row:
            _refresh_achievements_list(
                client, body, team_id, regional_schema, "Achievement not found."
            )
            return
        view = _achievement_edit_modal(team_id, regional_schema, row)
        client.views_push(trigger_id=body["trigger_id"], view=view)
    finally:
        conn.close()


app.action(EDIT_ACHIEVEMENT_ACTION_ID)(handle_edit_achievement)


def handle_delete_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack()
        return
    ack()
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    selected_id = _selected_achievement_id(body)
    if not selected_id:
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Select an achievement to delete."
        )
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM `{regional_schema}`.`achievements_awarded` "
                "WHERE achievement_id=%s",
                (selected_id,),
            )
            cnt = (cur.fetchone() or {}).get("cnt", 0)
            if cnt:
                achievements = _load_achievements(cur, regional_schema)
                client.views_update(
                    view_id=body["view"]["id"],
                    view=_achievements_list_modal(
                        team_id,
                        regional_schema,
                        achievements,
                        notice=f"Cannot delete: {cnt} award(s) reference this achievement.",
                    ),
                )
                return
            cur.execute(
                f"DELETE FROM `{regional_schema}`.`achievements_list` WHERE id=%s",
                (selected_id,),
            )
            conn.commit()
            achievements = _load_achievements(cur, regional_schema)
        view = _achievements_list_modal(team_id, regional_schema, achievements)
        client.views_update(view_id=body["view"]["id"], view=view)
    finally:
        conn.close()


app.action(DELETE_ACHIEVEMENT_ACTION_ID)(handle_delete_achievement)


def handle_achievements_list_submit(ack, body, client, logger):
    """Return from achievements list to settings — importable for unit tests."""
    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack(response_action="clear")
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region:
        logger.warning("Achievements list submit: region not found")
        ack(response_action="clear")
        return
    region = dict(region)
    region["team_id"] = team_id or region.get("team_id") or ""
    if regional_schema:
        region["schema_name"] = regional_schema
    ack(response_action="update", view=_config_modal(region))


app.view(ACHIEVEMENTS_LIST_CALLBACK_ID)(handle_achievements_list_submit)


def handle_config_submit(ack, body, client, logger):
    """Named listener for config modal save — importable for unit tests."""
    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack(response_action="errors", errors={"timezone": "Admin required"})
        return
    team_id, _, region = _region_context_from_body(body)
    if not region:
        ack(response_action="errors", errors={"timezone": "Region not found"})
        return
    values = {k: v for k, v in _parse_modal_values(body).items() if v is not None}
    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            sets = ", ".join(f"`{k}`=%s" for k in values)
            cur.execute(
                f"UPDATE `{pm}`.`regions` SET {sets} WHERE region=%s",
                (*values.values(), region["region"]),
            )
            conn.commit()
        ack(response_action="clear")
    finally:
        conn.close()


app.view(CALLBACK_ID)(handle_config_submit)


def handle_achievement_edit_submit(ack, body, client, logger):
    """Named listener for achievement add/edit save — importable for unit tests."""
    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack(response_action="errors", errors={"name": "Admin required"})
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        ack(response_action="errors", errors={"name": "Region not found"})
        return

    meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
    achievement_id = meta.get("achievement_id")
    values = _parse_achievement_form(body)
    errors = _validate_achievement(values)
    if errors:
        ack(response_action="errors", errors=errors)
        return

    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            if achievement_id:
                cur.execute(
                    f"SELECT id FROM `{regional_schema}`.`achievements_list` "
                    "WHERE code=%s AND id<>%s",
                    (values["code"], achievement_id),
                )
            else:
                cur.execute(
                    f"SELECT id FROM `{regional_schema}`.`achievements_list` WHERE code=%s",
                    (values["code"],),
                )
            if cur.fetchone():
                ack(response_action="errors", errors={"code": "Code already in use"})
                return

            if achievement_id:
                cur.execute(
                    f"""
                    UPDATE `{regional_schema}`.`achievements_list`
                    SET name=%s, description=%s, verb=%s, code=%s,
                        metric=%s, activity=%s, period=%s, threshold=%s
                    WHERE id=%s
                    """,
                    (
                        values["name"],
                        values["description"],
                        values["verb"],
                        values["code"],
                        values["metric"],
                        values["activity"],
                        values["period"],
                        values["threshold"],
                        achievement_id,
                    ),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO `{regional_schema}`.`achievements_list`
                    (name, description, verb, code, metric, activity, period, threshold)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        values["name"],
                        values["description"],
                        values["verb"],
                        values["code"],
                        values["metric"],
                        values["activity"],
                        values["period"],
                        values["threshold"],
                    ),
                )
            conn.commit()
            achievements = _load_achievements(cur, regional_schema)
            view = _achievements_list_modal(team_id, regional_schema, achievements)
            ack(response_action="update", view=view)
    finally:
        conn.close()


app.view(ACHIEVEMENT_EDIT_CALLBACK_ID)(handle_achievement_edit_submit)


# Schedule / PAX Reports / Kotter config listeners
from slack_schedule import register_schedule_listeners  # noqa: E402

register_schedule_listeners(app)


@app.event("app_home_opened")
def handle_app_home_opened(client, event, logger):
    """Minimal Home tab stub (full dashboard is a later plan)."""
    user_id = event.get("user")
    if not user_id:
        return
    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": "PAXMiner"},
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Configure reports and schedules with `/config-paxminer` "
                                "(workspace admins).\n\n"
                                "_Home dashboard charts coming soon._"
                            ),
                        },
                    },
                ],
            },
        )
    except Exception:
        logger.exception("app_home_opened views.publish failed")


def handler(event, context):
    """Lambda entrypoint: keep-warm short-circuit, else Bolt SlackRequestHandler."""
    if not is_http_request(event):
        return {"statusCode": 200, "body": "warm"}
    return SlackRequestHandler(app=app).handle(event, context)
