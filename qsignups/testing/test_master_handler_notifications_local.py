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
            with patch("slack.handlers.master.random.choice", side_effect=lambda templates: templates[0]):
                response = master_handler.assign_event_q(
                    client, user, "T1", log, datetime(2026, 5, 8, 5, 30), ao_display_name="The Bridge"
                )

    assert response.success is True
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "CAO1"
    assert kwargs["text"] == (
        ":fire: Sound off, PAX—the Q spot at *The Bridge* on *Friday, May 8 @ 0530* "
        "was wide open, and <@U_NEW> just called HC to lead from the front—updated by <@U_NEW>."
    )


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
            with patch("slack.handlers.master.random.choice", side_effect=lambda templates: templates[0]):
                response = master_handler.clear_event_q(
                    client, user, "T1", log, "The Bridge", datetime(2026, 5, 8, 5, 30)
                )

    assert response.success is True
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "CAO1"
    assert kwargs["text"] == (
        ":rotating_light: Sound off, PAX—<@U_OLD> released the HC for the Q spot at "
        "*The Bridge* on *Friday, May 8 @ 0530*, and the Q is back in the gloom looking "
        "for a HIM—updated by <@U_EDITOR>."
    )


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
                    with patch("slack.handlers.master.random.choice", side_effect=lambda templates: templates[0]):
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
    assert kwargs["text"] == (
        ":arrows_counterclockwise: Audible at *The Bridge* on *Friday, May 8 @ 0530*—"
        "<@U_OLD> passed the shovel flag to <@U_NEW>, who now has the Q—updated by <@U_EDITOR>."
    )


def test_q_change_message_rotations_keep_required_context() -> None:
    from slack.handlers import master as master_handler

    template_groups = (
        master_handler._OPEN_TO_CLAIMED_MESSAGES,
        master_handler._CLAIMED_TO_OPEN_MESSAGES,
        master_handler._REASSIGNED_MESSAGES,
    )

    assert all(len(templates) >= 4 for templates in template_groups)
    for templates in template_groups:
        for template in templates:
            message = template.format(
                actor="<@U_EDITOR>",
                location="*The Bridge* on *Friday, May 8 @ 0530*",
                new_q="<@U_NEW>",
                previous_q="<@U_OLD>",
                slot="the Q spot at *The Bridge* on *Friday, May 8 @ 0530*",
            )
            assert "The Bridge" in message
            assert "Friday, May 8 @ 0530" in message
            assert "<@U_EDITOR>" in message
            assert message.startswith(":")


def test_matching_q_state_does_not_notify() -> None:
    from slack.handlers import master as master_handler

    client = MagicMock()

    master_handler._notify_signup_state_change(
        client=client,
        logger=logging.getLogger("test"),
        ao_channel_id="CAO1",
        ao_display_name="The Bridge",
        actor=SimpleNamespace(id="U_EDITOR", name="Editor"),
        event_date=date(2026, 5, 8),
        event_time="0530",
        previous_q_id="U_SAME",
        previous_q_name="Same Pax",
        new_q_id="U_SAME",
        new_q_name="Same Pax",
    )

    client.chat_postMessage.assert_not_called()
