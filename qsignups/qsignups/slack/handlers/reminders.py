from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

import pytz
from slack_sdk import WebClient
from sqlalchemy import or_

import constants
from database import DbManager
from database.orm import Region
from database.orm.views import vwMasterEvents
from field_encryption import decrypt_field

_MESSAGE_LIMIT = 3500


@dataclass
class TeamReminderResult:
    team_id: str
    workspace_name: str
    q_enabled: bool
    ao_enabled: bool
    q_messages_sent: int = 0
    ao_messages_sent: int = 0
    errors: list[str] = field(default_factory=list)

    def error_count(self) -> int:
        return len(self.errors)

    def to_user_message(self, manual: bool = False) -> str:
        prefix = "*Manual Q sheet reminder blast complete.*" if manual else "*Weekly Q sheet reminder blast complete.*"
        parts = []
        if self.q_enabled:
            if self.q_messages_sent:
                parts.append(f"dropped {self.q_messages_sent} Q reminder notice(s) on the PAX")
            else:
                parts.append("all Q slots are filled—no reminders needed")
        else:
            parts.append("Q reminders are disabled")
        if self.ao_enabled:
            if self.ao_messages_sent:
                parts.append(f"posted {self.ao_messages_sent} AO weekly Weinke notice(s)")
            else:
                parts.append("no AO reminders were needed")
        else:
            parts.append("AO reminders are disabled")
        if self.errors:
            parts.append(f"{self.error_count()} error(s) occurred; check logs")
        return f"{prefix}\nWorkspace: *{self.workspace_name}*\n" + "; ".join(parts) + "."


@dataclass
class ReminderRunSummary:
    team_results: list[TeamReminderResult] = field(default_factory=list)

    def total_q_messages_sent(self) -> int:
        return sum(result.q_messages_sent for result in self.team_results)

    def total_ao_messages_sent(self) -> int:
        return sum(result.ao_messages_sent for result in self.team_results)

    def total_errors(self) -> int:
        return sum(result.error_count() for result in self.team_results)

    def to_log_message(self) -> str:
        return (
            "weekly reminder automation complete "
            f"workspaces={len(self.team_results)} "
            f"q_messages={self.total_q_messages_sent()} "
            f"ao_messages={self.total_ao_messages_sent()} "
            f"errors={self.total_errors()}"
        )


def _is_enabled(value) -> bool:
    return value in (1, True)


def _region_timezone(region: Region, logger: logging.Logger):
    tz_name = region.timezone or constants.app_timezone().zone
    try:
        return pytz.timezone(tz_name)
    except Exception:
        logger.warning("Unknown region timezone %r for team=%s; using app default", tz_name, region.team_id)
        return constants.app_timezone()


def _window_dates(region: Region, logger: logging.Logger):
    tz = _region_timezone(region, logger)
    today = datetime.now(tz=tz).date()
    days_since_sunday = (today.weekday() + 1) % 7
    start_date = today - timedelta(days=days_since_sunday)
    return start_date, start_date + timedelta(days=6)


def _format_event_time(raw_time: str | None) -> str:
    if not raw_time:
        return "time TBD"
    if len(raw_time) >= 4:
        return f"{raw_time[:2]}:{raw_time[2:4]}"
    return raw_time


def _event_line(event) -> str:
    ao_name = event.ao_display_name or event.ao_channel_id or "AO"
    event_type = event.event_type or "Workout"
    when = event.event_date.strftime("%a %m/%d")
    return f"- {when} - {ao_name} - {event_type} @ {_format_event_time(event.event_time)}"


def _leader_label(event) -> str:
    if getattr(event, "q_pax_id", None):
        return f"<@{event.q_pax_id}>"
    if getattr(event, "q_pax_name", None):
        return event.q_pax_name
    return "*OPEN*"


def _ao_event_line(event) -> str:
    event_type = event.event_type or "Workout"
    when = event.event_date.strftime("%a %m/%d")
    return (
        f"- {when} - {event_type} @ {_format_event_time(event.event_time)} "
        f"- Leader/Q: {_leader_label(event)}"
    )


def _compose_message(header: str, lines: list[str]) -> str:
    message = header
    shown = 0
    for idx, line in enumerate(lines):
        candidate = f"{message}\n{line}"
        if len(candidate) > _MESSAGE_LIMIT:
            remaining = len(lines) - idx
            suffix = f"\n- ... and {remaining} more"
            if len(message) + len(suffix) <= _MESSAGE_LIMIT:
                message += suffix
            break
        message = candidate
        shown = idx + 1
    if shown == 0 and len(lines) > 0:
        return header
    return message


def _team_events(team_id: str, region: Region, logger: logging.Logger):
    start_date, end_date = _window_dates(region, logger)
    events = DbManager.find_records(
        vwMasterEvents,
        [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.event_date >= start_date,
            vwMasterEvents.event_date <= end_date,
        ],
    )
    events.sort(
        key=lambda event: (
            event.event_date,
            event.ao_display_name or "",
            event.event_time or "",
            event.q_pax_id or "",
        )
    )
    return events, start_date, end_date


def _post_user_message(client: WebClient, user_id: str, message: str):
    try:
        client.chat_postMessage(channel=user_id, text=message)
        return
    except Exception:
        convo = client.conversations_open(users=[user_id])
        channel_id = (convo or {}).get("channel", {}).get("id")
        if not channel_id:
            raise RuntimeError(f"Unable to open DM channel for user {user_id}")
        client.chat_postMessage(channel=channel_id, text=message)


def _send_q_reminders(client: WebClient, result: TeamReminderResult, events, start_date, end_date, logger: logging.Logger) -> None:
    grouped = defaultdict(list)
    for event in events:
        if event.q_pax_id:
            grouped[event.q_pax_id].append(event)
    for user_id, user_events in grouped.items():
        header = (
            f":calendar: *HIM—you're on the Q sheet for the week of Sunday {start_date.strftime('%m/%d')} - Saturday {end_date.strftime('%m/%d')}*\n"
            "Lace up—here are the beatdowns you're leading:"
        )
        message = _compose_message(header, [_event_line(event) for event in user_events])
        try:
            _post_user_message(client, user_id, message)
            result.q_messages_sent += 1
        except Exception as exc:
            logger.exception("Q reminder send failed team=%s user=%s", result.team_id, user_id)
            result.errors.append(f"Q reminder to {user_id} failed: {exc}")


def _send_ao_reminders(client: WebClient, result: TeamReminderResult, events, start_date, end_date, logger: logging.Logger) -> None:
    grouped = defaultdict(list)
    for event in events:
        if event.ao_channel_id:
            grouped[event.ao_channel_id].append(event)
    for channel_id, open_events in grouped.items():
        ao_name = open_events[0].ao_display_name or channel_id
        header = (
            f":mega: *{ao_name} Weinke — Week of Sunday {start_date.strftime('%m/%d')} - Saturday {end_date.strftime('%m/%d')}*\n"
            "Here's who's bringing the pain this week:"
        )
        message = _compose_message(header, [_ao_event_line(event) for event in open_events])
        try:
            client.chat_postMessage(channel=channel_id, text=message)
            result.ao_messages_sent += 1
        except Exception as exc:
            logger.exception("AO reminder send failed team=%s channel=%s", result.team_id, channel_id)
            result.errors.append(f"AO reminder to {channel_id} failed: {exc}")


def send_team_reminders(client: WebClient, team_id: str, logger: logging.Logger, region: Region | None = None) -> TeamReminderResult:
    region = region or DbManager.get_record(Region, team_id)
    if not region:
        return TeamReminderResult(
            team_id=team_id,
            workspace_name=team_id,
            q_enabled=False,
            ao_enabled=False,
            errors=[f"Region {team_id} not found"],
        )

    result = TeamReminderResult(
        team_id=team_id,
        workspace_name=region.workspace_name or team_id,
        q_enabled=_is_enabled(region.signup_reminders),
        ao_enabled=_is_enabled(region.weekly_ao_reminders),
    )
    if not result.q_enabled and not result.ao_enabled:
        logger.info("send_team_reminders: all reminder toggles disabled team=%s", team_id)
        return result

    events, start_date, end_date = _team_events(team_id, region, logger)
    if result.q_enabled:
        _send_q_reminders(client, result, events, start_date, end_date, logger)
    if result.ao_enabled:
        _send_ao_reminders(client, result, events, start_date, end_date, logger)

    logger.info(
        "send_team_reminders: team=%s q_messages=%s ao_messages=%s errors=%s",
        team_id,
        result.q_messages_sent,
        result.ao_messages_sent,
        result.error_count(),
    )
    return result


def send_all_region_reminders(logger: logging.Logger) -> ReminderRunSummary:
    summary = ReminderRunSummary()
    regions = DbManager.find_records(
        Region,
        [or_(Region.signup_reminders == 1, Region.weekly_ao_reminders == 1)],
    )
    for region in regions:
        result = TeamReminderResult(
            team_id=region.team_id,
            workspace_name=region.workspace_name or region.team_id,
            q_enabled=_is_enabled(region.signup_reminders),
            ao_enabled=_is_enabled(region.weekly_ao_reminders),
        )
        if not region.bot_token:
            result.errors.append("No stored bot token for workspace")
            summary.team_results.append(result)
            logger.error("send_all_region_reminders: missing bot token team=%s", region.team_id)
            continue
        try:
            token = decrypt_field(region.bot_token)
            client = WebClient(token=token)
            result = send_team_reminders(client, region.team_id, logger, region=region)
        except Exception as exc:
            logger.exception("send_all_region_reminders: failed team=%s", region.team_id)
            result.errors.append(str(exc))
        summary.team_results.append(result)
    logger.info(summary.to_log_message())
    return summary
