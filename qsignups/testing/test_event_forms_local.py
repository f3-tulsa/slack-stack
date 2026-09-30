"""
Local tests for qsignups event form builders.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_build_open_slot_assignment_modal_defaults_to_current_user() -> None:
    from slack import actions
    from slack.forms import event

    view = event.build_open_slot_assignment_modal(
        "The Bridge",
        datetime(2026, 5, 8, 5, 30),
        "U_SELF",
    )

    assert view["type"] == "modal"
    assert view["callback_id"] == actions.ASSIGN_OPEN_SLOT_VIEW
    assert view["submit"]["text"] == "Assign"
    assert view["blocks"][0]["text"]["text"] == "Assign the Q slot for:"
    assert view["blocks"][2]["element"]["type"] == "users_select"
    assert view["blocks"][2]["element"]["initial_user"] == "U_SELF"
    meta = json.loads(view["private_metadata"])
    assert meta == {
        "ao_display_name": "The Bridge",
        "selected_date": "2026-05-08 05:30:00",
    }


if __name__ == "__main__":
    test_build_open_slot_assignment_modal_defaults_to_current_user()
    print("event form tests OK")
