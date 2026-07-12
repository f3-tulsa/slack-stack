import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")


def _region(paxminer_schema=None):
    rec = MagicMock()
    rec.paxminer_schema = paxminer_schema
    return rec


def test_coupling_guard_requires_schema_url_and_secret():
    from utilities.paxminer_achievements_webhook import achievements_coupling_configured

    with patch.dict(
        os.environ,
        {
            "PAXMINER_ACHIEVEMENTS_URL": "https://example.com",
            "PAXMINER_ACHIEVEMENTS_WEBHOOK_SECRET": "secret",
        },
        clear=False,
    ):
        assert achievements_coupling_configured(_region("f3test")) is True
        assert achievements_coupling_configured(_region(None)) is False


def test_trigger_skipped_when_uncoupled():
    from utilities.paxminer_achievements_webhook import trigger_achievement_webhook

    with patch.dict(os.environ, {}, clear=True):
        with patch("utilities.paxminer_achievements_webhook.requests.post") as post:
            trigger_achievement_webhook(
                region_record=_region(None),
                pax_user_ids={"U1"},
                bd_date="2026-07-01",
                ao_channel_id="C1",
                post_to_ao=True,
                logger=MagicMock(),
            )
            post.assert_not_called()


def test_trigger_posts_when_coupled():
    from utilities.paxminer_achievements_webhook import trigger_achievement_webhook

    env = {
        "PAXMINER_ACHIEVEMENTS_URL": "https://example.com/hook",
        "PAXMINER_ACHIEVEMENTS_WEBHOOK_SECRET": "secret-value",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("utilities.paxminer_achievements_webhook.requests.post") as post:
            trigger_achievement_webhook(
                region_record=_region("f3test"),
                pax_user_ids={"U1", "U2"},
                bd_date="2026-07-01",
                ao_channel_id="C_AO",
                post_to_ao=True,
                logger=MagicMock(),
            )
            post.assert_called_once()
            kwargs = post.call_args.kwargs
            assert kwargs["headers"]["X-Paxminer-Achievements-Webhook-Secret"] == "secret-value"
            payload = post.call_args.kwargs["data"]
            assert "f3test" in payload
            assert "U1" in payload and "U2" in payload
