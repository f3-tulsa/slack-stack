import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import preblast
from utilities.slack import actions


def _preblast_data():
    return {
        actions.PREBLAST_TITLE: "Morning",
        actions.PREBLAST_DATE: "2026-05-18",
        actions.PREBLAST_TIME: "0530",
        actions.PREBLAST_AO: "C_AO",
        actions.PREBLAST_Q: "U_Q",
        actions.PREBLAST_WHY: None,
        actions.PREBLAST_FNGS: None,
        actions.PREBLAST_COUPONS: None,
        actions.PREBLAST_MOLESKIN: None,
        actions.PREBLAST_DESTINATION: "The_AO",
    }


def test_retried_preblast_create_posts_once():
    claimed = set()

    def claim(key, kind, team_id):
        token = (key, kind)
        if token in claimed:
            return False
        claimed.add(token)
        return True

    body = {
        "view": {"id": "V_PRE", "callback_id": actions.PREBLAST_CALLBACK_ID},
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }
    client = MagicMock()
    region = MagicMock()
    region.paxminer_schema = None
    region.workspace_name = "ws"

    form = MagicMock()
    form.get_selected_values.side_effect = lambda _body: _preblast_data()

    with (
        patch("features.preblast.forms.PREBLAST_FORM", form),
        patch("features.preblast.get_user_names", return_value=(["Q"], [""])),
        patch("utilities.interaction_claims.claim_interaction", side_effect=claim),
    ):
        for _ in range(2):
            preblast.handle_preblast_post(body, client, MagicMock(), {}, region)

    assert client.chat_postMessage.call_count == 1
