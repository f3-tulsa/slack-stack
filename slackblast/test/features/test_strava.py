import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import strava
from utilities.slack import actions


def test_retried_strava_modify_posts_once():
    claimed = set()

    def claim(key, kind, team_id):
        token = (key, kind)
        if token in claimed:
            return False
        claimed.add(token)
        return True

    body = {
        "type": "view_submission",
        "view": {
            "id": "V_STRAVA",
            "private_metadata": json.dumps(
                {
                    "strava_activity_id": "99",
                    "channel_id": "C1",
                    "backblast_ts": "1.2",
                }
            ),
            "state": {"values": {}},
        },
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }
    form = MagicMock()
    form.get_selected_values.return_value = {
        actions.STRAVA_ACTIVITY_TITLE: "Run",
        actions.STRAVA_ACTIVITY_DESCRIPTION: "desc",
    }
    client = MagicMock()
    region = MagicMock()

    with (
        patch("features.strava.forms.STRAVA_ACTIVITY_MODIFY_FORM", form),
        patch("features.strava.update_strava_activity", return_value={"calories": 100, "distance": 1609}),
        patch("features.strava.static_image_url", return_value="https://example.com/strava.png"),
        patch("utilities.interaction_claims.claim_interaction", side_effect=claim),
    ):
        for _ in range(2):
            strava.handle_strava_modify(body, client, MagicMock(), {}, region)

    assert client.chat_postMessage.call_count == 1
    assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "1.2"


def test_strava_view_closed_does_not_post_or_claim():
    body = {
        "type": "view_closed",
        "view": {
            "id": "V_STRAVA",
            "private_metadata": json.dumps(
                {
                    "strava_activity_id": "99",
                    "channel_id": "C1",
                    "backblast_ts": "1.2",
                }
            ),
            "state": {"values": {}},
        },
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }
    client = MagicMock()

    with (
        patch("features.strava.update_strava_activity") as update,
        patch("features.strava.get_strava_activity") as fetch,
        patch("utilities.interaction_claims.claim_interaction") as claim,
    ):
        strava.handle_strava_modify(body, client, MagicMock(), {}, MagicMock())

    update.assert_not_called()
    fetch.assert_not_called()
    claim.assert_not_called()
    client.chat_postMessage.assert_not_called()
