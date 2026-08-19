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
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app._post_achievement_admin_notice"):
                            handle_delete_achievement(ack, body, client, logger)

    ack.assert_called_once_with()
    client.views_push.assert_not_called()
    client.views_update.assert_called_once()
    assert client.views_update.call_args.kwargs["view"]["callback_id"] == "paxminer-achievements-list-id"
    sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "DELETE FROM" in sql
    assert "achievements_awarded" in sql


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
    assert "submit" not in list_with
    accessory_ids = [
        (b.get("accessory") or {}).get("action_id")
        for b in list_with["blocks"]
        if b.get("type") == "section"
    ]
    assert "paxminer_achievement_edit" in accessory_ids
    sublines = [
        el.get("text")
        for b in list_with["blocks"]
        if b.get("type") == "context"
        for el in b.get("elements") or []
    ]
    assert any("Week - 6 posts" in (t or "") for t in sublines)
    assert any(
        el.get("action_id") == "paxminer_achievement_backfill"
        for b in list_with["blocks"]
        if b.get("type") == "actions"
        for el in b.get("elements") or []
    )
    assert any(
        el.get("action_id") == "paxminer_achievements_restore_defaults"
        for b in list_with["blocks"]
        if b.get("type") == "actions"
        for el in b.get("elements") or []
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
                    "reeval_pick": {
                        "paxminer_achievement_select": {"selected_option": {"value": "4"}}
                    },
                    "reeval_dates": {
                        "paxminer_achievement_reeval_from": {"selected_date": "2026-01-01"},
                        "paxminer_achievement_reeval_to": {"selected_date": "2026-08-18"},
                    },
                }
            },
        },
        "actions": [{"action_id": "paxminer_achievement_backfill", "value": "4"}],
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
    assert queue.call_args.kwargs["start"] == "2026-01-01"
    assert queue.call_args.kwargs["end"] == "2026-08-18"


def test_delete_achievement_posts_admin_notice():
    from slack_app import handle_delete_achievement

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
            "state": {
                "values": {
                    "achievement_pick": {
                        "paxminer_achievement_select": {
                            "selected_option": {"value": "3"}
                        }
                    }
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
                    return_value={"id": 3, "name": "Centurion", "code": "centurion"},
                ):
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app._post_achievement_admin_notice") as notice:
                            handle_delete_achievement(ack, body, client, logger)
    sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "DELETE FROM" in sql
    assert "achievements_awarded" in sql
    assert "achievement_versions" in sql
    assert "achievements_list" in sql
    notice.assert_called_once()
    client.views_push.assert_not_called()
    client.views_update.assert_called_once()


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


def test_operator_error_notice_includes_reason_without_log_channel():
    from slack_app import operator_error_notice
    from slack_sdk.errors import SlackApiError

    missing = operator_error_notice(ModuleNotFoundError("No module named 'achievements'"))
    assert "achievements" in missing
    assert "paxminer_logs" not in missing.lower()

    api = operator_error_notice(SlackApiError("fail", {"error": "invalid_blocks"}))
    assert "invalid_blocks" in api
    assert "paxminer_logs" not in api.lower()

    body = {
        "actions": [
            {
                "action_id": "paxminer_report_add",
                "text": {"type": "plain_text", "text": "Add custom report"},
            }
        ],
        "view": {"callback_id": "paxminer-reports-list-id"},
    }
    named = operator_error_notice(
        SlackApiError("fail", {"error": "push_limit_reached"}), body
    )
    assert named == "Something went wrong: push_limit_reached (Add custom report)"


def test_handle_error_dms_reason_not_log_channel():
    from slack_app import handle_error

    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    logger = MagicMock()
    body = {"user": {"id": "U1"}}
    handle_error(ModuleNotFoundError("No module named 'achievements'"), body, logger, client)

    client.chat_postMessage.assert_called_once()
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "achievements" in text
    assert "paxminer_logs" not in text.lower()


def test_slack_dockerfile_copies_imported_achievement_modules():
    """SlackFunction must ship pandas-free achievement helpers it imports at runtime."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile.slack").read_text(encoding="utf-8")
    copied: set[str] = set()
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        src = stripped.split()[1]
        if not src.startswith("PAXminer/"):
            continue
        rel = src[len("PAXminer/") :]
        abs_src = root / rel
        if rel.endswith("/") or abs_src.is_dir():
            for path in abs_src.rglob("*"):
                if path.is_file():
                    copied.add(str(path.relative_to(root)))
        else:
            copied.add(rel)

    forbidden_files = {
        "achievements/runner.py",
        "achievements/engine.py",
        "achievements/attendance.py",
        "achievements/leaderboard.py",
        "achievements/announcements.py",
    }
    assert copied.isdisjoint(forbidden_files)

    slack_py = [
        rel
        for rel in copied
        if rel.endswith(".py") and not rel.startswith("achievements/")
    ]
    needed: set[str] = set()
    to_scan = list(slack_py)
    seen: set[str] = set()
    while to_scan:
        rel = to_scan.pop()
        if rel in seen:
            continue
        seen.add(rel)
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module != "achievements" and not node.module.startswith("achievements."):
                continue
            modules = [node.module]
            if node.module == "achievements":
                modules.extend(f"achievements.{alias.name}" for alias in node.names)
            for mod in modules:
                file_rel = mod.replace(".", "/") + ".py"
                needed.add(file_rel)
                needed.add("achievements/__init__.py")
                if file_rel not in seen:
                    to_scan.append(file_rel)

    missing = sorted(path for path in needed if path not in copied)
    assert missing == [], f"Dockerfile.slack missing {missing}"

    # Data files opened by copied modules (JSON catalogs) must also be COPY'd.
    import re

    data_needed: set[str] = set()
    for rel in list(seen):
        path = root / rel
        if not path.is_file() or not rel.endswith(".py"):
            continue
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r'["\']([A-Za-z0-9_\-]+\.json)["\']', text):
            if (root / name).is_file():
                data_needed.add(name)
            else:
                candidate = path.parent / name
                if candidate.is_file():
                    data_needed.add(str(candidate.relative_to(root)))
    missing_data = sorted(p for p in data_needed if p not in copied)
    assert missing_data == [], f"Dockerfile.slack missing data files {missing_data}"


def test_copied_achievement_modules_stay_pandas_free():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    forbidden = {"pandas", "numpy", "matplotlib"}
    for name in ("achievements/activity.py", "achievements/versions.py", "achievements/__init__.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, name
                assert not node.module.startswith("achievements.engine")
                assert not node.module.startswith("achievements.runner")
                assert not node.module.startswith("achievements.attendance")
                assert not node.module.startswith("achievements.leaderboard")

def test_add_and_edit_achievement_update_view_instead_of_push():
    from slack_app import handle_add_achievement, handle_edit_achievement

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    region = {"region": "tulsa", "schema_name": "f3tulsa_test"}
    add_body = {
        "user": {"id": "U1"},
        "view": {"id": "V1", "private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}'},
    }
    edit_body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3tulsa_test"}',
            "state": {
                "values": {
                    "achievement_pick": {
                        "paxminer_achievement_select": {"selected_option": {"value": "7"}}
                    }
                }
            },
        },
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3tulsa_test", region),
        ):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_activity_options", return_value=["beatdown"]):
                    handle_add_achievement(ack, add_body, client, logger)
                    with patch(
                        "slack_app._load_achievement",
                        return_value={"id": 7, "name": "Six Pack", "code": "six_pack"},
                    ):
                        handle_edit_achievement(ack, edit_body, client, logger)

    assert client.views_update.call_count == 2
    client.views_push.assert_not_called()
    callbacks = [c.kwargs["view"]["callback_id"] for c in client.views_update.call_args_list]
    assert callbacks == ["paxminer-achievement-edit-id", "paxminer-achievement-edit-id"]

