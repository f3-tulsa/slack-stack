import json
import logging
import os
from datetime import datetime, timedelta, date

import constants
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_bolt.oauth.oauth_flow import OAuthFlow
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth import OAuthStateUtils
from slack_sdk.oauth.installation_store.sqlalchemy import SQLAlchemyInstallationStore

from utilities import safe_get, get_user

# from google import authenticate, commands

from database import DbManager, get_engine
from db_oauth_stores import FixedSQLAlchemyOAuthStateStore
from database.orm import AO, Master, helper
from database.orm.views import vwAOsSort, vwMasterEvents, vwWeeklyEvents
from permissions import (
    PermissionLevel,
    UserPermission,
    can_edit_any_q_slot,
    can_manage_ao,
    can_manage_events_for_ao,
    can_open_manage_region_calendar,
    resolve_user_permission,
    resolved_paxminer_regional_schema,
)

from slack import forms
from slack.confirm_modals import (
    RECURRING_Q_SLOT_WARNING,
    confirm_modal_view,
    delete_confirm_modal_view,
    format_field_change,
    load_modal_metadata,
)
from slack.forms import ao, event, home, settings
from slack.handlers import (
    settings as settings_handler,
    weekly as weekly_handler,
    master as master_handler,
    ao as ao_handler,
)
from slack import actions, inputs
from field_encryption import require_encryption_key

try:
    from snapshot_restore_py import register_after_restore
except ImportError:

    def register_after_restore(func):
        return func


require_encryption_key()


def get_oauth_flow():
    if constants.LOCAL_DEVELOPMENT:
        return None
    engine = get_engine()
    client_id = os.environ[constants.SLACK_CLIENT_ID]
    installation_store = SQLAlchemyInstallationStore(client_id=client_id, engine=engine)
    state_store = FixedSQLAlchemyOAuthStateStore(
        expiration_seconds=OAuthStateUtils.default_expiration_seconds,
        engine=engine,
    )
    if os.environ.get("CREATE_OAUTH_TABLES", "").strip().lower() in ("1", "true", "yes"):
        installation_store.create_tables()
        state_store.create_tables()
    settings = OAuthSettings(
        client_id=client_id,
        client_secret=os.environ[constants.SLACK_CLIENT_SECRET],
        scopes=os.environ[constants.SLACK_SCOPES].split(","),
        installation_store=installation_store,
        state_store=state_store,
    )
    return OAuthFlow(settings=settings)


# process_before_response must be True when running on FaaS
app = App(
    process_before_response=True,
    oauth_flow=get_oauth_flow(),
)

# Inputs
schedule_create_length_days = 365


def _team_id_from_interaction(body):
    return (
        safe_get(body, "team", "id")
        or safe_get(body, "view", "team_id")
        or safe_get(body, "user", "team_id")
    )


def _context_for_home_refresh(body, client, bolt_context):
    team_id = _team_id_from_interaction(body)
    bot_token = ""
    if bolt_context and bolt_context.get("bot_token"):
        bot_token = bolt_context["bot_token"]
    elif getattr(client, "token", None):
        bot_token = client.token
    return {
        "user_id": safe_get(body, "user", "id"),
        "team_id": team_id,
        "bot_token": bot_token,
    }


def _display_slack_user(client, user_id):
    if not user_id:
        return "(none)"
    try:
        info = client.users_info(user=user_id)
        return (
            safe_get(info, "user", "profile", "display_name")
            or safe_get(info, "user", "profile", "real_name")
            or user_id
        )
    except Exception:
        return user_id


def _fmt_event_time_hhmm(t: str) -> str:
    if not t or len(t) < 4:
        return t or ""
    return f"{t[:2]}:{t[2:]}"


def _resolve_permission(client, user_id: str) -> UserPermission:
    user_info = client.users_info(user=user_id)
    return resolve_user_permission(
        user_info, user_id, resolved_paxminer_regional_schema()
    )


def _permission_denied_home(client, user_id, team_id, logger, context, message=None):
    user = get_user(user_id, client)
    home.refresh(
        client,
        user,
        logger,
        message
        or "You don't have permission to perform this action. "
        "<https://slack.com/help/articles/218124397-Change-a-members-role|"
        "Request admin status from your local space admin or owner>.",
        team_id,
        context,
    )


def _require_slack_admin(client, user_id, team_id, logger, context) -> UserPermission | None:
    perm = _resolve_permission(client, user_id)
    if perm.level != PermissionLevel.ADMIN:
        _permission_denied_home(client, user_id, team_id, logger, context)
        return None
    return perm


def _require_calendar_manager(client, user_id, team_id, logger, context) -> UserPermission | None:
    perm = _resolve_permission(client, user_id)
    if not can_open_manage_region_calendar(perm):
        _permission_denied_home(client, user_id, team_id, logger, context)
        return None
    return perm


def _require_manage_events_for_ao(
    client, user_id, team_id, logger, context, ao_channel_id: str
) -> UserPermission | None:
    perm = _resolve_permission(client, user_id)
    if not can_manage_events_for_ao(perm, ao_channel_id):
        _permission_denied_home(client, user_id, team_id, logger, context)
        return None
    return perm


def _require_manage_ao_for_channel(
    client, user_id, team_id, logger, context, ao_channel_id: str
) -> UserPermission | None:
    perm = _resolve_permission(client, user_id)
    if not can_manage_ao(perm, ao_channel_id):
        _permission_denied_home(client, user_id, team_id, logger, context)
        return None
    return perm


def _allowed_event_ao_filter(perm: UserPermission):
    """None = all AOs; list restricts to AOQ channels."""
    if perm.level == PermissionLevel.ADMIN:
        return None
    if perm.level == PermissionLevel.AOQ:
        return perm.aoq_channel_ids
    return None


def _ao_channel_for_display_name(team_id: str, ao_display_name: str) -> str | None:
    rows = DbManager.find_records(
        AO, [AO.team_id == team_id, AO.ao_display_name == ao_display_name]
    )
    return rows[0].ao_channel_id if rows else None


def _viewer_can_edit_q_slot(client, user_id: str, q_pax_name: str | None) -> bool:
    perm = _resolve_permission(client, user_id)
    if can_edit_any_q_slot(perm):
        return True
    ui = client.users_info(user=user_id)
    un = (
        safe_get(ui, "user", "profile", "display_name")
        or safe_get(ui, "user", "profile", "real_name")
        or None
    )
    return bool(un and q_pax_name and un == q_pax_name)


def _recurring_edit_single_ao(team_id, user_id, client, logger, ao_channel_id):
    event.select_recurring_form_for_edit(
        team_id, user_id, client, logger, ao_channel_id=ao_channel_id
    )


def _recurring_delete_single_ao(team_id, user_id, client, logger, ao_channel_id):
    event.select_recurring_form_for_delete(
        team_id, user_id, client, logger, ao_channel_id=ao_channel_id
    )


def publish_manage_calendar_screen(user_id, client, logger, team_id, bolt_context):
    """Show the Manage Region Calendar home tab (Slack admin/owner or AOQ)."""
    perm = _resolve_permission(client, user_id)
    user = get_user(user_id, client)
    if not can_open_manage_region_calendar(perm):
        top_message = (
            "You must be a Slack admin, owner, or AOQ to manage the schedule. "
            "<https://slack.com/help/articles/218124397-Change-a-members-role|"
            "Request admin status from your local space admin or owner>."
        )
        home.refresh(client, user, logger, top_message, team_id, bolt_context)
        return

    if perm.level == PermissionLevel.ADMIN:
        ao_buttons = [inputs.ADD_AO_FORM, inputs.EDIT_AO_FORM, inputs.DELETE_AO_FORM]
    else:
        ao_buttons = [inputs.EDIT_AO_FORM]

    blocks = [
        forms.make_header_row("Choose an option to manage your AOs:"),
        forms.make_action_button_row(ao_buttons),
        forms.make_header_row("Choose an option to manage your Recurring Events:"),
        forms.make_action_button_row(
            [
                inputs.ADD_RECURRING_EVENT_FORM,
                inputs.EDIT_RECURRING_EVENT_FORM,
                inputs.DELETE_RECURRING_EVENT_FORM,
            ]
        ),
        forms.make_header_row("Choose an option to manage a Single Event:"),
        forms.make_action_button_row(
            [
                inputs.ADD_SINGLE_EVENT_FORM,
                inputs.EDIT_SINGLE_EVENT_FORM,
                inputs.DELETE_SINGLE_EVENT_FORM,
            ]
        ),
        forms.make_action_button_row([inputs.CANCEL_BUTTON]),
    ]
    try:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error("Error publishing manage calendar home tab: %s", e)


def _refresh_home_ack(ack):
    ack()


def _refresh_home_lazy(body, client, logger, context):
    """Weinke PNG + S3 + DB can exceed Slack's 3s ack window; run after ack via lazy listener."""
    import weinke  # lazy: Pillow + boto3 only when generating images

    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    user = get_user(user_id, client)
    top_message = f"Welcome to QSignups, {user.name}!"
    try:
        weinke.generate_and_store_weinke(team_id, logger)
    except Exception:
        logger.exception("Weinke generation failed; refreshing home without new images")
    home.refresh(client, user, logger, top_message, team_id, context)


app.action(actions.REFRESH_ACTION)(ack=_refresh_home_ack, lazy=[_refresh_home_lazy])


def redirect_blocks(team_id: str, app_id: str):
    return [
        {
            "type": "section",
            "block_id": "refresh_home",
            "text": {"type": "mrkdwn", "text": "Looking for me or having issues? Click the button below to go to QSignups! If your screen is blank, click the refresh button."},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "go_to_home",
                    "text": {"type": "plain_text", "text": ":calendar: Go to QSignups", "emoji": True},
                    "value": "go_to_home",
                    "url": f"slack://app?team={team_id}&id={app_id}&tab=home",
                },
                {
                    "type": "button",
                    "action_id": actions.REFRESH_ACTION,
                    "text": {"type": "plain_text", "text": ":arrows_clockwise: Refresh QSignups", "emoji": True},
                    "value": "refresh_home",
                },
            ]
        }
    ]

@app.event("app_mention")
def handle_app_mentions(body, logger, client):
    logger.info(f"INFO: {body}")
    team_id = body["team_id"]
    app_id = body["api_app_id"]
    blocks = redirect_blocks(team_id, app_id)
    client.chat_postMessage(channel=body["event"]["channel"], text="Hello!", blocks=blocks)

def qsignups_slash(ack, body, client, logger, context, respond):
    ack()
    context["user_id"]
    team_id = context["team_id"]
    app_id = body["api_app_id"]
    blocks = redirect_blocks(team_id, app_id)
    client.views_open(trigger_id=body["trigger_id"], view={"type": "modal", "callback_id": "redirect", "blocks": blocks, "title": {"type": "plain_text", "text": "QSignups"}})

app.command("/qsignups")(ack= lambda ack: ack(), lazy=[qsignups_slash])

@app.command("/hello")
def respond_to_slack_within_3_seconds(ack):
    # This method is for synchronous communication with the Slack API server
    ack("Thanks!")


# @app.command("/google")
# def connect_google_calendar(ack, respond, command):
#     # This method is for synchronous communication with the Slack API server
#     ack()
#     commands.execute_command(command["text"], command["team_id"], command, respond)


@app.command("/schedule")
def display_upcoming_schedule(ack):
    # This method is for synchronous communication with the Slack API server
    ack("To be implemented: Upcoming Schedule!")


@app.event("app_home_opened")
def update_home_tab(client, event, logger, context, body):
    logger.info(event)
    user_id = context["user_id"]
    team_id = context["team_id"]
    
    if not safe_get(body, "event", "view"):
        user = get_user(user_id, client)
        top_message = f"Welcome to QSignups, {user.name}!"
        home.refresh(client, user, logger, top_message, team_id, context)


# @app.action(inputs.GOOGLE_DISCONNECT.action)
# def handle_google_disconnect(ack, body, client, logger, context):
#     ack()
#     team_id = context["team_id"]
#     user_id = context["user_id"]
#     user = get_user(user_id, client)
#     result = authenticate.disconnect(team_id)
#     if result.success:
#         top_message = f'You have disconnected from Google!'
#         home.refresh(client, user, logger, top_message, team_id, context)
#     else:
#         top_message = f'Something went wrong trying to disconnect!'
#         home.refresh(client, user, logger, top_message, team_id, context)

# @app.action(inputs.GOOGLE_CONNECT.action)
# def handle_google_connect(ack, body, client, logger, context):
#     ack()
#     team_id = context["team_id"]
#     user_id = context["user_id"]
#     user = get_user(user_id, client)
#     result = authenticate.connect(team_id)
#     if result.success:
#         top_message = f'You have connected from Google!'
#         home.refresh(client, user, logger, top_message, team_id, context)
#     else:
#         top_message = f'Something went wrong trying to connect!'
#         home.refresh(client, user, logger, top_message, team_id, context)


# triggers when user chooses to schedule a q
# @app.action("schedule_q_button")
# def handle_take_q_button(ack, body, client, logger, context):
#     ack()
#     logger.info(body)
#     user_id = context["user_id"]
#     user = get_user(user_id, client)
#     team_id = context["team_id"]
#     home.refresh(client, user, logger)


# triggers when user chooses to manager the schedule
@app.action(actions.MANAGE_SCHEDULE_ACTION)
def handle_manager_schedule_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    publish_manage_calendar_screen(user_id, client, logger, team_id, context)


@app.action(actions.BACK_TO_MANAGE_ACTION)
def handle_back_to_manage(ack, body, client, logger, context):
    ack()
    logger.info(body)
    publish_manage_calendar_screen(context["user_id"], client, logger, context["team_id"], context)



@app.action(inputs.ADD_AO_FORM.action)
def handle_add_ao_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    if not _require_slack_admin(client, user_id, team_id, logger, context):
        return
    ao.add_form(team_id, user_id, client, logger)


@app.action(inputs.EDIT_AO_FORM.action)
def handle_edit_ao_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    ao.edit_form(team_id, user_id, client, logger, allowed_ao_channel_ids=allowed)


@app.action(inputs.DELETE_AO_FORM.action)
def handle_delete_ao_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    if not _require_slack_admin(client, user_id, team_id, logger, context):
        return
    ao.delete_form(team_id, user_id, client, logger)


@app.action(inputs.ADD_SINGLE_EVENT_FORM.action)
def handle_add_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    event.add_single_form(team_id, user_id, client, logger, allowed_ao_channel_ids=allowed)


@app.action(inputs.EDIT_SINGLE_EVENT_FORM.action)
def handle_edit_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    event.edit_single_form(team_id, user_id, client, logger, allowed_ao_channel_ids=allowed)


@app.action(inputs.DELETE_SINGLE_EVENT_FORM.action)
def handle_delete_single_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    event.delete_single_form(team_id, user_id, client, logger, allowed_ao_channel_ids=allowed)


@app.action(inputs.ADD_RECURRING_EVENT_FORM.action)
def handle_add_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    event.add_recurring_form(team_id, user_id, client, logger, allowed_ao_channel_ids=allowed)


@app.action(inputs.EDIT_RECURRING_EVENT_FORM.action)
def handle_edit_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    event.make_ao_section_selector(
        team_id,
        user_id,
        client,
        logger,
        label="Please select an AO to edit:",
        action=actions.EDIT_RECURRING_EVENT_AO_SELECT,
        allowed_ao_channel_ids=allowed,
        on_single_ao_callback=_recurring_edit_single_ao,
    )


@app.action(actions.EDIT_RECURRING_EVENT_AO_SELECT)
def handle_edit_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    ao_channel_id = inputs.SECTION_SELECTOR.get_selected_value(body)
    if not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ao_channel_id):
        return
    event.select_recurring_form_for_edit(
        team_id, user_id, client, logger, input_data=body, ao_channel_id=ao_channel_id
    )


@app.action(inputs.DELETE_RECURRING_EVENT_FORM.action)
def handle_delete_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    perm = _require_calendar_manager(client, user_id, team_id, logger, context)
    if not perm:
        return
    allowed = _allowed_event_ao_filter(perm)
    event.make_ao_section_selector(
        team_id,
        user_id,
        client,
        logger,
        label="Please select an AO to delete a recurring event from:",
        action=actions.DELETE_RECURRING_EVENT_AO_SELECT,
        allowed_ao_channel_ids=allowed,
        on_single_ao_callback=_recurring_delete_single_ao,
    )


@app.action(actions.DELETE_RECURRING_EVENT_AO_SELECT)
def handle_delete_single_event_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    ao_channel_id = inputs.SECTION_SELECTOR.get_selected_value(body)
    if not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ao_channel_id):
        return
    event.select_recurring_form_for_delete(
        team_id, user_id, client, logger, input_data=body, ao_channel_id=ao_channel_id
    )


@app.action(inputs.GENERAL_SETTINGS.action)
def handle_general_settings_form(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    user = get_user(user_id, client)
    if _resolve_permission(client, user_id).level != PermissionLevel.ADMIN:
        top_message = (
            "You must be a Slack admin or owner to manage the settings. "
            "<https://slack.com/help/articles/218124397-Change-a-members-role|"
            "Request admin status from your local space admin or owner>."
        )
        home.refresh(client, user, logger, top_message, team_id, context)
        return
    settings.general_form(team_id, user_id, client, logger)


@app.action(actions.DELETE_RECURRING_SELECT_ACTION)
def handle_delete_recurring_select(ack, body, client, logger, context):
    ack()
    logger.info(body)
    team_id = context["team_id"]
    user_id = context["user_id"]
    event_id = int(body["actions"][0]["value"])
    rows = DbManager.find_records(vwWeeklyEvents, [vwWeeklyEvents.id == event_id])
    if not rows:
        logger.error("delete recurring: weekly event id %s not found", event_id)
        return
    weekly_evt = rows[0]
    if not _require_manage_events_for_ao(
        client, user_id, team_id, logger, context, weekly_evt.ao_channel_id
    ):
        return
    summary_lines = [
        f"*{weekly_evt.event_type}* at *{weekly_evt.ao_display_name}*",
        f"{weekly_evt.event_day_of_week}s @ {_fmt_event_time_hhmm(weekly_evt.event_time)}",
    ]
    meta = {"v": 1, "event_id": event_id}
    view = delete_confirm_modal_view(
        actions.CONFIRM_DELETE_RECURRING_VIEW,
        meta,
        summary_lines,
        warning_markdown="This will delete all future occurrences of this recurring event. This cannot be undone.",
    )
    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error("Error opening delete recurring confirm modal: %s", e)


@app.view(actions.CONFIRM_DELETE_RECURRING_VIEW)
def handle_confirm_delete_recurring_view(ack, body, client, logger, context):
    ack()
    logger.info("confirm_delete_recurring_modal submit")
    meta = load_modal_metadata(body["view"]["private_metadata"])
    user_id = body["user"]["id"]
    team_id = _team_id_from_interaction(body)
    user = get_user(user_id, client)
    hctx = _context_for_home_refresh(body, client, context)
    wrows = DbManager.find_records(vwWeeklyEvents, [vwWeeklyEvents.id == int(meta["event_id"])])
    if wrows and not _require_manage_events_for_ao(
        client, user_id, team_id, logger, hctx, wrows[0].ao_channel_id
    ):
        return
    response = weekly_handler.delete(client, user_id, team_id, logger, str(meta["event_id"]))
    home.refresh(client, user, logger, response.message, team_id, hctx)


@app.action(actions.SELECT_SLOT_EDIT_RECURRING_EVENT_FORM)
def handle_edit_recurring_event_slot_select(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    input_data = body["actions"][0]["value"]
    event_id = int(input_data)
    wrows = DbManager.find_records(vwWeeklyEvents, [vwWeeklyEvents.id == event_id])
    if not wrows:
        return
    if not _require_manage_events_for_ao(
        client, user_id, team_id, logger, context, wrows[0].ao_channel_id
    ):
        return
    perm = _resolve_permission(client, user_id)
    allowed = _allowed_event_ao_filter(perm)
    event.edit_recurring_form(
        team_id, user_id, client, logger, input_data, allowed_ao_channel_ids=allowed
    )


@app.action(actions.EDIT_RECURRING_EVENT_ACTION)
def handle_edit_recurring_event(ack, body, client, logger, context):
    ack()
    logger.info(body)
    team_id = context["team_id"]
    user_id = context["user_id"]
    input_data = body["view"]["state"]["values"]
    event_id = int(json.loads(body["view"].get("private_metadata") or "{}")["event_id"])
    weekly_evt = DbManager.find_records(vwWeeklyEvents, [vwWeeklyEvents.id == event_id])[0]
    if not _require_manage_events_for_ao(
        client, user_id, team_id, logger, context, weekly_evt.ao_channel_id
    ):
        return
    ao_display_name = inputs.AO_SELECTOR.get_selected_value(input_data)
    event_day_of_week = inputs.WEEKDAY_SELECTOR.get_selected_value(input_data)
    event_time = inputs.START_TIME_SELECTOR.get_selected_value(input_data).replace(":", "")
    event_end_time = inputs.END_TIME_SELECTOR.get_selected_value(input_data)
    if event_end_time:
        event_end_time = event_end_time.replace(":", "")
    event_type = inputs.EVENT_TYPE_SELECTOR.get_selected_value(input_data)
    if event_type == "Custom":
        event_type = inputs.CUSTOM_EVENT_INPUT.get_selected_value(input_data) or "Custom"
    summary_lines = []
    for line in (
        format_field_change("Event type", weekly_evt.event_type, event_type),
        format_field_change("AO", weekly_evt.ao_display_name, ao_display_name),
        format_field_change("Day", weekly_evt.event_day_of_week, event_day_of_week),
        format_field_change(
            "Start time",
            _fmt_event_time_hhmm(weekly_evt.event_time),
            _fmt_event_time_hhmm(event_time),
        ),
        format_field_change(
            "End time",
            _fmt_event_time_hhmm(weekly_evt.event_end_time or ""),
            _fmt_event_time_hhmm(event_end_time or ""),
        ),
    ):
        if line:
            summary_lines.append(line)
    if not summary_lines:
        summary_lines.append("_No field changes detected._")
    meta = {"v": 1, "event_id": event_id, "input_data": input_data}
    view = confirm_modal_view(
        actions.CONFIRM_EDIT_RECURRING_EVENT_VIEW,
        meta,
        summary_lines,
        warning_markdown=RECURRING_Q_SLOT_WARNING,
    )
    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error("Error opening edit recurring confirm modal: %s", e)


@app.view(actions.CONFIRM_EDIT_RECURRING_EVENT_VIEW)
def handle_confirm_edit_recurring_view(ack, body, client, logger, context):
    ack()
    logger.info("confirm_edit_recurring_event_modal submit")
    meta = load_modal_metadata(body["view"]["private_metadata"])
    user_id = body["user"]["id"]
    team_id = _team_id_from_interaction(body)
    user = get_user(user_id, client)
    hctx = _context_for_home_refresh(body, client, context)
    wrows = DbManager.find_records(vwWeeklyEvents, [vwWeeklyEvents.id == int(meta["event_id"])])
    if wrows and not _require_manage_events_for_ao(
        client, user_id, team_id, logger, hctx, wrows[0].ao_channel_id
    ):
        return
    response = weekly_handler.edit_with_state_values(
        client, user_id, team_id, logger, meta["event_id"], meta["input_data"]
    )
    home.refresh(client, user, logger, response.message, team_id, hctx)


@app.action("delete_single_event_ao_select")
def handle_delete_single_event_ao_select(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    ao_display_name = body["actions"][0]["selected_option"]["text"]["text"]
    ao_channel_id = body["actions"][0]["selected_option"]["value"]
    if not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ao_channel_id):
        return

    events = DbManager.find_records(
        vwMasterEvents,
        [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.ao_channel_id == ao_channel_id,
            vwMasterEvents.event_date > datetime.now(tz=constants.app_timezone()) - timedelta(weeks=1),
            vwMasterEvents.event_date <= date.today() + timedelta(weeks=constants.EVENT_PICKER_WEEKS),
        ],
    )
    events.sort(key=lambda e: (e.event_date, e.event_time or ""))

    # Construct view
    # Top of view
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Please select a Q slot to delete for:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{ao_display_name}*"}},
        {"type": "divider"},
    ]

    for event in events[:90]:
        event_date_time = datetime.strptime(
            event.event_date.strftime("%Y-%m-%d") + " " + event.event_time, "%Y-%m-%d %H%M"
        )
        date_fmt = event_date_time.strftime("%a, %m-%d @ %H%M")
        date_fmt_value = event_date_time.strftime("%Y-%m-%d %H:%M:%S")

        # Build buttons
        if event.q_pax_id is None:
            date_status = "OPEN!"
        else:
            date_status = event.q_pax_name

        action_id = "delete_single_event_button"
        value = date_fmt_value + "|" + event.ao_channel_id
        # Button template
        new_button = inputs.ActionButton(
            label=f"{date_fmt}: {date_status}", value=value, action=action_id
        )
        # Append button to list
        blocks.append(forms.make_action_button_row([new_button]))

    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    # Publish view
    try:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error(f"Error publishing home tab: {e}")


@app.action("delete_single_event_button")
def delete_single_event_button(ack, client, body, context, logger):
    ack()
    logger.info(body)
    team_id = context["team_id"]
    user_id = context["user_id"]
    input_data = body["actions"][0]["value"]
    selected_list = str.split(input_data, "|")
    selected_date = selected_list[0]
    ao_channel_id = selected_list[1]
    if not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ao_channel_id):
        return
    selected_date_dt = datetime.strptime(selected_date, "%Y-%m-%d %H:%M:%S")
    selected_date_db = selected_date_dt.date().strftime("%Y-%m-%d")
    selected_time_db = selected_date_dt.time().strftime("%H%M")
    ev_rows = DbManager.find_records(
        vwMasterEvents,
        [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.ao_channel_id == ao_channel_id,
            vwMasterEvents.event_date == selected_date_dt.date(),
            vwMasterEvents.event_time == selected_time_db,
        ],
    )
    ev = ev_rows[0] if ev_rows else None
    ao_name = (ev.ao_display_name if ev else None) or ao_channel_id
    date_fmt = selected_date_dt.strftime("%a, %m-%d @ %H%M")
    q_status = (
        "OPEN!"
        if ev and ev.q_pax_id is None
        else (ev.q_pax_name if ev else "(unknown)")
    )
    summary_lines = [
        f"*{ao_name}*",
        f"{date_fmt} -- Q: {q_status}",
    ]
    meta = {"v": 1, "input_data": input_data}
    view = delete_confirm_modal_view(
        actions.CONFIRM_DELETE_EVENT_VIEW,
        meta,
        summary_lines,
        warning_markdown="This cannot be undone.",
    )
    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error("Error opening delete single event confirm modal: %s", e)


@app.view(actions.CONFIRM_DELETE_EVENT_VIEW)
def handle_confirm_delete_event_view(ack, body, client, logger, context):
    ack()
    logger.info("confirm_delete_event_modal submit")
    meta = load_modal_metadata(body["view"]["private_metadata"])
    user_id = body["user"]["id"]
    team_id = _team_id_from_interaction(body)
    user = get_user(user_id, client)
    hctx = _context_for_home_refresh(body, client, context)
    parts = str.split(meta["input_data"], "|")
    if len(parts) >= 2 and not _require_manage_events_for_ao(
        client, user_id, team_id, logger, hctx, parts[1]
    ):
        return
    response = master_handler.delete(client, user_id, team_id, logger, meta["input_data"])
    home.refresh(client, user, logger, response.message, team_id, hctx)


@app.action("edit_ao_select")
def handle_edit_ao_select(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]

    selected_channel = body["view"]["state"]["values"]["edit_ao_select"]["edit_ao_select"]["selected_option"]["value"]

    aos: list[vwAOsSort] = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id])

    if selected_channel not in [ao.ao_channel_id for ao in aos]:
        home.refresh(
            client,
            user,
            logger,
            top_message="Selected channel not found - PAXMiner may not have added it to the aos table yet",
            team_id=team_id,
            context=context,
        )
        return
    if not _require_manage_ao_for_channel(
        client, user_id, team_id, logger, context, selected_channel
    ):
        return
    ao.publish_edit_ao_home(client, user_id, team_id, logger, selected_channel)


@app.action(actions.EDIT_SINGLE_EVENT_AO_SELECT)
def handle_edit_event_ao_select(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    ao_channel_id, ao_display_name = inputs.SECTION_SELECTOR.get_selected_value(input_data=body, text_too=True)
    if not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ao_channel_id):
        return
    event.publish_single_event_edit_slots(
        team_id, user_id, client, logger, ao_channel_id, ao_display_name
    )


@app.action(actions.EDIT_AO_ACTION)
def submit_edit_ao_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    team_id = context["team_id"]
    user_id = context["user_id"]
    page_label = body["view"]["blocks"][0]["text"]["text"]
    input_data = body["view"]["state"]["values"]
    ao_channel_id = json.loads(body["view"].get("private_metadata") or "{}")["ao_channel_id"]
    if not _require_manage_ao_for_channel(client, user_id, team_id, logger, context, ao_channel_id):
        return
    new_name = input_data["ao_display_name"]["ao_display_name"]["value"]
    new_loc = input_data["ao_location_subtitle"]["ao_location_subtitle"]["value"]
    new_site_q = safe_get(input_data, "site_q_user_id", "site_q_user_id", "selected_user")
    aos = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id, vwAOsSort.ao_channel_id == ao_channel_id])
    old = aos[0] if aos else None
    old_name = (old.ao_display_name or "") if old else ""
    old_loc = (old.ao_location_subtitle or "") if old else ""
    old_site_q = ao_handler.get_site_q(ao_channel_id)
    summary_lines = []
    for line in (
        format_field_change("AO Title", old_name, new_name),
        format_field_change("Location", old_loc, new_loc),
        format_field_change(
            "AOQ", _display_slack_user(client, old_site_q), _display_slack_user(client, new_site_q)
        ),
    ):
        if line:
            summary_lines.append(line)
    if not summary_lines:
        summary_lines.append("_No field changes detected; values match current records._")
    meta = {"v": 1, "page_label": page_label, "ao_channel_id": ao_channel_id, "input_data": input_data}
    view = confirm_modal_view(actions.CONFIRM_EDIT_AO_VIEW, meta, summary_lines)
    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error("Error opening edit AO confirm modal: %s", e)


@app.view(actions.CONFIRM_EDIT_AO_VIEW)
def handle_confirm_edit_ao_view(ack, body, client, logger, context):
    ack()
    logger.info("confirm_edit_ao_modal submit")
    meta = load_modal_metadata(body["view"]["private_metadata"])
    user_id = body["user"]["id"]
    team_id = _team_id_from_interaction(body)
    user = get_user(user_id, client)
    hctx = _context_for_home_refresh(body, client, context)
    if not _require_manage_ao_for_channel(
        client, user_id, team_id, logger, hctx, meta["ao_channel_id"]
    ):
        return
    response = ao_handler.edit(
        client, user_id, team_id, logger, meta["ao_channel_id"], meta["input_data"]
    )
    home.refresh(client, user, logger, response.message, team_id, hctx)


@app.action(actions.DELETE_AO_ACTION)
def submit_delete_ao_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    team_id = context["team_id"]
    user_id = context["user_id"]
    ao_channel_id = body["actions"][0]["value"]
    if not _require_slack_admin(client, user_id, team_id, logger, context):
        return
    aos = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id, vwAOsSort.ao_channel_id == ao_channel_id])
    ao_rec = aos[0] if aos else None
    ao_name = (ao_rec.ao_display_name if ao_rec else None) or ao_channel_id
    summary_lines = [
        f"*AO*: `{ao_name}`"
    ]
    meta = {"v": 1, "ao_channel_id": ao_channel_id}
    view = delete_confirm_modal_view(
        actions.CONFIRM_DELETE_AO_VIEW,
        meta,
        summary_lines,
        warning_markdown=":warning: This will also delete all associated calendar events! This cannot be undone! :warning:",
    )
    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error("Error opening delete AO confirm modal: %s", e)


@app.view(actions.CONFIRM_DELETE_AO_VIEW)
def handle_confirm_delete_ao_view(ack, body, client, logger, context):
    ack()
    logger.info("confirm_delete_ao_modal submit")
    meta = load_modal_metadata(body["view"]["private_metadata"])
    user_id = body["user"]["id"]
    team_id = _team_id_from_interaction(body)
    user = get_user(user_id, client)
    hctx = _context_for_home_refresh(body, client, context)
    if not _require_slack_admin(client, user_id, team_id, logger, hctx):
        return
    response = ao_handler.delete(client, user_id, team_id, logger, meta["ao_channel_id"])
    home.refresh(client, user, logger, response.message, team_id, hctx)


@app.action(actions.EDIT_SETTINGS_ACTION)
def handle_submit_general_settings_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]
    if not _require_slack_admin(client, user_id, team_id, logger, context):
        return
    # Gather inputs from form
    input_data = body["view"]["state"]["values"]
    response = settings_handler.update(client, user_id, team_id, logger, input_data)
    # Take the user back home
    if response.success:
        top_message = "Success! Changed general region settings"
    else:
        top_message = f"Sorry, there was a problem of some sort; please try again or contact your local administrator / Weasel Shaker. Error:\n{response.message}"
    home.refresh(client, user, logger, top_message, team_id, context)


@app.action(actions.ADD_AO_ACTION)
def handle_submit_add_ao_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]
    if not _require_slack_admin(client, user_id, team_id, logger, context):
        return
    input_data = body["view"]["state"]["values"]
    response = ao_handler.insert(client, user_id, team_id, logger, input_data)
    home.refresh(client, user, logger, response.message, team_id, context)


@app.action(actions.ADD_RECURRING_EVENT_ACTION)
def handle_submit_add_recurring_event_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]

    # Gather inputs from form
    input_data = body["view"]["state"]["values"]
    ao_display_name = safe_get(
        input_data,
        "ao_display_name_select_action",
        "ao_display_name_select_action",
        "selected_option",
        "value",
    )
    ach = _ao_channel_for_display_name(team_id, ao_display_name) if ao_display_name else None
    if ach and not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ach):
        return
    response = weekly_handler.insert(client, user_id, team_id, logger, input_data)
    home.refresh(client, user, logger, response.message, team_id, context)


@app.action(actions.ADD_SINGLE_EVENT_ACTION)
def handle_submit_add_single_event_button(ack, body, client, logger, context):
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]
    input_data = body["view"]["state"]["values"]
    ao_display_name = safe_get(
        input_data,
        "ao_display_name_select_action",
        "ao_display_name_select_action",
        "selected_option",
        "value",
    )
    ach = _ao_channel_for_display_name(team_id, ao_display_name) if ao_display_name else None
    if ach and not _require_manage_events_for_ao(client, user_id, team_id, logger, context, ach):
        return
    response = master_handler.insert(client, user_id, team_id, logger, input_data)
    home.refresh(client, user, logger, response.message, team_id, context)


# triggered when user makes an ao selection
@app.action("ao-select")
def ao_select_slot(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    logger.info(body)

    user_id = context["user_id"]
    team_id = context["team_id"]
    ao_display_name = body["actions"][0]["selected_option"]["text"]["text"]
    ao_channel_id = body["actions"][0]["selected_option"]["value"]

    events = DbManager.find_records(
        Master,
        [
            Master.team_id == team_id,
            Master.ao_channel_id == ao_channel_id,
            Master.event_date > datetime.now(tz=constants.app_timezone()),
            Master.event_date <= date.today() + timedelta(weeks=constants.EVENT_PICKER_WEEKS),
        ],
    )
    events.sort(key=lambda e: (e.event_date, e.event_time or ""))

    # Construct view
    # Top of view
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Please select an open Q slot for:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{ao_display_name}*"}},
        {"type": "divider"},
    ]

    for event in events[:90]:
        event_date_time = datetime.strptime(
            event.event_date.strftime("%Y-%m-%d") + " " + event.event_time, "%Y-%m-%d %H%M"
        )
        date_fmt = event_date_time.strftime("%a, %m-%d @ %H%M")

        # If slot is empty, show green button with primary (green) style button
        if event.q_pax_id is None:
            date_status = "OPEN!"
            date_style = "primary"
            action_id = "date_select_button"
            value = str(event_date_time)
            button_text = "Take slot"
        # Otherwise default (grey) button, listing Qs name
        else:
            date_status = event.q_pax_name
            date_style = "default"
            action_id = "taken_date_select_button"
            value = str(event_date_time) + "|" + event.q_pax_name
            button_text = "Edit Slot"

        # Button template
        new_section = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{event.event_type} {date_fmt}: {date_status}"},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": button_text, "emoji": True},
                "action_id": action_id,
                "value": value,
            },
        }
        if date_style == "primary":
            new_section["accessory"]["style"] = "primary"

        # Append button to list
        blocks.append(new_section)

    # Cancel button
    blocks.append(forms.make_action_button_row([inputs.CANCEL_BUTTON]))

    # Publish view
    try:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error(f"Error publishing home tab: {e}")


# triggered when user selects open slot
@app.action("date_select_button")
def handle_date_select_button(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]
    user = get_user(user_id, client)

    # gather and format selected date and time
    selected_date = body["actions"][0]["value"]
    selected_dt = datetime.strptime(selected_date, "%Y-%m-%d %H:%M:%S")

    # gather info needed for message and SQL
    ao_display_name = body["view"]["blocks"][1]["text"]["text"].replace("*", "")

    response = master_handler.assign_event_q(
        client, user, team_id, logger, selected_dt, ao_display_name=ao_display_name
    )

    # Generate top message and go back home
    if response.success:
        top_message = f"Got it, {user.name}! I have you down for the Q at *{ao_display_name}* on *{selected_dt.strftime('%A, %B %-d @ %H%M')}*"
    else:
        top_message = response.message or "Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker."

    home.refresh(client, user, logger, top_message, team_id, context)


# triggered when user selects open slot on a message
@app.action("date_select_button_from_message")
def handle_date_select_button_from_message(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]
    input_data = body

    ao_channel_id = body["channel"]["id"]
    selected_date = input_data["actions"][0]["value"]
    response = master_handler.assign_event_q(client, user, team_id, logger, selected_date, ao_channel_id=ao_channel_id)
    if response.success:
        # gather info needed for message and SQL
        message_ts = body["message"]["ts"]
        message_blocks = body["message"]["blocks"]
        message_ts = input_data["message"]["ts"]
        message_blocks = input_data["message"]["blocks"]

        # Update original message
        open_count = 0
        block_num = -1
        for counter, block in enumerate(message_blocks):
            logger.debug(
                "comparing accessory value=%s selected_date=%s",
                safe_get(block, "accessory", "value"),
                selected_date,
            )
            if safe_get(block, "accessory", "value") == selected_date:
                block_num = counter

            if safe_get(block, "accessory", "text", "text"):
                if block["accessory"]["text"]["text"][-5] == "OPEN!":
                    open_count += 1

        logger.debug("assign_event_q message block_num=%s", block_num)
        if block_num >= 0:
            message_blocks[block_num]["text"]["text"] = message_blocks[block_num]["text"]["text"].replace(
                "OPEN!", user.name
            )
            message_blocks[block_num]["accessory"]["action_id"] = "ignore_button"
            message_blocks[block_num]["accessory"]["value"] = selected_date + "|" + user.name
            message_blocks[block_num]["accessory"]["text"]["text"] = user.name
            del message_blocks[block_num]["accessory"]["style"]

            # update top message
            open_count += -1
            if open_count == 1:
                open_msg = " I see there is an open spot - who wants it?"
            elif open_count > 1:
                open_msg = " I see there are some open spots - who wants them?"
            else:
                open_msg = ""

            message_blocks[0]["text"]["text"] = f"Hello HIMs! Here is your Q lineup for the week.{open_msg}"

            # publish update
            logging.info("sending blocks: %s", message_blocks)
            client.chat_update(channel=ao_channel_id, ts=message_ts, blocks=message_blocks)


# triggered when user selects closed slot on a message
@app.action("ignore_button")
def handle_ignore_button(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()


# triggered when user selects an already-taken slot
@app.action("taken_date_select_button")
def handle_taken_date_select_button(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    logger.info(body)

    user_id = context["user_id"]
    context["team_id"]
    user_info_dict = client.users_info(user=user_id)
    user_name = (
        safe_get(user_info_dict, "user", "profile", "display_name")
        or safe_get(user_info_dict, "user", "profile", "real_name")
        or None
    )

    selected_value = body["actions"][0]["value"]
    selected_list = str.split(selected_value, "|")
    selected_date = selected_list[0]
    datetime.strptime(selected_date, "%Y-%m-%d %H:%M:%S")
    selected_user = selected_list[1]
    selected_ao = body["view"]["blocks"][1]["text"]["text"].replace("*", "")

    perm = resolve_user_permission(
        user_info_dict, user_id, resolved_paxminer_regional_schema()
    )
    if (user_name == selected_user) or can_edit_any_q_slot(perm):
        label2 = "myself" if user_name == selected_user else selected_user
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Would you like to edit or clear this slot?"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Edit this event", "emoji": True},
                        "value": f"{selected_date}|{selected_ao}",
                        "action_id": "edit_single_event_button",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"Take {label2} off this Q slot", "emoji": True},
                        "value": f"{selected_date}|{selected_ao}",
                        "action_id": "clear_slot_button",
                        "style": "danger",
                    }
                ],
            },
            forms.make_action_button_row([inputs.CANCEL_BUTTON]),
        ]

        # Publish view
        try:
            client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
        except Exception as e:
            logger.error(f"Error publishing home tab: {e}")
    # Check to see if user matches selected user id OR if they are an admin
    # If so, bring up buttons:
    #   block 1: drop down to add special qualifier (VQ, Birthday Q, F3versary, Forge, etc.)
    #   block 2: danger button to take Q off slot
    #   block 3: cancel button that takes the user back home


@app.action("edit_single_event_button")
def handle_edit_single_event_button(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    logger.info(body)
    user_id = context["user_id"]
    team_id = context["team_id"]

    # gather and format selected date and time
    selected_list = str.split(body["actions"][0]["value"], "|")
    selected_date = selected_list[0]
    selected_date_dt = datetime.strptime(selected_date, "%Y-%m-%d %H:%M:%S")
    selected_date_db = selected_date_dt.date().strftime("%Y-%m-%d")
    selected_time_db = selected_date_dt.time().strftime("%H%M")

    # gather info needed for input form
    ao_display_name = selected_list[1]

    event = DbManager.find_records(
        vwMasterEvents,
        [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.ao_display_name == ao_display_name,
            vwMasterEvents.event_date == selected_date_dt.date(),
            vwMasterEvents.event_time == selected_time_db,
        ],
    )[0]

    if not _viewer_can_edit_q_slot(client, user_id, event.q_pax_name):
        user = get_user(user_id, client)
        home.refresh(
            client,
            user,
            logger,
            "You can only edit Q slots for yourself unless you are a Slack admin, AOQ, or have Q'd a beatdown.",
            team_id,
            context,
        )
        return

    q_pax_id = event.q_pax_id
    q_pax_name = event.q_pax_name
    event_special = event.event_special
    ao_channel_id = event.ao_channel_id

    # build special qualifier
    # TODO: have "other" / freeform option
    special_list = [
        "None",
        "The Forge",
        "VQ",
        "F3versary",
        "Birthday Q",
        "AO Launch",
        "IronPAX",
        "Convergence",
        "Flag Handoff",
        "Ghost Q",
        "Roulette Q",
        "Q School",
    ]
    special_options = []
    for option in special_list:
        new_option = {"text": {"type": "plain_text", "text": option, "emoji": True}, "value": option}
        special_options.append(new_option)

    if event_special in special_list:
        initial_special = special_options[special_list.index(event_special)]
    else:
        initial_special = special_options[0]

    user_select_element = {
        "type": "multi_users_select",
        "placeholder": {"type": "plain_text", "text": "Select the Q", "emoji": True},
        "action_id": "edit_event_q_select",
        "max_selected_items": 1,
    }
    if q_pax_id is not None:
        user_select_element["initial_users"] = [q_pax_id]

    if not event.event_end_time:
        end_time_default = datetime.strftime(
            selected_date_dt + timedelta(minutes=constants.DEFAULT_EVENT_DURATION_MINUTES), "%H%M"
        )

    # Build blocks
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Editing info for:\n{selected_date_db} @ {selected_time_db} @ {ao_display_name}\nQ: {q_pax_name}",
            },
        },
        {
            "type": "input",
            "block_id": "edit_event_datepicker",
            "element": {
                "type": "datepicker",
                "initial_date": selected_date_dt.strftime("%Y-%m-%d"),
                "placeholder": {"type": "plain_text", "text": "Select date", "emoji": True},
                "action_id": "edit_event_datepicker",
            },
            "label": {"type": "plain_text", "text": "Event Date", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "edit_event_timepicker",
            "element": {
                "type": "timepicker",
                "initial_time": datetime.time(selected_date_dt).strftime("%H:%M"),
                "placeholder": {"type": "plain_text", "text": "Select time", "emoji": True},
                "action_id": "edit_event_timepicker",
            },
            "label": {"type": "plain_text", "text": "Event Time", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "edit_event_end_timepicker",
            "element": {
                "type": "timepicker",
                "initial_time": datetime.strptime(event.event_end_time or end_time_default, "%H%M").strftime("%H:%M"),
                "placeholder": {"type": "plain_text", "text": "Select time", "emoji": True},
                "action_id": "edit_event_end_timepicker",
            },
            "label": {"type": "plain_text", "text": "Event End Time", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "edit_event_q_select",
            "element": user_select_element,
            "label": {"type": "plain_text", "text": "Q", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "edit_event_special_select",
            "element": {
                "type": "static_select",
                "placeholder": {"type": "plain_text", "text": "Special event?", "emoji": True},
                "options": special_options,
                "initial_option": initial_special,
                "action_id": "edit_event_special_select",
            },
            "label": {"type": "plain_text", "text": "Special Event Qualifier", "emoji": True},
        },
    ]

    # Sumbit / Cancel buttons
    submit_button = inputs.ActionButton(
        label="Submit", style="primary", value=ao_channel_id, action=actions.EDIT_EVENT_ACTION
    )
    blocks.append(forms.make_action_button_row([submit_button, inputs.BACK_TO_MANAGE_BUTTON]))

    edit_meta = {
        "original_date": selected_date_db,
        "original_time": selected_time_db,
        "ao_channel_id": ao_channel_id,
        "q_pax_name": q_pax_name,
    }

    # Publish view
    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks,
                "private_metadata": json.dumps(edit_meta),
            },
        )
    except Exception as e:
        logger.error(f"Error publishing home tab: {e}")


# triggered when user hits submit on event edit
@app.action(actions.EDIT_EVENT_ACTION)
def handle_submit_edit_event_button(ack, client, body, logger, context):
    ack()
    logger.info(body)
    team_id = context["team_id"]
    user_id = context["user_id"]
    pm = json.loads(body["view"].get("private_metadata") or "{}")
    original_date = pm["original_date"]
    original_time = pm["original_time"]
    ao_channel_id = pm.get("ao_channel_id") or body["actions"][0]["value"]
    results = body["view"]["state"]["values"]
    selected_date = results["edit_event_datepicker"]["edit_event_datepicker"]["selected_date"]
    selected_time = results["edit_event_timepicker"]["edit_event_timepicker"]["selected_time"].replace(":", "")
    selected_end_time = results["edit_event_end_timepicker"]["edit_event_end_timepicker"]["selected_time"].replace(
        ":", ""
    )
    selected_q_list = results["edit_event_q_select"]["edit_event_q_select"].get("selected_users") or []
    selected_special = results["edit_event_special_select"]["edit_event_special_select"]["selected_option"]["text"][
        "text"
    ]
    new_q_display = (
        _display_slack_user(client, selected_q_list[0])
        if selected_q_list
        else "(open / none)"
    )
    try:
        ao_rec = helper.find_ao(team_id, ao_channel_id=ao_channel_id)
        if not ao_rec:
            evt = None
        else:
            records = DbManager.find_records(
                Master,
                [
                    Master.team_id == team_id,
                    Master.ao_channel_id == ao_rec.ao_channel_id,
                    Master.event_date == datetime.strptime(original_date, "%Y-%m-%d"),
                    Master.event_time == original_time,
                ],
            )
            evt = records[0] if records else None
    except Exception:
        evt = None
    old_q = (evt.q_pax_name or "(open / none)") if evt else (pm.get("q_pax_name") or "(open / none)")
    old_q_name = evt.q_pax_name if evt else pm.get("q_pax_name")
    if not _viewer_can_edit_q_slot(client, user_id, old_q_name):
        user = get_user(user_id, client)
        home.refresh(
            client,
            user,
            logger,
            "You don't have permission to edit this Q slot.",
            team_id,
            context,
        )
        return
    old_special = evt.event_special if evt and evt.event_special else "None"
    if old_special is None:
        old_special = "None"
    summary_lines = []
    for line in (
        format_field_change("Event date", original_date, selected_date),
        format_field_change("Event time", _fmt_event_time_hhmm(original_time), _fmt_event_time_hhmm(selected_time)),
        format_field_change(
            "End time",
            _fmt_event_time_hhmm(evt.event_end_time) if evt and evt.event_end_time else "(default)",
            _fmt_event_time_hhmm(selected_end_time),
        ),
        format_field_change("Q", old_q, new_q_display),
        format_field_change("Special", str(old_special), selected_special),
    ):
        if line:
            summary_lines.append(line)
    if not summary_lines:
        summary_lines.append("_No field changes detected._")
    meta = {
        "v": 1,
        "ao_channel_id": ao_channel_id,
        "original_date": original_date,
        "original_time": original_time,
        "state_values": results,
    }
    view = confirm_modal_view(actions.CONFIRM_EDIT_EVENT_VIEW, meta, summary_lines)
    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error("Error opening edit single event confirm modal: %s", e)


@app.view(actions.CONFIRM_EDIT_EVENT_VIEW)
def handle_confirm_edit_event_view(ack, body, client, logger, context):
    ack()
    logger.info("confirm_edit_event_modal submit")
    meta = load_modal_metadata(body["view"]["private_metadata"])
    user_id = body["user"]["id"]
    team_id = _team_id_from_interaction(body)
    user = get_user(user_id, client)
    hctx = _context_for_home_refresh(body, client, context)
    try:
        ao_rec = helper.find_ao(team_id, ao_channel_id=meta["ao_channel_id"])
        records = DbManager.find_records(
            Master,
            [
                Master.team_id == team_id,
                Master.ao_channel_id == ao_rec.ao_channel_id,
                Master.event_date == datetime.strptime(meta["original_date"], "%Y-%m-%d"),
                Master.event_time == meta["original_time"],
            ],
        )
        evt0 = records[0] if records else None
        qn = evt0.q_pax_name if evt0 else None
    except Exception:
        qn = None
    if not _viewer_can_edit_q_slot(client, user_id, qn):
        home.refresh(
            client,
            user,
            logger,
            "You don't have permission to edit this Q slot.",
            team_id,
            hctx,
        )
        return
    response = master_handler.update_events_from_state(
        client,
        user,
        team_id,
        logger,
        meta["ao_channel_id"],
        meta["state_values"],
        meta["original_date"],
        meta["original_time"],
    )
    home.refresh(client, user, logger, response.message, team_id, hctx)


# triggered when user hits cancel or some other button that takes them home
@app.action("clear_slot_button")
def handle_clear_slot_button(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    logger.info(body)
    user_id = context["user_id"]
    user = get_user(user_id, client)
    team_id = context["team_id"]
    input_data = body["actions"][0]["value"]
    selected_list = str.split(input_data, "|")
    selected_date = datetime.strptime(selected_list[0], "%Y-%m-%d %H:%M:%S")

    # gather info needed for message and SQL
    ao_display_name = selected_list[1]
    result = helper.find_master_event(team_id, selected_date, ao_display_name=ao_display_name)
    if result and not _viewer_can_edit_q_slot(client, user_id, result.event.q_pax_name):
        home.refresh(
            client,
            user,
            logger,
            "You don't have permission to clear this Q slot.",
            team_id,
            context,
        )
        return

    response = master_handler.clear_event_q(client, user, team_id, logger, ao_display_name, selected_date)
    home.refresh(client, user, logger, response.message, team_id, context)


# triggered when user hits cancel or some other button that takes them home
@app.action(actions.CANCEL_BUTTON_ACTION)
def cancel_button_select(ack, client, body, logger, context):
    # acknowledge action and log payload
    ack()
    # print('Logging body and context:')
    # logging.info(body)
    # logging.info(context)
    user_id = context["user_id"]
    team_id = context["team_id"]
    user = get_user(user_id, client)
    top_message = f"Welcome to QSignups, {user.name}!"
    home.refresh(client, user, logger, top_message, team_id, context)


SlackRequestHandler.clear_all_log_handlers()
logger = logging.getLogger()
logger.setLevel(level=logging.INFO)
# logging.basicConfig(format="%(asctime)s %(message)s", level=logging.DEBUG)


def _warmup(log: logging.Logger) -> None:
    """Pre-warm DB pool and Fernet derivation for EventBridge keep-warm."""
    from sqlalchemy import text

    from database import get_engine
    from field_encryption import _get_fernet, require_encryption_key

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("Keep-warm: DB connection verified")
    except Exception:
        log.warning("Keep-warm: DB ping failed", exc_info=True)
    try:
        _get_fernet(require_encryption_key())
        log.info("Keep-warm: Fernet key derived")
    except Exception:
        log.warning("Keep-warm: Fernet derivation failed", exc_info=True)


@register_after_restore
def _on_snapstart_restore() -> None:
    """Dispose stale DB pool from snapshot and re-warm connections."""
    from database import get_engine

    try:
        get_engine().dispose()
    except Exception:
        pass
    _warmup(logger)


def handler(event, context):
    request_id = getattr(context, "aws_request_id", None) if context else None
    try:
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except json.JSONDecodeError as e:
                logger.warning(
                    "QSignups handler: event body is not JSON (request_id=%s): %s",
                    request_id,
                    e,
                )
        if isinstance(event, dict) and event.get("source") == "qsignups.extend-schedule":
            logger.info(
                "QSignups handler start kind=extend_schedule request_id=%s",
                request_id,
            )
            from slack.handlers.weekly import extend_all_schedules

            extend_all_schedules(logger)
            return {"statusCode": 200, "body": "OK"}

        if isinstance(event, dict) and (
            event.get("source") == "aws.events" or event.get("detail-type")
        ):
            _warmup(logger)
            return {"statusCode": 200, "body": "warm"}

        logger.info(
            "QSignups handler start kind=slack_bolt request_id=%s",
            request_id,
        )
        slack_handler = SlackRequestHandler(app=app)
        return slack_handler.handle(event, context)
    except Exception:
        logging.exception("QSignups handler failed")
        raise


# # -- OAuth flow -- #
# export SLACK_SIGNING_SECRET=***
# export SLACK_BOT_TOKEN=xoxb-***
# export SLACK_CLIENT_ID=111.111
# export SLACK_CLIENT_SECRET=***
# export SLACK_SCOPES=app_mentions:read,chat:write

# AWS IAM Role: bolt_python_s3_storage
#   - AmazonS3FullAccess
#   - AWSLambdaBasicExecutionRole

# rm -rf latest_slack_bolt && cp -pr ../../src latest_slack_bolt
# pip install python-lambda
# lambda deploy --config-file aws_lambda_oauth_config.yaml --requirements requirements_oauth.txt

if __name__ == "__main__":
    app.start(port=int(os.environ.get("PORT", 3000)))
