"""Unit tests for pure scheduling helpers (no DB / pandas)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

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


def test_already_ran_successfully():
    assert already_ran_successfully(
        {"last_run_on": date(2026, 7, 19), "last_run_status": "success"},
        date(2026, 7, 19),
    )
    assert not already_ran_successfully(
        {"last_run_on": date(2026, 7, 19), "last_run_status": "error"},
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


def test_destination_constraints():
    assert destination_valid_for_report("pax_charts", "dm_all_pax")
    assert not destination_valid_for_report("pax_charts", "all_ao_channels")
    assert destination_valid_for_report("kotter", "specific_channels")


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


def test_reports_list_and_edit_modals_have_submit():
    from config_schedule import _report_edit_modal, _reports_list_modal, _schedules_list_modal

    for view in (
        _reports_list_modal("T1", "f3test", []),
        _report_edit_modal("T1", "f3test", None),
        _schedules_list_modal("T1", "f3test", []),
    ):
        assert view.get("type") == "modal"
        assert view.get("submit")
