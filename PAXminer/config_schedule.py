"""Schedule list / edit modals and PAX Reports builder (Bolt UI helpers)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from slack_blocks import confirm_dialog, context, page_nav_elements, pencil_row
from scheduling import (
    ALLOWED_SOURCES,
    DESTINATION_TYPES,
    FREQUENCY_TYPES,
    MONTH_DAY_MODES,
    REPORT_KINDS,
    REPORT_TEMPLATES,
    TIME_WINDOW_TYPES,
    VALID_DESTINATIONS,
    destination_valid_for_report,
    destination_label,
    format_schedule_summary,
    parse_time_of_day,
    snap_time_to_tick,
    template_has,
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
REPORT_TEMPLATE_ACTION_ID = "paxminer_report_template"
LOAD_DEFAULTS_ACTION_ID = "paxminer_load_defaults"
DUPLICATE_SCHEDULE_ACTION_ID = "paxminer_schedule_duplicate"
DELETE_ALL_REPORTS_ACTION_ID = "paxminer_reports_delete_all"
RESTORE_REPORTS_ACTION_ID = "paxminer_reports_restore_defaults"
SCHEDULE_PAGE_PREV_ACTION_ID = "paxminer_schedule_page_prev"
SCHEDULE_PAGE_NEXT_ACTION_ID = "paxminer_schedule_page_next"
REPORTS_PAGE_PREV_ACTION_ID = "paxminer_reports_page_prev"
REPORTS_PAGE_NEXT_ACTION_ID = "paxminer_reports_page_next"

PAGE_SIZE = 15

# report_type values rendered by dedicated Python (not the custom builder).
CODE_RENDERED_REPORT_TYPES = frozenset(
    {
        "pax_charts",
        "q_charts",
        "region_leaderboard",
        "ao_leaderboard",
        "achievement_leaderboard",
        "achievement_almost_there",
        "award_achievements",
        "kotter",
    }
)


def is_code_rendered(report_type: str | None) -> bool:
    return (report_type or "") in CODE_RENDERED_REPORT_TYPES


def supports_time_window(report_type: str | None) -> bool:
    """True when the template declares a time window field."""
    return template_has(report_type, "window")

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


def action_row_id(payload: dict) -> int | None:
    """Integer `value` on the clicked button (pencil / edit-screen actions)."""
    action = (payload.get("actions") or [{}])[0]
    raw = action.get("value")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


_WINDOW_LABELS = {
    "relative_days": "Last N days",
    "last_month": "Last month",
    "this_month": "This month",
    "ytd": "YTD",
    "custom": "Custom",
}

_FREQ_LABELS = {
    "hourly": "Hourly",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "custom": "Custom",
}

_METRIC_HINTS = {
    "posts": "posts",
    "qs": "Qs",
    "distinct_aos": "AOs",
    "posts_at_single_ao": "posts at one AO",
}


def _iso_date(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if text else None


def _last_run_phrase(row: dict) -> str:
    status = (row.get("last_run_status") or "").strip().lower()
    on = _iso_date(row.get("last_run_on"))
    if not status and not on:
        return "Last run: never"
    label = status or "unknown"
    if on:
        return f"Last run: {label} ({on})"
    return f"Last run: {label}"


def _freq_phrase(row: dict) -> str:
    freq = row.get("frequency_type") or "monthly"
    tod = parse_time_of_day(row.get("time_of_day"))
    if freq == "hourly":
        return f"Hourly @ :{tod.minute:02d}"
    label = _FREQ_LABELS.get(freq, str(freq).title())
    return f"{label} @ {tod.strftime('%H:%M')}"


def schedule_subline(row: dict) -> str:
    enabled = "Enabled" if row.get("enabled") else "Disabled"
    dest = destination_label(row.get("destination_type"))
    return f"{enabled} | {dest} | {_freq_phrase(row)} | {_last_run_phrase(row)}"


def report_window_phrase(row: dict) -> str | None:
    if not supports_time_window(row.get("report_type")):
        return None
    wtype = row.get("time_window_type") or "last_month"
    if wtype == "relative_days":
        return f"Last {row.get('window_days') or 30} days"
    if wtype == "custom":
        start = _iso_date(row.get("window_start"))
        end = _iso_date(row.get("window_end"))
        if start and end:
            return f"{start} to {end}"
        return "Custom"
    return _WINDOW_LABELS.get(wtype, str(wtype).replace("_", " ").title())


def report_subline(row: dict) -> str:
    kind = "Builtin" if row.get("is_builtin") else "Custom"
    window = report_window_phrase(row)
    return f"{kind} | {window}" if window else kind


def duplicate_report_draft(cur, pm_schema: str, regional_schema: str, row: dict) -> dict:
    """Prefill Add-report without inserting. Code/name get a `_copy` suffix."""
    from schedule_schema import uniquify_definition_code

    fields = row.get("fields")
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except json.JSONDecodeError:
            fields = []
    return {
        "name": duplicate_name(row.get("name"), row.get("code")),
        "code": uniquify_definition_code(
            cur, pm_schema, regional_schema, row.get("code") or "report"
        ),
        "kind": row.get("kind") or "table",
        "source": row.get("source") or "bd_attendance",
        "fields": fields or [],
        "metric": row.get("metric") or "posts",
        "group_by": row.get("group_by") or "PAX",
        "top_n": str(row.get("top_n") or 20),
        "time_window_type": row.get("time_window_type") or "last_month",
        "window_days": str(row.get("window_days") or 30),
        "window_start": str(row.get("window_start") or ""),
        "window_end": str(row.get("window_end") or ""),
        "report_type": row.get("report_type") or "custom_report",
        "is_builtin": 0,
    }


def schedule_as_new_draft(schedule: dict) -> dict:
    """Prefill Add-schedule from an existing row (no id, no `_copy` suffix)."""
    return {
        "report_definition_id": str(schedule.get("report_definition_id") or ""),
        "destination_type": schedule.get("destination_type") or "specific_channels",
        "destination_channels": _json_list(schedule.get("destination_channels")),
        "destination_users": _json_list(schedule.get("destination_users")),
        "frequency_type": schedule.get("frequency_type") or "monthly",
        "day_of_week": str(
            schedule.get("day_of_week") if schedule.get("day_of_week") is not None else "6"
        ),
        "month_day_mode": schedule.get("month_day_mode") or "first",
        "day_of_month": str(schedule.get("day_of_month") or 1),
        "time_of_day": parse_time_of_day(schedule.get("time_of_day")).strftime("%H:%M"),
        "interval_days": str(
            (_json_obj(schedule.get("custom_spec")) or {}).get("interval_days") or 7
        ),
        "enabled": bool(schedule.get("enabled", 1)),
    }


def achievement_rule_hint(row: dict) -> str:
    period = str(row.get("period") or "year").title()
    metric = _METRIC_HINTS.get(row.get("metric") or "posts", row.get("metric") or "posts")
    return f"{period} - {row.get('threshold') or 1} {metric}"


def achievement_subline(row: dict) -> str:
    enabled = "Enabled" if int(row.get("enabled") or 1) else "Disabled"
    return f"{enabled} | {achievement_rule_hint(row)}"


def duplicate_name(name: str | None, code: str | None) -> str:
    base = (name or code or "copy").rstrip()
    return f"{base}_copy"


def _bulk_delete_restore(
    delete_id: str,
    restore_id: str,
    delete_confirm: dict,
    restore_confirm: dict,
) -> dict:
    return {
        "type": "actions",
        "block_id": "bulk_actions",
        "elements": [
            {
                "type": "button",
                "action_id": delete_id,
                "text": {"type": "plain_text", "text": "Delete All"},
                "style": "danger",
                "confirm": delete_confirm,
            },
            {
                "type": "button",
                "action_id": restore_id,
                "text": {"type": "plain_text", "text": "Restore Defaults"},
                "confirm": restore_confirm,
            },
        ],
    }


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
    del selected_schedule_id  # pencil rows replace the old dropdown
    blocks: list[dict] = []
    if notice:
        blocks.append(context(notice))
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Schedule* ({regional_schema})\n"
                    f"Times are *{timezone_name}* (region TZ). "
                    "Pencil opens Edit. Disable items you do not want "
                    "(especially PAX chart DMs and all-AO fan-out)."
                ),
            },
        }
    )
    total = len(schedules)
    max_page = max(0, (total - 1) // PAGE_SIZE) if total else 0
    page = min(max(page, 0), max_page)
    start = page * PAGE_SIZE
    page_rows = schedules[start : start + PAGE_SIZE]
    if page_rows:
        for row in page_rows:
            name = row.get("definition_name") or f"Schedule #{row.get('id')}"
            blocks.append(pencil_row(name, EDIT_SCHEDULE_ACTION_ID, str(row["id"])))
            blocks.append(context(schedule_subline(row)))
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No scheduled items yet._ Restore Defaults adds the builtin set.",
                },
            }
        )
    nav = page_nav_elements(
        page,
        total,
        SCHEDULE_PAGE_PREV_ACTION_ID,
        SCHEDULE_PAGE_NEXT_ACTION_ID,
        page_size=PAGE_SIZE,
    )
    if nav:
        blocks.append({"type": "actions", "block_id": "schedule_nav", "elements": nav})
    blocks.append(
        {
            "type": "actions",
            "block_id": "schedule_add",
            "elements": [
                {
                    "type": "button",
                    "action_id": ADD_SCHEDULE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Add"},
                    "style": "primary",
                }
            ],
        }
    )
    blocks.append(
        _bulk_delete_restore(
            DELETE_ALL_SCHEDULES_ACTION_ID,
            RESTORE_DEFAULTS_ACTION_ID,
            confirm_dialog(
                "Delete all schedules?",
                "Removes every schedule line for this region. Report definitions are kept.",
                "Delete All",
            ),
            confirm_dialog(
                "Restore defaults?",
                (
                    "Adds any missing builtin schedule rows (enabled). "
                    "Existing schedules are not deleted. Customized builtin reports keep "
                    "their edits; missing builtins are re-added."
                ),
                "Restore",
            ),
        )
    )
    return {
        "type": "modal",
        "callback_id": SCHEDULE_LIST_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema, page=page),
        "title": {"type": "plain_text", "text": "Schedule"},
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
    dest_opts = [_opt(d, destination_label(d)) for d in allowed_dests]
    dest_type = draft.get("destination_type") or allowed_dests[0]
    if dest_type not in allowed_dests:
        dest_type = allowed_dests[0]
        draft["destination_type"] = dest_type

    freq_opts = _select_options(FREQUENCY_TYPES)
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
                        "text": f"*{destination_label(dest_type)}* resolves automatically at send time.",
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

    if freq == "hourly":
        tod_opts = [
            {
                "text": {"type": "plain_text", "text": f":{minute:02d}"},
                "value": f"00:{minute:02d}",
            }
            for minute in (0, 15, 30, 45)
        ]
        snapped = snap_time_to_tick(parse_time_of_day(tod))
        tod = f"00:{snapped.minute:02d}"
        tod_label = "Minute of hour"
    else:
        tod_opts = time_of_day_options()
        tod_label = f"Time of day ({timezone_name})"
    blocks.append(
        {
            "type": "input",
            "block_id": "time_of_day",
            "label": {"type": "plain_text", "text": tod_label},
            "element": {
                "type": "static_select",
                "action_id": "val",
                "options": tod_opts,
                **_with_initial(tod_opts, tod),
            },
        }
    )
    enabled_opt = {
        "text": {"type": "plain_text", "text": "Run this schedule"},
        "value": "1",
    }
    enabled_el: dict[str, Any] = {
        "type": "checkboxes",
        "action_id": "val",
        "options": [enabled_opt],
    }
    if draft.get("enabled") not in (False, 0, "0", "false", "False"):
        enabled_el["initial_options"] = [enabled_opt]
    blocks.append(
        {
            "type": "input",
            "block_id": "enabled",
            "optional": True,
            "label": {"type": "plain_text", "text": "Enabled"},
            "element": enabled_el,
        }
    )

    schedule_id = (schedule or {}).get("id")
    is_edit = bool(schedule_id)
    if is_edit:
        sid = str(schedule_id)
        name = (
            (schedule or {}).get("definition_name")
            or draft.get("definition_name")
            or f"#{sid}"
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": "schedule_edit_extras",
                "elements": [
                    {
                        "type": "button",
                        "action_id": DUPLICATE_SCHEDULE_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Duplicate"},
                        "value": sid,
                    },
                    {
                        "type": "button",
                        "action_id": RUN_NOW_SCHEDULE_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Run Now"},
                        "value": sid,
                    },
                    {
                        "type": "button",
                        "action_id": DELETE_SCHEDULE_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Delete"},
                        "style": "danger",
                        "value": sid,
                        "confirm": confirm_dialog(
                            "Delete schedule?",
                            f"Remove the schedule for *{name}*?",
                        ),
                    },
                ],
            }
        )

    return {
        "type": "modal",
        "callback_id": SCHEDULE_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id, regional_schema, schedule_id=schedule_id, draft=draft
        ),
        "title": {"type": "plain_text", "text": "Edit schedule" if is_edit else "Add schedule"},
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
    if "enabled" in state:
        draft["enabled"] = "1" in _state_checkboxes(state, "enabled", "val")

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
    page: int = 0,
) -> dict:
    blocks: list[dict] = []
    if notice:
        blocks.append(context(notice))
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*PAX Reports* ({regional_schema})\n"
                    "Builtin reports render from code — rename, re-window, "
                    "schedule, duplicate, or delete them. Custom reports are fully "
                    "builder-editable."
                ),
            },
        }
    )
    total = len(definitions)
    max_page = max(0, (total - 1) // PAGE_SIZE) if total else 0
    page = min(max(page, 0), max_page)
    start = page * PAGE_SIZE
    page_rows = definitions[start : start + PAGE_SIZE]
    if page_rows:
        for row in page_rows:
            blocks.append(
                pencil_row(row.get("name") or row.get("code") or f"#{row.get('id')}", EDIT_REPORT_ACTION_ID, str(row["id"]))
            )
            blocks.append(context(report_subline(row)))
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No reports yet._ Restore Defaults adds the builtin reports and schedules.",
                },
            }
        )
    nav = page_nav_elements(
        page,
        total,
        REPORTS_PAGE_PREV_ACTION_ID,
        REPORTS_PAGE_NEXT_ACTION_ID,
        page_size=PAGE_SIZE,
    )
    if nav:
        blocks.append({"type": "actions", "block_id": "reports_nav", "elements": nav})
    blocks.append(
        {
            "type": "actions",
            "block_id": "reports_add",
            "elements": [
                {
                    "type": "button",
                    "action_id": ADD_REPORT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Add"},
                    "style": "primary",
                }
            ],
        }
    )
    blocks.append(
        _bulk_delete_restore(
            DELETE_ALL_REPORTS_ACTION_ID,
            RESTORE_REPORTS_ACTION_ID,
            confirm_dialog(
                "Delete all reports?",
                (
                    "Deletes every report definition for this region and every "
                    "schedule that references them. This cannot be undone."
                ),
                "Delete All",
            ),
            confirm_dialog(
                "Restore defaults?",
                (
                    "Adds any missing builtin reports and their default schedules. "
                    "Custom reports and customized builtins (`is_customized`) keep their edits."
                ),
                "Restore",
            ),
        )
    )
    return {
        "type": "modal",
        "callback_id": REPORTS_LIST_CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema, page=page),
        "title": {"type": "plain_text", "text": "PAX Reports"},
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
    is_add = not bool(row and row.get("id"))

    blocks: list[dict] = []
    if is_add:
        tpl_opts = [_opt(key, spec.get("label")) for key, spec in REPORT_TEMPLATES.items()]
        blocks.append(
            {
                "type": "input",
                "block_id": "report_type",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Template"},
                "element": {
                    "type": "static_select",
                    "action_id": REPORT_TEMPLATE_ACTION_ID,
                    "options": tpl_opts,
                    **_with_initial(tpl_opts, report_type),
                },
            }
        )
    blocks.append(
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
    )

    if not code_rendered or is_add:
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

    if code_rendered and template_has(report_type, "top_n"):
        default_n = REPORT_TEMPLATES.get(report_type, {}).get("default_top_n") or 10
        blocks.append(
            {
                "type": "input",
                "block_id": "top_n",
                "label": {"type": "plain_text", "text": "Top N"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "val",
                    "initial_value": str(draft.get("top_n") or default_n),
                },
            }
        )

    if show_window:
        window_opts = [_opt(v, _WINDOW_LABELS.get(v)) for v in TIME_WINDOW_TYPES]
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

    is_edit = bool(row and row.get("id"))
    if is_edit:
        rid = str(row["id"])
        code = row.get("code") or draft.get("code") or rid
        blocks.append(
            {
                "type": "actions",
                "block_id": "report_edit_extras",
                "elements": [
                    {
                        "type": "button",
                        "action_id": DUPLICATE_REPORT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Duplicate"},
                        "value": rid,
                    },
                    {
                        "type": "button",
                        "action_id": DELETE_REPORT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Delete"},
                        "style": "danger",
                        "value": rid,
                        "confirm": confirm_dialog(
                            "Delete report?",
                            (
                                f"Deletes `{code}` and any schedules that reference it. "
                                "Restore Defaults can bring builtins back."
                            ),
                        ),
                    },
                ],
            }
        )

    return {
        "type": "modal",
        "callback_id": REPORT_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id,
            regional_schema,
            definition_id=row["id"] if is_edit else None,
            draft=draft,
            report_type=report_type,
        ),
        "title": {"type": "plain_text", "text": "Edit report" if is_edit else "Add report"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def draft_from_report_state(state: dict, meta_draft: dict | None = None) -> dict:
    draft = dict(meta_draft or {})
    for key in ("name", "code", "top_n", "window_days"):
        if key in state:
            draft[key] = _state_text(state, key)
    for key, action in (
        ("kind", "val"),
        ("source", "val"),
        ("metric", "val"),
        ("group_by", "val"),
    ):
        if key in state:
            sel = _state_selected(state, key, action)
            if sel:
                draft[key] = sel
    if "time_window_type" in state:
        w = _state_selected(state, "time_window_type", REPORT_WINDOW_ACTION_ID)
        if w:
            draft["time_window_type"] = w
    if "report_type" in state:
        tmpl = _state_selected(state, "report_type", REPORT_TEMPLATE_ACTION_ID)
        if tmpl:
            draft["report_type"] = tmpl
    if "fields" in state:
        fields = state.get("fields", {}).get("val", {}).get("selected_options") or []
        draft["fields"] = [o["value"] for o in fields]
    for key in ("window_start", "window_end"):
        if key in state:
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
            if not template_has(report_type, "window")
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
    if not values.get("definition_id"):
        code = values.get("code") or ""
        if not code:
            errors["code"] = "Code is required"
        elif not re.match(r"^[a-z0-9_]+$", code):
            errors["code"] = "Use lowercase letters, numbers, underscores"
    elif not code_rendered:
        code = values.get("code") or ""
        if not code:
            errors["code"] = "Code is required"
        elif not re.match(r"^[a-z0-9_]+$", code):
            errors["code"] = "Use lowercase letters, numbers, underscores"
        if values.get("kind") not in REPORT_KINDS:
            errors["kind"] = "Invalid output"
        if values.get("source") not in ALLOWED_SOURCES:
            errors["source"] = "Invalid source"
    if not template_has(values.get("report_type"), "window"):
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
        "NO_POST_THRESHOLD": _to_int(state.get("NO_POST_THRESHOLD", {}).get("val", {}).get("value"), None),
        "REMINDER_WEEKS": _to_int(state.get("REMINDER_WEEKS", {}).get("val", {}).get("value"), None),
        "HOME_AO_CAPTURE": _to_int(state.get("HOME_AO_CAPTURE", {}).get("val", {}).get("value"), None),
        "NO_Q_THRESHOLD_WEEKS": _to_int(
            state.get("NO_Q_THRESHOLD_WEEKS", {}).get("val", {}).get("value"), None
        ),
        "NO_Q_THRESHOLD_POSTS": _to_int(
            state.get("NO_Q_THRESHOLD_POSTS", {}).get("val", {}).get("value"), None
        ),
    }


def validate_kotter_form(values: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (
        "NO_POST_THRESHOLD",
        "REMINDER_WEEKS",
        "HOME_AO_CAPTURE",
        "NO_Q_THRESHOLD_WEEKS",
        "NO_Q_THRESHOLD_POSTS",
    ):
        if values.get(key) is None:
            errors[key] = "Enter a whole number"
    return errors


def selected_schedule_id(payload: dict) -> int | None:
    action = (payload.get("actions") or [{}])[0]
    aid = action.get("action_id")
    if aid in {
        EDIT_SCHEDULE_ACTION_ID,
        DELETE_SCHEDULE_ACTION_ID,
        DUPLICATE_SCHEDULE_ACTION_ID,
        RUN_NOW_SCHEDULE_ACTION_ID,
        TOGGLE_SCHEDULE_ACTION_ID,
    }:
        rid = action_row_id(payload)
        if rid is not None:
            return rid
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    if meta.get("schedule_id") not in (None, ""):
        try:
            return int(meta["schedule_id"])
        except (TypeError, ValueError):
            pass
    state = payload.get("view", {}).get("state", {}).get("values", {})
    sel = state.get("schedule_pick", {}).get(SELECT_SCHEDULE_ACTION_ID, {}).get("selected_option")
    if not sel:
        return None
    try:
        return int(sel["value"])
    except (TypeError, ValueError, KeyError):
        return None


def selected_report_id(payload: dict) -> int | None:
    action = (payload.get("actions") or [{}])[0]
    aid = action.get("action_id")
    if aid in {
        EDIT_REPORT_ACTION_ID,
        DELETE_REPORT_ACTION_ID,
        DUPLICATE_REPORT_ACTION_ID,
    }:
        rid = action_row_id(payload)
        if rid is not None:
            return rid
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    if meta.get("definition_id") not in (None, ""):
        try:
            return int(meta["definition_id"])
        except (TypeError, ValueError):
            pass
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
