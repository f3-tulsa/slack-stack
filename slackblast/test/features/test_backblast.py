import os
import sys
from unittest.mock import MagicMock, patch

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are features.* and utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import backblast
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

