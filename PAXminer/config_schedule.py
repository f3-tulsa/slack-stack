"""Schedule list / edit modals and PAX Reports builder (Bolt UI helpers)."""

from __future__ import annotations

import json
import logging
from typing import Any

from scheduling import (
    ALLOWED_SOURCES,
    DESTINATION_TYPES,
    FREQUENCY_TYPES,
    MONTH_DAY_MODES,
    REPORT_KINDS,
    TIME_WINDOW_TYPES,
    VALID_DESTINATIONS,
    destination_valid_for_report,
    format_schedule_summary,
    parse_time_of_day,
    time_of_day_options,
)

LOG = logging.getLogger(__name__)

SCHEDULE_LIST_CALLBACK_ID = "paxminer-schedule-list-id"
SCHEDULE_EDIT_CALLBACK_ID = "paxminer-schedule-edit-id"
REPORTS_LIST_CALLBACK_ID = "paxminer-reports-list-id"
REPORT_EDIT_CALLBACK_ID = "paxminer-report-edit-id"
KOTTER_CONFIG_CALLBACK_ID = "paxminer-kotter-config-id"

OPEN_SCHEDULE_ACTION_ID = "paxminer_open_schedule"
OPEN_REPORTS_ACTION_ID = "paxminer_open_reports"
OPEN_KOTTER_CONFIG_ACTION_ID = "paxminer_open_kotter_config"
OPEN_ACHIEVEMENTS_ACTION_ID = "paxminer_open_achievements_hub"

ADD_SCHEDULE_ACTION_ID = "paxminer_schedule_add"
EDIT_SCHEDULE_ACTION_ID = "paxminer_schedule_edit"
DELETE_SCHEDULE_ACTION_ID = "paxminer_schedule_delete"
TOGGLE_SCHEDULE_ACTION_ID = "paxminer_schedule_toggle"
DELETE_ALL_SCHEDULES_ACTION_ID = "paxminer_schedule_delete_all"
RESTORE_DEFAULTS_ACTION_ID = "paxminer_schedule_restore_defaults"
RUN_NOW_SCHEDULE_ACTION_ID = "paxminer_schedule_run_now"
SELECT_SCHEDULE_ACTION_ID = "paxminer_schedule_select"
SCHEDULE_DEST_TYPE_ACTION_ID = "paxminer_schedule_dest_type"
SCHEDULE_FREQ_ACTION_ID = "paxminer_schedule_freq"
SCHEDULE_REPORT_ACTION_ID = "paxminer_schedule_report"

ADD_REPORT_ACTION_ID = "paxminer_report_add"
EDIT_REPORT_ACTION_ID = "paxminer_report_edit"
DELETE_REPORT_ACTION_ID = "paxminer_report_delete"
DUPLICATE_REPORT_ACTION_ID = "paxminer_report_duplicate"
SELECT_REPORT_ACTION_ID = "paxminer_report_select"
REPORT_WINDOW_ACTION_ID = "paxminer_report_window"
LOAD_DEFAULTS_ACTION_ID = "paxminer_load_defaults"

PAGE_SIZE = 8

# report_type values rendered by dedicated Python (not the custom builder).
CODE_RENDERED_REPORT_TYPES = frozenset(
    {
        "pax_charts",
        "q_charts",
        "region_leaderboard",
        "ao_leaderboard",
        "achievement_leaderboard",
        "award_achievements",
        "kotter",
    }
)


def is_code_rendered(report_type: str | None) -> bool:
    return (report_type or "") in CODE_RENDERED_REPORT_TYPES


def supports_time_window(report_type: str | None) -> bool:
    """Kotter and Award Achievements use their own engine windows, not a report window."""
    return is_code_rendered(report_type) and report_type not in (
        "kotter",
        "award_achievements",
    )

TIMEZONE_OPTIONS = [
    "America/New_York",
    "America/Detroit",
    "America/Chicago",
    "America/Indiana/Indianapolis",
    "America/Indiana/Knox",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "Pacific/Honolulu",
]

FIELD_OPTIONS = ("Date", "AO", "PAX", "Q", "CoQ", "pax_count", "fng_count", "posts", "distinct_aos")
METRIC_OPTIONS = ("posts", "qs", "distinct_aos", "pax_count", "fng_count")
GROUP_BY_OPTIONS = ("PAX", "AO", "Q")


def _metadata(team_id: str, regional_schema: str, **extra) -> str:
    payload = {"team_id": team_id, "regional_schema": regional_schema, **extra}
    return json.dumps(payload)


def _parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _opt(value: str, label: str | None = None) -> dict:
    return {"text": {"type": "plain_text", "text": (label or value)[:75]}, "value": value}


def _select_options(values: tuple[str, ...] | list[str]) -> list[dict]:
    return [_opt(v) for v in values]


def _find_option(options: list[dict], value: str | None) -> dict | None:
    if not value:
        return None
    for o in options:
        if o["value"] == value:
            return o
    return None


def _with_initial(options: list[dict], value: str | None) -> dict:
    """Return ``{"initial_option": ...}`` only when the value is in options (Slack rejects null)."""
    found = _find_option(options, value)
    return {"initial_option": found} if found else {}


def _state_selected(state: dict, block_id: str, action_id: str = "val") -> str:
    block = state.get(block_id, {}).get(action_id, {})
    sel = block.get("selected_option") or {}
    return (sel.get("value") or "").strip()


def _state_multi_channels(state: dict, block_id: str, action_id: str = "val") -> list[str]:
    block = state.get(block_id, {}).get(action_id, {})
    return list(block.get("selected_conversations") or block.get("selected_channels") or [])


def _state_multi_users(state: dict, block_id: str, action_id: str = "val") -> list[str]:
    block = state.get(block_id, {}).get(action_id, {})
    return list(block.get("selected_users") or [])


def _state_text(state: dict, block_id: str, action_id: str = "val") -> str:
    return (state.get(block_id, {}).get(action_id, {}).get("value") or "").strip()


def _state_checkboxes(state: dict, block_id: str, action_id: str) -> list[str]:
    opts = state.get(block_id, {}).get(action_id, {}).get("selected_options") or []
    return [o.get("value") for o in opts if o.get("value")]


def load_definitions(cur, pm_schema: str, regional_schema: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT * FROM `{pm_schema}`.`region_report_definitions`
        WHERE schema_name=%s ORDER BY is_builtin DESC, name
        """,
        (regional_schema,),
    )
    return list(cur.fetchall() or [])


def load_schedules(cur, pm_schema: str, regional_schema: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT s.*, d.name AS definition_name, d.report_type, d.code AS definition_code
        FROM `{pm_schema}`.`region_schedules` s
        JOIN `{pm_schema}`.`region_report_definitions` d ON d.id = s.report_definition_id
        WHERE s.schema_name=%s
        ORDER BY s.id
        """,
        (regional_schema,),
    )
    return list(cur.fetchall() or [])


def load_schedule(cur, pm_schema: str, schedule_id: int) -> dict | None:
    cur.execute(
        f"""
        SELECT s.*, d.name AS definition_name, d.report_type
        FROM `{pm_schema}`.`region_schedules` s
        JOIN `{pm_schema}`.`region_report_definitions` d ON d.id = s.report_definition_id
        WHERE s.id=%s
        """,
        (schedule_id,),
    )
    return cur.fetchone()


def load_definition(cur, pm_schema: str, definition_id: int) -> dict | None:
    cur.execute(
        f"SELECT * FROM `{pm_schema}`.`region_report_definitions` WHERE id=%s",
        (definition_id,),
    )
    return cur.fetchone()


def _schedules_list_modal(
    team_id: str,
    regional_schema: str,
    schedules: list[dict],
    *,
    timezone_name: str = "America/Chicago",
    page: int = 0,
    notice: str | None = None,
    selected_schedule_id: int | None = None,
) -> dict:
    blocks: list[dict] = []
    if notice:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": notice}]}
        )
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Schedule* ({regional_schema})\n"
                    f"Times are *{timezone_name}* (region TZ).\n"
                    "_Default items are enabled. Set a channel/destination for "
                    "specific-channel reports, and disable any you do not want "
                    "(especially PAX chart DMs and all-AO fan-out)._"
                ),
            },
        }
    )
    start = page * PAGE_SIZE
    page_rows = schedules[start : start + PAGE_SIZE]
    if page_rows:
        lines = [
            format_schedule_summary(
                s, {"name": s.get("definition_name"), "id": s.get("id")}
            )
            for s in page_rows
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        options = [
            _opt(
                str(s["id"]),
                f"{s.get('definition_name') or s['id']} ({'on' if s.get('enabled') else 'off'})",
            )
            for s in page_rows
        ]
        pick_element: dict = {
            "type": "static_select",
            "action_id": SELECT_SCHEDULE_ACTION_ID,
            "placeholder": {"type": "plain_text", "text": "Choose…"},
            "options": options,
        }
        if selected_schedule_id is not None:
            initial = _find_option(options, str(selected_schedule_id))
            if initial:
                pick_element["initial_option"] = initial
        blocks.append(
            {
                "type": "input",
                "block_id": "schedule_pick",
                "optional": True,
                "label": {"type": "plain_text", "text": "Select schedule item"},
                "element": pick_element,
            }
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "_No scheduled items yet._ Use *Load defaults* to add the "
                        "builtin reports and schedules from `report_defaults.json`."
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": "schedule_load_defaults",
                "elements": [
                    {
                        "type": "button",
                        "action_id": LOAD_DEFAULTS_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Load defaults"},
                        "style": "primary",
                    }
                ],
            }
        )

    nav: list[dict] = []
    if page > 0:
        nav.append(
            {
                "type": "button",
                "action_id": "paxminer_schedule_page_prev",
                "text": {"type": "plain_text", "text": "← Prev"},
                "value": str(page - 1),
            }
        )
    if start + PAGE_SIZE < len(schedules):
        nav.append(
            {
                "type": "button",
                "action_id": "paxminer_schedule_page_next",
                "text": {"type": "plain_text", "text": "Next →"},
                "value": str(page + 1),
            }
        )
    if nav:
        blocks.append({"type": "actions", "block_id": "schedule_nav", "elements": nav})

    blocks.append(
        {
            "type": "actions",
            "block_id": "schedule_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ADD_SCHEDULE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Add item"},
                    "style": "primary",
                },
                {
                    "type": "button",
                    "action_id": EDIT_SCHEDULE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Edit selected"},
                },
                {
                    "type": "button",
                    "action_id": TOGGLE_SCHEDULE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Enable/Disable"},
                },
                {
                    "type": "button",
                    "action_id": RUN_NOW_SCHEDULE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Run Now"},
                },
                {
                    "type": "button",
                    "action_id": DELETE_SCHEDULE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Delete selected"},
                    "style": "danger",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Delete schedule?"},
                        "text": {"type": "mrkdwn", "text": "Remove this scheduled item?"},
                        "confirm": {"type": "plain_text", "text": "Delete"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        }
    )
    blocks.append(
        {
            "type": "actions",
            "block_id": "schedule_bulk",
            "elements": [
                {
                    "type": "button",
                    "action_id": DELETE_ALL_SCHEDULES_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Delete All"},
                    "style": "danger",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Delete all schedules?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": "Removes every schedule line item for this region. Report definitions are kept.",
                        },
                        "confirm": {"type": "plain_text", "text": "Delete All"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "action_id": RESTORE_DEFAULTS_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Restore Defaults"},
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Restore defaults?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Adds any missing builtin schedule rows (enabled). "
                                "Existing schedules are not deleted. Builtin reports "
                                "you have edited (`is_customized`) keep their edits; "
                                "missing builtins are re-added from defaults. Then set "
                                "channels for specific-channel reports and disable any "
                                "you do not want."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Restore"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        }
    )
    return {
        "type": "modal",
        "callback_id": SCHEDULE_LIST_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema, page=page),
        "title": {"type": "plain_text", "text": "Schedule"},
        "submit": {"type": "plain_text", "text": "Done"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": blocks,
    }


def _schedule_edit_modal(
    team_id: str,
    regional_schema: str,
    definitions: list[dict],
    *,
    schedule: dict | None = None,
    timezone_name: str = "America/Chicago",
    draft: dict | None = None,
) -> dict:
    """Add/Edit schedule. ``draft`` preserves in-progress values across views_update."""
    draft = dict(draft or {})
    if schedule and not draft:
        draft = {
            "report_definition_id": str(schedule.get("report_definition_id") or ""),
            "destination_type": schedule.get("destination_type") or "specific_channels",
            "destination_channels": _json_list(schedule.get("destination_channels")),
            "destination_users": _json_list(schedule.get("destination_users")),
            "frequency_type": schedule.get("frequency_type") or "monthly",
            "day_of_week": str(schedule.get("day_of_week") if schedule.get("day_of_week") is not None else "6"),
            "month_day_mode": schedule.get("month_day_mode") or "first",
            "day_of_month": str(schedule.get("day_of_month") or 1),
            "time_of_day": parse_time_of_day(schedule.get("time_of_day")).strftime("%H:%M"),
            "interval_days": str((_json_obj(schedule.get("custom_spec")) or {}).get("interval_days") or 7),
            "enabled": bool(schedule.get("enabled", 1)),
        }
    draft.setdefault("destination_type", "specific_channels")
    draft.setdefault("frequency_type", "monthly")
    draft.setdefault("time_of_day", "07:00")
    draft.setdefault("enabled", True)

    def_opts = [
        _opt(str(d["id"]), f"{d['name']} ({d['report_type']})")
        for d in definitions
    ]
    if not def_opts:
        def_opts = [_opt("0", "No reports defined — add one first")]

    report_type = "custom_report"
    selected_def = draft.get("report_definition_id")
    for d in definitions:
        if str(d["id"]) == str(selected_def or ""):
            report_type = d.get("report_type") or report_type
            break
    if not selected_def and definitions:
        selected_def = str(definitions[0]["id"])
        report_type = definitions[0].get("report_type") or report_type
        draft["report_definition_id"] = selected_def

    allowed_dests = VALID_DESTINATIONS.get(report_type, DESTINATION_TYPES)
    dest_opts = [_opt(d) for d in allowed_dests]
    dest_type = draft.get("destination_type") or allowed_dests[0]
    if dest_type not in allowed_dests:
        dest_type = allowed_dests[0]
        draft["destination_type"] = dest_type

    freq_opts = _select_options(FREQUENCY_TYPES)
    tod_opts = time_of_day_options()
    tod = draft.get("time_of_day") or "07:00"

    blocks: list[dict] = [
        {
            "type": "input",
            "block_id": "report_definition_id",
            "label": {"type": "plain_text", "text": "Report"},
            "element": {
                "type": "static_select",
                "action_id": SCHEDULE_REPORT_ACTION_ID,
                "options": def_opts,
                **_with_initial(def_opts, str(selected_def) if selected_def else None),
            },
        },
        {
            "type": "input",
            "block_id": "destination_type",
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": "Destination"},
            "element": {
                "type": "static_select",
                "action_id": SCHEDULE_DEST_TYPE_ACTION_ID,
                "options": dest_opts,
                **_with_initial(dest_opts, dest_type),
            },
        },
    ]

    if dest_type == "specific_channels":
        el: dict[str, Any] = {
            "type": "multi_conversations_select",
            "action_id": "val",
            "placeholder": {"type": "plain_text", "text": "Select channel(s)"},
            "filter": {"include": ["public", "private"]},
        }
        initial = draft.get("destination_channels") or []
        if initial:
            el["initial_conversations"] = initial[:100]
        blocks.append(
            {
                "type": "input",
                "block_id": "destination_channels",
                "optional": True,
                "label": {"type": "plain_text", "text": "Specific channel(s)"},
                "element": el,
            }
        )
    elif dest_type == "dm_specific_pax":
        el = {
            "type": "multi_users_select",
            "action_id": "val",
            "placeholder": {"type": "plain_text", "text": "Select PAX"},
        }
        initial_u = draft.get("destination_users") or []
        if initial_u:
            el["initial_users"] = initial_u[:100]
        blocks.append(
            {
                "type": "input",
                "block_id": "destination_users",
                "optional": True,
                "label": {"type": "plain_text", "text": "Specific PAX"},
                "element": el,
            }
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Destination `{dest_type}` resolves automatically at send time.",
                    }
                ],
            }
        )

    freq = draft.get("frequency_type") or "monthly"
    blocks.append(
        {
            "type": "input",
            "block_id": "frequency_type",
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": "Frequency"},
            "element": {
                "type": "static_select",
                "action_id": SCHEDULE_FREQ_ACTION_ID,
                "options": freq_opts,
                **_with_initial(freq_opts, freq),
            },
        }
    )

    if freq == "weekly":
        dow_opts = [
            _opt("0", "Monday"),
            _opt("1", "Tuesday"),
            _opt("2", "Wednesday"),
            _opt("3", "Thursday"),
            _opt("4", "Friday"),
            _opt("5", "Saturday"),
            _opt("6", "Sunday"),
        ]
        dow = draft.get("day_of_week") or "6"
        blocks.append(
            {
                "type": "input",
                "block_id": "day_of_week",
                "label": {"type": "plain_text", "text": "Day of week"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": dow_opts,
                    **_with_initial(dow_opts, dow),
                },
            }
        )
    elif freq == "monthly":
        mode_opts = _select_options(MONTH_DAY_MODES)
        mode = draft.get("month_day_mode") or "first"
        blocks.append(
            {
                "type": "input",
                "block_id": "month_day_mode",
                "label": {"type": "plain_text", "text": "Month day mode"},
                "element": {
                    "type": "static_select",
                    "action_id": "val",
                    "options": mode_opts,
                    **_with_initial(mode_opts, mode),
                },
            }
        )
        blocks.append(
            {
                "type": "input",
                "block_id": "day_of_month",
                "optional": True,
                "label": {"type": "plain_text", "text": "Day of month (if specific)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(draft.get("day_of_month") or "1"),
                },
            }
        )
    elif freq == "custom":
        blocks.append(
            {
                "type": "input",
                "block_id": "interval_days",
                "label": {"type": "plain_text", "text": "Every N days"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(draft.get("interval_days") or "7"),
                },
            }
        )

    blocks.append(
        {
            "type": "input",
            "block_id": "time_of_day",
            "label": {"type": "plain_text", "text": f"Time of day ({timezone_name})"},
            "element": {
                "type": "static_select",
                "action_id": "val",
                "options": tod_opts,
                **_with_initial(tod_opts, tod),
            },
        }
    )
    enabled_opts = [_opt("1", "Enabled"), _opt("0", "Disabled")]
    en = "1" if draft.get("enabled") not in (False, 0, "0", "false", "False") else "0"
    blocks.append(
        {
            "type": "input",
            "block_id": "enabled",
            "label": {"type": "plain_text", "text": "Enabled"},
            "element": {
                "type": "static_select",
                "action_id": "val",
                "options": enabled_opts,
                **_with_initial(enabled_opts, en),
            },
        }
    )

    schedule_id = schedule["id"] if schedule else None
    return {
        "type": "modal",
        "callback_id": SCHEDULE_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id, regional_schema, schedule_id=schedule_id, draft=draft
        ),
        "title": {"type": "plain_text", "text": "Edit schedule" if schedule else "Add schedule"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def draft_from_schedule_state(state: dict, meta_draft: dict | None = None) -> dict:
    """Merge modal state into a draft dict for views_update preservation."""
    draft = dict(meta_draft or {})
    rid = _state_selected(state, "report_definition_id", SCHEDULE_REPORT_ACTION_ID) or draft.get(
        "report_definition_id"
    )
    if rid:
        draft["report_definition_id"] = rid
    dest = _state_selected(state, "destination_type", SCHEDULE_DEST_TYPE_ACTION_ID)
    if dest:
        draft["destination_type"] = dest
    freq = _state_selected(state, "frequency_type", SCHEDULE_FREQ_ACTION_ID)
    if freq:
        draft["frequency_type"] = freq
    if "destination_channels" in state:
        draft["destination_channels"] = _state_multi_channels(state, "destination_channels")
    if "destination_users" in state:
        draft["destination_users"] = _state_multi_users(state, "destination_users")
    dow = _state_selected(state, "day_of_week")
    if dow:
        draft["day_of_week"] = dow
    mode = _state_selected(state, "month_day_mode")
    if mode:
        draft["month_day_mode"] = mode
    dom = _state_text(state, "day_of_month")
    if dom:
        draft["day_of_month"] = dom
    interval = _state_text(state, "interval_days")
    if interval:
        draft["interval_days"] = interval
    tod = _state_selected(state, "time_of_day")
    if tod:
        draft["time_of_day"] = tod
    en = _state_selected(state, "enabled")
    if en != "":
        draft["enabled"] = en == "1"

    # Drop stale keys for fields hidden by the current frequency / destination.
    dest_type = draft.get("destination_type") or "specific_channels"
    freq_type = draft.get("frequency_type") or "monthly"
    if dest_type != "specific_channels":
        draft.pop("destination_channels", None)
    if dest_type != "dm_specific_pax":
        draft.pop("destination_users", None)
    if freq_type != "weekly":
        draft.pop("day_of_week", None)
    if freq_type != "monthly":
        draft.pop("month_day_mode", None)
        draft.pop("day_of_month", None)
    if freq_type != "custom":
        draft.pop("interval_days", None)
    return draft


def parse_schedule_form(payload: dict) -> dict:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    draft = draft_from_schedule_state(state, meta.get("draft"))
    freq = draft.get("frequency_type") or "monthly"
    custom_spec = None
    if freq == "custom":
        try:
            custom_spec = {"interval_days": int(draft.get("interval_days") or 7)}
        except ValueError:
            custom_spec = {"interval_days": 7}
    return {
        "schedule_id": meta.get("schedule_id"),
        "report_definition_id": int(draft["report_definition_id"])
        if str(draft.get("report_definition_id") or "").isdigit()
        else None,
        "destination_type": draft.get("destination_type") or "specific_channels",
        "destination_channels": draft.get("destination_channels") or [],
        "destination_users": draft.get("destination_users") or [],
        "frequency_type": freq,
        "day_of_week": int(draft["day_of_week"]) if draft.get("day_of_week") not in (None, "") else None,
        "month_day_mode": draft.get("month_day_mode") or "first",
        "day_of_month": int(draft["day_of_month"])
        if str(draft.get("day_of_month") or "").isdigit()
        else None,
        "time_of_day": draft.get("time_of_day") or "07:00",
        "custom_spec": custom_spec,
        "enabled": 1 if draft.get("enabled", True) else 0,
    }


def validate_schedule_form(values: dict, report_type: str | None) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not values.get("report_definition_id"):
        errors["report_definition_id"] = "Select a report"
    dest = values.get("destination_type")
    if report_type and dest and not destination_valid_for_report(report_type, dest):
        errors["destination_type"] = f"Invalid destination for {report_type}"
    if dest == "specific_channels" and not values.get("destination_channels"):
        errors["destination_channels"] = "Pick at least one channel"
    if dest == "dm_specific_pax" and not values.get("destination_users"):
        errors["destination_users"] = "Pick at least one PAX"
    freq = values.get("frequency_type")
    if freq not in FREQUENCY_TYPES:
        errors["frequency_type"] = "Invalid frequency"
    elif freq == "weekly":
        dow = values.get("day_of_week")
        if dow is None or not (0 <= int(dow) <= 6):
            errors["day_of_week"] = "Pick a day of week"
    elif freq == "monthly":
        mode = values.get("month_day_mode") or "first"
        if mode == "specific":
            dom = values.get("day_of_month")
            if dom is None or not (1 <= int(dom) <= 31):
                errors["day_of_month"] = "Enter a day of month (1–31)"
    elif freq == "custom":
        interval = (values.get("custom_spec") or {}).get("interval_days")
        try:
            n = int(interval)
            if n < 1:
                raise ValueError
        except (TypeError, ValueError):
            errors["interval_days"] = "Enter a positive number of days"
    return errors


def _reports_list_modal(
    team_id: str,
    regional_schema: str,
    definitions: list[dict],
    notice: str | None = None,
) -> dict:
    blocks: list[dict] = []
    if notice:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": notice}]})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*PAX Reports* ({regional_schema})\n"
                    "Builtin reports render from code — you can rename, re-window, "
                    "schedule, duplicate, and delete them. Custom reports are fully "
                    "builder-editable."
                ),
            },
        }
    )
    lines = []
    for d in definitions:
        if d.get("is_builtin"):
            customized = " · edited" if d.get("is_customized") else ""
            window = d.get("time_window_type") or "—"
            lines.append(
                f"• *{d['name']}* (`{d['code']}`) — builtin / {d['report_type']}"
                f" / window={window}{customized}"
            )
        else:
            lines.append(
                f"• *{d['name']}* (`{d['code']}`) — {d.get('kind') or 'custom'} / "
                f"{d.get('source') or d.get('report_type') or '-'}"
            )
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines[:40])}})
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "_No reports yet._ Use *Load defaults* to add the builtin "
                        "reports and schedules from `report_defaults.json`."
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": "reports_load_defaults",
                "elements": [
                    {
                        "type": "button",
                        "action_id": LOAD_DEFAULTS_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Load defaults"},
                        "style": "primary",
                    }
                ],
            }
        )

    if definitions:
        blocks.append(
            {
                "type": "input",
                "block_id": "report_pick",
                "optional": True,
                "label": {"type": "plain_text", "text": "Select report"},
                "element": {
                    "type": "static_select",
                    "action_id": SELECT_REPORT_ACTION_ID,
                    "options": [
                        _opt(str(d["id"]), f"{d['name']} ({d['code']})") for d in definitions
                    ],
                },
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ADD_REPORT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Add custom report"},
                    "style": "primary",
                },
                {
                    "type": "button",
                    "action_id": EDIT_REPORT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Edit selected"},
                },
                {
                    "type": "button",
                    "action_id": DUPLICATE_REPORT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Duplicate selected"},
                },
                {
                    "type": "button",
                    "action_id": DELETE_REPORT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Delete selected"},
                    "style": "danger",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Delete report?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Deletes this report and any schedules that reference it. "
                                "You can restore builtins later with Load/Restore defaults."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Delete"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        }
    )
    return {
        "type": "modal",
        "callback_id": REPORTS_LIST_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema),
        "title": {"type": "plain_text", "text": "PAX Reports"},
        "submit": {"type": "plain_text", "text": "Done"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": blocks,
    }


def _report_edit_modal(
    team_id: str,
    regional_schema: str,
    row: dict | None = None,
    draft: dict | None = None,
) -> dict:
    draft = dict(draft or {})
    report_type = (row or {}).get("report_type") or draft.get("report_type") or "custom_report"
    if row and not draft:
        fields = _json_list(row.get("fields"))
        draft = {
            "name": row.get("name") or "",
            "code": row.get("code") or "",
            "kind": row.get("kind") or "table",
            "source": row.get("source") or "bd_attendance",
            "fields": fields,
            "metric": row.get("metric") or "posts",
            "group_by": row.get("group_by") or "PAX",
            "top_n": str(row.get("top_n") or 20),
            "time_window_type": row.get("time_window_type") or "last_month",
            "window_days": str(row.get("window_days") or 30),
            "window_start": str(row.get("window_start") or ""),
            "window_end": str(row.get("window_end") or ""),
            "report_type": report_type,
        }
    draft.setdefault("report_type", report_type)
    draft.setdefault("kind", "table")
    draft.setdefault("source", "bd_attendance")
    draft.setdefault("time_window_type", "last_month")
    draft.setdefault("metric", "posts")
    draft.setdefault("group_by", "PAX")

    code_rendered = is_code_rendered(report_type)
    show_window = supports_time_window(report_type) or not code_rendered

    blocks: list[dict] = [
        {
            "type": "input",
            "block_id": "name",
            "label": {"type": "plain_text", "text": "Name"},
            "element": {
                "type": "plain_text_input",
                "action_id": "val",
                "initial_value": draft.get("name") or "",
            },
        },
    ]

    if not code_rendered:
        # New custom reports (and edits of customs) expose the full builder + code.
        blocks.append(
            {
                "type": "input",
                "block_id": "code",
                "label": {"type": "plain_text", "text": "Code (unique snake_case)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": draft.get("code") or "",
                },
            }
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"Renderer: `{report_type}` (code `{draft.get('code') or ''}`). "
                            "Source/fields/metric stay fixed for code-rendered reports."
                        ),
                    }
                ],
            }
        )

    if not code_rendered:
        kind_opts = _select_options(REPORT_KINDS)
        source_opts = _select_options(ALLOWED_SOURCES)
        metric_opts = _select_options(METRIC_OPTIONS)
        group_opts = _select_options(GROUP_BY_OPTIONS)
        field_opts = [_opt(f) for f in FIELD_OPTIONS]
        selected_fields = draft.get("fields") or []
        field_initial = [o for o in field_opts if o["value"] in selected_fields]
        blocks.extend(
            [
                {
                    "type": "input",
                    "block_id": "kind",
                    "label": {"type": "plain_text", "text": "Output"},
                    "element": {
                        "type": "static_select",
                        "action_id": "val",
                        "options": kind_opts,
                        **_with_initial(kind_opts, draft.get("kind") or "table"),
                    },
                },
                {
                    "type": "input",
                    "block_id": "source",
                    "label": {"type": "plain_text", "text": "Data source"},
                    "element": {
                        "type": "static_select",
                        "action_id": "val",
                        "options": source_opts,
                        **_with_initial(source_opts, draft.get("source") or "bd_attendance"),
                    },
                },
                {
                    "type": "input",
                    "block_id": "fields",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Fields"},
                    "element": {
                        "type": "multi_static_select",
                        "action_id": "val",
                        "options": field_opts,
                        **({"initial_options": field_initial} if field_initial else {}),
                    },
                },
                {
                    "type": "input",
                    "block_id": "metric",
                    "label": {"type": "plain_text", "text": "Metric"},
                    "element": {
                        "type": "static_select",
                        "action_id": "val",
                        "options": metric_opts,
                        **_with_initial(metric_opts, draft.get("metric") or "posts"),
                    },
                },
                {
                    "type": "input",
                    "block_id": "group_by",
                    "label": {"type": "plain_text", "text": "Group by"},
                    "element": {
                        "type": "static_select",
                        "action_id": "val",
                        "options": group_opts,
                        **_with_initial(group_opts, draft.get("group_by") or "PAX"),
                    },
                },
                {
                    "type": "input",
                    "block_id": "top_n",
                    "label": {"type": "plain_text", "text": "Top N"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "val",
                        "initial_value": str(draft.get("top_n") or "20"),
                    },
                },
            ]
        )

    if show_window:
        window_opts = _select_options(TIME_WINDOW_TYPES)
        wtype = draft.get("time_window_type") or "last_month"
        blocks.append(
            {
                "type": "input",
                "block_id": "time_window_type",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Time window"},
                "element": {
                    "type": "static_select",
                    "action_id": REPORT_WINDOW_ACTION_ID,
                    "options": window_opts,
                    **_with_initial(window_opts, wtype),
                },
            }
        )
        if wtype == "relative_days":
            blocks.append(
                {
                    "type": "input",
                    "block_id": "window_days",
                    "label": {"type": "plain_text", "text": "Last N days"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "val",
                        "initial_value": str(draft.get("window_days") or "30"),
                    },
                }
            )
        elif wtype == "custom":
            for bid, label, key in (
                ("window_start", "Start date", "window_start"),
                ("window_end", "End date", "window_end"),
            ):
                el: dict[str, Any] = {"type": "datepicker", "action_id": "val"}
                if draft.get(key):
                    el["initial_date"] = str(draft[key])[:10]
                blocks.append(
                    {
                        "type": "input",
                        "block_id": bid,
                        "label": {"type": "plain_text", "text": label},
                        "element": el,
                    }
                )
    elif report_type == "kotter":
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Kotter uses the *Kotter Report* thresholds (weeks without "
                            "posting / Q'ing), not a time window."
                        ),
                    }
                ],
            }
        )
    elif report_type == "award_achievements":
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Award rules are configured under *PAX Achievements*. "
                            "This schedule only controls when and where awards are posted."
                        ),
                    }
                ],
            }
        )

    return {
        "type": "modal",
        "callback_id": REPORT_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id,
            regional_schema,
            definition_id=row["id"] if row else None,
            draft=draft,
            report_type=report_type,
        ),
        "title": {"type": "plain_text", "text": "Edit report" if row else "Add report"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def draft_from_report_state(state: dict, meta_draft: dict | None = None) -> dict:
    draft = dict(meta_draft or {})
    for key in ("name", "code", "top_n", "window_days"):
        val = _state_text(state, key)
        if val:
            draft[key] = val
    for key, action in (
        ("kind", "val"),
        ("source", "val"),
        ("metric", "val"),
        ("group_by", "val"),
    ):
        sel = _state_selected(state, key, action)
        if sel:
            draft[key] = sel
    w = _state_selected(state, "time_window_type", REPORT_WINDOW_ACTION_ID)
    if w:
        draft["time_window_type"] = w
    fields = state.get("fields", {}).get("val", {}).get("selected_options") or []
    if fields:
        draft["fields"] = [o["value"] for o in fields]
    for key in ("window_start", "window_end"):
        d = state.get(key, {}).get("val", {}).get("selected_date")
        if d:
            draft[key] = d
    return draft


def parse_report_form(payload: dict) -> dict:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    draft = draft_from_report_state(state, meta.get("draft"))
    report_type = meta.get("report_type") or draft.get("report_type") or "custom_report"
    top_n = 20
    try:
        top_n = int(draft.get("top_n") or 20)
    except ValueError:
        top_n = 20
    window_days = 30
    try:
        window_days = int(draft.get("window_days") or 30)
    except ValueError:
        window_days = 30
    code_rendered = is_code_rendered(report_type)
    return {
        "definition_id": meta.get("definition_id"),
        "name": (draft.get("name") or "").strip(),
        "code": (draft.get("code") or "").strip(),
        "kind": draft.get("kind") or "table",
        "source": draft.get("source") or "bd_attendance",
        "fields": draft.get("fields") or [],
        "metric": draft.get("metric") or "posts",
        "group_by": draft.get("group_by") or "PAX",
        "top_n": top_n,
        "time_window_type": (
            None
            if report_type in ("kotter", "award_achievements")
            else (draft.get("time_window_type") or "last_month")
        ),
        "window_days": window_days,
        "window_start": (draft.get("window_start") or None) or None,
        "window_end": (draft.get("window_end") or None) or None,
        "report_type": report_type if code_rendered else "custom_report",
        "is_builtin": 0,
        "code_rendered": code_rendered,
    }


def validate_report_form(values: dict) -> dict[str, str]:
    import re

    errors: dict[str, str] = {}
    if not values.get("name"):
        errors["name"] = "Name is required"
    code_rendered = values.get("code_rendered") or is_code_rendered(values.get("report_type"))
    if not code_rendered:
        code = values.get("code") or ""
        if not code:
            errors["code"] = "Code is required"
        elif not re.match(r"^[a-z0-9_]+$", code):
            errors["code"] = "Use lowercase letters, numbers, underscores"
        if values.get("kind") not in REPORT_KINDS:
            errors["kind"] = "Invalid output"
        if values.get("source") not in ALLOWED_SOURCES:
            errors["source"] = "Invalid source"
    if values.get("report_type") in ("kotter", "award_achievements"):
        return errors
    if values.get("time_window_type") == "custom":
        if not values.get("window_start"):
            errors["window_start"] = "Start date required"
        if not values.get("window_end"):
            errors["window_end"] = "End date required"
    return errors


def _kotter_config_modal(team_id: str, regional_schema: str, region: dict) -> dict:
    def _iv(key, default):
        return str(region.get(key) if region.get(key) is not None else default)

    return {
        "type": "modal",
        "callback_id": KOTTER_CONFIG_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema),
        "title": {"type": "plain_text", "text": "Kotter Reports"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Kotter thresholds*\nWhen/where Kotter posts is controlled in *Schedule*.",
                },
            },
            *[
                {
                    "type": "input",
                    "block_id": key,
                    "label": {"type": "plain_text", "text": label},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "val",
                        "initial_value": _iv(key, default),
                    },
                }
                for key, label, default in (
                    ("NO_POST_THRESHOLD", "No-post threshold (weeks)", 2),
                    ("REMINDER_WEEKS", "Reminder window (weeks)", 2),
                    ("HOME_AO_CAPTURE", "Home AO capture (weeks)", 8),
                    ("NO_Q_THRESHOLD_WEEKS", "No-Q threshold (weeks)", 4),
                    ("NO_Q_THRESHOLD_POSTS", "No-Q threshold (posts)", 4),
                )
            ],
        ],
    }


def parse_kotter_form(payload: dict) -> dict:
    from config_paxminer import _to_int

    state = payload.get("view", {}).get("state", {}).get("values", {})
    return {
        "NO_POST_THRESHOLD": _to_int(state.get("NO_POST_THRESHOLD", {}).get("val", {}).get("value"), 2),
        "REMINDER_WEEKS": _to_int(state.get("REMINDER_WEEKS", {}).get("val", {}).get("value"), 2),
        "HOME_AO_CAPTURE": _to_int(state.get("HOME_AO_CAPTURE", {}).get("val", {}).get("value"), 8),
        "NO_Q_THRESHOLD_WEEKS": _to_int(
            state.get("NO_Q_THRESHOLD_WEEKS", {}).get("val", {}).get("value"), 4
        ),
        "NO_Q_THRESHOLD_POSTS": _to_int(
            state.get("NO_Q_THRESHOLD_POSTS", {}).get("val", {}).get("value"), 4
        ),
    }


def selected_schedule_id(payload: dict) -> int | None:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    sel = state.get("schedule_pick", {}).get(SELECT_SCHEDULE_ACTION_ID, {}).get("selected_option")
    if not sel:
        return None
    try:
        return int(sel["value"])
    except (TypeError, ValueError, KeyError):
        return None


def selected_report_id(payload: dict) -> int | None:
    state = payload.get("view", {}).get("state", {}).get("values", {})
    sel = state.get("report_pick", {}).get(SELECT_REPORT_ACTION_ID, {}).get("selected_option")
    if not sel:
        return None
    try:
        return int(sel["value"])
    except (TypeError, ValueError, KeyError):
        return None


def _json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _json_obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
