import copy
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features.config import attach_paxminer_settings_button
from utilities.slack import actions, forms


def _context_mentions_paxminer(form) -> bool:
    return any(
        "/config-paxminer" in str(getattr(getattr(b, "element", None), "initial_value", "") or "")
        for b in form.blocks
    )


def test_attach_paxminer_settings_button_when_linked():
    form = copy.deepcopy(forms.CONFIG_FORM)
    region = SimpleNamespace(paxminer_schema="f3test")
    with patch.dict(os.environ, {"PM_SLACK_APP_ID": "A123"}):
        assert attach_paxminer_settings_button(form, region, team_id="T1") is True
    labels = [getattr(e, "label", "") for b in form.blocks for e in getattr(b, "elements", [])]
    assert any("PAXMiner Settings" in str(x) for x in labels)
    assert not _context_mentions_paxminer(form)
    button = next(
        e
        for b in form.blocks
        for e in getattr(b, "elements", [])
        if getattr(e, "action", None) == actions.CONFIG_PAXMINER_SETTINGS
    )
    assert "A123" in button.url
    assert "T1" in button.url


def test_attach_paxminer_settings_button_skipped_without_schema():
    form = copy.deepcopy(forms.CONFIG_FORM)
    region = SimpleNamespace(paxminer_schema=None)
    with patch.dict(os.environ, {"PM_SLACK_APP_ID": "A123"}):
        assert attach_paxminer_settings_button(form, region, team_id="T1") is False
    assert _context_mentions_paxminer(form)


def test_attach_paxminer_settings_button_skipped_without_app_id():
    form = copy.deepcopy(forms.CONFIG_FORM)
    region = SimpleNamespace(paxminer_schema="f3test")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PM_SLACK_APP_ID", None)
        assert attach_paxminer_settings_button(form, region, team_id="T1") is False
    assert _context_mentions_paxminer(form)
