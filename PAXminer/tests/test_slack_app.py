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
    text = ack.call_args.kwargs.get("text", "")
    assert "admin" in text.lower()
    assert "PAXMiner settings" in text
    client.views_open.assert_not_called()


def test_home_view_settings_button_is_admin_only():
    from slack_app import HOME_OPEN_SETTINGS_ACTION_ID, _home_view

    guest = _home_view(admin=False)
    assert guest["type"] == "home"
    assert all(b.get("block_id") != "home_settings" for b in guest["blocks"])
    assert HOME_OPEN_SETTINGS_ACTION_ID not in str(guest)

    admin = _home_view(admin=True)
    actions = next(b for b in admin["blocks"] if b.get("block_id") == "home_settings")
    assert actions["elements"][0]["action_id"] == HOME_OPEN_SETTINGS_ACTION_ID


def test_home_open_settings_opens_hub_for_admin():
    from slack_app import handle_home_open_settings

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "trigger_id": "trig"}
    region = {"region": "tulsa", "schema_name": "f3tulsa_test"}

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app.connect_from_env") as mock_conn:
            mock_cur = MagicMock()
            mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
            mock_conn.return_value.cursor.return_value.__exit__.return_value = False
            with patch("slack_app._region_for_team", return_value=region):
                handle_home_open_settings(ack, body, client, logger)

    ack.assert_called_once_with()
    client.views_open.assert_called_once()


def test_home_open_settings_rejects_non_admin():
    from slack_app import handle_home_open_settings

    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "trigger_id": "trig"}

    with patch("slack_app.is_slack_admin", return_value=False):
        with patch("slack_app.notify_admin_required") as notice:
            handle_home_open_settings(ack, body, client, logger)

    ack.assert_called_once_with()
    notice.assert_called_once()
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
                    with patch("schedule_schema.ensure_log_channel_column", return_value=False):
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
            "enabled": 1,
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
    assert "paxminer_achievement_more" in accessory_ids
    overflow = next(
        b
        for b in list_with["blocks"]
        if (b.get("accessory") or {}).get("action_id") == "paxminer_achievement_more"
    )
    assert "Week - 6 posts" in overflow["text"]["text"]
    idx = list_with["blocks"].index(overflow)
    following = list_with["blocks"][idx + 1] if idx + 1 < len(list_with["blocks"]) else {}
    assert following.get("type") != "context"
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
    assert "enabled" not in edit_ids
    assert "apply_mode" not in edit_ids
    assert "no_end_date" not in edit_ids
    assert "range_mode" in edit_ids
    assert "emoji" in edit_ids
    range_block = next(b for b in edit["blocks"] if b.get("block_id") == "range_mode")
    range_labels = [o["text"]["text"] for o in range_block["element"]["options"]]
    assert "From when the achievement was created" in range_labels
    assert "Since the earning rules last changed" in range_labels
    assert "All attendance dates" in range_labels
    assert "Custom" in range_labels
    start_el = next(b for b in edit["blocks"] if b.get("block_id") == "effective_from")["element"]
    end_el = next(b for b in edit["blocks"] if b.get("block_id") == "effective_to")["element"]
    assert "initial_date" not in start_el
    assert "initial_date" not in end_el
    custom_row = {
        **achievements[0],
        "range_mode": "custom",
        "effective_from": "2026-02-01",
        "effective_to": "2026-12-31",
    }
    custom = _achievement_edit_modal("T1", "f3tulsa_test", custom_row)
    custom_start = next(b for b in custom["blocks"] if b.get("block_id") == "effective_from")["element"]
    custom_end = next(b for b in custom["blocks"] if b.get("block_id") == "effective_to")["element"]
    assert custom_start.get("initial_date") == "2026-02-01"
    assert custom_end.get("initial_date") == "2026-12-31"
    code_block = next(b for b in edit["blocks"] if b.get("block_id") == "code")
    assert code_block["type"] == "section"
    create = _achievement_edit_modal("T1", "f3tulsa_test", None)
    create_code = next(b for b in create["blocks"] if b.get("block_id") == "code")
    assert create_code["type"] == "input"
    create_range = next(b for b in create["blocks"] if b.get("block_id") == "range_mode")
    assert create_range["element"]["initial_option"]["value"] == "from_created"
    dup = _achievement_edit_modal(
        "T1",
        "f3tulsa_test",
        {
            "name": "The Priest",
            "code": "the_priest_copy",
            "metric": "posts",
            "activity": ["QSource"],
            "period": "year",
            "threshold": 25,
            "range_mode": "all_attendance",
            "effective_from": None,
            "effective_to": None,
        },
        prefill_from_source=True,
    )
    dup_code = next(b for b in dup["blocks"] if b.get("block_id") == "code")
    assert dup_code["type"] == "input"
    dup_range = next(b for b in dup["blocks"] if b.get("block_id") == "range_mode")
    assert dup_range["element"]["initial_option"]["value"] == "all_attendance"
    dup_custom = _achievement_edit_modal(
        "T1",
        "f3tulsa_test",
        {
            "name": "Custom Copy",
            "code": "custom_copy",
            "metric": "posts",
            "activity": "any",
            "period": "year",
            "threshold": 10,
            "range_mode": "custom",
            "effective_from": "2026-02-01",
            "effective_to": "2026-12-31",
        },
        prefill_from_source=True,
    )
    dup_start = next(b for b in dup_custom["blocks"] if b.get("block_id") == "effective_from")[
        "element"
    ]
    dup_end = next(b for b in dup_custom["blocks"] if b.get("block_id") == "effective_to")["element"]
    assert next(b for b in dup_custom["blocks"] if b.get("block_id") == "range_mode")["element"][
        "initial_option"
    ]["value"] == "custom"
    assert dup_start.get("initial_date") == "2026-02-01"
    assert dup_end.get("initial_date") == "2026-12-31"
    assert not any(b.get("block_id") == "apply_mode" for b in create["blocks"])
    overflows = [
        b.get("accessory") or {}
        for b in list_with["blocks"]
        if b.get("type") == "section" and (b.get("accessory") or {}).get("type") == "overflow"
    ]
    assert overflows
    assert overflows[0]["action_id"] == "paxminer_achievement_more"
    assert [o["text"]["text"] for o in overflows[0]["options"]] == [
        "Edit",
        "Duplicate",
        "Disable",
        "Delete",
    ]


def test_achievement_delete_confirm_names_pax_and_pluralizes():
    from config_paxminer import achievement_delete_confirm_text, _achievement_edit_modal
    from slack_blocks import confirm_dialog, counted_noun, delete_confirm_modal

    assert counted_noun(1, "award") == "1 award"
    assert counted_noun(27, "award") == "27 awards"
    assert counted_noun(1, "PAX", "PAX") == "1 PAX"
    assert counted_noun(12, "PAX", "PAX") == "12 PAX"

    many = achievement_delete_confirm_text("six_pack", 27, 12)
    assert "`six_pack`" in many
    assert "27 awards from 12 PAX" in many
    assert "award(s)" not in many
    assert "simply disable this achievement" in many
    one = achievement_delete_confirm_text("six_pack", 1, 1)
    assert "1 award from 1 PAX" in one
    none = achievement_delete_confirm_text("six_pack", 0, 0)
    assert "0 awards from 0 PAX" in none

    dialog = confirm_dialog("Delete achievement?", many)
    assert dialog["style"] == "danger"
    assert dialog["confirm"]["text"] == "Delete"
    restore = confirm_dialog("Restore defaults?", "Adds missing builtins.", "Restore")
    assert "style" not in restore

    row = {
        "id": 1,
        "name": "The Six Pack",
        "code": "six_pack",
        "metric": "posts",
        "activity": "beatdown",
        "period": "week",
        "threshold": 6,
        "award_count": 27,
        "pax_count": 12,
    }
    edit = _achievement_edit_modal("T1", "f3tulsa_test", row)
    extra_ids = [
        el.get("action_id")
        for b in edit["blocks"]
        if b.get("type") == "actions"
        for el in b.get("elements") or []
    ]
    assert "paxminer_achievement_delete" not in extra_ids
    assert "paxminer_achievement_duplicate" not in extra_ids
    modal = delete_confirm_modal(
        callback_id="paxminer-achievement-delete-confirm-id",
        title="Delete achievement?",
        warning=many,
        metadata="{}",
    )
    assert modal["submit"]["text"] == "Delete"
    assert "27 awards from 12 PAX" in modal["blocks"][0]["text"]["text"]


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
        "range_mode": "from_created",
        "effective_from": "2026-01-15",
        "effective_to": None,
        "first_created": "2026-01-15",
        "version_created": "2026-01-15",
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
        "range_mode": "from_created",
        "no_end_date": True,
        "effective_from": "2026-01-15",
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
                                with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                    with patch("achievements.range.ensure_achievement_range_columns"):
                                        with patch("achievements.versions.update_current_range"):
                                            with patch("achievements.versions.supersede_and_insert") as mint:
                                                handle_achievement_edit_submit(ack, body, MagicMock(), MagicMock())
    mint.assert_not_called()
    update_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "SET name=%s, description=%s, verb=%s, emoji=%s" in update_sql
    assert "enabled=%s" not in update_sql


def _achievement_edit_submit_body(
    *,
    include_activity: bool,
    selected_options=None,
    name="The Priest",
    include_exclude: bool = False,
    exclude_options=None,
):
    values = {
        "name": {"val": {"value": name}},
        "description": {"val": {"value": "d"}},
        "verb": {"val": {"value": "v"}},
        "metric": {"val": {"selected_option": {"value": "posts"}}},
        "period": {"val": {"selected_option": {"value": "year"}}},
        "threshold": {"val": {"value": "25"}},
        "range_mode": {"val": {"selected_option": {"value": "from_created"}}},
        "effective_from": {"val": {"selected_date": "2026-01-15"}},
    }
    if include_activity:
        values["activity"] = {
            "val": {"selected_options": selected_options if selected_options is not None else []}
        }
    if include_exclude:
        values["activity_exclude"] = {
            "val": {"selected_options": exclude_options if exclude_options is not None else []}
        }
    return {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test","achievement_id":3}',
            "state": {"values": values},
        },
    }


def _qsource_existing_row():
    return {
        "id": 3,
        "code": "the_priest",
        "name": "The Priest",
        "description": "d",
        "verb": "v",
        "metric": "posts",
        "activity": ["QSource"],
        "period": "year",
        "threshold": 25,
        "enabled": 1,
        "range_mode": "from_created",
        "effective_from": "2026-01-15",
        "effective_to": None,
        "first_created": "2026-01-15",
        "version_created": "2026-01-15",
    }


def test_name_only_save_without_activity_picker_preserves_filter():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = _achievement_edit_submit_body(include_activity=False, name="Priest")
    existing = _qsource_existing_row()
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app._validate_achievement", return_value={}):
                with patch("slack_app.connect_from_env") as mock_conn:
                    mock_cur = MagicMock()
                    mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                    mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                    with patch("slack_app._load_achievement", return_value=existing):
                        with patch("slack_app._load_achievements", return_value=[]):
                            with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                with patch("achievements.range.ensure_achievement_range_columns"):
                                    with patch("achievements.versions.update_current_range"):
                                        with patch("achievements.versions.supersede_and_insert") as mint:
                                            with patch(
                                                "slack_schedule.queue_achievement_backfill"
                                            ) as queue:
                                                handle_achievement_edit_submit(
                                                    ack, body, MagicMock(), MagicMock()
                                                )
    mint.assert_not_called()
    queue.assert_not_called()


def test_clearing_activity_chips_stores_any_and_queues_reeval():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = _achievement_edit_submit_body(include_activity=True, selected_options=[])
    existing = _qsource_existing_row()
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app._validate_achievement", return_value={}):
                with patch("slack_app.connect_from_env") as mock_conn:
                    mock_cur = MagicMock()
                    mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                    mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                    with patch("slack_app._load_achievement", return_value=existing):
                        with patch("slack_app._load_achievements", return_value=[]):
                            with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                with patch("achievements.range.ensure_achievement_range_columns"):
                                    with patch(
                                        "achievements.range.try_acquire_reeval_lock",
                                        return_value=(True, None),
                                    ):
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
    assert mint.call_args.kwargs["activity_filter"] == {"include": [], "exclude": []}
    queue.assert_called_once()
    assert queue.call_args.kwargs["action"] == "changed"


def test_overlapping_include_exclude_acks_form_error():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = _achievement_edit_submit_body(
        include_activity=True,
        selected_options=[{"value": "Bootcamp"}, {"value": "Rucking"}],
        include_exclude=True,
        exclude_options=[{"value": "Rucking"}],
    )
    existing = _qsource_existing_row()
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievement", return_value=existing):
                    with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                        with patch("achievements.range.ensure_achievement_range_columns"):
                            with patch("achievements.versions.supersede_and_insert") as mint:
                                handle_achievement_edit_submit(
                                    ack, body, MagicMock(), MagicMock()
                                )
    mint.assert_not_called()
    ack.assert_called_once()
    assert ack.call_args.kwargs["response_action"] == "errors"
    assert "Rucking" in ack.call_args.kwargs["errors"]["activity_exclude"]


def test_stored_overlap_without_pickers_saves():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = _achievement_edit_submit_body(include_activity=False, name="Priest")
    existing = _qsource_existing_row()
    existing["activity"] = {"include": ["Bootcamp"], "exclude": ["Bootcamp"]}
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievement", return_value=existing):
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                            with patch("achievements.range.ensure_achievement_range_columns"):
                                with patch("achievements.versions.update_current_range"):
                                    with patch("achievements.versions.supersede_and_insert") as mint:
                                        with patch("slack_schedule.queue_achievement_backfill"):
                                            with patch("slack_app._refresh_achievements_list"):
                                                handle_achievement_edit_submit(
                                                    ack, body, MagicMock(), MagicMock()
                                                )
    mint.assert_not_called()
    assert ack.call_args.kwargs.get("response_action") != "errors"


def test_name_only_save_keeps_catalog_dropped_exclude():
    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    body = _achievement_edit_submit_body(
        include_activity=True,
        selected_options=[],
        include_exclude=True,
        exclude_options=[{"value": "RetiredRuck"}],
        name="Renamed",
    )
    existing = _qsource_existing_row()
    existing["activity"] = {"include": [], "exclude": ["RetiredRuck"]}
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievement", return_value=existing):
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                            with patch("achievements.range.ensure_achievement_range_columns"):
                                with patch("achievements.versions.update_current_range"):
                                    with patch("achievements.versions.supersede_and_insert") as mint:
                                        with patch("slack_schedule.queue_achievement_backfill") as queue:
                                            with patch("slack_app._refresh_achievements_list"):
                                                handle_achievement_edit_submit(
                                                    ack, body, MagicMock(), MagicMock()
                                                )
    mint.assert_not_called()
    queue.assert_not_called()
    assert ack.call_args.kwargs.get("response_action") != "errors"


def test_reenable_does_not_mint_version_or_force_today():
    from slack_app import _toggle_achievement_enabled

    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "VLIST",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
        },
    }
    row = {
        "id": 3,
        "code": "six_pack",
        "enabled": 1,
        "name": "6 pack",
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievement", return_value=row):
                    with patch("slack_app._refresh_achievements_list") as refresh:
                        with patch("achievements.versions.supersede_and_insert") as mint:
                            _toggle_achievement_enabled(body, MagicMock(), MagicMock(), 3)
    mint.assert_not_called()
    sql = mock_cur.execute.call_args.args[0]
    assert "SET enabled = 1 - COALESCE(enabled, 0)" in sql
    refresh.assert_called_once()


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
        "range_mode": "from_created",
        "effective_from": "2026-03-01",
        "effective_to": None,
        "first_created": "2026-03-01",
        "version_created": "2026-03-01",
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
        "range_mode": "from_created",
        "no_end_date": True,
        "effective_from": "2026-03-01",
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
                                with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                    with patch("achievements.range.ensure_achievement_range_columns"):
                                        with patch(
                                            "achievements.range.try_acquire_reeval_lock",
                                            return_value=(True, None),
                                        ):
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
    assert mint.call_args.kwargs["range_mode"] == "from_created"
    queue.assert_called_once()
    assert queue.call_args.kwargs["automatic"] is True
    assert queue.call_args.kwargs["action"] == "changed"


def test_range_only_all_attendance_updates_current_and_queues():
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
        "range_mode": "from_created",
        "effective_from": "2026-03-01",
        "effective_to": None,
        "first_created": "2026-03-01",
        "version_created": "2026-03-01",
    }
    values = {
        "name": "6 pack",
        "description": "d",
        "verb": "v",
        "code": "six_pack",
        "metric": "posts",
        "activity_list": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "range_mode": "all_attendance",
        "no_end_date": True,
        "effective_from": "2025-01-01",
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
                                with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                    with patch("achievements.range.ensure_achievement_range_columns"):
                                        with patch(
                                            "achievements.range.try_acquire_reeval_lock",
                                            return_value=(True, None),
                                        ):
                                            with patch(
                                                "slack_schedule.queue_achievement_backfill"
                                            ) as queue:
                                                with patch(
                                                    "achievements.versions.supersede_and_insert"
                                                ) as mint:
                                                    with patch(
                                                        "achievements.versions.update_current_range"
                                                    ) as upd:
                                                        handle_achievement_edit_submit(
                                                            ack, body, MagicMock(), MagicMock()
                                                        )
    mint.assert_not_called()
    upd.assert_called_once()
    assert upd.call_args.kwargs["effective_from"] is None
    assert upd.call_args.kwargs["range_mode"] == "all_attendance"
    queue.assert_called_once()
    assert queue.call_args.kwargs["automatic"] is True
    assert queue.call_args.kwargs["action"] == "changed"


def test_description_only_does_not_move_since_rules_changed():
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
        "description": "old",
        "verb": "v",
        "metric": "posts",
        "activity": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "range_mode": "since_rules_changed",
        "effective_from": "2026-04-01",
        "effective_to": None,
        "first_created": "2026-01-01",
        "version_created": "2026-04-01",
    }
    values = {
        "name": "6 pack",
        "description": "typo fix",
        "verb": "v",
        "code": "six_pack",
        "metric": "posts",
        "activity_list": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "range_mode": "since_rules_changed",
        "no_end_date": True,
        "effective_from": "2026-04-01",
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
                                with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                    with patch("achievements.range.ensure_achievement_range_columns"):
                                        with patch("achievements.versions.update_current_range") as upd:
                                            with patch("achievements.versions.supersede_and_insert") as mint:
                                                with patch(
                                                    "slack_schedule.queue_achievement_backfill"
                                                ) as queue:
                                                    handle_achievement_edit_submit(
                                                        ack, body, MagicMock(), MagicMock()
                                                    )
    mint.assert_not_called()
    queue.assert_not_called()
    assert upd.call_args.kwargs["effective_from"] == "2026-04-01"


def test_narrowing_range_pushes_confirm_modal():
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
        "range_mode": "all_attendance",
        "effective_from": None,
        "effective_to": None,
        "first_created": "2026-01-01",
        "version_created": "2026-01-01",
    }
    values = {
        "name": "6 pack",
        "description": "d",
        "verb": "v",
        "code": "six_pack",
        "metric": "posts",
        "activity_list": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "range_mode": "from_created",
        "no_end_date": True,
        "effective_from": "2026-01-01",
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
                            with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                                with patch("achievements.range.ensure_achievement_range_columns"):
                                    with patch(
                                        "achievements.range.count_awards_outside_range",
                                        return_value=(5, 2),
                                    ):
                                        with patch("achievements.versions.supersede_and_insert") as mint:
                                            handle_achievement_edit_submit(
                                                ack, body, MagicMock(), MagicMock()
                                            )
    mint.assert_not_called()
    assert ack.call_args.kwargs["response_action"] == "push"
    assert ack.call_args.kwargs["view"]["callback_id"] == "paxminer-achievement-range-confirm-id"


def test_range_confirm_second_submit_saves_without_reprompt():
    import json

    from slack_app import handle_achievement_edit_submit

    ack = MagicMock()
    pending = {
        "name": "6 pack",
        "description": "d",
        "verb": "v",
        "code": "six_pack",
        "metric": "posts",
        "activity_list": ["beatdown"],
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "range_mode": "from_created",
        "no_end_date": True,
        "effective_from": "2026-01-01",
        "effective_to": None,
    }
    body = {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": json.dumps(
                {
                    "team_id": "T1",
                    "regional_schema": "f3test",
                    "achievement_id": 3,
                    "pending_values": pending,
                    "range_confirmed": True,
                }
            ),
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
        "range_mode": "all_attendance",
        "effective_from": None,
        "effective_to": None,
        "first_created": "2026-01-01",
        "version_created": "2026-01-01",
    }
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch("slack_app._region_context_from_body", return_value=("T1", "f3test", {"region": "t"})):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievement", return_value=existing):
                    with patch("slack_app._load_achievements", return_value=[]):
                        with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                            with patch("achievements.range.ensure_achievement_range_columns"):
                                with patch(
                                    "achievements.range.count_awards_outside_range",
                                    return_value=(5, 2),
                                ) as count:
                                    with patch(
                                        "achievements.range.try_acquire_reeval_lock",
                                        return_value=(True, None),
                                    ):
                                        with patch(
                                            "slack_schedule.queue_achievement_backfill"
                                        ):
                                            with patch(
                                                "achievements.versions.supersede_and_insert"
                                            ) as mint:
                                                with patch(
                                                    "achievements.versions.update_current_range"
                                                ) as upd:
                                                    handle_achievement_edit_submit(
                                                        ack, body, MagicMock(), MagicMock()
                                                    )
    count.assert_not_called()
    mint.assert_not_called()
    upd.assert_called_once()
    assert ack.call_args.kwargs.get("response_action") != "push"
    assert ack.call_args.kwargs.get("response_action") == "update"


def test_custom_missing_start_errors():
    from config_paxminer import _validate_achievement

    errors = _validate_achievement(
        {
            "name": "Six Pack",
            "code": "six_pack",
            "metric": "posts",
            "period": "week",
            "threshold": 6,
            "range_mode": "custom",
            "effective_from": None,
            "effective_to": None,
        }
    )
    assert "effective_from" in errors


def test_non_custom_leftover_picker_dates_are_ignored():
    from config_paxminer import _validate_achievement

    errors = _validate_achievement(
        {
            "name": "Six Pack",
            "code": "six_pack",
            "metric": "posts",
            "period": "week",
            "threshold": 6,
            "range_mode": "from_created",
            "effective_from": "2026-06-01",
            "effective_to": "2026-07-01",
        },
        first_created="2026-01-01",
        version_created="2026-01-01",
        earliest_beatdown="2025-01-01",
    )
    assert errors == {}


def test_empty_end_date_stores_null_to():
    from achievements.range import resolve_stored_range

    mode, from_date, to_date = resolve_stored_range(
        {
            "range_mode": "custom",
            "effective_from": "2026-02-01",
            "effective_to": None,
        }
    )
    assert mode == "custom"
    assert from_date == "2026-02-01"
    assert to_date is None


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
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("achievements.range.ensure_achievement_range_columns"):
                    with patch(
                        "achievements.range.try_acquire_reeval_lock",
                        return_value=(True, None),
                    ):
                        with patch("slack_schedule.queue_achievement_backfill") as queue:
                            with patch("slack_app._refresh_achievements_list"):
                                handle_backfill_achievement(ack, body, MagicMock(), MagicMock())
    ack.assert_called_once_with()
    queue.assert_called_once()
    assert queue.call_args.kwargs["achievement_id"] == 4
    assert queue.call_args.kwargs["start"] == "2026-01-01"
    assert queue.call_args.kwargs["end"] == "2026-08-18"
    assert queue.call_args.kwargs.get("automatic") is not True
    assert queue.call_args.kwargs["action"] == "re-evaluated"


def test_backfill_button_rejects_in_flight_lock():
    from slack_app import handle_backfill_achievement

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
            "state": {"values": {}},
        },
        "actions": [{"action_id": "paxminer_achievement_backfill", "value": "4"}],
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
                with patch("achievements.range.ensure_achievement_range_columns"):
                    with patch(
                        "achievements.range.try_acquire_reeval_lock",
                        return_value=(False, "A re-evaluate is already running."),
                    ):
                        with patch("slack_schedule.queue_achievement_backfill") as queue:
                            with patch("slack_app._refresh_achievements_list") as refresh:
                                handle_backfill_achievement(ack, body, MagicMock(), MagicMock())
    queue.assert_not_called()
    assert "already running" in refresh.call_args.args[4]


def test_backfill_button_clears_lock_when_queue_fails():
    from slack_app import handle_backfill_achievement

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
            "state": {"values": {}},
        },
        "actions": [{"action_id": "paxminer_achievement_backfill", "value": "4"}],
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
                with patch("achievements.range.ensure_achievement_range_columns"):
                    with patch(
                        "achievements.range.try_acquire_reeval_lock",
                        return_value=(True, None),
                    ):
                        with patch(
                            "slack_schedule.queue_achievement_backfill",
                            side_effect=RuntimeError("invoke failed"),
                        ):
                            with patch("achievements.range.clear_reeval_lock") as unlock:
                                with patch("slack_app._refresh_achievements_list") as refresh:
                                    handle_backfill_achievement(
                                        ack, body, MagicMock(), MagicMock()
                                    )
    unlock.assert_called_once()
    assert unlock.call_args.args[2] == 4
    assert "Could not queue" in refresh.call_args.args[4]


def test_backfill_does_not_clear_lock_before_acquire():
    from slack_app import handle_backfill_achievement

    ack = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
            "state": {"values": {}},
        },
        "actions": [{"action_id": "paxminer_achievement_backfill", "value": "4"}],
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
                with patch(
                    "achievements.range.ensure_achievement_range_columns",
                    side_effect=RuntimeError("cannot ALTER"),
                ):
                    with patch("achievements.range.clear_reeval_lock") as unlock:
                        with patch("slack_app._refresh_achievements_list"):
                            handle_backfill_achievement(
                                ack, body, MagicMock(), MagicMock()
                            )
    unlock.assert_not_called()


def test_duplicate_handler_prefills_from_source():
    from slack_app import handle_duplicate_achievement

    ack = MagicMock()
    client = MagicMock()
    body = {
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
            "state": {"values": {}},
        },
        "actions": [{"action_id": "paxminer_achievement_duplicate", "value": "3"}],
    }
    row = {
        "id": 3,
        "name": "The Priest",
        "code": "the_priest",
        "metric": "posts",
        "activity": ["QSource"],
        "period": "year",
        "threshold": 25,
        "range_mode": None,
        "effective_from": None,
        "effective_to": None,
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
                with patch("slack_app._load_achievement", return_value=row):
                    with patch("slack_app._load_activity_options", return_value=["QSource"]):
                        with patch(
                            "slack_app.uniquify_achievement_code",
                            return_value="the_priest_copy",
                        ):
                            with patch(
                                "slack_app._hydrate_range_row",
                                side_effect=lambda _c, _s, r: r,
                            ):
                                handle_duplicate_achievement(
                                    ack, body, client, MagicMock()
                                )
    view = client.views_update.call_args.kwargs["view"]
    range_block = next(b for b in view["blocks"] if b.get("block_id") == "range_mode")
    assert range_block["element"]["initial_option"]["value"] == "all_attendance"

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


def test_delete_all_achievements_loops_single_delete_and_logs_each():
    from slack_app import handle_delete_all_achievements

    ack = MagicMock()
    client = MagicMock()
    body = {
        "user": {"id": "UADMIN"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
        },
    }
    rows = [
        {"id": 1, "name": "Six Pack", "code": "six_pack"},
        {"id": 2, "name": "Centurion", "code": "centurion"},
    ]
    by_id = {1: rows[0], 2: rows[1]}
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", {"region": "t", "schema_name": "f3test"}),
        ):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app._load_achievements", return_value=rows):
                    with patch(
                        "slack_app._load_achievement",
                        side_effect=lambda _cur, _schema, aid: by_id[int(aid)],
                    ):
                        with patch(
                            "slack_app.achievement_award_impact",
                            side_effect=[(10, 4), (5, 3)],
                        ):
                            with patch("slack_app._log_actor_name", return_value="Klint"):
                                with patch(
                                    "slack_app._post_achievement_admin_notice"
                                ) as notice:
                                    with patch(
                                        "slack_app._refresh_achievements_list"
                                    ) as refresh:
                                        handle_delete_all_achievements(
                                            ack, body, client, MagicMock()
                                        )
    assert notice.call_count == 2
    logs = [call.args[2] for call in notice.call_args_list]
    channels = [call.args[1] for call in notice.call_args_list]
    assert "Six Pack" in logs[0]
    assert "Action: deleted" in logs[0]
    assert "Author: Klint" in logs[0]
    assert "Awards: 0 granted, 10 revoked, 0 unchanged" in logs[0]
    assert "Centurion" in logs[1]
    assert "Action: deleted" in logs[1]
    assert "was deleted by" in channels[0]
    assert "(10 revoked)" in channels[0]
    assert "Six Pack" in channels[0]
    sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert sql.count("achievements_awarded") >= 2
    assert sql.count("achievement_versions") >= 2
    assert sql.count("achievements_list") >= 2
    assert "Deleted 2 achievements and 15 awards." in refresh.call_args.args[4]


def test_delete_one_achievement_sql_matches_single_delete():
    from slack_app import _delete_one_achievement

    cur = MagicMock()
    with patch(
        "slack_app._load_achievement",
        return_value={"id": 3, "name": "Centurion", "code": "centurion"},
    ):
        with patch("slack_app.achievement_award_impact", return_value=(12, 8)):
            deleted = _delete_one_achievement(cur, "f3test", 3)
    assert deleted == {
        "name": "Centurion",
        "code": "centurion",
        "awards": 12,
        "pax": 8,
    }
    sql = " ".join(str(c) for c in cur.execute.call_args_list)
    assert "DELETE FROM `f3test`.`achievements_awarded` WHERE achievement_id=%s" in sql
    assert "DELETE FROM `f3test`.`achievement_versions` WHERE achievement_id=%s" in sql
    assert "DELETE FROM `f3test`.`achievements_list` WHERE id=%s" in sql


def test_restore_defaults_adds_like_new_and_queues_reeval():
    from achievements.activity import activity_filter_from_rule
    from config_paxminer import load_achievement_defaults
    from slack_app import handle_restore_achievements

    ack = MagicMock()
    client = MagicMock()
    body = {
        "user": {"id": "UADMIN"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
        },
    }
    seeds = load_achievement_defaults()
    next_id = {"n": 9}

    def execute(sql, params=None):
        if "INSERT INTO" in str(sql) and "achievements_list" in str(sql):
            next_id["n"] += 1
            mock_cur.lastrowid = next_id["n"]

    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", {"region": "t", "schema_name": "f3test"}),
        ):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = None
                mock_cur.execute.side_effect = execute
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                    with patch("achievements.range.ensure_achievement_range_columns"):
                        with patch(
                            "achievements.range.try_acquire_reeval_lock",
                            return_value=(True, None),
                        ):
                            with patch("achievements.versions.insert_version") as insert_ver:
                                with patch(
                                    "slack_schedule.queue_achievement_backfill"
                                ) as queue:
                                    with patch(
                                        "slack_app._post_achievement_admin_notice"
                                    ) as notice:
                                        with patch(
                                            "slack_app._refresh_achievements_list"
                                        ) as refresh:
                                            handle_restore_achievements(
                                                ack, body, client, MagicMock()
                                            )
    assert insert_ver.call_count == len(seeds)
    by_code = {call.kwargs["code"]: call.kwargs for call in insert_ver.call_args_list}
    for seed in seeds:
        kwargs = by_code[seed["code"]]
        assert kwargs["range_mode"] == "all_attendance"
        assert kwargs["activity_filter"] == activity_filter_from_rule(seed)
    assert by_code["the_priest"]["activity_filter"]["include"]
    assert by_code["the_monk"]["activity_filter"]["include"]
    assert by_code["leader_of_men"]["activity_filter"]["include"] == []
    assert queue.call_count == len(seeds)
    assert queue.call_args_list[0].kwargs["automatic"] is True
    assert queue.call_args_list[0].kwargs["achievement_id"] == 10
    assert queue.call_args_list[0].kwargs["actor"] == "UADMIN"
    assert queue.call_args_list[0].kwargs["action"] == "created"
    notice.assert_not_called()
    assert f"{len(seeds)} missing builtin" in refresh.call_args.args[4]
    assert "Re-evaluate queued" in refresh.call_args.args[4]


def test_restore_defaults_skips_existing_codes():
    from slack_app import handle_restore_achievements

    ack = MagicMock()
    body = {
        "user": {"id": "UADMIN"},
        "view": {
            "id": "V1",
            "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}',
        },
    }
    seeds = [
        {
            "name": "The Priest",
            "description": "d",
            "verb": "v",
            "code": "the_priest",
            "metric": "posts",
            "activity": "qsource",
            "period": "year",
            "threshold": 25,
        }
    ]
    with patch("slack_app.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", {"region": "t"}),
        ):
            with patch("slack_app.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = {"id": 1}
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_app.load_achievement_defaults", return_value=seeds):
                    with patch("slack_app.earliest_beatdown_date", return_value="2025-01-01"):
                        with patch("achievements.range.ensure_achievement_range_columns"):
                            with patch("achievements.versions.insert_version") as insert_ver:
                                with patch(
                                    "slack_schedule.queue_achievement_backfill"
                                ) as queue:
                                    with patch("slack_app._refresh_achievements_list"):
                                        handle_restore_achievements(
                                            ack, body, MagicMock(), MagicMock()
                                        )
    insert_ver.assert_not_called()
    queue.assert_not_called()


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


def test_overflow_row_and_parse_action():
    from slack_blocks import OVERFLOW_ENABLE, overflow_row, parse_overflow_action

    row = overflow_row("Six Pack", "paxminer_achievement_more", 7, enabled=False, subline="Disabled | Week - 6 posts")
    assert row["accessory"]["type"] == "overflow"
    assert "*Six Pack*" in row["text"]["text"]
    assert "_Disabled | Week - 6 posts_" in row["text"]["text"]
    labels = [o["text"]["text"] for o in row["accessory"]["options"]]
    assert labels == ["Edit", "Duplicate", "Enable", "Delete"]
    verb, oid = parse_overflow_action(
        {"selected_option": {"value": f"{OVERFLOW_ENABLE}:7"}}
    )
    assert verb == OVERFLOW_ENABLE
    assert oid == 7

