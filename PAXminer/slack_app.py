"""
Lightweight Slack Bolt front door for PAXMiner.

Acks interactive requests quickly; heavy work (Schedule Run Now) is
async-invoked on ScheduleFunction. Keep-warm EventBridge pings
short-circuit before Bolt.
"""

from __future__ import annotations

import logging
import os

from datetime import date

from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk.errors import SlackApiError

from config_paxminer import (
    ACHIEVEMENT_EDIT_CALLBACK_ID,
    ACHIEVEMENTS_LIST_CALLBACK_ID,
    ACHIEVEMENTS_PAGE_NEXT_ACTION_ID,
    ACHIEVEMENTS_PAGE_PREV_ACTION_ID,
    ADD_ACHIEVEMENT_ACTION_ID,
    BACKFILL_ACHIEVEMENT_ACTION_ID,
    CALLBACK_ID,
    DELETE_ACHIEVEMENT_ACTION_ID,
    DELETE_ALL_ACHIEVEMENTS_ACTION_ID,
    DUPLICATE_ACHIEVEMENT_ACTION_ID,
    EDIT_ACHIEVEMENT_ACTION_ID,
    REEVAL_FROM_ACTION_ID,
    REEVAL_TO_ACTION_ID,
    RESTORE_ACHIEVEMENTS_ACTION_ID,
    SELECT_ACHIEVEMENT_ACTION_ID,
    _achievement_edit_modal,
    _achievements_list_modal,
    _config_modal,
    _load_achievement,
    _load_achievements,
    _load_activity_options,
    _parse_achievement_form,
    _parse_metadata,
    _parse_modal_values,
    _reeval_dates_from_payload,
    _region_for_team,
    _registry_db,
    _selected_achievement_id,
    _validate_achievement,
    achievement_award_impact,
    delete_all_achievements,
    earliest_beatdown_date,
    restore_achievement_defaults,
    uniquify_achievement_code,
)
from paxminer_db import connect_from_env, paxminer_schema_from_env
from slack_blocks import counted_noun
from slack_http import ADMIN_REQUIRED_TEXT, is_http_request, is_slack_admin, notify_admin_required

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


def _error_control_label(body: dict | None) -> str:
    """Button label, else action_id, else view callback_id — for operator error DMs."""
    if not body:
        return ""
    actions = body.get("actions") or []
    action = actions[0] if actions else {}
    label = ((action.get("text") or {}).get("text") or "").strip()
    if not label:
        label = (action.get("action_id") or "").strip()
    if label:
        return label
    return ((body.get("view") or {}).get("callback_id") or "").strip()


def operator_error_notice(error: BaseException, body: dict | None = None) -> str:
    """Short operator-facing reason for an unhandled Bolt error. No log-channel pointer."""
    reason = ""
    if isinstance(error, SlackApiError):
        resp = error.response
        if resp is not None and hasattr(resp, "get"):
            reason = str(resp.get("error") or "").strip()
    if not reason:
        reason = str(error).strip() or type(error).__name__
    reason = " ".join(reason.split())
    control = _error_control_label(body)
    suffix = f" ({control})" if control else ""
    notice = f"Something went wrong: {reason}{suffix}"
    if len(notice) > 200:
        notice = notice[:197] + "..."
    return notice


@app.error
def handle_error(error, body, logger, client):
    logger.exception("Unhandled Slack Bolt error: %s", error)
    user_id = body.get("user_id") or (body.get("user") or {}).get("id")
    channel_id = body.get("channel_id") or (body.get("channel") or {}).get("id")
    notice = operator_error_notice(error, body)
    if user_id and channel_id:
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=notice,
            )
            return
        except Exception:
            logger.exception("Failed to send error ephemeral")
    if user_id:
        try:
            opened = client.conversations_open(users=user_id)
            dm = (opened.get("channel") or {}).get("id")
            if dm:
                client.chat_postMessage(channel=dm, text=notice)
        except Exception:
            logger.exception("Failed to send error DM")


def _strip_channel_initials(view: dict) -> dict:
    """Return a copy of the modal view without channels_select initial_channel."""
    import copy

    cleaned = copy.deepcopy(view)
    for block in cleaned.get("blocks") or []:
        element = block.get("element") or {}
        if element.get("type") == "channels_select":
            element.pop("initial_channel", None)
        if element.get("type") == "conversations_select":
            element.pop("initial_conversation", None)
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
        ack(text=ADMIN_REQUIRED_TEXT, response_type="ephemeral")
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
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            options = _load_activity_options(cur, regional_schema)
        view = _achievement_edit_modal(team_id, regional_schema, None, activity_options=options)
        client.views_update(view_id=body["view"]["id"], view=view)
    finally:
        conn.close()


app.action(ADD_ACHIEVEMENT_ACTION_ID)(handle_add_achievement)


def _refresh_achievements_list(
    client,
    body,
    team_id,
    regional_schema,
    notice: str | None = None,
    *,
    page=None,
    selected_id=None,
    reeval_from=None,
    reeval_to=None,
) -> None:
    """Re-render the list modal with an inline notice (modal actions have no response_url)."""
    meta = _parse_metadata((body.get("view") or {}).get("private_metadata"))
    if page is None:
        try:
            page = int(meta.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
    if selected_id is None and meta.get("selected_id") not in (None, ""):
        try:
            selected_id = int(meta["selected_id"])
        except (TypeError, ValueError):
            selected_id = None
    reeval_from = reeval_from or meta.get("reeval_from")
    reeval_to = reeval_to or meta.get("reeval_to")
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            achievements = _load_achievements(cur, regional_schema)
            if not reeval_from:
                chosen = next((a for a in achievements if a.get("id") == selected_id), None)
                reeval_from = _iso_date((chosen or {}).get("effective_from")) or earliest_beatdown_date(
                    cur, regional_schema
                )
        client.views_update(
            view_id=body["view"]["id"],
            view=_achievements_list_modal(
                team_id,
                regional_schema,
                achievements,
                notice=notice,
                page=page,
                selected_id=selected_id,
                reeval_from=reeval_from,
                reeval_to=reeval_to,
            ),
        )
    finally:
        conn.close()


def _iso_date(value) -> str | None:
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    text = str(value).strip()
    return text[:10] if text else None


def handle_edit_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
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
            options = _load_activity_options(cur, regional_schema)
            if row:
                awards, pax = achievement_award_impact(
                    cur, regional_schema, selected_id
                )
                row["award_count"] = awards
                row["pax_count"] = pax
        if not row:
            _refresh_achievements_list(
                client, body, team_id, regional_schema, "Achievement not found."
            )
            return
        view = _achievement_edit_modal(
            team_id, regional_schema, row, activity_options=options
        )
        client.views_update(view_id=body["view"]["id"], view=view)
    finally:
        conn.close()


app.action(EDIT_ACHIEVEMENT_ACTION_ID)(handle_edit_achievement)


def handle_delete_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
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
            row = _load_achievement(cur, regional_schema, selected_id)
            if not row:
                _refresh_achievements_list(
                    client, body, team_id, regional_schema, "Achievement not found."
                )
                return
            cnt, pax_cnt = achievement_award_impact(
                cur, regional_schema, selected_id
            )
            cur.execute(
                f"DELETE FROM `{regional_schema}`.`achievements_awarded` WHERE achievement_id=%s",
                (selected_id,),
            )
            cur.execute(
                f"DELETE FROM `{regional_schema}`.`achievement_versions` WHERE achievement_id=%s",
                (selected_id,),
            )
            cur.execute(
                f"DELETE FROM `{regional_schema}`.`achievements_list` WHERE id=%s",
                (selected_id,),
            )
            conn.commit()
        name = row.get("name") or "achievement"
        code = row.get("code") or name
        awards = counted_noun(cnt, "award")
        pax = counted_noun(pax_cnt, "PAX", "PAX")
        notice = f"Deleted `{code}` ({awards} from {pax} removed)."
        _refresh_achievements_list(client, body, team_id, regional_schema, notice)
        region = dict(region)
        region["schema_name"] = regional_schema
        _post_achievement_admin_notice(
            region,
            f"Achievement *{name}* was deleted along with {awards} from {pax}.",
            f"Achievement *{name}* was deleted by `{_log_actor_name(client, user_id)}` ({awards} from {pax} removed)",
        )
    except Exception:
        logger.exception("achievement delete failed id=%s", selected_id)
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not delete achievement — try again."
        )
    finally:
        conn.close()


app.action(DELETE_ACHIEVEMENT_ACTION_ID)(handle_delete_achievement)


def handle_backfill_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    selected_id = _selected_achievement_id(body)
    if not selected_id:
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Select an achievement to re-evaluate."
        )
        return
    start, end = _reeval_dates_from_payload(body)
    from slack_schedule import queue_achievement_backfill

    try:
        queue_achievement_backfill(
            schema=regional_schema,
            achievement_id=selected_id,
            actor=user_id,
            start=start,
            end=end,
        )
        range_s = f"{start or '…'} to {end or '…'}"
        _refresh_achievements_list(
            client,
            body,
            team_id,
            regional_schema,
            f"Re-evaluate queued for {range_s}. Watch the log channel.",
            selected_id=selected_id,
            reeval_from=start,
            reeval_to=end,
        )
    except Exception:
        logger.exception("queue achievement backfill failed")
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not queue re-evaluate."
        )


app.action(BACKFILL_ACHIEVEMENT_ACTION_ID)(handle_backfill_achievement)


def handle_duplicate_achievement(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    selected_id = _selected_achievement_id(body)
    if not selected_id:
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Open an achievement with the pencil first."
        )
        return
    from config_schedule import duplicate_name

    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            row = _load_achievement(cur, regional_schema, selected_id)
            options = _load_activity_options(cur, regional_schema)
            if not row:
                _refresh_achievements_list(
                    client, body, team_id, regional_schema, "Achievement not found."
                )
                return
            draft = dict(row)
            draft.pop("id", None)
            draft["code"] = uniquify_achievement_code(
                cur, regional_schema, row.get("code") or "achievement"
            )
            draft["name"] = duplicate_name(row.get("name"), row.get("code"))
        client.views_update(
            view_id=body["view"]["id"],
            view=_achievement_edit_modal(
                team_id, regional_schema, draft, activity_options=options
            ),
        )
    finally:
        conn.close()


app.action(DUPLICATE_ACHIEVEMENT_ACTION_ID)(handle_duplicate_achievement)


def handle_delete_all_achievements(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            counts = delete_all_achievements(cur, regional_schema)
            conn.commit()
        _refresh_achievements_list(
            client,
            body,
            team_id,
            regional_schema,
            (
                f"Deleted {counted_noun(counts['achievements'], 'achievement')} and "
                f"{counted_noun(counts['awards'], 'award')}."
            ),
            page=0,
        )
    except Exception:
        logger.exception("delete all achievements failed")
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not delete all achievements."
        )
    finally:
        conn.close()


app.action(DELETE_ALL_ACHIEVEMENTS_ACTION_ID)(handle_delete_all_achievements)


def handle_restore_achievements(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            added = restore_achievement_defaults(cur, regional_schema)
            conn.commit()
        _refresh_achievements_list(
            client,
            body,
            team_id,
            regional_schema,
            f"Restored defaults ({added} missing builtin(s) added).",
            page=0,
        )
    except Exception:
        logger.exception("restore achievement defaults failed")
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not restore achievement defaults."
        )
    finally:
        conn.close()


app.action(RESTORE_ACHIEVEMENTS_ACTION_ID)(handle_restore_achievements)


def handle_achievements_page(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    action = (body.get("actions") or [{}])[0]
    try:
        page = int(action.get("value") or 0)
    except ValueError:
        page = 0
    _refresh_achievements_list(
        client, body, team_id, regional_schema, page=page
    )


app.action(ACHIEVEMENTS_PAGE_PREV_ACTION_ID)(handle_achievements_page)
app.action(ACHIEVEMENTS_PAGE_NEXT_ACTION_ID)(handle_achievements_page)


def handle_reeval_controls(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    action = (body.get("actions") or [{}])[0]
    aid = action.get("action_id")
    selected_id = _selected_achievement_id(body)
    start, end = _reeval_dates_from_payload(body)
    if aid == SELECT_ACHIEVEMENT_ACTION_ID:
        conn = connect_from_env(_registry_db())
        try:
            with conn.cursor() as cur:
                row = _load_achievement(cur, regional_schema, selected_id) if selected_id else None
                start = _iso_date((row or {}).get("effective_from")) or earliest_beatdown_date(
                    cur, regional_schema
                )
        finally:
            conn.close()
    _refresh_achievements_list(
        client,
        body,
        team_id,
        regional_schema,
        selected_id=selected_id,
        reeval_from=start,
        reeval_to=end,
    )


app.action(SELECT_ACHIEVEMENT_ACTION_ID)(handle_reeval_controls)
app.action(REEVAL_FROM_ACTION_ID)(handle_reeval_controls)
app.action(REEVAL_TO_ACTION_ID)(handle_reeval_controls)


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


def _join_log_channel(client, channel_id: str | None, logger) -> None:
    """Best-effort conversations.join so PAXMiner can post to the picked log channel."""
    cid = (channel_id or "").strip()
    if not cid:
        return
    try:
        client.conversations_join(channel=cid)
    except SlackApiError as exc:
        err = (exc.response or {}).get("error")
        if err not in ("already_in_channel", "method_not_supported_for_channel_type"):
            logger.warning("join log channel failed channel=%s error=%s", cid, err)


def handle_config_submit(ack, body, client, logger):
    """Named listener for config modal save — importable for unit tests."""
    from schedule_schema import ensure_log_channel_column

    user_id = (body.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id, client=client):
        ack(response_action="errors", errors={"timezone": "Admin required"})
        return
    team_id, _, region = _region_context_from_body(body)
    if not region:
        ack(response_action="errors", errors={"timezone": "Region not found"})
        return
    region_key = region.get("region")
    if not region_key:
        ack(response_action="errors", errors={"timezone": "Region key missing"})
        return
    parsed = _parse_modal_values(body)
    timezone = (parsed.get("timezone") or "America/Chicago").strip() or "America/Chicago"
    log_channel = parsed.get("log_channel") or None
    pm = paxminer_schema_from_env()
    try:
        conn = connect_from_env(_registry_db())
    except Exception as exc:
        logger.exception("config submit connect failed")
        ack(response_action="errors", errors={"timezone": f"Save failed: {str(exc)[:120]}"})
        return
    try:
        with conn.cursor() as cur:
            ensure_log_channel_column(cur, pm)
            cur.execute(
                f"UPDATE `{pm}`.`regions` SET `timezone`=%s, `log_channel`=%s WHERE region=%s",
                (timezone, log_channel, region_key),
            )
            conn.commit()
        _join_log_channel(client, log_channel, logger)
        ack(response_action="clear")
    except Exception as exc:
        logger.exception("config submit failed")
        ack(response_action="errors", errors={"timezone": f"Save failed: {str(exc)[:120]}"})
    finally:
        conn.close()


app.view(CALLBACK_ID)(handle_config_submit)


def _effective_range(values: dict, *, inherit_from=None):
    mode = values.get("range_mode") or "going_forward"
    if mode == "all_previous":
        return inherit_from, values.get("effective_to") or None
    if mode == "custom":
        return values.get("effective_from") or inherit_from, values.get("effective_to") or None
    return date.today().isoformat(), values.get("effective_to") or None


def _log_actor_name(client, user_id: str) -> str:
    """Display name for paxminer_logs; never a mention or Slack user ID."""
    from slack_util import resolve_display_name

    return resolve_display_name(client, user_id)


def _post_achievement_admin_notice(region: dict, channel_text: str, log_text: str) -> None:
    from common.encryption import decrypt_field
    from slack_util import post_log, post_message, slack_client

    token_enc = region.get("slack_token")
    if not token_enc:
        return
    try:
        client = slack_client(decrypt_field(token_enc))
        channel = (region.get("achievement_channel") or "").strip() or None
        if channel:
            post_message(client, channel, channel_text)
        post_log(client, log_text, region=region)
    except Exception:
        logging.getLogger(__name__).debug("achievement admin notice skipped", exc_info=True)


def handle_achievement_edit_submit(ack, body, client, logger):
    """Named listener for achievement add/edit save — importable for unit tests."""
    from achievements.activity import activity_legacy_mirror
    from achievements.versions import insert_version, mirror_list_params, params_changed, supersede_and_insert
    from slack_schedule import queue_achievement_backfill

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
    if achievement_id:
        values["code"] = values.get("code") or ""
    errors = _validate_achievement(values, require_code=not bool(achievement_id))
    if errors:
        ack(response_action="errors", errors=errors)
        return

    try:
        conn = connect_from_env(_registry_db())
    except Exception as exc:
        logger.exception("achievement edit connect failed")
        ack(response_action="errors", errors={"name": f"Save failed: {str(exc)[:120]}"})
        return
    queued_backfill = False
    from_date = to_date = None
    try:
        with conn.cursor() as cur:
            if not achievement_id:
                cur.execute(
                    f"SELECT id FROM `{regional_schema}`.`achievements_list` WHERE code=%s",
                    (values["code"],),
                )
                if cur.fetchone():
                    ack(response_action="errors", errors={"code": "Code already in use"})
                    return
            existing = _load_achievement(cur, regional_schema, achievement_id) if achievement_id else None
            if achievement_id and existing:
                values["code"] = existing.get("code") or values["code"]
                cur.execute(
                    f"""
                    UPDATE `{regional_schema}`.`achievements_list`
                    SET name=%s, description=%s, verb=%s, enabled=%s
                    WHERE id=%s
                    """,
                    (
                        values["name"],
                        values["description"],
                        values["verb"],
                        values["enabled"],
                        achievement_id,
                    ),
                )
                was_enabled = int(existing.get("enabled") or 1)
                if not was_enabled and values["enabled"]:
                    from_date, to_date = date.today().isoformat(), None
                    supersede_and_insert(
                        cur,
                        regional_schema,
                        achievement_id=int(achievement_id),
                        code=values["code"],
                        metric=values["metric"],
                        activity_list=values["activity_list"],
                        period=values["period"],
                        threshold=values["threshold"],
                        effective_from=from_date,
                        effective_to=to_date,
                        created_by=user_id,
                    )
                elif params_changed(existing, values):
                    inherit = existing.get("effective_from")
                    if values.get("apply_mode") == "retroactive":
                        from_date, to_date = inherit, values.get("effective_to")
                        queued_backfill = True
                    else:
                        from_date, to_date = date.today().isoformat(), values.get("effective_to")
                    supersede_and_insert(
                        cur,
                        regional_schema,
                        achievement_id=int(achievement_id),
                        code=values["code"],
                        metric=values["metric"],
                        activity_list=values["activity_list"],
                        period=values["period"],
                        threshold=values["threshold"],
                        effective_from=from_date,
                        effective_to=to_date,
                        created_by=user_id,
                    )
                else:
                    # Cosmetic only: keep versions; still mirror if list columns drifted.
                    pass
            else:
                from_date, to_date = _effective_range(values)
                cur.execute(
                    f"""
                    INSERT INTO `{regional_schema}`.`achievements_list`
                    (name, description, verb, code, metric, activity, period, threshold, enabled)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        values["name"],
                        values["description"],
                        values["verb"],
                        values["code"],
                        values["metric"],
                        activity_legacy_mirror(values["activity_list"])[:32],
                        values["period"],
                        values["threshold"],
                        values["enabled"],
                    ),
                )
                achievement_id = int(cur.lastrowid)
                insert_version(
                    cur,
                    regional_schema,
                    achievement_id=achievement_id,
                    code=values["code"],
                    metric=values["metric"],
                    activity_list=values["activity_list"],
                    period=values["period"],
                    threshold=values["threshold"],
                    effective_from=from_date,
                    effective_to=to_date,
                    created_by=user_id,
                    version=1,
                )
                if values.get("range_mode") in ("all_previous", "custom"):
                    queued_backfill = True
            conn.commit()
            if queued_backfill and achievement_id:
                try:
                    queue_achievement_backfill(
                        schema=regional_schema,
                        achievement_id=int(achievement_id),
                        actor=user_id,
                        start=from_date,
                        end=to_date,
                    )
                except Exception:
                    logger.exception("queue backfill after edit failed")
            achievements = _load_achievements(cur, regional_schema)
            view = _achievements_list_modal(team_id, regional_schema, achievements)
            ack(response_action="update", view=view)
    except Exception as exc:
        logger.exception("achievement edit submit failed")
        ack(response_action="errors", errors={"name": f"Save failed: {str(exc)[:120]}"})
    finally:
        conn.close()


app.view(ACHIEVEMENT_EDIT_CALLBACK_ID)(handle_achievement_edit_submit)


# Schedule / PAX Reports / Kotter config listeners
from slack_schedule import register_schedule_listeners  # noqa: E402

register_schedule_listeners(app)


HOME_OPEN_SETTINGS_ACTION_ID = "paxminer_home_open_settings"


def _home_view(*, admin: bool) -> dict:
    """App Home for every user; Settings control is admin-only."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "PAXMiner"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "_Home dashboard charts coming soon._",
            },
        },
    ]
    if admin:
        blocks.append(
            {
                "type": "actions",
                "block_id": "home_settings",
                "elements": [
                    {
                        "type": "button",
                        "action_id": HOME_OPEN_SETTINGS_ACTION_ID,
                        "text": {"type": "plain_text", "text": "PAXMiner Settings"},
                        "style": "primary",
                    }
                ],
            }
        )
    return {"type": "home", "blocks": blocks}


@app.event("app_home_opened")
def handle_app_home_opened(client, event, logger):
    """Publish Home for every user; Settings button is admin-only."""
    user_id = event.get("user")
    if not user_id:
        return
    try:
        admin = is_slack_admin(user_id, client=client)
        client.views_publish(user_id=user_id, view=_home_view(admin=admin))
    except Exception:
        logger.exception("app_home_opened views.publish failed")


def handle_home_open_settings(ack, body, client, logger):
    """Named listener for the Home Settings button — importable for unit tests."""
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    team_id = (body.get("team") or {}).get("id") or body.get("team_id") or ""
    trigger_id = body.get("trigger_id") or ""
    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            region = _region_for_team(cur, pm, team_id)
        if not region or not trigger_id:
            return
        region = dict(region)
        region["team_id"] = team_id
        _open_config_modal(client, trigger_id, region, logger)
    except Exception:
        logger.exception("home settings open failed")
    finally:
        conn.close()


app.action(HOME_OPEN_SETTINGS_ACTION_ID)(handle_home_open_settings)


def handler(event, context):
    """Lambda entrypoint: keep-warm short-circuit, else Bolt SlackRequestHandler."""
    if not is_http_request(event):
        return {"statusCode": 200, "body": "warm"}
    return SlackRequestHandler(app=app).handle(event, context)
