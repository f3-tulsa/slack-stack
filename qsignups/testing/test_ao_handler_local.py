"""
Local tests for AO insert/edit handlers and PAXminer regional site_q helpers.

Run from repo root:
  cd qsignups/qsignups && PYTHONPATH=. python3 ../testing/test_ao_handler_local.py
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_insert_calls_set_site_q_with_user() -> None:
    from database.orm import AO
    from slack.handlers import ao as ao_handler

    log = logging.getLogger("test")
    client = MagicMock()
    captured = {}

    def capture_create(record: AO):
        captured["record"] = record
        return record

    input_data = {
        "add_ao_channel_select": {"add_ao_channel_select": {"selected_channel": "C111"}},
        "ao_display_name": {"ao_display_name": {"value": "The Bridge"}},
        "ao_location_subtitle": {"ao_location_subtitle": {"value": "Park"}},
        "site_q_user_id": {"site_q_user_id": {"selected_user": "U09ABC"}},
    }

    with patch("slack.handlers.ao.DbManager.create_record", side_effect=capture_create):
        with patch("slack.handlers.ao.set_site_q") as set_sq:
            resp = ao_handler.insert(client, "U_editor", "T1", log, input_data)

    assert resp.success is True
    assert captured["record"].ao_channel_id == "C111"
    assert captured["record"].team_id == "T1"
    set_sq.assert_called_once_with("C111", "U09ABC")


def test_insert_set_site_q_none_when_optional_absent() -> None:
    from database.orm import AO
    from slack.handlers import ao as ao_handler

    log = logging.getLogger("test")
    client = MagicMock()

    input_data = {
        "add_ao_channel_select": {"add_ao_channel_select": {"selected_channel": "C222"}},
        "ao_display_name": {"ao_display_name": {"value": "AO X"}},
        "ao_location_subtitle": {"ao_location_subtitle": {"value": "Here"}},
    }

    with patch("slack.handlers.ao.DbManager.create_record", return_value=MagicMock()):
        with patch("slack.handlers.ao.set_site_q") as set_sq:
            resp = ao_handler.insert(client, "U_editor", "T1", log, input_data)

    assert resp.success is True
    set_sq.assert_called_once_with("C222", None)


def test_edit_calls_set_site_q() -> None:
    from database.orm import AO
    from slack.handlers import ao as ao_handler

    log = logging.getLogger("test")
    client = MagicMock()
    captured = {}

    def capture_update(cls, filters, fields):
        captured["fields"] = fields

    page_label = "*Edit AO:*\n*Bridge*\nC333"
    input_data = {
        "ao_display_name": {"ao_display_name": {"value": "Bridge"}},
        "ao_location_subtitle": {"ao_location_subtitle": {"value": "96th St"}},
        "site_q_user_id": {"site_q_user_id": {"selected_user": "U068WFS1MT6"}},
    }

    with patch("slack.handlers.ao.DbManager.update_records", side_effect=capture_update):
        with patch("slack.handlers.ao.set_site_q") as set_sq:
            resp = ao_handler.edit(client, "U_editor", "T1", log, page_label, input_data)

    assert resp.success is True
    assert set(captured["fields"].keys()) == {AO.ao_display_name, AO.ao_location_subtitle}
    set_sq.assert_called_once_with("C333", "U068WFS1MT6")


def test_edit_set_site_q_none_when_unset() -> None:
    from slack.handlers import ao as ao_handler

    log = logging.getLogger("test")
    client = MagicMock()

    page_label = "*Edit AO:*\n*Bridge*\nC333"
    input_data = {
        "ao_display_name": {"ao_display_name": {"value": "Bridge"}},
        "ao_location_subtitle": {"ao_location_subtitle": {"value": "96th St"}},
    }

    with patch("slack.handlers.ao.DbManager.update_records"):
        with patch("slack.handlers.ao.set_site_q") as set_sq:
            resp = ao_handler.edit(client, "U_editor", "T1", log, page_label, input_data)

    assert resp.success is True
    set_sq.assert_called_once_with("C333", None)


def test_get_site_q_reads_when_schema_set() -> None:
    from slack.handlers.ao import get_site_q

    with patch.dict(os.environ, {"PAXMINER_REGIONAL_SCHEMA": "f3ttown_prod"}):
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.first.return_value = ("U123",)
        mock_conn.execute.return_value = mock_result
        cm = MagicMock()
        cm.__enter__.return_value = mock_conn
        cm.__exit__.return_value = None
        mock_engine = MagicMock()
        mock_engine.connect.return_value = cm

        with patch("slack.handlers.ao.get_engine", return_value=mock_engine):
            out = get_site_q("C999")

    assert out == "U123"
    mock_conn.execute.assert_called_once()


def test_get_site_q_returns_none_without_schema() -> None:
    from slack.handlers.ao import get_site_q

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PAXMINER_REGIONAL_SCHEMA", None)
        with patch("slack.handlers.ao.get_engine") as ge:
            out = get_site_q("C999")
    assert out is None
    ge.assert_not_called()


if __name__ == "__main__":
    test_insert_calls_set_site_q_with_user()
    test_insert_set_site_q_none_when_optional_absent()
    test_edit_calls_set_site_q()
    test_edit_set_site_q_none_when_unset()
    test_get_site_q_reads_when_schema_set()
    test_get_site_q_returns_none_without_schema()
    print("ao_handler site_q (regional aos) tests OK")
