"""`/config-paxminer` Slack modal — channels, toggles, Kotter thresholds, chart settings."""

from __future__ import annotations

import json
import logging
import os

from common.encryption import decrypt_field
from paxminer_db import connect_from_env, paxminer_schema_from_env
from slack_http import http_response, is_slack_admin
from slack_util import slack_client

LOG = logging.getLogger(__name__)

CALLBACK_ID = "paxminer-config-id"


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
    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "title": {"type": "plain_text", "text": "PAXMiner Settings"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input",
                "block_id": "features",
                "label": {"type": "plain_text", "text": "Enabled features"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "features",
                    "options": [
                        {"text": {"type": "plain_text", "text": "Achievements"}, "value": "achievements"},
                        {"text": {"type": "plain_text", "text": "Kotter reports"}, "value": "kotter"},
                        {"text": {"type": "plain_text", "text": "Achievement leaderboard"}, "value": "leaderboard"},
                    ],
                    "initial_options": [
                        {"text": {"type": "plain_text", "text": v.title()}, "value": v}
                        for v in features
                    ],
                },
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
                "element": {
                    "type": "checkboxes",
                    "action_id": "charts",
                    "options": [
                        {"text": {"type": "plain_text", "text": "PAX charts"}, "value": "pax"},
                        {"text": {"type": "plain_text", "text": "Q charts"}, "value": "q"},
                        {"text": {"type": "plain_text", "text": "Region leaderboard"}, "value": "region_lb"},
                        {"text": {"type": "plain_text", "text": "AO leaderboard"}, "value": "ao_lb"},
                    ],
                    "initial_options": [
                        {"text": {"type": "plain_text", "text": "x"}, "value": v} for v in charts
                    ],
                },
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
    }


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
        token = decrypt_field(region["slack_token"]) if region.get("slack_token") else os.environ.get("PM_SLACK_TOKEN")
        client = slack_client(token)
        client.views_open(trigger_id=trigger_id, view=_config_modal(region))
        return http_response(200, "")
    finally:
        conn.close()


def handle_config_submit(payload: dict) -> dict:
    user_id = (payload.get("user") or {}).get("id", "")
    team_id = (payload.get("team") or {}).get("id", "")
    if not is_slack_admin(user_id):
        return http_response(200, {"response_action": "errors", "errors": {"features": "Admin required"}})
    values = _parse_modal_values(payload)
    pm = paxminer_schema_from_env()
    conn = connect_from_env(_registry_db())
    try:
        with conn.cursor() as cur:
            region = _region_for_team(cur, pm, team_id)
            if not region:
                return http_response(200, {"response_action": "errors", "errors": {"features": "Region not found"}})
            sets = ", ".join(f"`{k}`=%s" for k in values)
            cur.execute(
                f"UPDATE `{pm}`.`regions` SET {sets} WHERE region=%s",
                (*values.values(), region["region"]),
            )
            conn.commit()
        return http_response(200, {"response_action": "clear"})
    finally:
        conn.close()
