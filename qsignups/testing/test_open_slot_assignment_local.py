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


def test_handle_date_select_button_publishes_assignment_view() -> None:
    app_mod = _load_app_module()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "actions": [{"value": "2026-05-08 05:30:00"}],
        "view": {"blocks": [{}, {"text": {"text": "*The Bridge*"}}]},
    }
    context = {"user_id": "U_SELF", "team_id": "T1"}

    with patch.object(app_mod.event, "publish_open_slot_assignment_view") as publish_view:
        app_mod.handle_date_select_button(ack, client, body, logger, context)

    ack.assert_called_once()
    publish_view.assert_called_once_with(
        user_id="U_SELF",
        client=client,
        logger=logger,
        ao_display_name="The Bridge",
        selected_dt=app_mod.datetime(2026, 5, 8, 5, 30),
        initial_user_id="U_SELF",
    )


def test_handle_date_select_button_from_message_publishes_assignment_view() -> None:
    app_mod = _load_app_module()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "channel": {"id": "CAO1"},
        "actions": [{"value": "2026-05-08 05:30:00"}],
    }
    context = {"user_id": "U_SELF", "team_id": "T1"}

    with patch.object(
        app_mod.helper,
        "find_ao",
        return_value=type("AOObj", (), {"ao_display_name": "The Bridge"})(),
    ):
        with patch.object(app_mod.event, "publish_open_slot_assignment_view") as publish_view:
            app_mod.handle_date_select_button_from_message(ack, client, body, logger, context)

    ack.assert_called_once()
    publish_view.assert_called_once_with(
        user_id="U_SELF",
        client=client,
        logger=logger,
        ao_display_name="The Bridge",
        selected_dt=app_mod.datetime(2026, 5, 8, 5, 30),
        initial_user_id="U_SELF",
    )


if __name__ == "__main__":
    test_handle_date_select_button_publishes_assignment_view()
    test_handle_date_select_button_from_message_publishes_assignment_view()
    print("open slot assignment action tests OK")
