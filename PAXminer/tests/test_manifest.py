"""Guard PAXMiner Slack app manifest invariants."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / "manifest.json"
_MANIFESTS = (_MANIFEST, _ROOT / "manifest-prod.json")


def test_manifest_has_no_incoming_webhook_scope():
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    scopes = data["oauth_config"]["scopes"]["bot"]
    assert "incoming-webhook" not in scopes


def test_manifest_enables_app_home_and_app_home_opened():
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    app_home = data["features"]["app_home"]
    assert app_home.get("home_tab_enabled") is True
    # Messages tab must stay enabled so bot DMs (awards, charts) are visible.
    assert app_home.get("messages_tab_enabled") is True
    events = data["settings"]["event_subscriptions"]
    assert "app_home_opened" in events.get("bot_events", [])
    assert events.get("request_url")


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=lambda p: p.name)
def test_manifest_includes_emoji_read_and_reactions_write(manifest):
    data = json.loads(manifest.read_text(encoding="utf-8"))
    scopes = data["oauth_config"]["scopes"]["bot"]
    assert "emoji:read" in scopes
    assert "reactions:write" in scopes
    # Grants post via chat:write.public without joining, but reactions.add
    # requires membership — the T-clap fire depends on being able to join.
    assert "chat:write.public" in scopes
    assert "channels:join" in scopes


def test_manifest_needs_no_options_load_url():
    """The award emoji is a text input; nothing uses external_select any more."""
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    interactivity = data["settings"]["interactivity"]
    assert interactivity.get("is_enabled") is True
    assert "message_menu_options_url" not in interactivity
