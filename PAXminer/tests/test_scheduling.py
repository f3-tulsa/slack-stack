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
    assert mock_run.call_args.kwargs["regional_schema"] == "f3test"
    assert result.get("channel_count") == 1 or result.get("posted_channels")


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
        "f3ttown_test",
        {
            "schedule_id": 1,
            "report_type": "kotter",
            "ok": True,
            "channel_count": 2,
            "duration_s": 1.5,
        },
    )
    assert line.startswith("- Schedule (f3ttown_test) #1 (kotter): success")
    assert "2 channel(s)" in line

    line = format_schedule_log_line(
        "f3ttown_test",
        {
            "schedule_id": 2,
            "report_type": "kotter",
            "ok": True,
            "skipped": "no destinations configured",
        },
    )
    assert "skipped - no destinations configured" in line

    line = format_schedule_log_line(
        "f3ttown_test",
        {"schedule_id": 3, "report_type": "kotter", "ok": False, "error": "boom"},
    )
    assert "FAILED - boom" in line


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
    assert log_lines[0].startswith("- Schedule (f3ttown_test) #1 (kotter)")
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
    assert len(BUILTIN_DEFINITIONS) == 7
    assert len(DEFAULT_SCHEDULES) == 7
    assert "award_achievements" in codes
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
        with patch(
            "schedule_schema.upsert_builtin_definitions",
            return_value={"award_achievements": 42},
        ):
            cur.fetchone.return_value = {"c": 0}
            first = backfill_award_achievements_schedules(cur, "paxminer_test")
            assert first["schedules_inserted"] == 1
            insert_calls = [
                c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0])
            ]
            assert len(insert_calls) == 1
            args = insert_calls[0].args[1]
            assert args[2] == "specific_channels"
            assert "C_ACH" in args[3]
            assert args[4] == "daily"

            cur.reset_mock()
            cur.fetchall.return_value = [region]
            cur.fetchone.return_value = {"c": 1}
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
    assert "posted: The Fort (C111)" in text
    assert "failed: Brickyard (C222) - not_in_channel" in text


def test_format_schedule_log_line_includes_destinations():
    from schedule_runner import format_schedule_log_line

    line = format_schedule_log_line(
        "f3ttown_test",
        {
            "schedule_id": 4,
            "report_type": "q_charts",
            "ok": False,
            "error": "all channel uploads failed",
            "posted_channels": [],
            "failed_channels": [{"ao": "AO1", "channel_id": "C1", "reason": "missing_scope"}],
        },
    )
    assert "FAILED" in line
    assert "failed: AO1 (C1) - missing_scope" in line


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

    # last_month default matches calendar prior month
    from datetime import datetime, timezone

    fixed = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    w = resolve_time_window(
        {"time_window_type": "last_month"},
        timezone_name="America/Chicago",
        utc_now=fixed,
    )
    assert w == (date(2026, 6, 1), date(2026, 6, 30))


def test_reports_list_empty_shows_load_defaults():
    from config_schedule import LOAD_DEFAULTS_ACTION_ID, _reports_list_modal, _schedules_list_modal

    reports = _reports_list_modal("T1", "f3test", [])
    actions = [b for b in reports["blocks"] if b.get("type") == "actions"]
    assert any(
        LOAD_DEFAULTS_ACTION_ID in [e.get("action_id") for e in a.get("elements", [])]
        for a in actions
    )

    schedules = _schedules_list_modal("T1", "f3test", [])
    actions = [b for b in schedules["blocks"] if b.get("type") == "actions"]
    assert any(
        LOAD_DEFAULTS_ACTION_ID in [e.get("action_id") for e in a.get("elements", [])]
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


def test_reports_list_has_duplicate_action():
    from config_schedule import DUPLICATE_REPORT_ACTION_ID, _reports_list_modal

    view = _reports_list_modal(
        "T1",
        "f3test",
        [
            {
                "id": 1,
                "name": "Kotter",
                "code": "kotter",
                "report_type": "kotter",
                "is_builtin": 1,
            }
        ],
    )
    action_ids = []
    for b in view["blocks"]:
        for e in b.get("elements") or []:
            if e.get("action_id"):
                action_ids.append(e["action_id"])
    assert DUPLICATE_REPORT_ACTION_ID in action_ids


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
