"""Unit tests for the lightweight Slack Bolt front door."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")
os.environ.setdefault("PM_SLACK_TOKEN", "xoxb-test-token")
os.environ.setdefault("PM_SLACK_SIGNING_SECRET", "test-signing-secret-16")
os.environ.setdefault("STAGE", "test")


def test_handler_warm_path_skips_bolt():
    with patch("slack_app.SlackRequestHandler") as mock_handler_cls:
        from slack_app import handler

        resp = handler({}, None)
        assert resp == {"statusCode": 200, "body": "warm"}
        mock_handler_cls.assert_not_called()


def test_handler_http_dispatches_to_bolt():
    with patch("slack_app.SlackRequestHandler") as mock_handler_cls:
        mock_handler_cls.return_value.handle.return_value = {"statusCode": 200, "body": "ok"}
        from slack_app import handler

        event = {"requestContext": {"http": {"method": "POST"}}, "body": ""}
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        mock_handler_cls.assert_called_once()
        mock_handler_cls.return_value.handle.assert_called_once_with(event, None)


def test_config_command_admin_acks_empty_and_opens_modal():
    from slack_app import handle_config_command

    ack = MagicMock()
    client = MagicMock()
    respond = MagicMock()
    logger = MagicMock()
    region = {
        "region": "tulsa",
        "schema_name": "f3tulsa_test",
        "send_achievements": 1,
        "send_aoq_reports": 0,
        "send_achievement_leaderboard": 0,
        "send_pax_charts": 0,
        "send_q_charts": 0,
        "send_region_leaderboard": 0,
        "send_ao_leaderboard": 0,
        "achievement_channel": "C12345678",
        "kotter_channel": "",
        "firstf_channel": "",
    }
    body = {"user_id": "U1", "team_id": "T1", "trigger_id": "trig"}

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app.connect_from_env") as mock_conn:
            mock_cur = MagicMock()
            mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
            mock_conn.return_value.cursor.return_value.__exit__.return_value = False
            with patch("slack_app._region_for_team", return_value=region):
                handle_config_command(ack, body, client, logger, respond)

    ack.assert_called_once_with()
    client.views_open.assert_called_once()
    assert client.views_open.call_args.kwargs["trigger_id"] == "trig"
    assert "view" in client.views_open.call_args.kwargs


def test_config_command_non_admin_acks_ephemeral_once():
    from slack_app import handle_config_command

    ack = MagicMock()
    client = MagicMock()
    respond = MagicMock()
    logger = MagicMock()
    body = {"user_id": "U1", "team_id": "T1", "trigger_id": "trig"}

    with patch("slack_app.is_slack_admin", return_value=False):
        handle_config_command(ack, body, client, logger, respond)

    assert ack.call_count == 1
    assert "admin" in ack.call_args.kwargs.get("text", "").lower()
    client.views_open.assert_not_called()


def test_delete_achievement_updates_view():
    from slack_app import handle_delete_achievement

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}',
            "state": {
                "values": {
                    "achievement_pick": {
                        "paxminer_achievement_select": {
                            "selected_option": {"value": "7"}
                        }
                    }
                }
            },
        },
    }
    region = {"region": "tulsa", "schema_name": "f3tulsa_test"}

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3tulsa_test", region)):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                mock_cur.fetchone.return_value = {"cnt": 0}
                with patch("slack_app._load_achievements", return_value=[]):
                    handle_delete_achievement(ack, body, client, logger)

    ack.assert_called_once_with()
    client.views_update.assert_called_once()
    assert client.views_update.call_args.kwargs["view_id"] == "V1"


def test_config_submit_clear_on_success():
    from slack_app import handle_config_submit

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}',
            "state": {"values": {}},
        },
    }
    region = {"region": "tulsa", "schema_name": "f3tulsa_test"}
    values = {
        "send_achievements": 1,
        "send_aoq_reports": 0,
        "send_achievement_leaderboard": 0,
        "achievement_channel": "C1",
        "kotter_channel": "",
        "firstf_channel": "",
        "send_pax_charts": 0,
        "send_q_charts": 0,
        "send_region_leaderboard": 0,
        "send_ao_leaderboard": 0,
        "NO_POST_THRESHOLD": 2,
        "REMINDER_WEEKS": 2,
        "HOME_AO_CAPTURE": 8,
        "NO_Q_THRESHOLD_WEEKS": 4,
        "NO_Q_THRESHOLD_POSTS": 4,
    }

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3tulsa_test", region)):
            with patch("slack_app._parse_modal_values", return_value=values):
                with patch("slack_app.connect_from_env") as mock_conn:
                    mock_cur = MagicMock()
                    mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                    mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                    handle_config_submit(ack, body, client, logger)

    ack.assert_called_once_with(response_action="clear")


def _assert_modals_with_inputs_have_submit(views: list[dict]) -> None:
    for view in views:
        has_input = any(b.get("type") == "input" for b in view.get("blocks") or [])
        if has_input:
            assert "submit" in view, f"modal {view.get('callback_id')} has input blocks but no submit"


def test_modals_with_input_blocks_include_submit():
    """Regression: Slack rejects input-block modals without submit (achievements list 500)."""
    from config_paxminer import (
        _achievement_edit_modal,
        _achievements_list_modal,
        _config_modal,
    )

    region = {
        "region": "tulsa",
        "schema_name": "f3tulsa_test",
        "team_id": "T1",
        "send_achievements": 1,
        "send_aoq_reports": 0,
        "send_achievement_leaderboard": 0,
        "send_pax_charts": 0,
        "send_q_charts": 0,
        "send_region_leaderboard": 0,
        "send_ao_leaderboard": 0,
    }
    achievements = [
        {
            "id": 1,
            "name": "The Six Pack",
            "code": "six_pack",
            "metric": "posts",
            "activity": "beatdown",
            "period": "week",
            "threshold": 6,
        }
    ]
    views = [
        _config_modal(region),
        _achievements_list_modal("T1", "f3tulsa_test", []),
        _achievements_list_modal("T1", "f3tulsa_test", achievements),
        _achievement_edit_modal("T1", "f3tulsa_test", None),
        _achievement_edit_modal("T1", "f3tulsa_test", achievements[0]),
    ]
    _assert_modals_with_inputs_have_submit(views)
    list_with = _achievements_list_modal("T1", "f3tulsa_test", achievements)
    assert list_with["submit"]["text"] == "Done"
    delete_btn = next(
        el
        for b in list_with["blocks"]
        if b.get("block_id") == "achievement_actions"
        for el in b["elements"]
        if el.get("action_id") == "paxminer_achievement_delete"
    )
    assert "confirm" in delete_btn


def test_edit_achievement_no_selection_updates_view_with_notice():
    from slack_app import handle_edit_achievement

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}',
            "state": {"values": {}},
        },
    }
    region = {"region": "tulsa", "schema_name": "f3tulsa_test"}

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3tulsa_test", region)):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievements", return_value=[]):
                    handle_edit_achievement(ack, body, client, logger)

    ack.assert_called_once_with()
    client.views_update.assert_called_once()
    view = client.views_update.call_args.kwargs["view"]
    notice = view["blocks"][0]
    assert notice["type"] == "context"
    assert "Select an achievement" in notice["elements"][0]["text"]


def test_delete_achievement_no_selection_updates_view_with_notice():
    from slack_app import handle_delete_achievement

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}',
            "state": {"values": {}},
        },
    }
    region = {"region": "tulsa", "schema_name": "f3tulsa_test"}

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3tulsa_test", region)):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievements", return_value=[]):
                    handle_delete_achievement(ack, body, client, logger)

    ack.assert_called_once_with()
    client.views_update.assert_called_once()
    view = client.views_update.call_args.kwargs["view"]
    assert view["blocks"][0]["type"] == "context"
    assert "Select an achievement" in view["blocks"][0]["elements"][0]["text"]


def test_achievements_list_submit_updates_to_config_modal():
    from slack_app import handle_achievements_list_submit

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {"private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}'},
    }
    region = {
        "region": "tulsa",
        "schema_name": "f3tulsa_test",
        "send_achievements": 1,
        "send_aoq_reports": 0,
        "send_achievement_leaderboard": 0,
        "send_pax_charts": 0,
        "send_q_charts": 0,
        "send_region_leaderboard": 0,
        "send_ao_leaderboard": 0,
    }

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3tulsa_test", region)):
            handle_achievements_list_submit(ack, body, client, logger)

    ack.assert_called_once()
    kwargs = ack.call_args.kwargs
    assert kwargs["response_action"] == "update"
    assert kwargs["view"]["callback_id"] == "paxminer-config-id"
