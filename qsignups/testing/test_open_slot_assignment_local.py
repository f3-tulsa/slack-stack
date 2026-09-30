"""
Local tests for open-slot assignment actions.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def _load_app_module():
    import sys as sys_mod

    with patch.dict(os.environ, {"DB_ENCRYPTION_KEY": "ci-test-encryption-key-32chars!"}, clear=False):
        with patch("slack_bolt.app.app.App._init_middleware_list", lambda *args, **kwargs: None):
            sys_mod.modules.pop("app", None)
            import app as app_mod

    return app_mod


def test_handle_date_select_button_opens_assignment_modal() -> None:
    app_mod = _load_app_module()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "actions": [{"value": "2026-05-08 05:30:00"}],
        "trigger_id": "TRIGGER1",
        "view": {"blocks": [{}, {"text": {"text": "*The Bridge*"}}]},
    }
    context = {"user_id": "U_SELF", "team_id": "T1"}

    with patch.object(app_mod.event, "open_open_slot_assignment_modal") as open_modal:
        app_mod.handle_date_select_button(ack, client, body, logger, context)

    ack.assert_called_once()
    open_modal.assert_called_once_with(
        trigger_id="TRIGGER1",
        client=client,
        logger=logger,
        ao_display_name="The Bridge",
        selected_dt=app_mod.datetime(2026, 5, 8, 5, 30),
        initial_user_id="U_SELF",
    )


def test_handle_date_select_button_from_message_opens_assignment_modal() -> None:
    app_mod = _load_app_module()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "channel": {"id": "CAO1"},
        "actions": [{"value": "2026-05-08 05:30:00"}],
        "trigger_id": "TRIGGER2",
    }
    context = {"user_id": "U_SELF", "team_id": "T1"}

    with patch.object(
        app_mod.helper,
        "find_ao",
        return_value=type("AOObj", (), {"ao_display_name": "The Bridge"})(),
    ):
        with patch.object(app_mod.event, "open_open_slot_assignment_modal") as open_modal:
            app_mod.handle_date_select_button_from_message(ack, client, body, logger, context)

    ack.assert_called_once()
    open_modal.assert_called_once_with(
        trigger_id="TRIGGER2",
        client=client,
        logger=logger,
        ao_display_name="The Bridge",
        selected_dt=app_mod.datetime(2026, 5, 8, 5, 30),
        initial_user_id="U_SELF",
    )


def test_handle_submit_assign_open_slot_button_assigns_self() -> None:
    app_mod = _load_app_module()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    actor = app_mod.User(id="U_SELF", name="Self Pax")
    body = {
        "user": {"id": "U_SELF", "team_id": "T1"},
        "view": {
            "private_metadata": '{"ao_display_name":"The Bridge","selected_date":"2026-05-08 05:30:00"}',
            "state": {"values": {"open_slot_q_select": {"open_slot_q_select": {"selected_user": "U_SELF"}}}},
        }
    }
    context = {"user_id": "U_SELF", "team_id": "T1"}

    with patch.object(app_mod, "get_user", return_value=actor) as get_user:
        with patch.object(
            app_mod.master_handler,
            "assign_event_q",
            return_value=type("Resp", (), {"success": True, "message": "ok"})(),
        ) as assign_event_q:
            with patch.object(app_mod.home, "refresh") as refresh:
                app_mod.handle_submit_assign_open_slot_button(ack, client, body, logger, context)

    ack.assert_called_once()
    assert get_user.call_count >= 2
    assign_event_q.assert_called_once_with(
        client,
        actor,
        "T1",
        logger,
        app_mod.datetime(2026, 5, 8, 5, 30),
        ao_display_name="The Bridge",
        assigned_user=actor,
    )
    refresh.assert_called_once()
    assert "You're on the Q sheet" in refresh.call_args.args[3]


def test_handle_submit_assign_open_slot_button_assigns_other_user() -> None:
    app_mod = _load_app_module()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    actor = app_mod.User(id="U_EDITOR", name="Editor")
    assignee = app_mod.User(id="U_OTHER", name="Other Pax")
    body = {
        "user": {"id": "U_EDITOR", "team_id": "T1"},
        "view": {
            "private_metadata": '{"ao_display_name":"The Bridge","selected_date":"2026-05-08 05:30:00"}',
            "state": {"values": {"open_slot_q_select": {"open_slot_q_select": {"selected_user": "U_OTHER"}}}},
        }
    }
    context = {"user_id": "U_EDITOR", "team_id": "T1"}

    with patch.object(app_mod, "get_user", side_effect=[actor, assignee]):
        with patch.object(
            app_mod.master_handler,
            "assign_event_q",
            return_value=type("Resp", (), {"success": True, "message": "ok"})(),
        ) as assign_event_q:
            with patch.object(app_mod.home, "refresh") as refresh:
                app_mod.handle_submit_assign_open_slot_button(ack, client, body, logger, context)

    ack.assert_called_once()
    assign_event_q.assert_called_once_with(
        client,
        actor,
        "T1",
        logger,
        app_mod.datetime(2026, 5, 8, 5, 30),
        ao_display_name="The Bridge",
        assigned_user=assignee,
    )
    refresh.assert_called_once()
    assert "*Other Pax* now has the Q slot" in refresh.call_args.args[3]


if __name__ == "__main__":
    test_handle_date_select_button_opens_assignment_modal()
    test_handle_date_select_button_from_message_opens_assignment_modal()
    test_handle_submit_assign_open_slot_button_assigns_self()
    test_handle_submit_assign_open_slot_button_assigns_other_user()
    print("open slot assignment action tests OK")
