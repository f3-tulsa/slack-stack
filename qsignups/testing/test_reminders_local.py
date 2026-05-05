"""
Local tests for reminder grouping, region-wide reminder dispatch, and saved radio rendering.

Run from repo root:
  cd qsignups/qsignups && PYTHONPATH=. python ..\testing\test_reminders_local.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def _event(ao_channel_id: str, ao_display_name: str, q_pax_id: str | None, event_date: date, event_time: str, event_type: str):
    return SimpleNamespace(
        ao_channel_id=ao_channel_id,
        ao_display_name=ao_display_name,
        q_pax_id=q_pax_id,
        event_date=event_date,
        event_time=event_time,
        event_type=event_type,
    )


def test_radio_buttons_restore_saved_string_value() -> None:
    from slack import inputs

    field = inputs.Q_REMINDER_RADIO.as_form_field(initial_value="enabled")
    assert field["element"]["initial_option"]["value"] == "enabled"


def test_send_team_reminders_groups_q_and_ao_messages() -> None:
    from slack.handlers.reminders import send_team_reminders

    region = SimpleNamespace(
        team_id="T1",
        workspace_name="F3 Tulsa",
        signup_reminders=1,
        weekly_ao_reminders=1,
        timezone="US/Central",
    )
    events = [
        _event("CAO1", "The Bridge", "U1", date(2026, 5, 6), "0530", "Bootcamp"),
        _event("CAO1", "The Bridge", "U1", date(2026, 5, 8), "0530", "Bootcamp"),
        _event("CAO2", "The Forge", None, date(2026, 5, 7), "1730", "Beatdown"),
    ]
    client = MagicMock()

    with patch("slack.handlers.reminders.DbManager.find_records", return_value=events):
        result = send_team_reminders(client, "T1", logging.getLogger("test"), region=region)

    assert result.q_messages_sent == 1
    assert result.ao_messages_sent == 1
    assert result.error_count() == 0
    assert client.chat_postMessage.call_count == 2


def test_send_team_reminders_respects_disabled_flags() -> None:
    from slack.handlers.reminders import send_team_reminders

    region = SimpleNamespace(
        team_id="T1",
        workspace_name="F3 Tulsa",
        signup_reminders=0,
        weekly_ao_reminders=0,
        timezone="US/Central",
    )
    client = MagicMock()

    result = send_team_reminders(client, "T1", logging.getLogger("test"), region=region)

    assert result.q_messages_sent == 0
    assert result.ao_messages_sent == 0
    client.chat_postMessage.assert_not_called()


def test_send_all_region_reminders_uses_stored_bot_tokens() -> None:
    from slack.handlers.reminders import ReminderRunSummary, send_all_region_reminders

    region = SimpleNamespace(
        team_id="T1",
        workspace_name="F3 Tulsa",
        signup_reminders=1,
        weekly_ao_reminders=0,
        bot_token="encrypted",
        timezone="US/Central",
    )
    team_result = SimpleNamespace(
        q_messages_sent=1,
        ao_messages_sent=0,
        error_count=lambda: 0,
    )

    with patch("slack.handlers.reminders.DbManager.find_records", return_value=[region]):
        with patch("slack.handlers.reminders.decrypt_field", return_value="xoxb-test"):
            with patch("slack.handlers.reminders.WebClient") as web_client:
                with patch("slack.handlers.reminders.send_team_reminders", return_value=team_result) as send_team:
                    summary = send_all_region_reminders(logging.getLogger("test"))

    assert isinstance(summary, ReminderRunSummary)
    web_client.assert_called_once_with(token="xoxb-test")
    send_team.assert_called_once()


if __name__ == "__main__":
    test_radio_buttons_restore_saved_string_value()
    test_send_team_reminders_groups_q_and_ao_messages()
    test_send_team_reminders_respects_disabled_flags()
    test_send_all_region_reminders_uses_stored_bot_tokens()
    print("reminder tests OK")
