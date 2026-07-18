"""Guard PAXMiner Slack app manifest invariants."""

from __future__ import annotations

import json
from pathlib import Path

_MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"


def test_manifest_has_no_incoming_webhook_scope():
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    scopes = data["oauth_config"]["scopes"]["bot"]
    assert "incoming-webhook" not in scopes


def test_manifest_enables_app_home_and_app_home_opened():
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    app_home = data["features"]["app_home"]
    assert app_home.get("home_tab_enabled") is True
    events = data["settings"]["event_subscriptions"]
    assert "app_home_opened" in events.get("bot_events", [])
    assert events.get("request_url")
