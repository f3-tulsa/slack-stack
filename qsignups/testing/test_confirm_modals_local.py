"""
Tests for confirmation modals, manage-calendar navigation helpers, and delete confirm modals.

Run from repo root:
  cd qsignups/qsignups && PYTHONPATH=. python3 ../testing/test_confirm_modals_local.py
"""
from __future__ import annotations

import os
import sys

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_format_field_change_unchanged_returns_none() -> None:
    from slack.confirm_modals import format_field_change

    assert format_field_change("X", "a", "a") is None
    assert format_field_change("X", None, None) is None


def test_format_field_change_shows_arrow() -> None:
    from slack.confirm_modals import format_field_change

    line = format_field_change("Title", "Old", "New")
    assert line is not None
    assert "Title" in line
    assert "Old" in line
    assert "New" in line


def test_confirm_modal_view_json_roundtrip() -> None:
    from slack import actions
    from slack.confirm_modals import confirm_modal_view, load_modal_metadata

    meta = {"v": 1, "foo": "bar"}
    view = confirm_modal_view(
        actions.CONFIRM_EDIT_AO_VIEW,
        meta,
        ["*Line one*"],
        warning_markdown=":warning: *Test*",
    )
    assert view["type"] == "modal"
    assert view["callback_id"] == actions.CONFIRM_EDIT_AO_VIEW
    assert view["submit"]["text"] == "Confirm"
    loaded = load_modal_metadata(view["private_metadata"])
    assert loaded["foo"] == "bar"
    assert any(":warning:" in str(b) for b in view["blocks"])


def test_delete_confirm_modal_view_json_roundtrip() -> None:
    from slack import actions
    from slack.confirm_modals import delete_confirm_modal_view, load_modal_metadata

    meta = {"v": 1, "event_id": 42}
    view = delete_confirm_modal_view(
        actions.CONFIRM_DELETE_RECURRING_VIEW,
        meta,
        ["*Bootcamp* at *The Murph*", "Tuesdays @ 05:30"],
        warning_markdown="This cannot be undone.",
    )
    assert view["type"] == "modal"
    assert view["callback_id"] == actions.CONFIRM_DELETE_RECURRING_VIEW
    assert view["submit"]["text"] == "Delete"
    loaded = load_modal_metadata(view["private_metadata"])
    assert loaded["event_id"] == 42
    assert any("You are about to delete" in str(b) for b in view["blocks"])


if __name__ == "__main__":
    test_format_field_change_unchanged_returns_none()
    test_format_field_change_shows_arrow()
    test_confirm_modal_view_json_roundtrip()
    test_delete_confirm_modal_view_json_roundtrip()
    print("confirm modals / delete confirm modal tests OK")
