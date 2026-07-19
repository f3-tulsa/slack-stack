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


def test_schedules_list_preserves_selected_option():
    from config_schedule import SELECT_SCHEDULE_ACTION_ID, _schedules_list_modal

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
    view = _schedules_list_modal(
        "T1", "f3test", schedules, selected_schedule_id=9
    )
    pick = next(b for b in view["blocks"] if b.get("block_id") == "schedule_pick")
    assert pick["element"]["action_id"] == SELECT_SCHEDULE_ACTION_ID
    assert pick["element"]["initial_option"]["value"] == "9"
    assert "last run: skipped" in view["blocks"][1]["text"]["text"]


def test_post_log_swallows_client_errors():
    from unittest.mock import MagicMock, patch

    from slack_util import post_log

    client = MagicMock()
    with patch("slack_util.post_message", side_effect=RuntimeError("boom")):
        post_log(client, "- Schedule (test): FAILED - boom")  # must not raise


def test_format_run_result_variants():
    from schedule_runner import format_run_result

    text, _ = format_run_result(
        {"schedule_id": 1, "report_type": "kotter", "ok": True, "channel_count": 2, "duration_s": 1.5}
    )
    assert "success" in text and "2 channel" in text

    text, _ = format_run_result(
        {"schedule_id": 2, "report_type": "kotter", "ok": True, "skipped": "no destinations configured"}
    )
    assert "skipped" in text and "no destinations" in text

    text, _ = format_run_result(
        {"schedule_id": 3, "report_type": "kotter", "ok": False, "error": "boom"}
    )
    assert "failed" in text and "boom" in text


def test_format_schedule_log_line_variants():
    from schedule_runner import format_schedule_log_line

    line = format_schedule_log_line(
        "Tulsa",
        {
            "schedule_id": 1,
            "report_type": "kotter",
            "ok": True,
            "channel_count": 2,
            "duration_s": 1.5,
        },
    )
    assert line.startswith("- Schedule (Tulsa) #1 (kotter): success")
    assert "2 channel(s)" in line

    line = format_schedule_log_line(
        "Tulsa",
        {
            "schedule_id": 2,
            "report_type": "kotter",
            "ok": True,
            "skipped": "no destinations configured",
        },
    )
    assert "skipped - no destinations configured" in line

    line = format_schedule_log_line(
        "Tulsa",
        {"schedule_id": 3, "report_type": "kotter", "ok": False, "error": "boom"},
    )
    assert "FAILED - boom" in line


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
    mock_log.assert_not_called()


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
                    with patch("schedule_runner.notify_run_result") as mock_notify:
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
    mock_notify.assert_called_once()
    assert mock_notify.call_args.args[1] == "U9"
    assert mock_notify.call_args.args[2] == result


def test_report_defaults_json_consistency():
    from scheduling import BUILTIN_DEFINITIONS, DEFAULT_SCHEDULES, VALID_DESTINATIONS

    codes = {d["code"] for d in BUILTIN_DEFINITIONS}
    assert len(BUILTIN_DEFINITIONS) == 6
    assert len(DEFAULT_SCHEDULES) == 6
    assert {s["code"] for s in DEFAULT_SCHEDULES} == codes
    for s in DEFAULT_SCHEDULES:
        assert s.get("enabled") is True
        defn = next(d for d in BUILTIN_DEFINITIONS if d["code"] == s["code"])
        assert s["destination_type"] in VALID_DESTINATIONS[defn["report_type"]]


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
        *[{"id": code_ids[d["code"]]} for d in __import__("scheduling").BUILTIN_DEFINITIONS],
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
