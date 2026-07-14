"""`/config-paxminer` Slack modal — channels, toggles, Kotter thresholds, achievements CRUD."""

from __future__ import annotations

import json
import logging
import os
import re

from common.encryption import decrypt_field
from paxminer_db import connect_from_env, paxminer_schema_from_env
from slack_http import http_response, is_slack_admin
from slack_util import slack_client

LOG = logging.getLogger(__name__)

CALLBACK_ID = "paxminer-config-id"
ACHIEVEMENTS_LIST_CALLBACK_ID = "paxminer-achievements-list-id"
ACHIEVEMENT_EDIT_CALLBACK_ID = "paxminer-achievement-edit-id"

MANAGE_ACHIEVEMENTS_ACTION_ID = "paxminer_manage_achievements"
ADD_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_add"
EDIT_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_edit"
DELETE_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_delete"
SELECT_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_select"

METRICS = ("posts", "qs", "distinct_aos", "posts_at_single_ao")
ACTIVITIES = ("beatdown", "qsource", "any")
PERIODS = ("week", "month", "year")

_CODE_RE = re.compile(r"^[a-z0-9_]+$")


def _registry_db() -> str:
    return (
        os.environ.get("PAXMINER_REGISTRY_DATABASE")
        or os.environ.get("PAXMINER_SCHEMA")
        or "paxminer"
    ).strip()


def _region_for_team(cur, pm_schema: str, team_id: str) -> dict | None:
    sb_schema = os.environ.get("SLACKBLAST_SCHEMA") or f"slackblast_{os.environ.get('STAGE', 'test')}"
    cur.execute(
        f"""
        SELECT r.* FROM `{pm_schema}`.`regions` r
        JOIN `{sb_schema}`.regions sb ON sb.paxminer_schema = r.schema_name
        WHERE sb.team_id = %s LIMIT 1
        """,
        (team_id,),
    )
    return cur.fetchone()


def _metadata(team_id: str, regional_schema: str, achievement_id: int | None = None) -> str:
    payload = {"team_id": team_id, "regional_schema": regional_schema}
    if achievement_id is not None:
        payload["achievement_id"] = achievement_id
    return json.dumps(payload)


def _parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _select_options(values: tuple[str, ...]) -> list[dict]:
    return [{"text": {"type": "plain_text", "text": v}, "value": v} for v in values]


def _achievement_summary(row: dict) -> str:
    return (
        f"*{row['name']}* (`{row['code']}`) — "
        f"{row['metric']}/{row['activity']}/{row['period']} ≥ {row['threshold']}"
    )


FEATURE_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Achievements"}, "value": "achievements"},
    {"text": {"type": "plain_text", "text": "Kotter reports"}, "value": "kotter"},
    {"text": {"type": "plain_text", "text": "Achievement leaderboard"}, "value": "leaderboard"},
]

CHART_OPTIONS = [
    {"text": {"type": "plain_text", "text": "PAX charts"}, "value": "pax"},
    {"text": {"type": "plain_text", "text": "Q charts"}, "value": "q"},
    {"text": {"type": "plain_text", "text": "Region leaderboard"}, "value": "region_lb"},
    {"text": {"type": "plain_text", "text": "AO leaderboard"}, "value": "ao_lb"},
]


def _selected_options(all_options: list[dict], selected_values: list[str]) -> list[dict]:
    selected = set(selected_values)
    return [opt for opt in all_options if opt["value"] in selected]


def _config_modal(region: dict) -> dict:
    features = []
    if region.get("send_achievements"):
        features.append("achievements")
    if region.get("send_aoq_reports"):
        features.append("kotter")
    if region.get("send_achievement_leaderboard"):
        features.append("leaderboard")
    charts = []
    if region.get("send_pax_charts"):
        charts.append("pax")
    if region.get("send_q_charts"):
        charts.append("q")
    if region.get("send_region_leaderboard"):
        charts.append("region_lb")
    if region.get("send_ao_leaderboard"):
        charts.append("ao_lb")
    feature_options = list(FEATURE_OPTIONS)
    chart_options = list(CHART_OPTIONS)
    feature_initial = _selected_options(feature_options, features)
    chart_initial = _selected_options(chart_options, charts)
    team_id = region.get("team_id") or ""
    regional_schema = region.get("schema_name") or ""
    features_element: dict = {
        "type": "checkboxes",
        "action_id": "features",
        "options": feature_options,
    }
    if feature_initial:
        features_element["initial_options"] = feature_initial
    charts_element: dict = {
        "type": "checkboxes",
        "action_id": "charts",
        "options": chart_options,
    }
    if chart_initial:
        charts_element["initial_options"] = chart_initial
    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema),
        "title": {"type": "plain_text", "text": "PAXMiner Settings"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input",
                "block_id": "features",
                "label": {"type": "plain_text", "text": "Enabled features"},
                "element": features_element,
            },
            {
                "type": "input",
                "block_id": "achievement_channel",
                "label": {"type": "plain_text", "text": "Achievement channel ID"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": region.get("achievement_channel") or "",
                },
            },
            {
                "type": "input",
                "block_id": "kotter_channel",
                "label": {"type": "plain_text", "text": "Kotter channel ID"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": region.get("kotter_channel") or "",
                },
            },
            {
                "type": "input",
                "block_id": "firstf_channel",
                "label": {"type": "plain_text", "text": "1stF channel for charts"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": region.get("firstf_channel") or "",
                },
            },
            {
                "type": "input",
                "block_id": "charts",
                "label": {"type": "plain_text", "text": "Monthly charts"},
                "element": charts_element,
            },
            {
                "type": "input",
                "block_id": "NO_POST_THRESHOLD",
                "label": {"type": "plain_text", "text": "Kotter: no-post threshold (weeks)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(region.get("NO_POST_THRESHOLD") or 2),
                },
            },
            {
                "type": "input",
                "block_id": "REMINDER_WEEKS",
                "label": {"type": "plain_text", "text": "Kotter: reminder window (weeks)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(region.get("REMINDER_WEEKS") or 2),
                },
            },
            {
                "type": "input",
                "block_id": "HOME_AO_CAPTURE",
                "label": {"type": "plain_text", "text": "Kotter: home AO capture (weeks)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(region.get("HOME_AO_CAPTURE") or 8),
                },
            },
            {
                "type": "input",
                "block_id": "NO_Q_THRESHOLD_WEEKS",
                "label": {"type": "plain_text", "text": "Kotter: no-Q threshold (weeks)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(region.get("NO_Q_THRESHOLD_WEEKS") or 4),
                },
            },
            {
                "type": "input",
                "block_id": "NO_Q_THRESHOLD_POSTS",
                "label": {"type": "plain_text", "text": "Kotter: no-Q threshold (posts)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(region.get("NO_Q_THRESHOLD_POSTS") or 4),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Achievement catalog*\nAdd, edit, or remove achievement rules."},
                "accessory": {
                    "type": "button",
                    "action_id": MANAGE_ACHIEVEMENTS_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Manage achievements"},
                },
            },
        ],
    }


def _achievements_list_modal(team_id: str, regional_schema: str, achievements: list[dict]) -> dict:
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Achievements* ({regional_schema})"},
        }
    ]
    if achievements:
        lines = [_achievement_summary(a) for a in achievements[:40]]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append(
            {
                "type": "input",
                "block_id": "achievement_pick",
                "optional": True,
                "label": {"type": "plain_text", "text": "Select achievement to edit or delete"},
                "element": {
                    "type": "static_select",
                    "action_id": SELECT_ACHIEVEMENT_ACTION_ID,
                    "placeholder": {"type": "plain_text", "text": "Choose…"},
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": f"{a['name']} ({a['code']})"[:75]},
                            "value": str(a["id"]),
                        }
                        for a in achievements
                    ],
                },
            }
        )
    else:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No achievements defined yet._"}}
        )
    blocks.extend(
        [
            {
                "type": "actions",
                "block_id": "achievement_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ADD_ACHIEVEMENT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Add achievement"},
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "action_id": EDIT_ACHIEVEMENT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Edit selected"},
                    },
                    {
                        "type": "button",
                        "action_id": DELETE_ACHIEVEMENT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Delete selected"},
                        "style": "danger",
                    },
                ],
            }
        ]
    )
    return {
        "type": "modal",
        "callback_id": ACHIEVEMENTS_LIST_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema),
        "title": {"type": "plain_text", "text": "Achievements"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": blocks,
    }


def _achievement_edit_modal(
    team_id: str,
    regional_schema: str,
    row: dict | None = None,
) -> dict:
    is_edit = row is not None
    return {
        "type": "modal",
        "callback_id": ACHIEVEMENT_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema, row["id"] if row else None),
        "title": {"type": "plain_text", "text": "Edit achievement" if is_edit else "Add achievement"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "name",
                "label": {"type": "plain_text", "text": "Name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": (row or {}).get("name") or "",
                },
            },
            {
                "type": "input",
                "block_id": "description",
                "label": {"type": "plain_text", "text": "Description"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": (row or {}).get("description") or "",
                },
            },
            {
                "type": "input",
                "block_id": "verb",
                "label": {"type": "plain_text", "text": "Verb (award message)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": (row or {}).get("verb") or "",
                },
            },
            {
                "type": "input",
                "block_id": "code",
                "label": {"type": "plain_text", "text": "Code (snake_case, unique)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": (row or {}).get("code") or "",
                },
            },
            {
                "type": "input",
                "block_id": "metric",
                "label": {"type": "plain_text", "text": "Metric"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": (row or {}).get("metric") or "posts"},
                        "value": (row or {}).get("metric") or "posts",
                    },
                    "options": _select_options(METRICS),
                },
            },
            {
                "type": "input",
                "block_id": "activity",
                "label": {"type": "plain_text", "text": "Activity"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": (row or {}).get("activity") or "beatdown"},
                        "value": (row or {}).get("activity") or "beatdown",
                    },
                    "options": _select_options(ACTIVITIES),
                },
            },
            {
                "type": "input",
                "block_id": "period",
                "label": {"type": "plain_text", "text": "Period"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": (row or {}).get("period") or "year"},
                        "value": (row or {}).get("period") or "year",
                    },
                    "options": _select_options(PERIODS),
                },
            },
            {
                "type": "input",
                "block_id": "threshold",
                "label": {"type": "plain_text", "text": "Threshold"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str((row or {}).get("threshold") or 1),
                },
            },
        ],
    }


def _parse_modal_values(payload: dict) -> dict:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    features = [o["value"] for o in state.get("features", {}).get("features", {}).get("selected_options", [])]
    charts = [o["value"] for o in state.get("charts", {}).get("charts", {}).get("selected_options", [])]
    return {
        "send_achievements": 1 if "achievements" in features else 0,
        "send_aoq_reports": 1 if "kotter" in features else 0,
        "send_achievement_leaderboard": 1 if "leaderboard" in features else 0,
        "achievement_channel": state.get("achievement_channel", {}).get("val", {}).get("value", "").strip(),
        "kotter_channel": state.get("kotter_channel", {}).get("val", {}).get("value", "").strip(),
        "firstf_channel": state.get("firstf_channel", {}).get("val", {}).get("value", "").strip(),
        "send_pax_charts": 1 if "pax" in charts else 0,
        "send_q_charts": 1 if "q" in charts else 0,
        "send_region_leaderboard": 1 if "region_lb" in charts else 0,
        "send_ao_leaderboard": 1 if "ao_lb" in charts else 0,
        "NO_POST_THRESHOLD": int(state.get("NO_POST_THRESHOLD", {}).get("val", {}).get("value", "2") or 2),
        "REMINDER_WEEKS": int(state.get("REMINDER_WEEKS", {}).get("val", {}).get("value", "2") or 2),
        "HOME_AO_CAPTURE": int(state.get("HOME_AO_CAPTURE", {}).get("val", {}).get("value", "8") or 8),
        "NO_Q_THRESHOLD_WEEKS": int(
            state.get("NO_Q_THRESHOLD_WEEKS", {}).get("val", {}).get("value", "4") or 4
        ),
        "NO_Q_THRESHOLD_POSTS": int(
            state.get("NO_Q_THRESHOLD_POSTS", {}).get("val", {}).get("value", "4") or 4
        ),
    }


def _parse_achievement_form(payload: dict) -> dict:
    state = payload.get("view", {}).get("state", {}).get("values", {})

    def _text(block_id: str) -> str:
        return state.get(block_id, {}).get("val", {}).get("value", "").strip()

    def _select(block_id: str) -> str:
        sel = state.get(block_id, {}).get("val", {}).get("selected_option") or {}
        return sel.get("value", "").strip()

    return {
        "name": _text("name"),
        "description": _text("description"),
        "verb": _text("verb"),
        "code": _text("code"),
        "metric": _select("metric") or "posts",
        "activity": _select("activity") or "beatdown",
        "period": _select("period") or "year",
        "threshold": int(_text("threshold") or "1"),
    }


def _validate_achievement(values: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not values["name"]:
        errors["name"] = "Name is required"
    if not values["code"]:
        errors["code"] = "Code is required"
    elif not _CODE_RE.match(values["code"]):
        errors["code"] = "Use lowercase letters, numbers, and underscores"
    if values["metric"] not in METRICS:
        errors["metric"] = "Invalid metric"
    if values["activity"] not in ACTIVITIES:
        errors["activity"] = "Invalid activity"
    if values["period"] not in PERIODS:
        errors["period"] = "Invalid period"
    if values["threshold"] < 1:
        errors["threshold"] = "Threshold must be at least 1"
    return errors


def _load_achievements(cur, schema: str) -> list[dict]:
    cur.execute(f"SELECT * FROM `{schema}`.`achievements_list` ORDER BY name")
    return list(cur.fetchall() or [])


def _load_achievement(cur, schema: str, achievement_id: int) -> dict | None:
    cur.execute(f"SELECT * FROM `{schema}`.`achievements_list` WHERE id=%s", (achievement_id,))
    return cur.fetchone()


def _selected_achievement_id(payload: dict) -> int | None:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    sel = state.get("achievement_pick", {}).get(SELECT_ACHIEVEMENT_ACTION_ID, {}).get("selected_option")
    if not sel:
        return None
    try:
        return int(sel["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _region_context(payload: dict) -> tuple[str, str, dict | None]:
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    team_id = meta.get("team_id") or (payload.get("team") or {}).get("id", "")
    regional_schema = meta.get("regional_schema", "")
    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            region = _region_for_team(cur, pm, team_id) if team_id else None
        return team_id, regional_schema or (region or {}).get("schema_name", ""), region
    finally:
        conn.close()


def handle_config_command(team_id: str, user_id: str, trigger_id: str) -> dict:
    if not is_slack_admin(user_id):
        return http_response(200, {"response_type": "ephemeral", "text": "Workspace admin required."})
    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            region = _region_for_team(cur, pm, team_id)
        if not region:
            return http_response(200, {"response_type": "ephemeral", "text": "No PAXMiner region linked to this workspace."})
        region = dict(region)
        region["team_id"] = team_id
        token = decrypt_field(region["slack_token"]) if region.get("slack_token") else os.environ.get("PM_SLACK_TOKEN")
        client = slack_client(token)
        client.views_open(trigger_id=trigger_id, view=_config_modal(region))
        return http_response(200, "")
    finally:
        conn.close()


def handle_config_submit(payload: dict) -> dict:
    user_id = (payload.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id):
        return http_response(200, {"response_action": "errors", "errors": {"features": "Admin required"}})
    team_id, _, region = _region_context(payload)
    if not region:
        return http_response(200, {"response_action": "errors", "errors": {"features": "Region not found"}})
    values = _parse_modal_values(payload)
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
        return http_response(200, {"response_action": "clear"})
    finally:
        conn.close()


def handle_config_block_actions(payload: dict) -> dict:
    user_id = (payload.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id):
        return http_response(200, {"response_type": "ephemeral", "text": "Admin required."})
    action = ((payload.get("actions") or [{}])[0]).get("action_id", "")
    team_id, regional_schema, region = _region_context(payload)
    if not region or not regional_schema:
        return http_response(200, {"response_type": "ephemeral", "text": "Region not found."})

    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            achievements = _load_achievements(cur, regional_schema)

            if action == MANAGE_ACHIEVEMENTS_ACTION_ID:
                view = _achievements_list_modal(team_id, regional_schema, achievements)
                return http_response(200, {"response_action": "push", "view": view})

            if action == ADD_ACHIEVEMENT_ACTION_ID:
                view = _achievement_edit_modal(team_id, regional_schema, None)
                return http_response(200, {"response_action": "push", "view": view})

            selected_id = _selected_achievement_id(payload)
            if action == EDIT_ACHIEVEMENT_ACTION_ID:
                if not selected_id:
                    return http_response(
                        200,
                        {"response_type": "ephemeral", "text": "Select an achievement to edit."},
                    )
                row = _load_achievement(cur, regional_schema, selected_id)
                if not row:
                    return http_response(200, {"response_type": "ephemeral", "text": "Achievement not found."})
                view = _achievement_edit_modal(team_id, regional_schema, row)
                return http_response(200, {"response_action": "push", "view": view})

            if action == DELETE_ACHIEVEMENT_ACTION_ID:
                if not selected_id:
                    return http_response(
                        200,
                        {"response_type": "ephemeral", "text": "Select an achievement to delete."},
                    )
                cur.execute(
                    f"SELECT COUNT(*) AS cnt FROM `{regional_schema}`.`achievements_awarded` "
                    "WHERE achievement_id=%s",
                    (selected_id,),
                )
                cnt = (cur.fetchone() or {}).get("cnt", 0)
                if cnt:
                    return http_response(
                        200,
                        {
                            "response_type": "ephemeral",
                            "text": f"Cannot delete: {cnt} award(s) reference this achievement.",
                        },
                    )
                cur.execute(f"DELETE FROM `{regional_schema}`.`achievements_list` WHERE id=%s", (selected_id,))
                conn.commit()
                achievements = _load_achievements(cur, regional_schema)
                view = _achievements_list_modal(team_id, regional_schema, achievements)
                return http_response(200, {"response_action": "update", "view": view})
    finally:
        conn.close()

    return http_response(400, {"ok": False, "error": "Unsupported action"})


def handle_achievement_edit_submit(payload: dict) -> dict:
    user_id = (payload.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id):
        return http_response(200, {"response_action": "errors", "errors": {"name": "Admin required"}})
    team_id, regional_schema, region = _region_context(payload)
    if not region or not regional_schema:
        return http_response(200, {"response_action": "errors", "errors": {"name": "Region not found"}})

    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    achievement_id = meta.get("achievement_id")
    values = _parse_achievement_form(payload)
    errors = _validate_achievement(values)
    if errors:
        return http_response(200, {"response_action": "errors", "errors": errors})

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
                return http_response(
                    200,
                    {"response_action": "errors", "errors": {"code": "Code already in use"}},
                )

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
            return http_response(200, {"response_action": "update", "view": view})
    finally:
        conn.close()


def handle_achievements_list_submit(payload: dict) -> dict:
    """Close achievements sub-view and return to main settings."""
    user_id = (payload.get("user") or {}).get("id", "")
    if not is_slack_admin(user_id):
        return http_response(200, {"response_action": "errors", "errors": {"achievement_pick": "Admin required"}})
    team_id, _, region = _region_context(payload)
    if not region:
        return http_response(200, {"response_action": "clear"})
    region = dict(region)
    region["team_id"] = team_id
    return http_response(200, {"response_action": "update", "view": _config_modal(region)})
