import os
import sys
from unittest.mock import MagicMock, patch

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are features.* and utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import backblast
from utilities.database.orm import PaxminerUser
from utilities.slack import actions


def _base_backblast_data():
    return {
        actions.BACKBLAST_TITLE: "DRQ beatdown",
        actions.BACKBLAST_DATE: "2026-05-18",
        actions.BACKBLAST_AO: "C_DOWNRANGE",
        actions.BACKBLAST_Q: "U_DRQ",
        actions.BACKBLAST_COQ: [],
        actions.BACKBLAST_PAX: ["U_PAX1"],
        actions.BACKBLAST_NONSLACK_PAX: None,
        actions.BACKBLAST_FNGS: None,
        actions.BACKBLAST_COUNT: None,
        actions.BACKBLAST_MOLESKIN: {"type": "section", "text": {"type": "mrkdwn", "text": "moleskin"}},
        actions.BACKBLAST_DESTINATION: "The_AO",
        actions.BACKBLAST_EMAIL_SEND: "no",
        actions.BACKBLAST_FILE: [],
        actions.BACKBLAST_FILE_IDS: [],
        actions.BACKBLAST_FILE_SLACK_URLS: [],
    }


@patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names")
@patch("features.backblast.parse_rich_block", return_value="moleskin text")
@patch("features.backblast.get_channel_name", return_value="downrange")
def test_handle_backblast_post_uses_empty_icon_url_when_q_url_missing(
    _mock_channel_name,
    _mock_parse,
    _mock_replace,
):
    """Missing Q profile URL should not crash backblast submission."""

    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    def _get_user_names(_users, _logger, _client, return_urls=False, user_records=None):
        if return_urls:
            return ["DRQ"], []
        return ["PAX One"]

    body = {
        "view": {"callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_OP"},
    }
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456"}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}

    region_record = MagicMock()
    region_record.paxminer_schema = None
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_get_user_names),
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_OP"},
            region_record=region_record,
        )

    assert client.chat_postMessage.called
    assert client.chat_postMessage.call_args.kwargs["icon_url"] == ""


@patch("features.backblast.get_channel_id", return_value=None)
@patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names")
@patch("features.backblast.parse_rich_block", return_value="moleskin text")
@patch("features.backblast.get_channel_name", return_value="downrange")
def test_handle_backblast_post_app_q_uses_submitter_for_db_q_user_id(
    _mock_channel_name,
    _mock_parse,
    _mock_replace,
    _mock_get_channel_id,
):
    """App/bot Q identities (e.g. DRQ) should not collide on AO/date PK constraints."""

    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    q_user_record = MagicMock(spec=PaxminerUser)
    q_user_record.user_id = "U_DRQ"
    q_user_record.app = 1

    created_backblast_records = []

    def _capture_backblast_record(*, schema, record):
        created_backblast_records.append(record)
        return record

    body = {
        "view": {"callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_SUBMITTER"},
    }
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456", "message": {"edited": {"ts": "123.457"}}}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}

    region_record = MagicMock()
    region_record.paxminer_schema = "f3testregion"
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=lambda *_args, **_kwargs: (["DRQ"], ["https://avatar"]) if _kwargs.get("return_urls") else ["PAX One"]),
        patch("features.backblast.DbManager.find_records", return_value=[q_user_record]),
        patch("features.backblast.DbManager.create_record", side_effect=_capture_backblast_record),
        patch("features.backblast.DbManager.create_records", return_value=None),
        patch("features.backblast.ensure_users_in_db", return_value=None),
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=region_record,
        )

    assert len(created_backblast_records) == 1
    assert created_backblast_records[0].q_user_id == "U_SUBMITTER"


@patch("features.backblast.trigger_achievement_webhook")
@patch("features.backblast.get_channel_id", return_value=None)
@patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names")
@patch("features.backblast.parse_rich_block", return_value="moleskin text")
@patch("features.backblast.get_channel_name", return_value="downrange")
def test_handle_backblast_post_triggers_achievement_webhook_when_coupled(
    _mock_channel_name,
    _mock_parse,
    _mock_replace,
    _mock_get_channel_id,
    mock_webhook,
):
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    body = {
        "view": {"callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_SUBMITTER"},
    }
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456", "message": {"edited": {"ts": "123.457"}}}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}

    region_record = MagicMock()
    region_record.paxminer_schema = "f3testregion"
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False
    region_record.post_achievements_to_ao = 1

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch(
            "features.backblast.get_user_names",
            side_effect=lambda *_args, **_kwargs: (["DRQ"], ["https://avatar"])
            if _kwargs.get("return_urls")
            else ["PAX One"],
        ),
        patch("features.backblast.DbManager.find_records", return_value=[]),
        patch("features.backblast.DbManager.create_record", return_value=MagicMock()),
        patch("features.backblast.DbManager.create_records", return_value=None),
        patch("features.backblast.ensure_users_in_db", return_value=None),
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=region_record,
        )

    mock_webhook.assert_called_once()
    kwargs = mock_webhook.call_args.kwargs
    assert kwargs["region_record"] is region_record
    assert "U_DRQ" in kwargs["pax_user_ids"]
    assert "U_PAX1" in kwargs["pax_user_ids"]
    assert kwargs["post_to_ao"] is True
    assert kwargs["ao_channel_id"] == "C_DOWNRANGE"


@patch("features.backblast.trigger_achievement_webhook")
@patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names")
@patch("features.backblast.parse_rich_block", return_value="moleskin text")
@patch("features.backblast.get_channel_name", return_value="downrange")
def test_handle_backblast_post_skips_webhook_when_uncoupled(
    _mock_channel_name,
    _mock_parse,
    _mock_replace,
    mock_webhook,
):
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    body = {
        "view": {"callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_OP"},
    }
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456"}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}

    region_record = MagicMock()
    region_record.paxminer_schema = None
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch(
            "features.backblast.get_user_names",
            side_effect=lambda *_args, **_kwargs: (["DRQ"], []) if _kwargs.get("return_urls") else ["PAX One"],
        ),
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_OP"},
            region_record=region_record,
        )

    mock_webhook.assert_not_called()

