"""`/config-paxminer` Slack modal builders and DB helpers (Bolt listeners live in slack_app)."""

from __future__ import annotations

import json
import logging
import os
import re

LOG = logging.getLogger(__name__)

CALLBACK_ID = "paxminer-config-id"
ACHIEVEMENTS_LIST_CALLBACK_ID = "paxminer-achievements-list-id"
ACHIEVEMENT_EDIT_CALLBACK_ID = "paxminer-achievement-edit-id"
ACHIEVEMENT_DELETE_CALLBACK_ID = "paxminer-achievement-delete-id"

ADD_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_add"
EDIT_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_edit"
DELETE_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_delete"
BACKFILL_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_backfill"
SELECT_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_select"

METRICS = ("posts", "qs", "distinct_aos", "posts_at_single_ao")
PERIODS = ("week", "month", "year")
RANGE_MODES = ("going_forward", "all_previous", "custom")
APPLY_MODES = ("going_forward", "retroactive")

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


def _with_initial(options: list[dict], value: str | None) -> dict:
    """Omit initial_option when value is not in options (Slack rejects null)."""
    if not value:
        return {}
    for o in options:
        if o.get("value") == value:
            return {"initial_option": o}
    return {}


def _achievement_summary(row: dict) -> str:
    from achievements.activity import activity_list_from_rule

    activities = activity_list_from_rule(row)
    activity_label = ", ".join(activities) if activities else "all activities"
    version = row.get("version") or row.get("version_key") or "v?"
    enabled = "on" if int(row.get("enabled") or 1) else "off"
    return (
        f"*{row['name']}* (`{row['code']}` {version}) — "
        f"{row.get('metric')}/{row.get('period')} ≥ {row.get('threshold')} · {activity_label} · {enabled}"
    )


def _config_modal(region: dict) -> dict:
    """Hub modal: timezone + entry points for Achievements / Reports / Kotter / Schedule."""
    from config_schedule import (
        OPEN_ACHIEVEMENTS_ACTION_ID,
        OPEN_KOTTER_CONFIG_ACTION_ID,
        OPEN_REPORTS_ACTION_ID,
        OPEN_SCHEDULE_ACTION_ID,
        TIMEZONE_OPTIONS,
    )

    team_id = region.get("team_id") or ""
    regional_schema = region.get("schema_name") or ""
    tz = (region.get("timezone") or "America/Chicago").strip() or "America/Chicago"
    tz_opts = [{"text": {"type": "plain_text", "text": t}, "value": t} for t in TIMEZONE_OPTIONS]
    if tz not in TIMEZONE_OPTIONS:
        tz_opts = [{"text": {"type": "plain_text", "text": tz}, "value": tz}] + tz_opts
    tz_initial = next((o for o in tz_opts if o["value"] == tz), tz_opts[0])

    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema),
        "title": {"type": "plain_text", "text": "PAXMiner Settings"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input",
                "block_id": "timezone",
                "label": {"type": "plain_text", "text": "Region timezone"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": tz_opts,
                    "initial_option": tz_initial,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Configure*\n"
                        "• *PAX Achievements* — award rules\n"
                        "• *PAX Reports* — builtin + custom report definitions\n"
                        "• *Kotter Reports* — thresholds\n"
                        "• *Schedule* — when/where awards, charts, Kotter, and "
                        "leaderboards post"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "hub_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": OPEN_ACHIEVEMENTS_ACTION_ID,
                        "text": {"type": "plain_text", "text": "PAX Achievements"},
                    },
                    {
                        "type": "button",
                        "action_id": OPEN_REPORTS_ACTION_ID,
                        "text": {"type": "plain_text", "text": "PAX Reports"},
                    },
                    {
                        "type": "button",
                        "action_id": OPEN_KOTTER_CONFIG_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Kotter Reports"},
                    },
                    {
                        "type": "button",
                        "action_id": OPEN_SCHEDULE_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Schedule"},
                        "style": "primary",
                    },
                ],
            },
        ],
    }


def _achievements_list_modal(
    team_id: str,
    regional_schema: str,
    achievements: list[dict],
    notice: str | None = None,
) -> dict:
    blocks: list[dict] = []
    if notice:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": notice}],
            }
        )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Achievements* ({regional_schema})"},
        }
    )
    if achievements:
        # Slack caps: static_select ≤100 options; section mrkdwn ≈3000 chars.
        page = achievements[:100]
        lines = [_achievement_summary(a) for a in page[:40]]
        summary = "\n".join(lines)
        if len(summary) > 2900:
            summary = summary[:2890] + "\n…"
        if len(achievements) > len(page):
            summary += f"\n_Showing {len(page)} of {len(achievements)} — open Edit to manage more._"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary}})
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
                        for a in page
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
                        "text": {"type": "plain_text", "text": "Delete / disable"},
                        "style": "danger",
                    },
                    {
                        "type": "button",
                        "action_id": BACKFILL_ACHIEVEMENT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Backfill / re-run"},
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
        "submit": {"type": "plain_text", "text": "Done"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": blocks,
    }


def _achievement_edit_modal(
    team_id: str,
    regional_schema: str,
    row: dict | None = None,
    *,
    activity_options: list[str] | None = None,
) -> dict:
    from datetime import date as _date

    from achievements.activity import BUILTIN_ACTIVITY_TYPES, activity_list_from_rule

    is_edit = row is not None
    src = dict(row or {})
    options = activity_options or list(BUILTIN_ACTIVITY_TYPES)
    activity_opts = _select_options(tuple(options))
    selected_activities = activity_list_from_rule(src)
    initial_activities = [o for o in activity_opts if o["value"] in selected_activities]
    enabled = int(src.get("enabled") or 1) == 1
    range_mode = "going_forward" if not is_edit else "all_previous"
    if src.get("effective_from") is None and is_edit:
        range_mode = "all_previous"
    elif src.get("effective_from"):
        range_mode = "custom" if is_edit else "going_forward"
    blocks: list[dict] = []
    if is_edit:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{_achievement_summary(src)}\n"
                        "_Code is immutable. Changing metric/activity/period/threshold "
                        "creates a new version; a rename of the concept should be a new code._"
                    ),
                },
            }
        )
    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "name",
                "label": {"type": "plain_text", "text": "Name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": src.get("name") or "",
                },
            },
            {
                "type": "input",
                "block_id": "description",
                "label": {"type": "plain_text", "text": "Description"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": src.get("description") or "",
                },
            },
            {
                "type": "input",
                "block_id": "verb",
                "label": {"type": "plain_text", "text": "Verb (award message)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": src.get("verb") or "",
                },
            },
        ]
    )
    if is_edit:
        blocks.append(
            {
                "type": "section",
                "block_id": "code",
                "text": {"type": "mrkdwn", "text": f"*Code:* `{src.get('code')}`"},
            }
        )
    else:
        blocks.append(
            {
                "type": "input",
                "block_id": "code",
                "label": {"type": "plain_text", "text": "Code (snake_case, unique)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": "",
                },
            }
        )
    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "enabled",
                "optional": True,
                "label": {"type": "plain_text", "text": "Enabled"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "val",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "Award this achievement"},
                            "value": "1",
                        }
                    ],
                    **({"initial_options": [
                        {
                            "text": {"type": "plain_text", "text": "Award this achievement"},
                            "value": "1",
                        }
                    ]} if enabled else {}),
                },
            },
            {
                "type": "input",
                "block_id": "metric",
                "label": {"type": "plain_text", "text": "Metric"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": _select_options(METRICS),
                    **_with_initial(_select_options(METRICS), src.get("metric") or "posts"),
                },
            },
            {
                "type": "input",
                "block_id": "activity",
                "optional": True,
                "label": {"type": "plain_text", "text": "Activity types (empty = all)"},
                "element": {
                    "type": "multi_static_select",
                    "action_id": "val",
                    "options": activity_opts,
                    **({"initial_options": initial_activities} if initial_activities else {}),
                },
            },
            {
                "type": "input",
                "block_id": "period",
                "label": {"type": "plain_text", "text": "Period"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": _select_options(PERIODS),
                    **_with_initial(_select_options(PERIODS), src.get("period") or "year"),
                },
            },
            {
                "type": "input",
                "block_id": "threshold",
                "label": {"type": "plain_text", "text": "Threshold"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(src.get("threshold") or 1),
                },
            },
            {
                "type": "input",
                "block_id": "range_mode",
                "label": {"type": "plain_text", "text": "Effective date range"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "Going forward only"},
                            "value": "going_forward",
                        },
                        {
                            "text": {"type": "plain_text", "text": "All previous attendance dates"},
                            "value": "all_previous",
                        },
                        {
                            "text": {"type": "plain_text", "text": "Custom range"},
                            "value": "custom",
                        },
                    ],
                    **_with_initial(
                        [
                            {"text": {"type": "plain_text", "text": "Going forward only"}, "value": "going_forward"},
                            {"text": {"type": "plain_text", "text": "All previous attendance dates"}, "value": "all_previous"},
                            {"text": {"type": "plain_text", "text": "Custom range"}, "value": "custom"},
                        ],
                        range_mode,
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "effective_from",
                "optional": True,
                "label": {"type": "plain_text", "text": "From (custom range)"},
                "element": {
                    "type": "datepicker",
                    "action_id": "val",
                    **(
                        {"initial_date": str(src["effective_from"])[:10]}
                        if src.get("effective_from")
                        else {"initial_date": _date.today().isoformat()}
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "effective_to",
                "optional": True,
                "label": {"type": "plain_text", "text": "Through (optional)"},
                "element": {"type": "datepicker", "action_id": "val"},
            },
        ]
    )
    if is_edit:
        blocks.append(
            {
                "type": "input",
                "block_id": "apply_mode",
                "label": {"type": "plain_text", "text": "If earning rules change"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "Going forward only"},
                            "value": "going_forward",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Apply to previous (queue backfill)",
                            },
                            "value": "retroactive",
                        },
                    ],
                    **_with_initial(
                        [
                            {"text": {"type": "plain_text", "text": "Going forward only"}, "value": "going_forward"},
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "Apply to previous (queue backfill)",
                                },
                                "value": "retroactive",
                            },
                        ],
                        "going_forward",
                    ),
                },
            }
        )
    return {
        "type": "modal",
        "callback_id": ACHIEVEMENT_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema, src["id"] if is_edit else None),
        "title": {"type": "plain_text", "text": "Edit achievement" if is_edit else "Add achievement"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _achievement_delete_modal(team_id: str, regional_schema: str, row: dict, award_count: int) -> dict:
    name = row.get("name") or "this achievement"
    text = (
        f"This achievement has {award_count} award(s). Deleting it will also delete those "
        f"{award_count} awards permanently. To stop awarding it while keeping PAX history, "
        f"disable it instead."
    )
    if award_count == 0:
        text = f"Delete *{name}*? It has no awards, so only the rule will be removed."
    return {
        "type": "modal",
        "callback_id": ACHIEVEMENT_DELETE_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema, row["id"]),
        "title": {"type": "plain_text", "text": "Delete or disable"},
        "submit": {"type": "plain_text", "text": "Confirm"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "input",
                "block_id": "delete_action",
                "label": {"type": "plain_text", "text": "What should happen?"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": f"Disable and keep {award_count} award(s)",
                            },
                            "value": "disable",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": f"Delete achievement and all {award_count} award(s)",
                            },
                            "value": "delete",
                        },
                    ],
                    **_with_initial(
                        [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": f"Disable and keep {award_count} award(s)",
                                },
                                "value": "disable",
                            }
                        ],
                        "disable" if award_count else "delete",
                    ),
                },
            },
        ],
    }


def _to_int(value, default):
    """Parse an int from free-text input; return default on empty/invalid."""
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_modal_values(payload: dict) -> dict:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    tz_sel = state.get("timezone", {}).get("val", {}).get("selected_option") or {}
    timezone = (tz_sel.get("value") or "America/Chicago").strip()
    return {
        "timezone": timezone,
    }


def _parse_achievement_form(payload: dict) -> dict:
    state = payload.get("view", {}).get("state", {}).get("values", {})

    def _text(block_id: str) -> str:
        return (state.get(block_id, {}).get("val", {}) or {}).get("value", "") or ""
        # strip below

    def _select(block_id: str) -> str:
        sel = (state.get(block_id, {}).get("val", {}) or {}).get("selected_option") or {}
        return (sel.get("value") or "").strip()

    def _multi(block_id: str) -> list[str]:
        opts = (state.get(block_id, {}).get("val", {}) or {}).get("selected_options") or []
        return [str(o.get("value")).strip() for o in opts if o.get("value")]

    def _date(block_id: str) -> str | None:
        return (state.get(block_id, {}).get("val", {}) or {}).get("selected_date")

    def _checked(block_id: str) -> bool:
        opts = (state.get(block_id, {}).get("val", {}) or {}).get("selected_options") or []
        return any(o.get("value") == "1" for o in opts)

    raw_threshold = _text("threshold").strip()
    if not raw_threshold:
        threshold = 1
    else:
        threshold = _to_int(raw_threshold, None)

    enabled_state = state.get("enabled")
    if enabled_state is None:
        enabled = 1
    else:
        enabled = 1 if _checked("enabled") else 0

    return {
        "name": _text("name").strip(),
        "description": _text("description").strip(),
        "verb": _text("verb").strip(),
        "code": _text("code").strip(),
        "metric": _select("metric") or "posts",
        "activity_list": _multi("activity"),
        "period": _select("period") or "year",
        "threshold": threshold,
        "enabled": enabled,
        "range_mode": _select("range_mode") or "going_forward",
        "effective_from": _date("effective_from"),
        "effective_to": _date("effective_to"),
        "apply_mode": _select("apply_mode") or "going_forward",
    }


def _validate_achievement(values: dict, *, require_code: bool = True) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not values["name"]:
        errors["name"] = "Name is required"
    if require_code:
        if not values["code"]:
            errors["code"] = "Code is required"
        elif not _CODE_RE.match(values["code"]):
            errors["code"] = "Use lowercase letters, numbers, and underscores"
    if values["metric"] not in METRICS:
        errors["metric"] = "Invalid metric"
    if values["period"] not in PERIODS:
        errors["period"] = "Invalid period"
    if values["threshold"] is None:
        errors["threshold"] = "Enter a whole number"
    elif values["threshold"] < 1:
        errors["threshold"] = "Threshold must be at least 1"
    return errors


def _load_achievements(cur, schema: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT a.*, v.version, v.version_key, v.metric AS version_metric,
               v.activity AS version_activity, v.period AS version_period,
               v.threshold AS version_threshold, v.effective_from, v.effective_to
        FROM `{schema}`.`achievements_list` a
        LEFT JOIN `{schema}`.`achievement_versions` v
          ON v.achievement_id = a.id AND v.superseded_at IS NULL
        ORDER BY a.name
        """
    )
    rows = list(cur.fetchall() or [])
    for row in rows:
        if row.get("version_metric"):
            row["metric"] = row["version_metric"]
        if row.get("version_period"):
            row["period"] = row["version_period"]
        if row.get("version_threshold") is not None:
            row["threshold"] = row["version_threshold"]
        if row.get("version_activity") is not None:
            row["activity"] = row["version_activity"]
        if row.get("version"):
            row["version"] = f"v{row['version']}"
    return rows


def _load_achievement(cur, schema: str, achievement_id: int) -> dict | None:
    cur.execute(
        f"""
        SELECT a.*, v.version, v.version_key, v.metric AS version_metric,
               v.activity AS version_activity, v.period AS version_period,
               v.threshold AS version_threshold, v.effective_from, v.effective_to
        FROM `{schema}`.`achievements_list` a
        LEFT JOIN `{schema}`.`achievement_versions` v
          ON v.achievement_id = a.id AND v.superseded_at IS NULL
        WHERE a.id=%s
        """,
        (achievement_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    if row.get("version_metric"):
        row["metric"] = row["version_metric"]
    if row.get("version_period"):
        row["period"] = row["version_period"]
    if row.get("version_threshold") is not None:
        row["threshold"] = row["version_threshold"]
    if row.get("version_activity") is not None:
        row["activity"] = row["version_activity"]
    if row.get("version"):
        row["version"] = f"v{row['version']}"
    return row


def _load_activity_options(cur, schema: str) -> list[str]:
    from achievements.activity import BUILTIN_ACTIVITY_TYPES

    found: list[str] = []
    try:
        cur.execute(
            f"""
            SELECT DISTINCT activity_type FROM `{schema}`.`beatdowns`
            WHERE activity_type IS NOT NULL AND activity_type != ''
            ORDER BY activity_type
            """
        )
        found = [str(r["activity_type"]) for r in (cur.fetchall() or []) if r.get("activity_type")]
    except Exception:
        found = []
    seen: list[str] = []
    for item in [*found, *BUILTIN_ACTIVITY_TYPES]:
        if item and item not in seen:
            seen.append(item)
    return seen[:100]


def _selected_achievement_id(payload: dict) -> int | None:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    sel = state.get("achievement_pick", {}).get(SELECT_ACHIEVEMENT_ACTION_ID, {}).get("selected_option")
    if not sel:
        return None
    try:
        return int(sel["value"])
    except (KeyError, TypeError, ValueError):
        return None

