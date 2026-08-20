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
    ACHIEVEMENT_DELETE_CONFIRM_CALLBACK_ID,
    ACHIEVEMENT_EDIT_CALLBACK_ID,
    ACHIEVEMENT_RANGE_CONFIRM_CALLBACK_ID,
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
    MORE_ACHIEVEMENT_ACTION_ID,
    REEVAL_FROM_ACTION_ID,
    REEVAL_TO_ACTION_ID,
    RESTORE_ACHIEVEMENTS_ACTION_ID,
    SELECT_ACHIEVEMENT_ACTION_ID,
    _achievement_edit_modal,
    _achievement_range_confirm_modal,
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
    _hydrate_range_row,
    _metadata,
    achievement_delete_confirm_text,
    earliest_beatdown_date,
    load_achievement_defaults,
    uniquify_achievement_code,
)
from paxminer_db import connect_from_env, paxminer_schema_from_env
from slack_blocks import (
    OVERFLOW_DELETE,
    OVERFLOW_DISABLE,
    OVERFLOW_DUPLICATE,
    OVERFLOW_EDIT,
    OVERFLOW_ENABLE,
    counted_noun,
    delete_confirm_modal,
    parse_overflow_action,
)
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
            options = _load_activity_options(cur, regional_schema, team_id)
            draft = _hydrate_range_row(cur, regional_schema, {})
        view = _achievement_edit_modal(team_id, regional_schema, draft, activity_options=options)
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
    view_id=None,
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
    view_id = view_id or meta.get("list_view_id") or (body.get("view") or {}).get("id")
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
            view_id=view_id or body["view"]["id"],
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
            options = _load_activity_options(cur, regional_schema, team_id)
            if row:
                awards, pax = achievement_award_impact(
                    cur, regional_schema, selected_id
                )
                row["award_count"] = awards
                row["pax_count"] = pax
                _hydrate_range_row(cur, regional_schema, row)
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


def _noop_ack(*_a, **_k):
    return None


def _toggle_achievement_enabled(body, client, logger, achievement_id: int) -> None:
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE `{regional_schema}`.`achievements_list` "
                "SET enabled = 1 - COALESCE(enabled, 0) WHERE id=%s",
                (achievement_id,),
            )
            row = _load_achievement(cur, regional_schema, achievement_id)
            conn.commit()
        enabled = int((row or {}).get("enabled") or 0) == 1
        code = (row or {}).get("code") or achievement_id
        notice = f"Enabled `{code}`." if enabled else f"Disabled `{code}`."
        _refresh_achievements_list(client, body, team_id, regional_schema, notice)
    except Exception:
        logger.exception("achievement toggle failed id=%s", achievement_id)
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not update enabled flag — try again."
        )
    finally:
        conn.close()


def _push_achievement_delete_confirm(body, client, logger, achievement_id: int) -> None:
    team_id, regional_schema, region = _region_context_from_body(body)
    if not region or not regional_schema:
        return
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            row = _load_achievement(cur, regional_schema, achievement_id)
            awards, pax = (
                achievement_award_impact(cur, regional_schema, achievement_id) if row else (0, 0)
            )
        if not row:
            _refresh_achievements_list(
                client, body, team_id, regional_schema, "Achievement not found."
            )
            return
        code = row.get("code") or str(achievement_id)
        warning = achievement_delete_confirm_text(code, awards, pax)
        client.views_push(
            trigger_id=body["trigger_id"],
            view=delete_confirm_modal(
                callback_id=ACHIEVEMENT_DELETE_CONFIRM_CALLBACK_ID,
                title="Delete achievement?",
                warning=warning,
                metadata=_metadata(
                    team_id,
                    regional_schema,
                    achievement_id,
                    list_view_id=body["view"]["id"],
                ),
            ),
        )
    except Exception:
        logger.exception("achievement delete confirm failed id=%s", achievement_id)
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not open delete confirmation."
        )
    finally:
        conn.close()


def handle_achievement_more(ack, body, client, logger):
    user_id = (body.get("user") or {}).get("id", "")
    ack()
    if not is_slack_admin(user_id, client=client):
        notify_admin_required(client, body)
        return
    action = (body.get("actions") or [{}])[0]
    verb, row_id = parse_overflow_action(action)
    if not verb or not row_id:
        team_id, regional_schema, _region = _region_context_from_body(body)
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not read that menu action."
        )
        return
    inner = dict(body)
    inner["actions"] = [{**action, "value": str(row_id), "action_id": action.get("action_id")}]
    if verb == OVERFLOW_EDIT:
        handle_edit_achievement(_noop_ack, inner, client, logger)
    elif verb == OVERFLOW_DUPLICATE:
        handle_duplicate_achievement(_noop_ack, inner, client, logger)
    elif verb in (OVERFLOW_DISABLE, OVERFLOW_ENABLE):
        _toggle_achievement_enabled(body, client, logger, row_id)
    elif verb == OVERFLOW_DELETE:
        _push_achievement_delete_confirm(body, client, logger, row_id)


app.action(MORE_ACHIEVEMENT_ACTION_ID)(handle_achievement_more)


def _delete_one_achievement(cur, schema: str, achievement_id: int) -> dict | None:
    """Remove one achievement's awards, versions, and list row. Returns log fields."""
    row = _load_achievement(cur, schema, achievement_id)
    if not row:
        return None
    awards, pax = achievement_award_impact(cur, schema, achievement_id)
    cur.execute(
        f"DELETE FROM `{schema}`.`achievements_awarded` WHERE achievement_id=%s",
        (achievement_id,),
    )
    cur.execute(
        f"DELETE FROM `{schema}`.`achievement_versions` WHERE achievement_id=%s",
        (achievement_id,),
    )
    cur.execute(
        f"DELETE FROM `{schema}`.`achievements_list` WHERE id=%s",
        (achievement_id,),
    )
    name = row.get("name") or "achievement"
    return {
        "name": name,
        "code": row.get("code") or name,
        "awards": awards,
        "pax": pax,
    }


def _announce_achievement_deleted(region: dict, client, user_id: str, deleted: dict) -> None:
    """Same paxminer_logs / channel line as a manual single delete."""
    awards = counted_noun(deleted["awards"], "award")
    pax = counted_noun(deleted["pax"], "PAX", "PAX")
    name = deleted["name"]
    _post_achievement_admin_notice(
        region,
        f"Achievement *{name}* was deleted along with {awards} from {pax}.",
        (
            f"Achievement *{name}* was deleted by `{_log_actor_name(client, user_id)}` "
            f"({awards} from {pax} removed)"
        ),
    )


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
            deleted = _delete_one_achievement(cur, regional_schema, selected_id)
            if not deleted:
                _refresh_achievements_list(
                    client, body, team_id, regional_schema, "Achievement not found."
                )
                return
            conn.commit()
        awards = counted_noun(deleted["awards"], "award")
        pax = counted_noun(deleted["pax"], "PAX", "PAX")
        notice = f"Deleted `{deleted['code']}` ({awards} from {pax} removed)."
        _refresh_achievements_list(client, body, team_id, regional_schema, notice)
        region = dict(region)
        region["schema_name"] = regional_schema
        _announce_achievement_deleted(region, client, user_id, deleted)
    except Exception:
        logger.exception("achievement delete failed id=%s", selected_id)
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not delete achievement — try again."
        )
    finally:
        conn.close()


app.action(DELETE_ACHIEVEMENT_ACTION_ID)(handle_delete_achievement)
app.view(ACHIEVEMENT_DELETE_CONFIRM_CALLBACK_ID)(handle_delete_achievement)


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
    from achievements.range import (
        clear_reeval_lock,
        ensure_achievement_range_columns,
        try_acquire_reeval_lock,
    )
    from slack_schedule import queue_achievement_backfill

    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            ensure_achievement_range_columns(cur, regional_schema)
            ok, lock_msg = try_acquire_reeval_lock(cur, regional_schema, int(selected_id))
            conn.commit()
            if not ok:
                _refresh_achievements_list(
                    client,
                    body,
                    team_id,
                    regional_schema,
                    lock_msg or "A re-evaluate is already running for this achievement.",
                    selected_id=selected_id,
                    reeval_from=start,
                    reeval_to=end,
                )
                return
        try:
            queue_achievement_backfill(
                schema=regional_schema,
                achievement_id=selected_id,
                actor=user_id,
                start=start,
                end=end,
                automatic=False,
            )
        except Exception:
            logger.exception("queue achievement backfill failed")
            with conn.cursor() as cur:
                clear_reeval_lock(cur, regional_schema, int(selected_id))
                conn.commit()
            _refresh_achievements_list(
                client, body, team_id, regional_schema, "Could not queue re-evaluate."
            )
            return
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
    finally:
        conn.close()


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
            client, body, team_id, regional_schema, "Open an achievement from More first."
        )
        return
    from config_schedule import duplicate_name

    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            row = _load_achievement(cur, regional_schema, selected_id)
            options = _load_activity_options(cur, regional_schema, team_id)
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
            _hydrate_range_row(cur, regional_schema, draft)
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
            rows = _load_achievements(cur, regional_schema)
            deleted: list[dict] = []
            for row in rows:
                aid = row.get("id")
                if aid is None:
                    continue
                one = _delete_one_achievement(cur, regional_schema, int(aid))
                if one:
                    deleted.append(one)
            conn.commit()
        awards_n = sum(int(d["awards"]) for d in deleted)
        _refresh_achievements_list(
            client,
            body,
            team_id,
            regional_schema,
            (
                f"Deleted {counted_noun(len(deleted), 'achievement')} and "
                f"{counted_noun(awards_n, 'award')}."
            ),
            page=0,
        )
        region = dict(region)
        region["schema_name"] = regional_schema
        for one in deleted:
            _announce_achievement_deleted(region, client, user_id, one)
    except Exception:
        logger.exception("delete all achievements failed")
        _refresh_achievements_list(
            client, body, team_id, regional_schema, "Could not delete all achievements."
        )
    finally:
        conn.close()


app.action(DELETE_ALL_ACHIEVEMENTS_ACTION_ID)(handle_delete_all_achievements)


def _values_from_builtin_seed(seed: dict) -> dict:
    """Form-shaped values for a catalog seed. Builtins cover all attendance dates."""
    from achievements.activity import activity_list_from_rule
    from achievements.range import RANGE_ALL_ATTENDANCE

    return {
        "name": seed["name"],
        "description": seed["description"],
        "verb": seed["verb"],
        "code": seed["code"],
        "metric": seed["metric"],
        "activity_list": activity_list_from_rule(seed),
        "period": seed["period"],
        "threshold": int(seed["threshold"]),
        "enabled": 1,
        "range_mode": RANGE_ALL_ATTENDANCE,
        "effective_from": None,
        "effective_to": None,
    }


def _add_one_achievement(cur, schema: str, values: dict, user_id: str, *, mode, from_date, to_date) -> int:
    """Same list + version insert as saving a new achievement from the add form."""
    from achievements.activity import activity_legacy_mirror
    from achievements.versions import insert_version

    cur.execute(
        f"""
        INSERT INTO `{schema}`.`achievements_list`
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
            values.get("enabled", 1),
        ),
    )
    achievement_id = int(cur.lastrowid)
    insert_version(
        cur,
        schema,
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
        range_mode=mode,
    )
    return achievement_id


def handle_restore_achievements(ack, body, client, logger):
    from achievements.range import (
        clear_reeval_lock,
        ensure_achievement_range_columns,
        resolve_stored_range,
        should_auto_queue,
        try_acquire_reeval_lock,
    )
    from slack_schedule import queue_achievement_backfill

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
        queued: list[tuple[int, str | None, str | None]] = []
        with conn.cursor() as cur:
            ensure_achievement_range_columns(cur, regional_schema)
            earliest = earliest_beatdown_date(cur, regional_schema)
            today = date.today().isoformat()
            added = 0
            for seed in load_achievement_defaults():
                cur.execute(
                    f"SELECT id FROM `{regional_schema}`.`achievements_list` WHERE code=%s",
                    (seed["code"],),
                )
                if cur.fetchone():
                    continue
                values = _values_from_builtin_seed(seed)
                mode, from_date, to_date = resolve_stored_range(
                    values,
                    first_created=today,
                    version_created=today,
                    minting=False,
                )
                achievement_id = _add_one_achievement(
                    cur,
                    regional_schema,
                    values,
                    user_id,
                    mode=mode,
                    from_date=from_date,
                    to_date=to_date,
                )
                added += 1
                if should_auto_queue(
                    is_new=True,
                    params_changed=False,
                    range_changed=True,
                    mode=mode,
                ):
                    ok, _lock_msg = try_acquire_reeval_lock(
                        cur, regional_schema, int(achievement_id)
                    )
                    if ok:
                        queued.append((int(achievement_id), from_date, to_date))
            conn.commit()
            for achievement_id, from_date, to_date in queued:
                queue_start, queue_end = _reeval_window(from_date, to_date, earliest)
                try:
                    queue_achievement_backfill(
                        schema=regional_schema,
                        achievement_id=achievement_id,
                        actor=user_id,
                        start=queue_start,
                        end=queue_end,
                        automatic=True,
                    )
                except Exception:
                    logger.exception(
                        "queue backfill after restore failed id=%s", achievement_id
                    )
                    clear_reeval_lock(cur, regional_schema, achievement_id)
                    conn.commit()
        notice = f"Restored defaults ({added} missing builtin(s) added)."
        if queued:
            notice += " Re-evaluate queued for each."
        _refresh_achievements_list(
            client,
            body,
            team_id,
            regional_schema,
            notice,
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


def _reeval_window(from_date, to_date, earliest) -> tuple[str | None, str | None]:
    start = from_date or earliest
    end = to_date or date.today().isoformat()
    return start, end


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
    from achievements.range import (
        clear_reeval_lock,
        count_awards_outside_range,
        ensure_achievement_range_columns,
        range_changed,
        resolve_stored_range,
        should_auto_queue,
        try_acquire_reeval_lock,
        window_narrowed,
    )
    from achievements.versions import (
        params_changed,
        supersede_and_insert,
        update_current_range,
    )
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
    if achievement_id not in (None, ""):
        try:
            achievement_id = int(achievement_id)
        except (TypeError, ValueError):
            achievement_id = None
    range_confirmed = bool(meta.get("range_confirmed"))
    values = meta.get("pending_values") or _parse_achievement_form(body)
    if achievement_id:
        values["code"] = values.get("code") or ""

    try:
        conn = connect_from_env(_registry_db())
    except Exception as extra:
        logger.exception("achievement edit connect failed")
        ack(response_action="errors", errors={"name": f"Save failed: {str(extra)[:120]}"})
        return

    notice = None
    try:
        with conn.cursor() as cur:
            ensure_achievement_range_columns(cur, regional_schema)
            earliest = earliest_beatdown_date(cur, regional_schema)
            existing = _load_achievement(cur, regional_schema, achievement_id) if achievement_id else None
            errors = _validate_achievement(
                values,
                require_code=not bool(achievement_id),
                first_created=(existing or {}).get("first_created"),
                version_created=(existing or {}).get("version_created"),
                earliest_beatdown=earliest,
            )
            if errors:
                ack(response_action="errors", errors=errors)
                return
            if not achievement_id:
                cur.execute(
                    f"SELECT id FROM `{regional_schema}`.`achievements_list` WHERE code=%s",
                    (values["code"],),
                )
                if cur.fetchone():
                    ack(response_action="errors", errors={"code": "Code already in use"})
                    return

            params_did_change = bool(existing) and params_changed(existing, values)
            first_created = (existing or {}).get("first_created")
            version_created = (existing or {}).get("version_created")
            if not existing:
                first_created = version_created = date.today().isoformat()
            mode, from_date, to_date = resolve_stored_range(
                values,
                first_created=first_created,
                version_created=version_created,
                minting=params_did_change,
            )
            range_did_change = range_changed(existing, mode, from_date, to_date)

            if (
                existing
                and not range_confirmed
                and window_narrowed(
                    existing.get("effective_from"),
                    existing.get("effective_to"),
                    from_date,
                    to_date,
                )
            ):
                awards, pax = count_awards_outside_range(
                    cur, regional_schema, int(achievement_id), from_date, to_date
                )
                if awards:
                    ack(
                        response_action="push",
                        view=_achievement_range_confirm_modal(
                            team_id,
                            regional_schema,
                            achievement_id=int(achievement_id),
                            values=values,
                            award_count=awards,
                            pax_count=pax,
                        ),
                    )
                    return

            if achievement_id and existing:
                values["code"] = existing.get("code") or values["code"]
                cur.execute(
                    f"""
                    UPDATE `{regional_schema}`.`achievements_list`
                    SET name=%s, description=%s, verb=%s
                    WHERE id=%s
                    """,
                    (
                        values["name"],
                        values["description"],
                        values["verb"],
                        achievement_id,
                    ),
                )
                if params_did_change:
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
                        range_mode=mode,
                    )
                else:
                    update_current_range(
                        cur,
                        regional_schema,
                        int(achievement_id),
                        effective_from=from_date,
                        effective_to=to_date,
                        range_mode=mode,
                    )
            else:
                achievement_id = _add_one_achievement(
                    cur,
                    regional_schema,
                    values,
                    user_id,
                    mode=mode,
                    from_date=from_date,
                    to_date=to_date,
                )

            should_queue = should_auto_queue(
                is_new=not bool(existing),
                params_changed=params_did_change,
                range_changed=range_did_change,
                mode=mode,
            )
            queued = False
            lock_msg = None
            if should_queue and achievement_id:
                ok, lock_msg = try_acquire_reeval_lock(
                    cur, regional_schema, int(achievement_id)
                )
                queued = ok
            conn.commit()
            queue_start, queue_end = _reeval_window(from_date, to_date, earliest)
            if should_queue and queued and achievement_id:
                try:
                    queue_achievement_backfill(
                        schema=regional_schema,
                        achievement_id=int(achievement_id),
                        actor=user_id,
                        start=queue_start,
                        end=queue_end,
                        automatic=True,
                    )
                    notice = (
                        f"Re-evaluate queued for {queue_start or '…'} to {queue_end or '…'}."
                    )
                except Exception:
                    logger.exception("queue backfill after edit failed")
                    clear_reeval_lock(cur, regional_schema, int(achievement_id))
                    conn.commit()
                    notice = "Saved, but re-evaluate could not be queued."
            elif should_queue and lock_msg:
                notice = f"Saved. {lock_msg}"
            achievements = _load_achievements(cur, regional_schema)
            view = _achievements_list_modal(
                team_id, regional_schema, achievements, notice=notice
            )
            ack(response_action="update", view=view)
    except Exception as extra:
        logger.exception("achievement edit submit failed")
        ack(response_action="errors", errors={"name": f"Save failed: {str(extra)[:120]}"})
    finally:
        conn.close()


app.view(ACHIEVEMENT_EDIT_CALLBACK_ID)(handle_achievement_edit_submit)
app.view(ACHIEVEMENT_RANGE_CONFIRM_CALLBACK_ID)(handle_achievement_edit_submit)


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
