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
        "achievement_channel": "C12345678",
        "timezone": "America/Chicago",
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
        "trigger_id": "trig",
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
                with patch(
                    "slack_app._load_achievement",
                    return_value={"id": 7, "name": "Six Pack", "code": "six_pack"},
                ):
                    handle_delete_achievement(ack, body, client, logger)

    ack.assert_called_once_with()
    client.views_push.assert_called_once()
    assert client.views_push.call_args.kwargs["view"]["callback_id"] == "paxminer-achievement-delete-id"


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
        "timezone": "America/Chicago",
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
        "achievement_channel": "C12345678",
        "timezone": "America/Chicago",
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
    assert "confirm" not in delete_btn
    assert any(
        el.get("action_id") == "paxminer_achievement_backfill"
        for b in list_with["blocks"]
        if b.get("block_id") == "achievement_actions"
        for el in b["elements"]
    )
    edit = _achievement_edit_modal("T1", "f3tulsa_test", achievements[0])
    edit_ids = [b.get("block_id") for b in edit["blocks"] if b.get("block_id")]
    assert "enabled" in edit_ids
    assert "apply_mode" in edit_ids
    assert "range_mode" in edit_ids
    code_block = next(b for b in edit["blocks"] if b.get("block_id") == "code")
    assert code_block["type"] == "section"
    create = _achievement_edit_modal("T1", "f3tulsa_test", None)
    create_code = next(b for b in create["blocks"] if b.get("block_id") == "code")
    assert create_code["type"] == "input"
    assert not any(b.get("block_id") == "apply_mode" for b in create["blocks"])


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
        "achievement_channel": "C12345678",
        "timezone": "America/Chicago",
    }

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3tulsa_test", region)):
            handle_achievements_list_submit(ack, body, client, logger)

    ack.assert_called_once()
    kwargs = ack.call_args.kwargs
    assert kwargs["response_action"] == "update"
    assert kwargs["view"]["callback_id"] == "paxminer-config-id"


def test_cosmetic_edit_does_not_mint_version():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test","achievement_id":3}',
            "state": {"values": {}},
        },
    }
    existing = {
        "id": 3,
        "code": "six_pack",
        "name": "6 pack",
        "description": "d",
        "verb": "v",
        "metric": "posts",
        "activity": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
    }
    values = {
        "name": "Six Pack",
        "description": "d2",
        "verb": "v2",
        "code": "six_pack",
        "metric": "posts",
        "activity_list": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "range_mode": "going_forward",
        "apply_mode": "going_forward",
        "effective_from": None,
        "effective_to": None,
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app._parse_achievement_form", return_value=values):
                with patch("slack_app._validate_achievement", return_value={}):
                    with patch("slack_app.connect_from_env") as mock_conn:
                        mock_cur = MagicMock()
                        mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                        mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                        with patch("slack_app._load_achievement", return_value=existing):
                            with patch("slack_app._load_achievements", return_value=[]):
                                with patch("achievements.versions.supersede_and_insert") as mint:
                                    handle_achievement_edit_submit(ack, body, MagicMock(), MagicMock())
    mint.assert_not_called()
    update_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "SET name=%s, description=%s, verb=%s, enabled=%s" in update_sql


def test_parameter_edit_mints_version_and_can_queue_backfill():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = {
        "user": {"id": "UADMIN"},
        "view": {
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test","achievement_id":3}',
            "state": {"values": {}},
        },
    }
    existing = {
        "id": 3,
        "code": "six_pack",
        "name": "6 pack",
        "description": "d",
        "verb": "v",
        "metric": "posts",
        "activity": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "effective_from": "2026-03-01",
    }
    values = {
        "name": "6 pack",
        "description": "d",
        "verb": "v",
        "code": "six_pack",
        "metric": "posts",
        "activity_list": ["beatdown"],
        "period": "week",
        "threshold": 8,
        "enabled": 1,
        "range_mode": "going_forward",
        "apply_mode": "retroactive",
        "effective_from": None,
        "effective_to": None,
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app._parse_achievement_form", return_value=values):
                with patch("slack_app._validate_achievement", return_value={}):
                    with patch("slack_app.connect_from_env") as mock_conn:
                        mock_cur = MagicMock()
                        mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                        mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                        with patch("slack_app._load_achievement", return_value=existing):
                            with patch("slack_app._load_achievements", return_value=[]):
                                with patch(
                                    "slack_schedule.queue_achievement_backfill"
                                ) as queue:
                                    with patch(
                                        "achievements.versions.supersede_and_insert"
                                    ) as mint:
                                        handle_achievement_edit_submit(
                                            ack, body, MagicMock(), MagicMock()
                                        )
    mint.assert_called_once()
    assert mint.call_args.kwargs["effective_from"] == "2026-03-01"
    queue.assert_called_once()


def test_backfill_button_queues_worker():
    from slack_app import handle_backfill_achievement

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "trigger_id": "trig",
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
            "state": {
                "values": {
                    "achievement_pick": {
                        "paxminer_achievement_select": {"selected_option": {"value": "4"}}
                    }
                }
            },
        },
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", {"region": "t"}),
        ):
            with patch("slack_schedule.queue_achievement_backfill") as queue:
                with patch("slack_app._refresh_achievements_list"):
                    handle_backfill_achievement(ack, body, MagicMock(), MagicMock())
    ack.assert_called_once_with()
    queue.assert_called_once()
    assert queue.call_args.kwargs["achievement_id"] == 4


def test_delete_submit_disable_keeps_awards():
    from slack_app import handle_achievement_delete_submit

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test","achievement_id":3}',
            "state": {
                "values": {
                    "delete_action": {"val": {"selected_option": {"value": "disable"}}}
                }
            },
        },
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", {"region": "t", "schema_name": "f3test"}),
        ):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                mock_cur.fetchone.return_value = {"cnt": 12}
                with patch(
                    "slack_app._load_achievement",
                    return_value={"id": 3, "name": "Centurion"},
                ):
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app._post_achievement_admin_notice"):
                            handle_achievement_delete_submit(
                                ack, body, MagicMock(), MagicMock()
                            )
    sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "SET enabled=0" in sql
    assert "DELETE FROM" not in sql


def test_delete_submit_delete_all_clears_awards():
    from slack_app import handle_achievement_delete_submit

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test","achievement_id":3}',
            "state": {
                "values": {
                    "delete_action": {"val": {"selected_option": {"value": "delete"}}}
                }
            },
        },
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", {"region": "t"}),
        ):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                mock_cur.fetchone.return_value = {"cnt": 12}
                with patch(
                    "slack_app._load_achievement",
                    return_value={"id": 3, "name": "Centurion"},
                ):
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app._post_achievement_admin_notice"):
                            handle_achievement_delete_submit(
                                ack, body, MagicMock(), MagicMock()
                            )
    sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "DELETE FROM" in sql
    assert "achievements_awarded" in sql
    assert "achievement_versions" in sql
    assert "achievements_list" in sql


def test_slack_function_stays_pandas_free():
    """Interactive Lambda must not import pandas or the achievements engine."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    slack_files = (
        "slack_app.py",
        "config_paxminer.py",
        "config_schedule.py",
        "slack_schedule.py",
        "slack_http.py",
        "slack_blocks.py",
        "slack_util.py",
    )
    forbidden = {
        "pandas",
        "achievements.runner",
        "achievements.engine",
        "achievements.attendance",
        "achievements.leaderboard",
        "achievements.period",
    }
    for name in slack_files:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "pandas", name
                assert node.module not in forbidden, f"{name} imports {node.module}"
