import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import welcome


def _body(event_id="Ev123"):
    return {
        "event_id": event_id,
        "team_id": "T123",
        "event": {"type": "team_join", "user": {"id": "U123"}},
    }


def _region(dm=True, channel=True):
    return SimpleNamespace(
        welcome_dm_enable=dm,
        welcome_dm_template={"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}},
        welcome_channel_enable=channel,
        welcome_channel="C123",
        workspace_name="F3 Test",
    )


def test_retried_team_join_posts_each_destination_once():
    claimed = set()

    def claim(event_id, destination, team_id, user_id):
        key = (event_id, destination)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    client = MagicMock()
    with patch("features.welcome.claim_welcome_delivery", side_effect=claim):
        welcome.handle_team_join(_body(), client, MagicMock(), {}, _region())
        welcome.handle_team_join(_body(), client, MagicMock(), {}, _region())

    assert client.chat_postMessage.call_count == 2
    assert {call.kwargs["channel"] for call in client.chat_postMessage.call_args_list} == {"U123", "C123"}


@pytest.mark.parametrize(
    ("dm_enabled", "channel_enabled", "expected_destination"),
    [
        (True, False, welcome.WELCOME_DM_DESTINATION),
        (False, True, welcome.WELCOME_CHANNEL_DESTINATION),
    ],
)
def test_team_join_claims_only_enabled_destination(dm_enabled, channel_enabled, expected_destination):
    client = MagicMock()
    with patch("features.welcome.claim_welcome_delivery", return_value=True) as claim:
        welcome.handle_team_join(
            _body(),
            client,
            MagicMock(),
            {},
            _region(dm=dm_enabled, channel=channel_enabled),
        )

    claim.assert_called_once_with("Ev123", expected_destination, "T123", "U123")
    client.chat_postMessage.assert_called_once()


def test_failed_post_releases_only_its_delivery_claim():
    client = MagicMock()
    client.chat_postMessage.side_effect = RuntimeError("Slack unavailable")

    with (
        patch("features.welcome.claim_welcome_delivery", return_value=True),
        patch("features.welcome.release_welcome_delivery") as release,
        pytest.raises(RuntimeError, match="Slack unavailable"),
    ):
        welcome.handle_team_join(_body(), client, MagicMock(), {}, _region(dm=True, channel=False))

    release.assert_called_once_with("Ev123", welcome.WELCOME_DM_DESTINATION)


def test_missing_event_id_refuses_enabled_welcome():
    with pytest.raises(ValueError, match="missing event_id"):
        welcome.handle_team_join(
            _body(event_id=None),
            MagicMock(),
            MagicMock(),
            {},
            _region(),
        )


def test_disabled_welcome_does_not_require_event_id():
    client = MagicMock()
    welcome.handle_team_join(
        _body(event_id=None),
        client,
        MagicMock(),
        {},
        _region(dm=False, channel=False),
    )
    client.chat_postMessage.assert_not_called()


def _duplicate_error(code):
    return IntegrityError("INSERT", {}, Exception(code, "Duplicate entry"))


def test_duplicate_delivery_claim_returns_false():
    session = MagicMock()
    session.flush.side_effect = _duplicate_error(1062)

    @contextmanager
    def transaction():
        yield session

    with patch("features.welcome.DbManager.transaction", transaction):
        assert welcome.claim_welcome_delivery("Ev123", "channel", "T123", "U123") is False


def test_non_duplicate_delivery_claim_error_propagates():
    session = MagicMock()
    session.flush.side_effect = _duplicate_error(1213)

    @contextmanager
    def transaction():
        yield session

    with (
        patch("features.welcome.DbManager.transaction", transaction),
        pytest.raises(IntegrityError),
    ):
        welcome.claim_welcome_delivery("Ev123", "channel", "T123", "U123")
