"""Unit tests for pure scheduling helpers (no DB / pandas)."""

from __future__ import annotations

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")

from scheduling import (
    already_ran_successfully,
    destination_valid_for_report,
    is_due_now,
    is_due_today,
    parse_time_of_day,
    region_local_now,
    resolve_time_window,
    snap_time_to_tick,
    time_of_day_options,
)


def test_time_of_day_options_are_96_fifteen_minute_slots():
    opts = time_of_day_options()
    assert len(opts) == 96
    assert opts[0]["value"] == "00:00"
    assert opts[-1]["value"] == "23:45"
    assert all(o["value"].endswith(("00", "15", "30", "45")) for o in opts)


def test_is_due_today_weekly_and_monthly():
    sunday = date(2026, 7, 19)  # Sunday
    assert is_due_today({"frequency_type": "weekly", "day_of_week": 6}, sunday)
    assert not is_due_today({"frequency_type": "weekly", "day_of_week": 0}, sunday)

    assert is_due_today({"frequency_type": "monthly", "month_day_mode": "first"}, date(2026, 7, 1))
    assert is_due_today({"frequency_type": "monthly", "month_day_mode": "last"}, date(2026, 2, 28))
    # Leap year Feb 29
    assert is_due_today({"frequency_type": "monthly", "month_day_mode": "last"}, date(2024, 2, 29))
    # Clamp day 31 in February
    assert is_due_today(
        {"frequency_type": "monthly", "month_day_mode": "specific", "day_of_month": 31},
        date(2026, 2, 28),
    )


def test_is_due_now_at_or_after_and_idempotency():
    # 2026-07-19 18:05 UTC = 13:05 America/Chicago (CDT)
    utc = datetime(2026, 7, 19, 18, 5, tzinfo=ZoneInfo("UTC"))
    schedule = {
        "frequency_type": "weekly",
        "day_of_week": 6,  # Sunday
        "time_of_day": "13:00",
        "last_run_on": None,
        "last_run_status": None,
    }
    assert is_due_now(schedule, timezone_name="America/Chicago", utc_now=utc)

    early = datetime(2026, 7, 19, 17, 0, tzinfo=ZoneInfo("UTC"))  # 12:00 CDT
    assert not is_due_now(schedule, timezone_name="America/Chicago", utc_now=early)

    schedule["last_run_on"] = date(2026, 7, 19)
    schedule["last_run_status"] = "success"
    assert not is_due_now(schedule, timezone_name="America/Chicago", utc_now=utc)

    schedule["last_run_status"] = "error"
    assert is_due_now(schedule, timezone_name="America/Chicago", utc_now=utc)


def test_hourly_is_due_today_and_minute_gate():
    from scheduling import FREQUENCY_TYPES, already_ran_this_hour, format_schedule_summary

    assert "hourly" in FREQUENCY_TYPES
    assert is_due_today({"frequency_type": "hourly"}, date(2026, 7, 19))

    # 13:20 CDT = 18:20 UTC
    utc = datetime(2026, 7, 19, 18, 20, tzinfo=ZoneInfo("UTC"))
    schedule = {
        "frequency_type": "hourly",
        "time_of_day": "00:15:00",
        "last_run_at": None,
        "last_run_status": None,
    }
    assert is_due_now(schedule, timezone_name="America/Chicago", utc_now=utc)

    early = datetime(2026, 7, 19, 18, 5, tzinfo=ZoneInfo("UTC"))  # 13:05 CDT
    assert not is_due_now(schedule, timezone_name="America/Chicago", utc_now=early)

    schedule["last_run_at"] = datetime(2026, 7, 19, 13, 15)
    schedule["last_run_status"] = "success"
    assert already_ran_this_hour(schedule, datetime(2026, 7, 19, 13, 45))
    assert not is_due_now(schedule, timezone_name="America/Chicago", utc_now=utc)

    # Next hour clears the guard
    next_hour = datetime(2026, 7, 19, 19, 20, tzinfo=ZoneInfo("UTC"))  # 14:20 CDT
    assert is_due_now(schedule, timezone_name="America/Chicago", utc_now=next_hour)

    # Minute > 45 (e.g. :50) snaps to :45 so the :45 tick still fires.
    late = datetime(2026, 7, 19, 18, 50, tzinfo=ZoneInfo("UTC"))  # 13:50 CDT
    late_sched = {
        "frequency_type": "hourly",
        "time_of_day": "00:50:00",
        "last_run_at": None,
        "last_run_status": None,
    }
    assert is_due_now(late_sched, timezone_name="America/Chicago", utc_now=late)
    at_tick = datetime(2026, 7, 19, 18, 45, tzinfo=ZoneInfo("UTC"))  # 13:45 CDT
    assert is_due_now(late_sched, timezone_name="America/Chicago", utc_now=at_tick)

    # skipped is terminal for the hour (no re-fire every tick)
    schedule["last_run_status"] = "skipped"
    schedule["last_run_at"] = datetime(2026, 7, 19, 13, 15)
    assert already_ran_this_hour(schedule, datetime(2026, 7, 19, 13, 45))
    assert not is_due_now(schedule, timezone_name="America/Chicago", utc_now=utc)

    summary = format_schedule_summary(
        {
            "id": 1,
            "name": "Award Achievements",
            "destination_type": "specific_channels",
            "frequency_type": "hourly",
            "time_of_day": "00:15:00",
            "enabled": 1,
        },
        {"name": "Award Achievements"},
    )
    assert "hourly @ :15" in summary


def test_mark_schedule_status_tolerates_missing_last_run_at():
    from datetime import date, datetime
    from unittest.mock import MagicMock

    from schedule_runner import mark_schedule_status, reset_last_run_at_probe

    reset_last_run_at_probe()
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    class UnknownColumn(Exception):
        def __init__(self):
            super().__init__(1054, "Unknown column 'last_run_at' in 'field list'")
            self.args = (1054, "Unknown column 'last_run_at' in 'field list'")

    cur.execute.side_effect = [UnknownColumn(), None]
    mark_schedule_status(
        conn,
        "paxminer_test",
        7,
        date(2026, 7, 19),
        "running",
        local_dt=datetime(2026, 7, 19, 13, 15),
    )
    assert cur.execute.call_count == 2
    second_sql = cur.execute.call_args_list[1].args[0]
    assert "last_run_at" not in second_sql
    assert "last_run_on" in second_sql
    conn.commit.assert_called()

    # Cached miss skips the last_run_at write on the next call
    cur.reset_mock()
    cur.execute.side_effect = None
    mark_schedule_status(
        conn,
        "paxminer_test",
        8,
        date(2026, 7, 19),
        "success",
        local_dt=datetime(2026, 7, 19, 13, 16),
    )
    assert cur.execute.call_count == 1
    assert "last_run_at" not in cur.execute.call_args.args[0]
    reset_last_run_at_probe()


def test_award_achievements_destination_and_dispatch():
    from unittest.mock import MagicMock, patch

    from schedule_runner import _dispatch_report
    from scheduling import destination_valid_for_report

    assert destination_valid_for_report("award_achievements", "specific_channels")
    assert not destination_valid_for_report("award_achievements", "dm_all_pax")

    region = {
        "schema_name": "f3test",
        "slack_token": "enc",
        "send_achievements": 0,
        "achievement_channel": None,
    }
    schedule = {
        "destination_type": "specific_channels",
        "destination_channels": '["C_AWARD"]',
    }
    definition = {"report_type": "award_achievements", "code": "award_achievements"}

    with patch("schedule_runner.decrypt_field", return_value="xoxb-test"):
        with patch("schedule_runner.connect_from_env") as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch(
                "schedule_runner.resolve_destinations",
                return_value=[{"kind": "channel", "id": "C_AWARD"}],
            ):
                with patch(
                    "achievements.runner.run_achievements_for_region",
                    return_value={"grants": 1, "revokes": 0},
                ) as mock_run:
                    result = _dispatch_report(
                        MagicMock(), "paxminer_test", region, schedule, definition
                    )

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["channel_override"] == "C_AWARD"
    assert mock_run.call_args.kwargs["post_channels"] == ["C_AWARD"]
    assert mock_run.call_args.kwargs["regional_schema"] == "f3test"
    assert result.get("posted_channels") == [{"ao": "awards", "channel_id": "C_AWARD"}]


def test_award_dispatch_omits_posted_channels_when_nothing_posted():
    from unittest.mock import MagicMock, patch

    from schedule_runner import _dispatch_report

    region = {"schema_name": "f3test", "slack_token": "enc"}
    schedule = {
        "destination_type": "specific_channels",
        "destination_channels": '["C_AWARD"]',
    }
    definition = {"report_type": "award_achievements", "code": "award_achievements"}
    with patch("schedule_runner.decrypt_field", return_value="xoxb-test"):
        with patch("schedule_runner.connect_from_env", return_value=MagicMock()):
            with patch(
                "schedule_runner.resolve_destinations",
                return_value=[{"kind": "channel", "id": "C_AWARD"}],
            ):
                with patch(
                    "achievements.runner.run_achievements_for_region",
                    return_value={"grants": 0, "revokes": 0},
                ):
                    result = _dispatch_report(
                        MagicMock(), "paxminer_test", region, schedule, definition
                    )
    assert "posted_channels" not in result
    assert result.get("channel_count") == 0


def test_already_ran_successfully():
    assert already_ran_successfully(
        {"last_run_on": date(2026, 7, 19), "last_run_status": "success"},
        date(2026, 7, 19),
    )
    assert not already_ran_successfully(
        {"last_run_on": date(2026, 7, 19), "last_run_status": "error"},
        date(2026, 7, 19),
    )
    # Crashed / in-flight Run Now must not block the tick
    assert not already_ran_successfully(
        {"last_run_on": date(2026, 7, 19), "last_run_status": "running"},
        date(2026, 7, 19),
    )
    assert not already_ran_successfully(
        {"last_run_on": date(2026, 7, 19), "last_run_status": "skipped"},
        date(2026, 7, 19),
    )


def test_custom_interval_days():
    local = date(2026, 7, 19)
    assert is_due_today(
        {"frequency_type": "custom", "custom_spec": {"interval_days": 7}, "last_run_on": None},
        local,
    )
    assert is_due_today(
        {
            "frequency_type": "custom",
            "custom_spec": {"interval_days": 7},
            "last_run_on": date(2026, 7, 12),
        },
        local,
    )
    assert not is_due_today(
        {
            "frequency_type": "custom",
            "custom_spec": {"interval_days": 7},
            "last_run_on": date(2026, 7, 15),
        },
        local,
    )


def test_resolve_time_window_last_month_and_ytd():
    utc = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("UTC"))
    start, end = resolve_time_window(
        {"time_window_type": "last_month"},
        timezone_name="America/Chicago",
        utc_now=utc,
    )
    assert start == date(2026, 6, 1)
    assert end == date(2026, 6, 30)

    start, end = resolve_time_window(
        {"time_window_type": "ytd"},
        timezone_name="America/Chicago",
        utc_now=utc,
    )
    assert start == date(2026, 1, 1)
    assert end == region_local_now("America/Chicago", utc_now=utc).date()

    start, end = resolve_time_window(
        {"time_window_type": "this_month"},
        timezone_name="America/Chicago",
        utc_now=utc,
    )
    assert start == date(2026, 7, 1)
    assert end == region_local_now("America/Chicago", utc_now=utc).date()


def test_destination_constraints():
    assert destination_valid_for_report("pax_charts", "dm_all_pax")
    assert not destination_valid_for_report("pax_charts", "all_ao_channels")
    assert destination_valid_for_report("kotter", "specific_channels")
    assert destination_valid_for_report("achievement_almost_there", "specific_channels")


def test_parse_and_snap_time():
    assert parse_time_of_day("13:15") == time(13, 15)
    assert snap_time_to_tick(time(13, 17)) == time(13, 15)


def test_schedule_edit_modal_has_submit_and_tod_options():
    from config_schedule import _schedule_edit_modal

    view = _schedule_edit_modal(
        "T1",
        "f3test",
        [{"id": 1, "name": "Kotter", "report_type": "kotter", "code": "kotter"}],
        timezone_name="America/Chicago",
    )
    assert view.get("submit")
    tod = next(b for b in view["blocks"] if b.get("block_id") == "time_of_day")
    assert len(tod["element"]["options"]) == 96


def test_hourly_schedule_edit_uses_minute_of_hour_picker():
    from config_schedule import _schedule_edit_modal

    view = _schedule_edit_modal(
        "T1",
        "f3test",
        [{"id": 1, "name": "Awards", "report_type": "award_achievements", "code": "award_achievements"}],
        schedule={
            "id": 3,
            "report_definition_id": 1,
            "destination_type": "specific_channels",
            "destination_channels": '["C1"]',
            "frequency_type": "hourly",
            "time_of_day": "00:50:00",
            "enabled": 1,
        },
        timezone_name="America/Chicago",
    )
    tod = next(b for b in view["blocks"] if b.get("block_id") == "time_of_day")
    assert tod["label"]["text"] == "Minute of hour"
    values = [o["value"] for o in tod["element"]["options"]]
    assert values == ["00:00", "00:15", "00:30", "00:45"]
    assert tod["element"]["initial_option"]["value"] == "00:45"


def test_reports_list_and_edit_modals_have_submit():
    from config_schedule import _report_edit_modal, _reports_list_modal, _schedules_list_modal

    for view in (
        _reports_list_modal("T1", "f3test", []),
        _schedules_list_modal("T1", "f3test", []),
    ):
        assert view.get("type") == "modal"
        assert "submit" not in view
        assert view.get("close")["text"] == "Back"
    edit = _report_edit_modal("T1", "f3test", None)
    assert edit.get("submit")


def test_format_schedule_summary_includes_last_run():
    from scheduling import format_schedule_summary

    line = format_schedule_summary(
        {
            "id": 3,
            "destination_type": "specific_channels",
            "frequency_type": "weekly",
            "time_of_day": "13:00:00",
            "enabled": 1,
            "last_run_status": "success",
            "last_run_on": date(2026, 7, 18),
        },
        {"name": "Kotter"},
    )
    assert "Kotter" in line
    assert "last run: success (2026-07-18)" in line


def test_schedules_list_uses_pencil_rows_and_subline():
    from config_schedule import EDIT_SCHEDULE_ACTION_ID, _schedules_list_modal

    schedules = [
        {
            "id": 9,
            "definition_name": "Kotter",
            "destination_type": "specific_channels",
            "frequency_type": "weekly",
            "time_of_day": "07:00:00",
            "enabled": 1,
            "last_run_status": "skipped",
            "last_run_on": date(2026, 7, 18),
        }
    ]
    view = _schedules_list_modal("T1", "f3test", schedules, selected_schedule_id=9)
    pencils = [
        b["accessory"]
        for b in view["blocks"]
        if b.get("type") == "section" and b.get("accessory")
    ]
    assert pencils[0]["action_id"] == EDIT_SCHEDULE_ACTION_ID
    assert pencils[0]["value"] == "9"
    assert pencils[0]["text"]["text"] == "✏️ Edit"
    sub = " ".join(
        el.get("text") or ""
        for b in view["blocks"]
        if b.get("type") == "context"
        for el in b.get("elements") or []
    )
    assert "Enabled" in sub
    assert "Weekly @ 07:00" in sub
    assert "Last run: skipped (2026-07-18)" in sub
    assert "submit" not in view


def test_post_log_swallows_client_errors():
    from unittest.mock import MagicMock, patch

    from slack_util import post_log

    client = MagicMock()
    with patch("slack_util.post_message", side_effect=RuntimeError("boom")):
        post_log(client, "- Schedule (test): FAILED - boom")  # must not raise


def test_resolve_log_channel_prefers_stored_id():
    from unittest.mock import MagicMock, patch

    from slack_util import post_log, resolve_log_channel

    assert resolve_log_channel(None) == "paxminer_logs"
    assert resolve_log_channel({"log_channel": " CLOG99 "}) == "CLOG99"
    assert resolve_log_channel({"log_channel": ""}) == "paxminer_logs"

    client = MagicMock()
    with patch("slack_util.post_message") as post:
        post_log(client, "hello", region={"log_channel": "CLOG99"})
    post.assert_called_once()
    assert post.call_args.args[1] == "CLOG99"


def test_ensure_log_channel_column_adds_when_missing():
    from unittest.mock import MagicMock

    from schedule_schema import ensure_log_channel_column

    cur = MagicMock()
    cur.fetchone.return_value = {"c": 0}
    assert ensure_log_channel_column(cur, "paxminer") is True
    assert "log_channel" in cur.execute.call_args_list[-1][0][0]


def test_ensure_log_channel_column_skips_when_present():
    from unittest.mock import MagicMock

    from schedule_schema import ensure_log_channel_column

    cur = MagicMock()
    cur.fetchone.return_value = {"c": 1}
    assert ensure_log_channel_column(cur, "paxminer") is False
    assert cur.execute.call_count == 1


def test_format_run_result_variants():
    from schedule_runner import format_run_result

    text, _ = format_run_result(
        {
            "definition_name": "Kotter",
            "ok": True,
            "channel_count": 2,
            "specified_channels": ["C1", "C2"],
            "destination_type": "specific_channels",
            "duration_s": 0.6,
        }
    )
    assert "The *Kotter* report was run as scheduled" in text
    assert "Status: success (0.6s)" in text
    assert "Number of Messages: 2" in text
    assert "<#C1>" in text

    text, _ = format_run_result(
        {
            "definition_name": "Kotter",
            "ok": True,
            "skipped": "no destinations configured",
            "duration_s": 0.1,
        }
    )
    assert "Status: skipped (0.1s)" in text
    assert "no destinations configured" in text
    assert "Destination(s): none" in text

    text, _ = format_run_result(
        {
            "definition_name": "Kotter",
            "ok": False,
            "error": "boom",
            "specified_channels": ["C1"],
            "message_count": 0,
            "duration_s": 1.2,
        }
    )
    assert "Status: failed (1.2s)" in text
    assert "\nboom\n" in text or text.splitlines()[2] == "boom"
    assert "Destination(s): none" in text


def test_format_schedule_log_line_variants():
    from schedule_runner import format_schedule_log_line

    line = format_schedule_log_line(
        "f3ttown_test",
        {
            "definition_name": "Kotter",
            "ok": True,
            "channel_count": 2,
            "specified_channels": ["C0APR1E1137"],
            "destination_type": "specific_channels",
            "duration_s": 0.5,
        },
    )
    assert line.startswith("The *Kotter* report was run as scheduled")
    assert "Status: success (0.5s)" in line
    assert "<#C0APR1E1137>" in line
    assert "Number of Messages: 2" in line
    assert "<@" not in line

    line = format_schedule_log_line(
        "f3ttown_test",
        {
            "definition_name": "Kotter",
            "ok": True,
            "skipped": "no destinations configured",
        },
    )
    assert "Status: skipped (0.0s)" in line
    assert "no destinations configured" in line

    line = format_schedule_log_line(
        "f3ttown_test",
        {
            "definition_name": "Kotter",
            "ok": False,
            "error": "boom",
            "specified_channels": ["C1"],
            "message_count": 0,
        },
    )
    assert "Status: failed (0.0s)" in line
    assert "boom" in line
    assert "Destination(s): none" in line


def test_post_schedule_outcome_log_uses_schema_name():
    from unittest.mock import MagicMock, patch

    from schedule_runner import _post_schedule_outcome_log

    region = {
        "region": "Tulsa",
        "schema_name": "f3ttown_test",
        "slack_token": "enc",
    }
    result = {
        "schedule_id": 1,
        "report_type": "kotter",
        "ok": True,
        "channel_count": 1,
    }
    log_lines: list[str] = []
    with patch("schedule_runner.decrypt_field", return_value="xoxb-test"):
        with patch("schedule_runner.slack_client", return_value=MagicMock()):
            with patch(
                "schedule_runner.post_log",
                side_effect=lambda _c, text, **_k: log_lines.append(text),
            ):
                _post_schedule_outcome_log(region, result)

    assert len(log_lines) == 1
    assert log_lines[0].startswith("The *kotter* report was run as scheduled")
    assert "Tulsa" not in log_lines[0]


def test_resolve_destinations_empty_specific_channels():
    from schedule_runner import resolve_destinations

    assert resolve_destinations(
        None,
        {"destination_type": "specific_channels", "destination_channels": []},
    ) == []
    assert resolve_destinations(
        None,
        {"destination_type": "specific_channels", "destination_channels": "[]"},
    ) == []


def test_dispatch_skips_empty_specific_channels_without_expanding():
    from unittest.mock import MagicMock, patch

    from schedule_runner import _dispatch_report

    region = {"schema_name": "f3test", "slack_token": "enc"}
    schedule = {
        "destination_type": "specific_channels",
        "destination_channels": [],
    }
    definition = {"report_type": "pax_charts"}
    mock_conn = MagicMock()
    with patch("schedule_runner.connect_from_env", return_value=mock_conn):
        with patch("schedule_runner.decrypt_field", return_value="xoxb-test"):
            with patch("monthly_charts.PAXcharter.run_pax_charter") as mock_pax:
                result = _dispatch_report(None, "paxminer_test", region, schedule, definition)
    assert result.get("skipped") == "no destinations configured"
    mock_pax.assert_not_called()


def test_run_one_schedule_item_logs_automatic_not_manual():
    from unittest.mock import MagicMock, patch

    from schedule_runner import run_one_schedule_item

    schedule = {
        "id": 11,
        "schema_name": "f3test",
        "report_definition_id": 5,
        "enabled": 1,
    }
    region = {
        "schema_name": "f3test",
        "region": "Tulsa",
        "slack_token": "enc",
        "timezone": "America/Chicago",
    }
    definition = {"id": 5, "report_type": "kotter"}
    dispatch_result = {"channel_count": 1, "user_count": 0}

    mock_conn = MagicMock()

    def _run(*, manual: bool):
        with patch("schedule_runner._load_region", return_value=region):
            with patch("schedule_runner._load_definition", return_value=definition):
                with patch("schedule_runner.is_due_now", return_value=True):
                    with patch("schedule_runner.mark_schedule_status"):
                        with patch(
                            "schedule_runner._dispatch_report", return_value=dispatch_result
                        ):
                            with patch("schedule_runner._post_schedule_outcome_log") as mock_log:
                                out = run_one_schedule_item(
                                    mock_conn,
                                    "paxminer",
                                    schedule,
                                    force=True,
                                    manual=manual,
                                )
                                return out, mock_log

    out, mock_log = _run(manual=False)
    assert out["ok"] is True
    mock_log.assert_called_once()
    assert mock_log.call_args.args[0]["region"] == "Tulsa"
    assert mock_log.call_args.args[1]["schedule_id"] == 11

    out, mock_log = _run(manual=True)
    assert out["ok"] is True
    mock_log.assert_called_once()


def test_queue_run_now_payload_includes_notify_user():
    import json
    from unittest.mock import MagicMock, patch

    import slack_schedule

    mock_client = MagicMock()
    with patch.dict("os.environ", {"SCHEDULE_FUNCTION_NAME": "paxminer-test-schedule"}):
        with patch("boto3.client", return_value=mock_client):
            slack_schedule.queue_run_now(42, "U123")
    kwargs = mock_client.invoke.call_args.kwargs
    assert kwargs["InvocationType"] == "Event"
    payload = json.loads(kwargs["Payload"].decode("utf-8"))
    assert payload == {
        "source": "run_now",
        "schedule_id": 42,
        "force": True,
        "notify_user": "U123",
    }


def test_schedule_handler_notifies_user_on_completion():
    import json
    from unittest.mock import MagicMock, patch

    from handlers import schedule_handler

    result = {"schedule_id": 7, "ok": True, "report_type": "kotter", "channel_count": 1}
    row = {"id": 7, "schema_name": "f3test", "report_definition_id": 1}
    region = {"schema_name": "f3test", "slack_token": "enc"}

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.side_effect = [row, region]

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                    with patch(
                        "schedule_runner.run_one_schedule_item", return_value=result
                    ) as mock_run:
                        resp = schedule_handler(
                            {
                                "source": "run_now",
                                "schedule_id": 7,
                                "force": True,
                                "notify_user": "U9",
                            },
                            None,
                        )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["ok"] is True
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("manual") is True
    assert mock_run.call_args.kwargs.get("notify_user") == "U9"


def test_schedule_handler_run_now_does_not_dm_admin():
    from unittest.mock import MagicMock, patch

    from handlers import schedule_handler

    result = {"schedule_id": 7, "ok": True, "report_type": "kotter", "channel_count": 1}
    row = {"id": 7, "schema_name": "f3test", "report_definition_id": 1}

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = row

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("schedule_runner.run_one_schedule_item", return_value=result):
                    with patch("schedule_runner.notify_run_result") as mock_dm:
                        schedule_handler(
                            {
                                "source": "run_now",
                                "schedule_id": 7,
                                "force": True,
                                "notify_user": "U9",
                            },
                            None,
                        )
    mock_dm.assert_not_called()


def test_report_defaults_json_consistency():
    from scheduling import BUILTIN_DEFINITIONS, DEFAULT_SCHEDULES, VALID_DESTINATIONS

    codes = {d["code"] for d in BUILTIN_DEFINITIONS}
    assert len(BUILTIN_DEFINITIONS) == 8
    assert len(DEFAULT_SCHEDULES) == 8
    assert "award_achievements" in codes
    assert "achievement_almost_there" in codes
    assert {s["code"] for s in DEFAULT_SCHEDULES} == codes
    for s in DEFAULT_SCHEDULES:
        assert s.get("enabled") is True
        defn = next(d for d in BUILTIN_DEFINITIONS if d["code"] == s["code"])
        assert s["destination_type"] in VALID_DESTINATIONS[defn["report_type"]]


def test_backfill_award_achievements_schedules_idempotent():
    from unittest.mock import MagicMock, patch

    from schedule_schema import backfill_award_achievements_schedules

    cur = MagicMock()
    region = {
        "schema_name": "f3ttown_test",
        "achievement_channel": "C_ACH",
        "send_achievements": 1,
    }
    cur.fetchall.return_value = [region]

    with patch("schedule_schema._table_exists", return_value=True):
        cur.fetchone.side_effect = [{"id": 42}, {"c": 0}]
        first = backfill_award_achievements_schedules(cur, "paxminer_test")
        assert first["schedules_inserted"] == 1
        assert first["definitions_ensured"] == 0
        insert_calls = [
            c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0])
        ]
        assert len(insert_calls) == 1
        args = insert_calls[0].args[1]
        assert args[2] == "specific_channels"
        assert "C_ACH" in args[3]
        assert args[4] == "daily"
        assert ", 0)" in insert_calls[0].args[0]

        cur.reset_mock()
        cur.fetchall.return_value = [region]
        cur.fetchone.side_effect = [{"id": 42}, {"c": 1}]
        second = backfill_award_achievements_schedules(cur, "paxminer_test")
        assert second["schedules_inserted"] == 0
        assert second["skipped"] == 1
        assert not any(
            "INSERT INTO" in str(c.args[0]) for c in cur.execute.call_args_list
        )


def test_seed_default_schedules_uses_defaults_json():
    from unittest.mock import MagicMock

    from schedule_schema import seed_default_schedules
    from scheduling import DEFAULT_SCHEDULES

    cur = MagicMock()
    # upsert_builtin_definitions path: SELECT then fetchone for each definition
    code_ids = {s["code"]: i + 1 for i, s in enumerate(DEFAULT_SCHEDULES)}

    def fetchone_side_effect():
        # First COUNT for skip check, then each definition SELECT after INSERT/UPDATE
        return {"c": 0}

    cur.fetchone.side_effect = [
        {"c": 0},  # skip_if_any_schedules count
        *[{"c": 0} for _ in DEFAULT_SCHEDULES],  # merge_only per-definition counts
    ]

    # Patch upsert to return stable ids so we only assert INSERT args
    from unittest.mock import patch

    with patch(
        "schedule_schema.upsert_builtin_definitions",
        return_value=code_ids,
    ):
        inserted = seed_default_schedules(
            cur,
            "paxminer_test",
            {"schema_name": "f3ttown", "send_pax_charts": 0},
            skip_if_any_schedules=True,
        )

    assert inserted == len(DEFAULT_SCHEDULES)
    insert_calls = [
        c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0])
    ]
    assert len(insert_calls) == len(DEFAULT_SCHEDULES)
    for call, item in zip(insert_calls, DEFAULT_SCHEDULES):
        args = call.args[1]
        assert args[2] == item["destination_type"]
        assert args[3] is None  # empty specific_channels
        assert args[7] == 1  # enabled


def test_parse_time_of_day_timedelta_bytes_fractional():
    from datetime import timedelta

    assert parse_time_of_day(timedelta(hours=7, minutes=0)) == time(7, 0)
    assert parse_time_of_day(timedelta(hours=7, minutes=0, microseconds=500000)) == time(7, 0)
    assert parse_time_of_day(b"07:15:00") == time(7, 15)
    assert parse_time_of_day("7:00:00.500000") == time(7, 0)
    assert parse_time_of_day("not-a-time") == time(7, 0)
    assert parse_time_of_day(None) == time(7, 0)


def test_format_run_result_includes_posted_failed_channels():
    from schedule_runner import format_run_result

    text, _ = format_run_result(
        {
            "schedule_id": 9,
            "report_type": "q_charts",
            "ok": True,
            "channel_count": 1,
            "posted_channels": [{"ao": "The Fort", "channel_id": "C111"}],
            "failed_channels": [
                {"ao": "Brickyard", "channel_id": "C222", "reason": "not_in_channel"}
            ],
        }
    )
    assert "Status: success (0.0s)" in text
    assert "<#C111>" in text
    assert "Destination(s): <#C111>" in text


def test_format_schedule_log_line_includes_destinations():
    from schedule_runner import format_schedule_log_line

    line = format_schedule_log_line(
        "f3ttown_test",
        {
            "definition_name": "Q charts",
            "ok": False,
            "error": "all channel uploads failed: not_in_channel",
            "specified_channels": ["C1"],
            "message_count": 0,
        },
    )
    assert "Status: failed (0.0s)" in line
    assert "all channel uploads failed" in line
    assert "Destination(s): none" in line
    assert "Number of Messages: 0" in line


def test_apply_delivery_result_marks_error_when_all_failed():
    from schedule_runner import _apply_delivery_result

    out = _apply_delivery_result(
        {
            "posted_channels": [],
            "failed_channels": [{"ao": "x", "channel_id": "C1", "reason": "boom"}],
        },
        attempted_channels=1,
    )
    assert out["ok"] is False
    assert out["channel_count"] == 0
    assert "all channel uploads failed" in out["error"]


def test_validate_schedule_form_frequency_fields():
    from config_schedule import validate_schedule_form

    base = {
        "report_definition_id": 1,
        "destination_type": "all_ao_channels",
        "destination_channels": [],
        "destination_users": [],
    }
    assert "day_of_week" in validate_schedule_form(
        {**base, "frequency_type": "weekly", "day_of_week": None}, "q_charts"
    )
    assert "day_of_month" in validate_schedule_form(
        {
            **base,
            "frequency_type": "monthly",
            "month_day_mode": "specific",
            "day_of_month": None,
        },
        "q_charts",
    )
    assert "interval_days" in validate_schedule_form(
        {**base, "frequency_type": "custom", "custom_spec": {"interval_days": 0}},
        "q_charts",
    )
    assert not validate_schedule_form(
        {**base, "frequency_type": "weekly", "day_of_week": 6}, "q_charts"
    )


def test_draft_from_schedule_state_clears_stale_fields():
    from config_schedule import draft_from_schedule_state

    draft = draft_from_schedule_state(
        {
            "frequency_type": {
                "paxminer_schedule_freq": {"selected_option": {"value": "weekly"}}
            },
            "destination_type": {
                "paxminer_schedule_dest_type": {
                    "selected_option": {"value": "all_ao_channels"}
                }
            },
            "day_of_week": {"val": {"selected_option": {"value": "6"}}},
        },
        {
            "month_day_mode": "first",
            "day_of_month": "15",
            "destination_channels": ["C1"],
            "interval_days": "7",
        },
    )
    assert draft["frequency_type"] == "weekly"
    assert draft["day_of_week"] == "6"
    assert "month_day_mode" not in draft
    assert "day_of_month" not in draft
    assert "destination_channels" not in draft
    assert "interval_days" not in draft


def test_restore_defaults_is_idempotent():
    from unittest.mock import MagicMock, patch

    from schedule_schema import seed_default_schedules
    from scheduling import DEFAULT_SCHEDULES

    cur = MagicMock()
    code_ids = {s["code"]: i + 1 for i, s in enumerate(DEFAULT_SCHEDULES)}

    # First restore: no existing schedules for any definition → insert all
    cur.fetchone.side_effect = [{"c": 0} for _ in DEFAULT_SCHEDULES]
    with patch("schedule_schema.upsert_builtin_definitions", return_value=code_ids):
        first = seed_default_schedules(
            cur, "paxminer_test", {"schema_name": "f3ttown"}, merge_only=True
        )
    assert first == len(DEFAULT_SCHEDULES)

    # Second restore: every definition already has a row → insert 0
    cur2 = MagicMock()
    cur2.fetchone.side_effect = [{"c": 1} for _ in DEFAULT_SCHEDULES]
    with patch("schedule_schema.upsert_builtin_definitions", return_value=code_ids):
        second = seed_default_schedules(
            cur2, "paxminer_test", {"schema_name": "f3ttown"}, merge_only=True
        )
    assert second == 0
    insert_calls = [
        c for c in cur2.execute.call_args_list if "INSERT INTO" in str(c.args[0])
    ]
    assert insert_calls == []


def test_schedule_edit_modal_omits_null_initial_option():
    from config_schedule import _schedule_edit_modal

    view = _schedule_edit_modal(
        "T1",
        "f3ttown_test",
        [{"id": 1, "name": "Kotter", "report_type": "kotter"}],
        schedule={
            "id": 42,
            "report_definition_id": 1,
            "destination_type": "specific_channels",
            "destination_channels": "[]",
            "destination_users": None,
            "frequency_type": "monthly",
            "month_day_mode": "first",
            "time_of_day": "07:00:00",
            "enabled": 1,
        },
    )
    for block in view["blocks"]:
        el = block.get("element") or {}
        if "initial_option" in el:
            assert el["initial_option"] is not None


def test_format_window_label_and_calendar_month():
    from datetime import date

    from scheduling import (
        format_window_label,
        is_calendar_month,
        resolve_time_window,
        window_file_tag,
    )

    start, end = date(2026, 6, 1), date(2026, 6, 30)
    assert is_calendar_month(start, end)
    assert format_window_label(start, end) == "June 2026"
    assert window_file_tag(start, end) == "Jun2026"

    start2, end2 = date(2026, 6, 1), date(2026, 7, 15)
    assert not is_calendar_month(start2, end2)
    assert "Jun 01" in format_window_label(start2, end2)
    assert " to " in format_window_label(start2, end2)
    assert " - " not in format_window_label(start2, end2)

    # last_month default matches calendar prior month
    from datetime import datetime, timezone

    fixed = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    w = resolve_time_window(
        {"time_window_type": "last_month"},
        timezone_name="America/Chicago",
        utc_now=fixed,
    )
    assert w == (date(2026, 6, 1), date(2026, 6, 30))


def test_reports_list_empty_shows_restore_defaults():
    from config_schedule import (
        RESTORE_DEFAULTS_ACTION_ID,
        RESTORE_REPORTS_ACTION_ID,
        _reports_list_modal,
        _schedules_list_modal,
    )

    reports = _reports_list_modal("T1", "f3test", [])
    actions = [b for b in reports["blocks"] if b.get("type") == "actions"]
    assert any(
        RESTORE_REPORTS_ACTION_ID in [e.get("action_id") for e in a.get("elements", [])]
        for a in actions
    )

    schedules = _schedules_list_modal("T1", "f3test", [])
    actions = [b for b in schedules["blocks"] if b.get("type") == "actions"]
    assert any(
        RESTORE_DEFAULTS_ACTION_ID in [e.get("action_id") for e in a.get("elements", [])]
        for a in actions
    )


def test_report_edit_modal_code_rendered_vs_custom():
    from config_schedule import _report_edit_modal

    builtin = _report_edit_modal(
        "T1",
        "f3test",
        {
            "id": 1,
            "name": "Q charts",
            "code": "q_charts",
            "report_type": "q_charts",
            "is_builtin": 1,
            "time_window_type": "last_month",
        },
    )
    block_ids = [b.get("block_id") for b in builtin["blocks"] if b.get("block_id")]
    assert "name" in block_ids
    assert "time_window_type" in block_ids
    assert "source" not in block_ids
    assert "kind" not in block_ids
    assert "code" not in block_ids

    kotter = _report_edit_modal(
        "T1",
        "f3test",
        {
            "id": 2,
            "name": "Kotter",
            "code": "kotter",
            "report_type": "kotter",
            "is_builtin": 1,
            "time_window_type": None,
        },
    )
    kotter_ids = [b.get("block_id") for b in kotter["blocks"] if b.get("block_id")]
    assert "name" in kotter_ids
    assert "time_window_type" not in kotter_ids

    custom = _report_edit_modal("T1", "f3test", None)
    custom_ids = [b.get("block_id") for b in custom["blocks"] if b.get("block_id")]
    assert "code" in custom_ids
    assert "source" in custom_ids
    assert "kind" in custom_ids
    assert "report_type" in custom_ids


def test_report_edit_modal_template_fields_and_almost_there():
    from config_schedule import REPORT_TEMPLATE_ACTION_ID, _report_edit_modal

    add = _report_edit_modal("T1", "f3test", None)
    assert add["callback_id"]
    add_ids = [b.get("block_id") for b in add["blocks"] if b.get("block_id")]
    assert "report_type" in add_ids
    tpl = next(b for b in add["blocks"] if b.get("block_id") == "report_type")
    assert tpl["element"]["action_id"] == REPORT_TEMPLATE_ACTION_ID

    almost = _report_edit_modal(
        "T1",
        "f3test",
        {
            "id": 9,
            "name": "Almost there",
            "code": "achievement_almost_there",
            "report_type": "achievement_almost_there",
            "is_builtin": 1,
        },
    )
    almost_ids = [b.get("block_id") for b in almost["blocks"] if b.get("block_id")]
    assert "name" in almost_ids
    assert "top_n" in almost_ids
    assert "time_window_type" not in almost_ids
    assert "report_type" not in almost_ids


def test_reports_list_has_pencil_not_duplicate():
    from config_schedule import (
        DUPLICATE_REPORT_ACTION_ID,
        EDIT_REPORT_ACTION_ID,
        _report_edit_modal,
        _reports_list_modal,
    )

    row = {
        "id": 1,
        "name": "Kotter",
        "code": "kotter",
        "report_type": "kotter",
        "is_builtin": 1,
    }
    view = _reports_list_modal("T1", "f3test", [row])
    action_ids = []
    for b in view["blocks"]:
        acc = b.get("accessory") or {}
        if acc.get("action_id"):
            action_ids.append(acc["action_id"])
        for e in b.get("elements") or []:
            if e.get("action_id"):
                action_ids.append(e["action_id"])
    assert EDIT_REPORT_ACTION_ID in action_ids
    assert DUPLICATE_REPORT_ACTION_ID not in action_ids
    pencils = [
        (b.get("accessory") or {}).get("text", {}).get("text")
        for b in view["blocks"]
        if b.get("type") == "section"
    ]
    assert "✏️ Edit" in pencils
    edit = _report_edit_modal("T1", "f3test", row)
    edit_ids = [
        e.get("action_id")
        for b in edit["blocks"]
        for e in b.get("elements") or []
    ]
    assert DUPLICATE_REPORT_ACTION_ID in edit_ids


def test_uniquify_and_duplicate_definition():
    from unittest.mock import MagicMock

    from schedule_schema import duplicate_definition, uniquify_definition_code

    cur = MagicMock()
    # First candidate taken, second free
    cur.fetchone.side_effect = [{"c": 1}, {"c": 0}, {"id": 99, "code": "kotter_copy_2", "name": "Kotter (copy)"}]
    code = uniquify_definition_code(cur, "pm", "f3", "kotter")
    assert code == "kotter_copy_2"

    cur2 = MagicMock()
    cur2.fetchone.side_effect = [
        {"c": 0},  # uniquify first try
        {
            "id": 7,
            "code": "kotter_copy",
            "name": "Kotter (copy)",
            "report_type": "kotter",
            "is_builtin": 0,
        },
    ]
    copy = duplicate_definition(
        cur2,
        "pm",
        "f3",
        {"code": "kotter", "name": "Kotter", "report_type": "kotter", "is_builtin": 1},
    )
    assert copy["code"] == "kotter_copy"
    assert copy["is_builtin"] == 0
    insert_sql = cur2.execute.call_args_list[1].args[0]
    assert "is_builtin" in insert_sql or "0,0" in str(cur2.execute.call_args_list[1])


def test_delete_definition_and_schedules():
    from unittest.mock import MagicMock

    from schedule_schema import delete_definition_and_schedules

    cur = MagicMock()
    cur.rowcount = 2
    # Two deletes; rowcount applies to last — simulate via side_effect on property is hard;
    # just assert both DELETEs ran.
    counts = delete_definition_and_schedules(cur, "pm", 5, "f3")
    assert len(cur.execute.call_args_list) == 2
    assert "region_schedules" in cur.execute.call_args_list[0].args[0]
    assert "region_report_definitions" in cur.execute.call_args_list[1].args[0]
    assert counts["schedules"] == 2
    assert counts["definitions"] == 2


def test_dispatch_passes_window_to_q_charts():
    from datetime import date
    from unittest.mock import MagicMock, patch

    from schedule_runner import _dispatch_report

    regional = MagicMock()
    registry = MagicMock()
    definition = {
        "report_type": "q_charts",
        "time_window_type": "last_month",
        "code": "q_charts",
        "name": "Q charts",
    }
    schedule = {
        "destination_type": "specific_channels",
        "destination_channels": '["C1"]',
        "destination_users": None,
    }
    region = {
        "schema_name": "f3ttown_test",
        "region": "Tulsa",
        "slack_token": "enc",
        "timezone": "America/Chicago",
    }
    captured = {}

    def fake_q(*args, **kwargs):
        captured["window"] = kwargs.get("window")
        return {"posted_channels": [{"ao": "x", "channel_id": "C1"}]}

    with (
        patch("schedule_runner.decrypt_field", return_value="tok"),
        patch("schedule_runner.connect_from_env", return_value=regional),
        patch(
            "schedule_runner.resolve_destinations",
            return_value=[{"kind": "channel", "id": "C1"}],
        ),
        patch(
            "schedule_runner.resolve_time_window",
            return_value=(date(2026, 6, 1), date(2026, 6, 30)),
        ),
        patch("monthly_charts.Qcharter.run_q_charter", side_effect=fake_q),
    ):
        _dispatch_report(registry, "paxminer", region, schedule, definition)

    assert captured["window"] == (date(2026, 6, 1), date(2026, 6, 30))


def test_ddl_includes_is_customized():
    from schedule_schema import DDL_REGION_REPORT_DEFINITIONS

    assert "is_customized" in DDL_REGION_REPORT_DEFINITIONS


def test_format_schedule_log_line_manual_vs_scheduled_and_dm_dest():
    from schedule_runner import format_schedule_log_line

    scheduled = format_schedule_log_line(
        "x",
        {
            "definition_name": "Achievement leaderboard",
            "ok": True,
            "specified_channels": ["C0APR1E1137"],
            "destination_type": "specific_channels",
            "message_count": 1,
            "duration_s": 0.4,
        },
    )
    assert scheduled.startswith("The *Achievement leaderboard* report was run as scheduled")
    assert "<@" not in scheduled
    assert "Destination(s): <#C0APR1E1137>" in scheduled
    assert "Status: success (0.4s)" in scheduled

    manual = format_schedule_log_line(
        "x",
        {
            "definition_name": "Achievement leaderboard",
            "ok": False,
            "error": "all channel uploads failed: not_in_channel",
            "notify_user": "UADMIN",
            "specified_channels": ["C0APR1E1137"],
            "message_count": 0,
            "duration_s": 1.2,
        },
    )
    assert "was run manually by `UADMIN`" in manual
    assert "<@UADMIN>" not in manual
    assert "Status: failed (1.2s)" in manual
    assert "all channel uploads failed: not_in_channel" in manual
    assert "Number of Messages: 0" in manual
    assert "Destination(s): none" in manual

    dm = format_schedule_log_line(
        "x",
        {
            "definition_name": "PAX charts (DM)",
            "ok": True,
            "destination_type": "dm_all_pax",
            "user_count": 12,
            "channel_count": 0,
            "duration_s": 8.5,
        },
    )
    assert "The *PAX charts (DM)* report was run as scheduled" in dm
    assert "Destination(s): DM to all PAX" in dm
    assert "specified PAX" not in dm
    assert "Number of Messages: 12" in dm
    assert "Status: success (8.5s)" in dm


def test_format_schedule_log_line_results_period_and_specific_pax():
    from schedule_runner import format_schedule_log_line

    awarded = format_schedule_log_line(
        "x",
        {
            "definition_name": "Award Achievements",
            "ok": True,
            "results_line": "14 rules, 0 granted, 0 revoked, 0 held",
            "period_start": "2026-01-01",
            "period_end": "2026-08-18",
            "message_count": 0,
            "duration_s": 0.6,
        },
    )
    assert "Results: 14 rules, 0 granted, 0 revoked, 0 held" in awarded
    assert "Period: 2026-01-01 to 2026-08-18" in awarded
    assert ".." not in awarded
    assert "Destination(s): none" in awarded

    named = format_schedule_log_line(
        "x",
        {
            "definition_name": "Kotter report",
            "ok": True,
            "message_count": 1,
            "destination_type": "specific_channels",
            "posted_channels": [{"channel_id": "C9"}],
        },
    )
    assert "The *Kotter* report was run as scheduled" in named
    assert "Kotter report report" not in named

    specific_dm = format_schedule_log_line(
        "x",
        {
            "definition_name": "PAX charts (DM)",
            "ok": True,
            "destination_type": "dm_specific_pax",
            "message_count": 2,
            "posted_users": [{"user_id": "U1", "pax": "Nacho"}, {"user_id": "U2", "pax": "Honey Badger"}],
        },
    )
    assert "Destination(s): DM to specific PAX (`Nacho` `Honey Badger`)" in specific_dm
    assert "<@" not in specific_dm


def test_format_schedule_summary_uses_destination_label():
    from scheduling import format_schedule_summary

    line = format_schedule_summary(
        {
            "id": 1,
            "destination_type": "dm_all_pax",
            "frequency_type": "monthly",
            "time_of_day": "07:00:00",
            "enabled": 1,
        },
        {"name": "PAX charts (DM)"},
    )
    assert "DM to all PAX" in line
    assert "dm_all_pax" not in line


def test_queue_achievement_backfill_serializes_dates():
    import json
    from datetime import date
    from unittest.mock import MagicMock, patch

    import slack_schedule

    mock_client = MagicMock()
    with patch.dict("os.environ", {"SCHEDULE_FUNCTION_NAME": "paxminer-test-schedule"}):
        with patch("boto3.client", return_value=mock_client):
            slack_schedule.queue_achievement_backfill(
                schema="f3test",
                achievement_id=4,
                actor="U1",
                start=date(2026, 3, 1),
                end=date(2026, 8, 18),
            )
    payload = json.loads(mock_client.invoke.call_args.kwargs["Payload"].decode("utf-8"))
    assert payload == {
        "source": "achievement_rule_backfill",
        "schema": "f3test",
        "achievement_id": 4,
        "actor": "U1",
        "start": "2026-03-01",
        "end": "2026-08-18",
    }

class _CatchApp:
    def __init__(self):
        self.actions = {}
        self.views = {}

    def action(self, action_id):
        def wrap(fn):
            self.actions[action_id] = fn
            return fn
        return wrap

    def view(self, callback_id):
        def wrap(fn):
            self.views[callback_id] = fn
            return fn
        return wrap


def _schedule_handlers():
    from slack_schedule import register_schedule_listeners

    app = _CatchApp()
    register_schedule_listeners(app)
    return app


def _modal_action_body(**extra):
    body = {
        "user": {"id": "U1"},
        "trigger_id": "trig",
        "view": {"id": "V1", "private_metadata": '{"team_id":"T1","regional_schema":"f3test"}'},
    }
    body.update(extra)
    return body


def test_add_edit_report_and_schedule_update_view_not_push():
    """S1: Add/Edit replace the current list view instead of pushing a third modal."""
    from config_schedule import (
        ADD_REPORT_ACTION_ID,
        ADD_SCHEDULE_ACTION_ID,
        EDIT_REPORT_ACTION_ID,
        EDIT_SCHEDULE_ACTION_ID,
    )
    from unittest.mock import MagicMock, patch

    os.environ.setdefault("PM_SLACK_TOKEN", "xoxb-test-token")
    os.environ.setdefault("PM_SLACK_SIGNING_SECRET", "test-signing-secret-16")

    app = _schedule_handlers()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    region = {"region": "t", "schema_name": "f3test", "timezone": "America/Chicago"}
    report_body = _modal_action_body()
    report_body["view"]["state"] = {
        "values": {
            "report_pick": {
                "paxminer_report_select": {"selected_option": {"value": "9"}}
            }
        }
    }
    schedule_body = _modal_action_body()
    schedule_body["view"]["state"] = {
        "values": {
            "schedule_pick": {
                "paxminer_schedule_select": {"selected_option": {"value": "4"}}
            }
        }
    }

    with patch("slack_schedule.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", region),
        ):
            with patch("slack_schedule.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch("slack_schedule.load_definitions", return_value=[]):
                    app.actions[ADD_REPORT_ACTION_ID](ack, report_body, client, logger)
                    app.actions[ADD_SCHEDULE_ACTION_ID](ack, schedule_body, client, logger)
                with patch(
                    "slack_schedule.load_definition",
                    return_value={"id": 9, "code": "pax", "name": "PAX"},
                ):
                    app.actions[EDIT_REPORT_ACTION_ID](ack, report_body, client, logger)
                with patch(
                    "slack_schedule.load_schedule",
                    return_value={"id": 4, "report_definition_id": 9},
                ):
                    with patch("slack_schedule.load_definitions", return_value=[]):
                        app.actions[EDIT_SCHEDULE_ACTION_ID](ack, schedule_body, client, logger)

    client.views_push.assert_not_called()
    assert client.views_update.call_count == 4
    callbacks = [c.kwargs["view"]["callback_id"] for c in client.views_update.call_args_list]
    assert callbacks[0] == "paxminer-report-edit-id"
    assert callbacks[1] == "paxminer-schedule-edit-id"
    assert callbacks[2] == "paxminer-report-edit-id"
    assert callbacks[3] == "paxminer-schedule-edit-id"


def test_duplicate_report_opens_add_form_and_does_not_push():
    from config_schedule import DUPLICATE_REPORT_ACTION_ID
    from unittest.mock import MagicMock, patch

    os.environ.setdefault("PM_SLACK_TOKEN", "xoxb-test-token")
    os.environ.setdefault("PM_SLACK_SIGNING_SECRET", "test-signing-secret-16")

    app = _schedule_handlers()
    ack = MagicMock()
    client = MagicMock()
    logger = MagicMock()
    body = _modal_action_body()
    body["actions"] = [{"action_id": DUPLICATE_REPORT_ACTION_ID, "value": "9"}]
    region = {"region": "t", "schema_name": "f3test"}
    with patch("slack_schedule.is_slack_admin", return_value=True):
        with patch(
            "slack_app._region_context_from_body",
            return_value=("T1", "f3test", region),
        ):
            with patch("slack_schedule.connect_from_env") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.cursor.return_value.__enter__.return_value = mock_cur
                mock_conn.return_value.cursor.return_value.__exit__.return_value = False
                with patch(
                    "slack_schedule.load_definition",
                    return_value={"id": 9, "code": "pax", "name": "PAX", "report_type": "pax_charts"},
                ):
                    with patch(
                        "slack_schedule.duplicate_report_draft",
                        return_value={"code": "pax_copy", "name": "PAX_copy", "report_type": "pax_charts"},
                    ) as draft_fn:
                        app.actions[DUPLICATE_REPORT_ACTION_ID](
                            ack, body, client, logger
                        )
    draft_fn.assert_called_once()
    client.views_push.assert_not_called()
    client.views_update.assert_called_once()
    view = client.views_update.call_args.kwargs["view"]
    assert view["callback_id"] == "paxminer-report-edit-id"
    assert "Add" in view["title"]["text"]


def test_reports_and_achievements_lists_paginate():
    from config_paxminer import ACHIEVEMENTS_PAGE_NEXT_ACTION_ID, _achievements_list_modal
    from config_schedule import PAGE_SIZE, REPORTS_PAGE_NEXT_ACTION_ID, _reports_list_modal

    defs = [
        {"id": i, "name": f"Report {i}", "code": f"r{i}", "report_type": "custom_report", "is_builtin": 0}
        for i in range(1, PAGE_SIZE + 2)
    ]
    page0 = _reports_list_modal("T1", "f3test", defs, page=0)
    page1 = _reports_list_modal("T1", "f3test", defs, page=1)
    p0_ids = [
        (b.get("accessory") or {}).get("value")
        for b in page0["blocks"]
        if (b.get("accessory") or {}).get("action_id")
    ]
    p1_ids = [
        (b.get("accessory") or {}).get("value")
        for b in page1["blocks"]
        if (b.get("accessory") or {}).get("action_id")
    ]
    assert "1" in p0_ids
    assert str(PAGE_SIZE + 1) in p1_ids
    assert any(
        e.get("action_id") == REPORTS_PAGE_NEXT_ACTION_ID
        for b in page0["blocks"]
        for e in b.get("elements") or []
    )

    ach = [
        {
            "id": i,
            "name": f"A{i}",
            "code": f"a{i}",
            "enabled": 1,
            "period": "month",
            "metric": "qs",
            "threshold": 6,
        }
        for i in range(1, PAGE_SIZE + 2)
    ]
    a0 = _achievements_list_modal("T1", "f3test", ach, page=0)
    assert any(
        e.get("action_id") == ACHIEVEMENTS_PAGE_NEXT_ACTION_ID
        for b in a0["blocks"]
        for e in b.get("elements") or []
    )
    assert any(
        e.get("action_id") == "paxminer_achievement_backfill"
        for b in a0["blocks"]
        for e in b.get("elements") or []
    )
    a1 = _achievements_list_modal("T1", "f3test", ach, page=1)
    assert any(
        e.get("action_id") == "paxminer_achievement_backfill"
        for b in a1["blocks"]
        for e in b.get("elements") or []
    )


def test_achievement_defaults_json_matches_seeds():
    import json
    from pathlib import Path

    from achievements.achievement_rules import ACHIEVEMENT_SEEDS

    path = Path(__file__).resolve().parent.parent / "achievement_defaults.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert catalog == ACHIEVEMENT_SEEDS
    assert len(catalog) == 14
    assert {s["code"] for s in catalog} == {s["code"] for s in ACHIEVEMENT_SEEDS}


def test_this_month_and_report_title_contract():
    from scheduling import (
        REPORT_TEMPLATES,
        format_report_title,
        template_has,
    )

    assert "this_month" in __import__("scheduling", fromlist=["TIME_WINDOW_TYPES"]).TIME_WINDOW_TYPES
    assert "achievement_almost_there" in REPORT_TEMPLATES
    assert template_has("achievement_leaderboard", "window")
    assert not template_has("achievement_almost_there", "window")
    assert template_has("achievement_almost_there", "top_n")
    heading = format_report_title("Achievement leaderboard", (date(2026, 8, 1), date(2026, 8, 18)))
    assert "Achievement leaderboard" in heading
    assert "YTD" not in heading
    assert "YTD" not in format_report_title("Region leaderboard", (date(2026, 7, 1), date(2026, 7, 31)))


def test_region_leaderboard_drops_bonus_ytd_and_honors_top_n():
    import inspect

    from monthly_charts.Leaderboard_Charter import run_region_leaderboard
    from schedule_schema import upsert_builtin_definitions

    src = inspect.getsource(run_region_leaderboard)
    assert "include_ytd = False" in src
    assert "top_n" in src
    assert "title" in src
    assert "top_n" in inspect.getsource(upsert_builtin_definitions)



def test_pax_chart_dm_text_mentions_slack_user():
    from monthly_charts.PAXcharter import pax_chart_dm_text

    text = pax_chart_dm_text("U01ABCDEF12", "Nacho", None, "July 2026")
    assert text.startswith("Hey <@U01ABCDEF12>")
    assert "July 2026" in text
