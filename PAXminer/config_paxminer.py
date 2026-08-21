"""`/config-paxminer` Slack modal builders and DB helpers (Bolt listeners live in slack_app)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path

from slack_blocks import confirm_dialog, context, counted_noun, overflow_row, page_nav_elements, parse_overflow_action
from config_schedule import PAGE_SIZE, _bulk_delete_restore, achievement_subline

LOG = logging.getLogger(__name__)

CALLBACK_ID = "paxminer-config-id"
ACHIEVEMENTS_LIST_CALLBACK_ID = "paxminer-achievements-list-id"
ACHIEVEMENT_EDIT_CALLBACK_ID = "paxminer-achievement-edit-id"
ACHIEVEMENT_RANGE_CONFIRM_CALLBACK_ID = "paxminer-achievement-range-confirm-id"
ADD_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_add"
EDIT_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_edit"
MORE_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_more"
DELETE_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_delete"
ACHIEVEMENT_DELETE_CONFIRM_CALLBACK_ID = "paxminer-achievement-delete-confirm-id"
BACKFILL_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_backfill"
SELECT_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_select"
DUPLICATE_ACHIEVEMENT_ACTION_ID = "paxminer_achievement_duplicate"
DELETE_ALL_ACHIEVEMENTS_ACTION_ID = "paxminer_achievements_delete_all"
RESTORE_ACHIEVEMENTS_ACTION_ID = "paxminer_achievements_restore_defaults"
REEVAL_FROM_ACTION_ID = "paxminer_achievement_reeval_from"
REEVAL_TO_ACTION_ID = "paxminer_achievement_reeval_to"
ACHIEVEMENTS_PAGE_PREV_ACTION_ID = "paxminer_achievements_page_prev"
ACHIEVEMENTS_PAGE_NEXT_ACTION_ID = "paxminer_achievements_page_next"
# Distinct from the shared "val" action_id so the options handler matches only
# the emoji picker.
EMOJI_OPTIONS_ACTION_ID = "paxminer_achievement_emoji"

METRICS = ("posts", "qs", "distinct_aos", "posts_at_single_ao")
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


def _metadata(team_id: str, regional_schema: str, achievement_id: int | None = None, **extra) -> str:
    payload = {"team_id": team_id, "regional_schema": regional_schema, **extra}
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
    from achievements.copy import achievement_rule_phrase

    version = row.get("version") or row.get("version_key") or "v?"
    enabled = "on" if int(row.get("enabled") or 0) else "off"
    emoji = str(row.get("emoji") or "").strip().strip(":")
    emoji_bit = f" :{emoji}:" if emoji else ""
    return (
        f"*{row['name']}* (`{row['code']}` {version}) — "
        f"{achievement_rule_phrase(row)} · {enabled}{emoji_bit}"
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
    log_ch = (region.get("log_channel") or "").strip()
    log_element = {
        "type": "conversations_select",
        "action_id": "val",
        "placeholder": {"type": "plain_text", "text": "Defaults to #paxminer_logs"},
        "filter": {"include": ["public", "private"], "exclude_bot_users": True},
    }
    if log_ch:
        log_element["initial_conversation"] = log_ch

    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "private_metadata": _metadata(team_id, regional_schema),
        "title": {"type": "plain_text", "text": "PAXMiner Settings"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
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
                "type": "input",
                "block_id": "log_channel",
                "optional": True,
                "label": {"type": "plain_text", "text": "PAXMiner log channel"},
                "hint": {
                    "type": "plain_text",
                    "text": (
                        "Leave empty to keep looking up #paxminer_logs by name. "
                        "Invite both the PAXMiner and Slackblast bots."
                    ),
                },
                "element": log_element,
            },
        ],
    }


def _achievements_list_modal(
    team_id: str,
    regional_schema: str,
    achievements: list[dict],
    notice: str | None = None,
    page: int = 0,
    selected_id: int | None = None,
    reeval_from: str | None = None,
    reeval_to: str | None = None,
) -> dict:
    today = date.today().isoformat()
    to_s = (reeval_to or today)[:10]
    from_s = (reeval_from or today)[:10]
    total = len(achievements)
    max_page = max(0, (total - 1) // PAGE_SIZE) if total else 0
    page = min(max(page, 0), max_page)
    start = page * PAGE_SIZE
    page_rows = achievements[start : start + PAGE_SIZE]
    if selected_id is None and page_rows:
        selected_id = page_rows[0].get("id")
    selected = next((a for a in achievements if a.get("id") == selected_id), None)

    blocks: list[dict] = []
    if notice:
        blocks.append(context(notice))
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Achievements* ({regional_schema})"},
        }
    )
    if page_rows:
        for row in page_rows:
            blocks.append(
                overflow_row(
                    row.get("name") or row.get("code") or f"#{row.get('id')}",
                    MORE_ACHIEVEMENT_ACTION_ID,
                    row["id"],
                    enabled=int(row.get("enabled") or 0) == 1,
                    subline=achievement_subline(row),
                )
            )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No achievements defined yet._ Add Missing Defaults adds the builtin catalog."},
            }
        )
    nav = page_nav_elements(
        page,
        total,
        ACHIEVEMENTS_PAGE_PREV_ACTION_ID,
        ACHIEVEMENTS_PAGE_NEXT_ACTION_ID,
        page_size=PAGE_SIZE,
    )
    if nav:
        blocks.append({"type": "actions", "block_id": "achievements_nav", "elements": nav})
    blocks.append(
        {
            "type": "actions",
            "block_id": "achievements_add",
            "elements": [
                {
                    "type": "button",
                    "action_id": ADD_ACHIEVEMENT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Add"},
                    "style": "primary",
                }
            ],
        }
    )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Re-evaluate awards*"},
        }
    )
    blocks.append(
        context(
            "Grants and revokes for the selected achievement in this date range "
            "using current rules. No T-claps or PAX DMs — watch the log channel. "
            "A channel summary posts only if something actually changed."
        )
    )
    if achievements:
        opts = [
            {
                "text": {"type": "plain_text", "text": (a.get("name") or a.get("code") or str(a["id"]))[:75]},
                "value": str(a["id"]),
            }
            for a in achievements[:100]
        ]
        pick: dict = {
            "type": "static_select",
            "action_id": SELECT_ACHIEVEMENT_ACTION_ID,
            "placeholder": {"type": "plain_text", "text": "Achievement"},
            "options": opts,
        }
        if selected_id is not None:
            initial = next((o for o in opts if o["value"] == str(selected_id)), None)
            if initial:
                pick["initial_option"] = initial
        blocks.append({"type": "actions", "block_id": "reeval_pick", "elements": [pick]})
        blocks.append(
            {
                "type": "actions",
                "block_id": "reeval_dates",
                "elements": [
                    {
                        "type": "datepicker",
                        "action_id": REEVAL_FROM_ACTION_ID,
                        "initial_date": from_s,
                        "placeholder": {"type": "plain_text", "text": "From"},
                    },
                    {
                        "type": "datepicker",
                        "action_id": REEVAL_TO_ACTION_ID,
                        "initial_date": to_s,
                        "placeholder": {"type": "plain_text", "text": "Through"},
                    },
                ],
            }
        )
        label = (selected or {}).get("name") or (selected or {}).get("code") or "this achievement"
        range_label = f"{from_s} to {to_s}"
        blocks.append(
            {
                "type": "actions",
                "block_id": "reeval_go",
                "elements": [
                    {
                        "type": "button",
                        "action_id": BACKFILL_ACHIEVEMENT_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Re-evaluate"},
                        "value": str(selected_id or ""),
                        "confirm": confirm_dialog(
                            "Re-evaluate awards?",
                            f"Re-evaluate *{label}* from {range_label} using current rules?",
                            "Re-evaluate",
                        ),
                    }
                ],
            }
        )
    blocks.append(
        _bulk_delete_restore(
            DELETE_ALL_ACHIEVEMENTS_ACTION_ID,
            RESTORE_ACHIEVEMENTS_ACTION_ID,
            confirm_dialog(
                "Delete all achievements?",
                (
                    "Deletes every achievement in this region, including every award "
                    "of those codes. Custom achievements are not restored later. "
                    "This cannot be undone."
                ),
                "Delete All",
            ),
            confirm_dialog(
                "Add missing defaults?",
                (
                    "Adds only builtin codes that are not already present. "
                    "Existing rows are left as-is, including builtins you have edited. "
                    "For a true reset, Delete All first, then Add Missing Defaults. "
                    "Each added achievement queues an automatic re-evaluate over all attendance."
                ),
                "Add Missing",
            ),
        )
    )
    return {
        "type": "modal",
        "callback_id": ACHIEVEMENTS_LIST_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id,
            regional_schema,
            page=page,
            selected_id=selected_id,
            reeval_from=from_s,
            reeval_to=to_s,
        ),
        "title": {"type": "plain_text", "text": "Achievements"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": blocks,
    }


def _achievement_edit_modal(
    team_id: str,
    regional_schema: str,
    row: dict | None = None,
    *,
    activity_options: list[str] | None = None,
    prefill_from_source: bool = False,
    client=None,
) -> dict:
    from achievements.activity import (
        activity_filter_from_rule,
        map_activities_to_options,
        unique_activity_labels,
    )

    is_edit = bool(row and row.get("id"))
    src = dict(row or {})
    spec = activity_filter_from_rule(src)
    options = unique_activity_labels(
        [
            o
            for o in (activity_options or [])
            if str(o).strip() and str(o).strip().lower() != "beatdown"
        ]
    )
    # Keep stored labels selectable when the catalog still has types, so a
    # retired Event Type is not silently dropped on the next save. An empty
    # catalog still hides both pickers and preserves the stored filter.
    if options:
        options = unique_activity_labels([*options, *spec["include"], *spec["exclude"]])
    activity_opts = _select_options(tuple(options))
    selected_activities = map_activities_to_options(spec["include"], options)
    initial_activities = [o for o in activity_opts if o["value"] in selected_activities]
    selected_excludes = map_activities_to_options(spec["exclude"], options)
    initial_excludes = [o for o in activity_opts if o["value"] in selected_excludes]
    from achievements.range import (
        RANGE_CUSTOM,
        RANGE_FROM_CREATED,
        iso_date,
        normalize_range_mode,
        range_mode_hint,
        range_mode_options,
    )

    # Add (empty src) defaults to from-created. Duplicate passes
    # prefill_from_source so a NULL range_mode derives the same way Edit does.
    if is_edit or prefill_from_source:
        range_mode = normalize_range_mode(
            src.get("range_mode"), effective_from=src.get("effective_from")
        )
    else:
        range_mode = RANGE_FROM_CREATED
    is_custom = range_mode == RANGE_CUSTOM
    start_display = iso_date(src.get("effective_from")) if is_custom else None
    end_display = iso_date(src.get("effective_to")) if is_custom else None
    range_opts = range_mode_options()
    range_hint = range_mode_hint(
        range_mode,
        first_created=src.get("first_created"),
        version_created=src.get("version_created"),
        earliest_beatdown=src.get("earliest_beatdown"),
    )
    from achievements.emoji import emoji_select_element, normalize_emoji_name

    stored_emoji = normalize_emoji_name(src.get("emoji"))
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
            {
                "type": "input",
                "block_id": "emoji",
                "optional": True,
                "label": {"type": "plain_text", "text": "Award reaction (optional)"},
                "hint": {
                    "type": "plain_text",
                    "text": "Type to search every emoji in this workspace. "
                    "Added next to :fire: on the public award message.",
                },
                "element": emoji_select_element(EMOJI_OPTIONS_ACTION_ID, stored_emoji),
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
                    "initial_value": src.get("code") or "",
                },
            }
        )
    blocks.append(
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
        }
    )
    if activity_opts:
        blocks.append(
            {
                "type": "input",
                "block_id": "activity",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Include event types (empty = all)",
                },
                "hint": {
                    "type": "plain_text",
                    "text": "Slackblast Event Types. Empty include matches unlabeled posts too.",
                },
                "element": {
                    "type": "multi_static_select",
                    "action_id": "val",
                    "options": activity_opts,
                    **({"initial_options": initial_activities} if initial_activities else {}),
                },
            }
        )
        blocks.append(
            {
                "type": "input",
                "block_id": "activity_exclude",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Exclude event types (optional)",
                },
                "hint": {
                    "type": "plain_text",
                    "text": "These types never count, even when Include is empty.",
                },
                "element": {
                    "type": "multi_static_select",
                    "action_id": "val",
                    "options": activity_opts,
                    **({"initial_options": initial_excludes} if initial_excludes else {}),
                },
            }
        )
    else:
        # No block_id: view state must omit "activity" so save preserves the stored filter.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "_No Slackblast Event Types configured. "
                            "This achievement matches every event, including unlabeled posts._"
                        ),
                    }
                ],
            }
        )
    blocks.extend(
        [
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
                    "options": range_opts,
                    **_with_initial(range_opts, range_mode),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": range_hint,
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "effective_from",
                "optional": True,
                "label": {"type": "plain_text", "text": "Start date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "val",
                    **({"initial_date": start_display} if start_display else {}),
                },
            },
            {
                "type": "input",
                "block_id": "effective_to",
                "optional": True,
                "label": {"type": "plain_text", "text": "End date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "val",
                    **({"initial_date": end_display} if end_display else {}),
                },
            },
        ]
    )
    return {
        "type": "modal",
        "callback_id": ACHIEVEMENT_EDIT_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id, regional_schema, src["id"] if is_edit else None
        ),
        "title": {"type": "plain_text", "text": "Edit achievement" if is_edit else "Add achievement"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
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
    log_sel = (state.get("log_channel") or {}).get("val") or {}
    log_channel = (log_sel.get("selected_conversation") or "").strip() or None
    return {
        "timezone": timezone,
        "log_channel": log_channel,
    }


def _parse_achievement_form(payload: dict) -> dict:
    from achievements.activity import (
        canonicalize_activity_filter,
        canonicalize_exclude_filter,
        unique_activity_labels,
    )

    state = payload.get("view", {}).get("state", {}).get("values", {})

    def _text(block_id: str) -> str:
        return (state.get(block_id, {}).get("val", {}) or {}).get("value", "") or ""
        # strip below

    def _select(block_id: str, action_id: str = "val") -> str:
        sel = (state.get(block_id, {}).get(action_id, {}) or {}).get("selected_option") or {}
        return (sel.get("value") or "").strip()

    def _multi(block_id: str) -> list[str]:
        opts = (state.get(block_id, {}).get("val", {}) or {}).get("selected_options") or []
        return canonicalize_activity_filter(
            unique_activity_labels(
                [str(o.get("value")).strip() for o in opts if o.get("value")]
            )
        )

    def _multi_exclude(block_id: str) -> list[str]:
        opts = (state.get(block_id, {}).get("val", {}) or {}).get("selected_options") or []
        return canonicalize_exclude_filter(
            unique_activity_labels(
                [str(o.get("value")).strip() for o in opts if o.get("value")]
            )
        )

    def _date(block_id: str) -> str | None:
        return (state.get(block_id, {}).get("val", {}) or {}).get("selected_date")

    threshold = _to_int(_text("threshold").strip(), None)

    include = _multi("activity") if "activity" in state else None
    exclude = _multi_exclude("activity_exclude") if "activity_exclude" in state else None
    parsed = {
        "name": _text("name").strip(),
        "description": _text("description").strip(),
        "verb": _text("verb").strip(),
        "code": _text("code").strip(),
        "metric": _select("metric") or "posts",
        "period": _select("period") or "year",
        "threshold": threshold,
        "range_mode": _select("range_mode") or "from_created",
        "effective_from": _date("effective_from"),
        "effective_to": _date("effective_to"),
        "activity_list": include,
        "activity_filter": {"include": include, "exclude": exclude},
        "emoji": _select("emoji", EMOJI_OPTIONS_ACTION_ID) or _select("emoji"),
    }
    from achievements.emoji import normalize_emoji_name

    parsed["emoji"] = normalize_emoji_name(parsed["emoji"])
    return parsed


def _validate_achievement(
    values: dict,
    *,
    require_code: bool = True,
    first_created=None,
    version_created=None,
    earliest_beatdown=None,
) -> dict[str, str]:
    from achievements.activity import activity_filter_conflicts
    from achievements.range import range_validation_errors

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
    spec = values.get("activity_filter")
    if isinstance(spec, dict):
        conflicts = activity_filter_conflicts(
            {
                "include": spec.get("include") or [],
                "exclude": spec.get("exclude") or [],
            }
        )
        # Only name the exclude block when it was actually submitted. After
        # resolve, include/exclude are never None, and Slack rejects an errors
        # payload for a block_id that is not in the view.
        if conflicts and values.get("activity_exclude_submitted", True):
            named = conflicts[0]
            errors["activity_exclude"] = f"{named} is in both Include and Exclude"
    errors.update(
        range_validation_errors(
            values,
            first_created=first_created,
            version_created=version_created,
            earliest_beatdown=earliest_beatdown,
        )
    )
    return errors


def _load_achievements(cur, schema: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT a.*, v.version, v.version_key, v.metric AS version_metric,
               v.activity AS version_activity, v.period AS version_period,
               v.threshold AS version_threshold, v.effective_from, v.effective_to,
               v.range_mode, v.created AS version_created
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
               v.threshold AS version_threshold, v.effective_from, v.effective_to,
               v.range_mode, v.created AS version_created,
               v1.created AS first_created
        FROM `{schema}`.`achievements_list` a
        LEFT JOIN `{schema}`.`achievement_versions` v
          ON v.achievement_id = a.id AND v.superseded_at IS NULL
        LEFT JOIN `{schema}`.`achievement_versions` v1
          ON v1.achievement_id = a.id AND v1.version = 1
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


def _load_activity_options(cur, schema: str, team_id: str | None = None) -> list[str]:
    """Event Type options from slackblast.regions.custom_fields. No builtins, no beatdown."""
    from achievements.activity import event_type_options_from_custom_fields

    sb_schema = os.environ.get("SLACKBLAST_SCHEMA") or f"slackblast_{os.environ.get('STAGE', 'test')}"
    try:
        if team_id:
            cur.execute(
                f"SELECT custom_fields FROM `{sb_schema}`.`regions` WHERE team_id=%s LIMIT 1",
                (team_id,),
            )
        else:
            cur.execute(
                f"SELECT custom_fields FROM `{sb_schema}`.`regions` WHERE paxminer_schema=%s LIMIT 1",
                (schema,),
            )
        row = cur.fetchone() or {}
    except Exception:
        return []
    return event_type_options_from_custom_fields(row.get("custom_fields"))


def _selected_achievement_id(payload: dict) -> int | None:
    action = (payload.get("actions") or [{}])[0]
    aid = action.get("action_id")
    if aid in {
        EDIT_ACHIEVEMENT_ACTION_ID,
        MORE_ACHIEVEMENT_ACTION_ID,
        DELETE_ACHIEVEMENT_ACTION_ID,
        DUPLICATE_ACHIEVEMENT_ACTION_ID,
        BACKFILL_ACHIEVEMENT_ACTION_ID,
    }:
        verb, oid = parse_overflow_action(action)
        if oid is not None:
            return oid
        raw = action.get("value")
        try:
            if raw not in (None, ""):
                return int(raw)
        except (TypeError, ValueError):
            pass
        sel = action.get("selected_option") or {}
        try:
            if sel.get("value"):
                return int(sel["value"])
        except (TypeError, ValueError):
            pass
    if aid == SELECT_ACHIEVEMENT_ACTION_ID:
        sel = action.get("selected_option") or {}
        try:
            return int(sel["value"])
        except (TypeError, ValueError, KeyError):
            pass
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    for key in ("selected_id", "achievement_id"):
        if meta.get(key) not in (None, ""):
            try:
                return int(meta[key])
            except (TypeError, ValueError):
                pass
    state = payload.get("view", {}).get("state", {}).get("values", {})
    for block in ("reeval_pick", "achievement_pick"):
        sel = state.get(block, {}).get(SELECT_ACHIEVEMENT_ACTION_ID, {}).get("selected_option")
        if sel:
            try:
                return int(sel["value"])
            except (TypeError, ValueError, KeyError):
                continue
    return None


def _reeval_dates_from_payload(payload: dict) -> tuple[str | None, str | None]:
    meta = _parse_metadata((payload.get("view") or {}).get("private_metadata"))
    from_s = meta.get("reeval_from")
    to_s = meta.get("reeval_to")
    action = (payload.get("actions") or [{}])[0]
    aid = action.get("action_id")
    picked = action.get("selected_date")
    if aid == REEVAL_FROM_ACTION_ID and picked:
        from_s = picked
    if aid == REEVAL_TO_ACTION_ID and picked:
        to_s = picked
    state = payload.get("view", {}).get("state", {}).get("values", {})
    dates = state.get("reeval_dates") or {}
    if dates.get(REEVAL_FROM_ACTION_ID, {}).get("selected_date"):
        from_s = dates[REEVAL_FROM_ACTION_ID]["selected_date"]
    if dates.get(REEVAL_TO_ACTION_ID, {}).get("selected_date"):
        to_s = dates[REEVAL_TO_ACTION_ID]["selected_date"]
    return from_s, to_s


def uniquify_achievement_code(cur, schema: str, base: str) -> str:
    candidate = f"{base}_copy"
    n = 2
    while True:
        cur.execute(
            f"SELECT id FROM `{schema}`.`achievements_list` WHERE code=%s",
            (candidate,),
        )
        if not cur.fetchone():
            return candidate
        candidate = f"{base}_copy_{n}"
        n += 1


def load_achievement_defaults() -> list[dict]:
    path = Path(__file__).resolve().parent / "achievement_defaults.json"
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("achievement_defaults.json must be a list")
    return data


def achievement_delete_confirm_text(code: str, award_count: int, pax_count: int) -> str:
    """Native Slack confirm copy for deleting one achievement from the edit modal."""
    awards = counted_noun(award_count, "award")
    pax = counted_noun(pax_count, "PAX", "PAX")
    return (
        f"Are you sure you want to delete the `{code}` achievement? If you do, it will "
        f"remove {awards} from {pax}. This cannot be undone! To keep the historical "
        "awards and not award anything new, simply disable this achievement."
    )


def achievement_award_impact(cur, schema: str, achievement_id: int) -> tuple[int, int]:
    """``(award_count, distinct_pax_count)`` for one achievement code."""
    cur.execute(
        f"SELECT COUNT(*) AS awards, COUNT(DISTINCT pax_id) AS pax "
        f"FROM `{schema}`.`achievements_awarded` WHERE achievement_id=%s",
        (achievement_id,),
    )
    row = cur.fetchone() or {}
    return int(row.get("awards") or 0), int(row.get("pax") or 0)


def count_achievement_awards(cur, schema: str, achievement_id: int) -> int:
    awards, _pax = achievement_award_impact(cur, schema, achievement_id)
    return awards


def earliest_beatdown_date(cur, schema: str) -> str | None:
    try:
        cur.execute(f"SELECT MIN(bd_date) AS d FROM `{schema}`.`beatdowns`")
        d = (cur.fetchone() or {}).get("d")
        return str(d)[:10] if d else None
    except Exception:
        return None


def _hydrate_range_row(cur, schema: str, row: dict | None) -> dict | None:
    """Attach earliest beatdown so the edit modal can prefill All attendance dates."""
    if row is None:
        return None
    row["earliest_beatdown"] = earliest_beatdown_date(cur, schema)
    return row


def _achievement_range_confirm_modal(
    team_id: str,
    regional_schema: str,
    *,
    achievement_id: int,
    values: dict,
    award_count: int,
    pax_count: int,
) -> dict:
    from achievements.range import range_confirm_text

    return {
        "type": "modal",
        "callback_id": ACHIEVEMENT_RANGE_CONFIRM_CALLBACK_ID,
        "private_metadata": _metadata(
            team_id,
            regional_schema,
            achievement_id,
            pending_values=values,
            range_confirmed=True,
        ),
        "title": {"type": "plain_text", "text": "Confirm range change"},
        "submit": {"type": "plain_text", "text": "Revoke and save"},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": range_confirm_text(award_count, pax_count),
                },
            }
        ],
    }

