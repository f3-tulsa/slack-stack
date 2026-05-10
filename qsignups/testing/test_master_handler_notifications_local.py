"""
Local tests for AO-channel notifications on Q slot state changes.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_assign_event_q_notifies_ao_channel() -> None:
    from slack.handlers import master as master_handler

    log = logging.getLogger("test")
    client = MagicMock()
    user = SimpleNamespace(id="U_NEW", name="New Pax")
    result = SimpleNamespace(
        ao=SimpleNamespace(ao_channel_id="CAO1", ao_display_name="The Bridge"),
        event=SimpleNamespace(id=7, event_date=date(2026, 5, 8), event_time="0530", q_pax_id=None, q_pax_name=None),
    )

    with patch("slack.handlers.master.helper.find_master_event", return_value=result):
        with patch("slack.handlers.master.DbManager.update_record"):
            response = master_handler.assign_event_q(
                client, user, "T1", log, datetime(2026, 5, 8, 5, 30), ao_display_name="The Bridge"
            )

    assert response.success is True
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "CAO1"
    assert "Previous: *OPEN*" in kwargs["text"]
    assert "Now: <@U_NEW>" in kwargs["text"]


def test_clear_event_q_notifies_ao_channel() -> None:
    from slack.handlers import master as master_handler

    log = logging.getLogger("test")
    client = MagicMock()
    user = SimpleNamespace(id="U_EDITOR", name="Editor")
    result = SimpleNamespace(
        ao=SimpleNamespace(ao_channel_id="CAO1", ao_display_name="The Bridge"),
        event=SimpleNamespace(
            id=8, event_date=date(2026, 5, 8), event_time="0530", q_pax_id="U_OLD", q_pax_name="Old Pax", google_event_id=None
        ),
    )

    with patch("slack.handlers.master.helper.find_master_event", return_value=result):
        with patch("slack.handlers.master.DbManager.update_record"):
            response = master_handler.clear_event_q(
                client, user, "T1", log, "The Bridge", datetime(2026, 5, 8, 5, 30)
            )

    assert response.success is True
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "CAO1"
    assert "Previous: <@U_OLD>" in kwargs["text"]
    assert "Now: *OPEN*" in kwargs["text"]


def test_update_events_from_state_notifies_only_when_q_changes() -> None:
    from slack.handlers import master as master_handler

    log = logging.getLogger("test")
    client = MagicMock()
    user = SimpleNamespace(id="U_EDITOR", name="Editor")
    records = [
        SimpleNamespace(
            id=9,
            q_pax_id="U_OLD",
            q_pax_name="Old Pax",
            event_date=date(2026, 5, 8),
            event_time="0530",
            google_event_id=None,
        )
    ]
    state_values = {
        "edit_event_datepicker": {"edit_event_datepicker": {"selected_date": "2026-05-08"}},
        "edit_event_timepicker": {"edit_event_timepicker": {"selected_time": "05:30"}},
        "edit_event_end_timepicker": {"edit_event_end_timepicker": {"selected_time": "06:15"}},
        "edit_event_q_select": {"edit_event_q_select": {"selected_users": ["U_NEW"]}},
        "edit_event_special_select": {"edit_event_special_select": {"selected_option": {"text": {"text": "None"}}}},
    }

    client.users_info.return_value = {"user": {"profile": {"display_name": "New Pax"}}}

    with patch("slack.handlers.master.DbManager.get_record"):
        with patch(
            "slack.handlers.master.helper.find_ao",
            return_value=SimpleNamespace(ao_channel_id="CAO1", ao_display_name="The Bridge"),
        ):
            with patch("slack.handlers.master.DbManager.find_records", side_effect=[records, []]):
                with patch("slack.handlers.master.DbManager.update_records"):
                    response = master_handler.update_events_from_state(
                        client,
                        user,
                        "T1",
                        log,
                        "CAO1",
                        state_values,
                        "2026-05-08",
                        "0530",
                    )

    assert response.success is True
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "CAO1"
    assert "Previous: <@U_OLD>" in kwargs["text"]
    assert "Now: <@U_NEW>" in kwargs["text"]

