"""
Local verification for extend-schedule (mocked DB + handler routing).

Run from repo root:
  cd qsignups/qsignups && PYTHONPATH=. python3 ../testing/test_extend_schedule_local.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_extend_all_schedules_no_db() -> None:
    from slack.handlers.weekly import extend_all_schedules

    mock_session = MagicMock()
    query_mock = MagicMock()
    mock_session.query.return_value = query_mock
    query_mock.all.return_value = []

    with patch("slack.handlers.weekly.get_session", return_value=mock_session):
        with patch("slack.handlers.weekly.close_session"):
            extend_all_schedules(logging.getLogger("test"))


def test_handler_routes_extend_schedule() -> None:
    """Import app without Slack auth.test (Bolt calls it in _init_middleware_list)."""
    import sys as sys_mod
    from unittest.mock import patch

    with patch.dict(os.environ, {"DB_ENCRYPTION_KEY": "ci-test-encryption-key-32chars!"}, clear=False):
        with patch("slack_bolt.app.app.App._init_middleware_list", lambda *args, **kwargs: None):
            with patch("slack.handlers.weekly.extend_all_schedules") as ext:
                sys_mod.modules.pop("app", None)
                import app as app_mod

                out = app_mod.handler({"source": "qsignups.extend-schedule"}, None)
                ext.assert_called_once()
                assert out.get("statusCode") == 200


def test_build_recurring_master_rows() -> None:
    from slack.handlers.weekly import _build_recurring_master_rows

    # Jan 6, 13, 20, 27 2025 are Mondays; horizon exclusive Jan 28 -> four Mondays
    start = date(2025, 1, 6)
    horizon = date(2025, 1, 28)
    rows = _build_recurring_master_rows(
        "C123",
        "Monday",
        "0530",
        "0615",
        "Bootcamp",
        "T1",
        True,
        start,
        horizon,
    )
    assert len(rows) == 4
    assert all(r.event_day_of_week == "Monday" for r in rows)
    assert rows[0].event_date == date(2025, 1, 6)


def test_extend_orphan_cleanup_calls_delete() -> None:
    """Orphan distinct tuple (not in Weekly) triggers delete_records."""
    from database.orm import Master, Weekly
    from slack.handlers.weekly import extend_all_schedules

    w1 = MagicMock()
    w1.ao_channel_id = "C1"
    w1.event_day_of_week = "Monday"
    w1.event_time = "0530"
    w1.team_id = "T1"

    orphan_row = MagicMock()
    orphan_row.ao_channel_id = "C1"
    orphan_row.event_day_of_week = "Tuesday"  # no Weekly for Tuesday at this time/AO
    orphan_row.event_time = "0530"
    orphan_row.team_id = "T1"

    weekly_q = MagicMock()
    weekly_q.all.return_value = [w1]

    orphan_q = MagicMock()
    orphan_q.filter.return_value = orphan_q
    orphan_q.distinct.return_value = orphan_q
    orphan_q.all.return_value = [orphan_row]

    max_q = MagicMock()
    max_q.filter.return_value = max_q
    max_q.group_by.return_value = max_q
    max_q.all.return_value = []

    call = [0]

    def query_side_effect(*args, **kwargs):
        call[0] += 1
        if call[0] == 1:
            return weekly_q
        if call[0] == 2:
            return orphan_q
        if call[0] == 3:
            return max_q
        raise AssertionError(f"unexpected query call {call[0]}")

    mock_session = MagicMock()
    mock_session.query.side_effect = query_side_effect

    with patch("slack.handlers.weekly.get_session", return_value=mock_session):
        with patch("slack.handlers.weekly.close_session"):
            with patch("slack.handlers.weekly.DbManager.delete_records") as del_mock:
                with patch("slack.handlers.weekly.DbManager.create_records") as create_mock:
                    extend_all_schedules(logging.getLogger("test"))

    del_mock.assert_called_once()
    args, kwargs = del_mock.call_args
    assert args[0] is Master
    create_mock.assert_called_once()


def test_extend_gap_fill_inserts_from_day_after_max() -> None:
    """When max recurring date is today+30, gap-fill adds rows after that."""
    from database.orm import Master, Weekly
    from slack.handlers.weekly import extend_all_schedules

    fixed_today = date(2025, 6, 1)

    w1 = MagicMock()
    w1.ao_channel_id = "C1"
    w1.event_day_of_week = "Monday"
    w1.event_time = "0530"
    w1.event_end_time = "0615"
    w1.event_type = "Bootcamp"
    w1.team_id = "T1"

    weekly_q = MagicMock()
    weekly_q.all.return_value = [w1]

    orphan_q = MagicMock()
    orphan_q.filter.return_value = orphan_q
    orphan_q.distinct.return_value = orphan_q
    orphan_q.all.return_value = []

    max_q = MagicMock()
    max_q.filter.return_value = max_q
    max_q.group_by.return_value = max_q
    max_row = MagicMock()
    max_row.ao_channel_id = "C1"
    max_row.event_day_of_week = "Monday"
    max_row.event_time = "0530"
    max_row.team_id = "T1"
    max_row.max_d = fixed_today + timedelta(days=30)
    max_q.all.return_value = [max_row]

    call = [0]

    def query_side_effect(*args, **kwargs):
        call[0] += 1
        if call[0] == 1:
            return weekly_q
        if call[0] == 2:
            return orphan_q
        if call[0] == 3:
            return max_q
        raise AssertionError(f"unexpected query call {call[0]}")

    mock_session = MagicMock()
    mock_session.query.side_effect = query_side_effect

    with patch("slack.handlers.weekly.get_session", return_value=mock_session):
        with patch("slack.handlers.weekly.close_session"):
            with patch("slack.handlers.weekly.DbManager.delete_records") as del_mock:
                with patch("slack.handlers.weekly.DbManager.create_records") as create_mock:
                    with patch("slack.handlers.weekly.date", wraps=date) as mock_date:
                        mock_date.today.return_value = fixed_today
                        extend_all_schedules(logging.getLogger("test"))

    del_mock.assert_not_called()
    create_mock.assert_called_once()
    inserted = create_mock.call_args[0][0]
    assert len(inserted) >= 1
    max_date = fixed_today + timedelta(days=30)
    start = max_date + timedelta(days=1)
    expected_first = start
    while expected_first.strftime("%A") != "Monday":
        expected_first += timedelta(days=1)
    assert inserted[0].event_date == expected_first


def test_weekly_edit_deletes_old_series_and_creates_new_rows() -> None:
    """Edit removes future rows for original series and inserts new schedule."""
    from slack.handlers.weekly import edit

    original = MagicMock()
    original.ao_channel_id = "COLD"
    original.event_day_of_week = "Monday"
    original.event_time = "0530"
    original.id = 42

    ao = MagicMock()
    ao.ao_channel_id = "CNEW"

    body = {
        "view": {
            "state": {"values": {}},
            "blocks": [{"elements": [{"text": "42"}]}],
        }
    }

    with patch("slack.handlers.weekly.inputs.AO_SELECTOR.get_selected_value", return_value="AO Name"):
        with patch("slack.handlers.weekly.inputs.WEEKDAY_SELECTOR.get_selected_value", return_value="Wednesday"):
            with patch(
                "slack.handlers.weekly.inputs.START_TIME_SELECTOR.get_selected_value",
                return_value="05:30",
            ):
                with patch(
                    "slack.handlers.weekly.inputs.END_TIME_SELECTOR.get_selected_value",
                    return_value="06:15",
                ):
                    with patch(
                        "slack.handlers.weekly.inputs.EVENT_TYPE_SELECTOR.get_selected_value",
                        return_value="Bootcamp",
                    ):
                        with patch("slack.handlers.weekly.DbManager.find_records", return_value=[ao]):
                            with patch("slack.handlers.weekly.DbManager.get_record", return_value=original):
                                            with patch("slack.handlers.weekly.DbManager.update_record") as up_mock:
                                                with patch("slack.handlers.weekly.DbManager.delete_records") as del_mock:
                                                    with patch("slack.handlers.weekly.DbManager.create_records") as cr_mock:
                                                        with patch("slack.handlers.weekly.date", wraps=date) as mock_date:
                                                            mock_date.today.return_value = date(2025, 6, 4)
                                                            resp = edit(
                                                                None,
                                                                "U1",
                                                                "T1",
                                                                logging.getLogger("test"),
                                                                body,
                                                            )

    assert resp.success is True
    up_mock.assert_called_once()
    del_mock.assert_called_once()
    cr_mock.assert_called_once()
    new_rows = cr_mock.call_args[0][0]
    assert all(r.event_day_of_week == "Wednesday" for r in new_rows)
    assert all(r.ao_channel_id == "CNEW" for r in new_rows)


def main() -> None:
    # Before any import of app.py (require_encryption_key + Bolt App)
    os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-min-16")
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-for-local-extend-test")
    os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

    test_extend_all_schedules_no_db()
    print("extend_all_schedules (empty weeklies, mocked session) OK")
    test_handler_routes_extend_schedule()
    print("handler extend-schedule routing OK")
    test_build_recurring_master_rows()
    print("_build_recurring_master_rows OK")
    test_extend_orphan_cleanup_calls_delete()
    print("extend_orphan_cleanup OK")
    test_extend_gap_fill_inserts_from_day_after_max()
    print("extend_gap_fill OK")
    test_weekly_edit_deletes_old_series_and_creates_new_rows()
    print("weekly.edit delete-and-recreate OK")


if __name__ == "__main__":
    main()
