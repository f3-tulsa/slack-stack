"""Unit tests for slack_util mention helpers."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

from slack_util import is_slack_user_id, mention, workspace_user_ids


def test_is_slack_user_id_accepts_valid():
    assert is_slack_user_id("U0ANGDEBSKE")
    assert is_slack_user_id("W01234567")
    assert is_slack_user_id("U01ABCDEF23")


def test_is_slack_user_id_rejects_junk():
    assert not is_slack_user_id(None)
    assert not is_slack_user_id("")
    assert not is_slack_user_id("nan")
    assert not is_slack_user_id("none")
    assert not is_slack_user_id(math.nan)
    # Synthetic seed IDs are Slack-shaped; roster / known_ids rejects them at mention time.
    assert is_slack_user_id("USEEDPAX12XXXX")
    assert not is_slack_user_id("SEEDPAX01")
    assert not is_slack_user_id("C01234567")  # channel
    assert not is_slack_user_id(12345)


def test_mention_real_id():
    assert mention("U0ANGDEBSKE", "Ben Baldwin") == "<@U0ANGDEBSKE>"


def test_mention_fallback_id_when_not_in_roster():
    # Prefer user ID in backticks over display name when Slack cannot resolve.
    assert (
        mention("USEEDPAX12XXXX", "[SEED] PAX 12", known_ids={"U0ANGDEBSKE"})
        == "`USEEDPAX12XXXX`"
    )


def test_mention_fallback_id_only():
    assert mention("USEEDPAX12XXXX", known_ids=set()) == "`USEEDPAX12XXXX`"


def test_mention_fallback_name_when_no_id():
    assert mention(None, "Ben Baldwin") == "`Ben Baldwin`"


def test_mention_unknown():
    assert mention(None) == "`unknown PAX`"
    assert mention(math.nan) == "`unknown PAX`"
    assert mention("nan", "none") == "`unknown PAX`"


def test_mention_without_roster_still_tags_shaped_ids():
    # known_ids=None → format-only validation (Slack hiccup path)
    assert mention("U0ANGDEBSKE", "Ben") == "<@U0ANGDEBSKE>"


def test_workspace_user_ids_paginates():
    client = MagicMock()
    client.users_list.side_effect = [
        {
            "members": [{"id": "U0ANGDEBSKE"}, {"id": "USLACKBOT"}],
            "response_metadata": {"next_cursor": "page2"},
        },
        {
            "members": [{"id": "U0ANAPT3F2S"}],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    ids = workspace_user_ids(client)
    assert ids == {"U0ANGDEBSKE", "USLACKBOT", "U0ANAPT3F2S"}
    assert client.users_list.call_count == 2


def test_workspace_user_ids_returns_none_on_failure():
    client = MagicMock()
    client.users_list.side_effect = RuntimeError("boom")
    assert workspace_user_ids(client) is None


def test_workspace_user_ids_terminates_on_mock_client():
    """Bare MagicMock returns a truthy non-str next_cursor; must not loop forever."""
    client = MagicMock()
    assert workspace_user_ids(client) == set()
    assert client.users_list.call_count == 1


def test_workspace_user_ids_stops_on_repeated_cursor():
    client = MagicMock()
    page = {
        "members": [{"id": "U0ANGDEBSKE"}],
        "response_metadata": {"next_cursor": "stuck"},
    }
    client.users_list.return_value = page
    ids = workspace_user_ids(client)
    assert ids == {"U0ANGDEBSKE"}
    # First page + second page sees the same cursor and stops.
    assert client.users_list.call_count == 2
