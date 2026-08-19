"""Unit tests for achievement effective-date range modes and the re-eval lock."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from achievements.range import (
    RANGE_ALL_ATTENDANCE,
    RANGE_CUSTOM,
    RANGE_FROM_CREATED,
    RANGE_SINCE_RULES_CHANGED,
    REEVAL_STALE_AFTER,
    backfill_range_mode,
    ensure_achievement_range_columns,
    normalize_range_mode,
    range_validation_errors,
    resolve_stored_range,
    should_auto_queue,
    try_acquire_reeval_lock,
    window_narrowed,
)


def test_normalize_legacy_and_null_modes():
    assert normalize_range_mode("going_forward") == RANGE_FROM_CREATED
    assert normalize_range_mode("all_previous") == RANGE_ALL_ATTENDANCE
    assert normalize_range_mode(None, effective_from=None) == RANGE_ALL_ATTENDANCE
    assert normalize_range_mode(None, effective_from="2026-03-01") == RANGE_CUSTOM


def test_resolve_four_modes():
    mode, start, end = resolve_stored_range(
        {"range_mode": RANGE_FROM_CREATED, "no_end_date": True},
        first_created="2026-01-15",
        version_created="2026-02-01",
    )
    assert (mode, start, end) == (RANGE_FROM_CREATED, "2026-01-15", None)

    mode, start, end = resolve_stored_range(
        {"range_mode": RANGE_SINCE_RULES_CHANGED, "no_end_date": True},
        first_created="2026-01-15",
        version_created="2026-04-01",
        minting=True,
        today="2026-08-19",
    )
    assert (mode, start, end) == (RANGE_SINCE_RULES_CHANGED, "2026-08-19", None)

    mode, start, end = resolve_stored_range(
        {"range_mode": RANGE_ALL_ATTENDANCE, "no_end_date": True},
        first_created="2026-01-15",
        version_created="2026-04-01",
    )
    assert (mode, start, end) == (RANGE_ALL_ATTENDANCE, None, None)

    mode, start, end = resolve_stored_range(
        {
            "range_mode": RANGE_CUSTOM,
            "effective_from": "2026-02-01",
            "effective_to": "2026-12-31",
            "no_end_date": False,
        }
    )
    assert (mode, start, end) == (RANGE_CUSTOM, "2026-02-01", "2026-12-31")


def test_custom_requires_start_and_non_custom_rejects_mismatch():
    errors = range_validation_errors(
        {"range_mode": RANGE_CUSTOM, "no_end_date": True, "effective_from": None}
    )
    assert "effective_from" in errors
    errors = range_validation_errors(
        {
            "range_mode": RANGE_FROM_CREATED,
            "no_end_date": True,
            "effective_from": "2026-06-01",
        },
        first_created="2026-01-01",
    )
    assert "effective_from" in errors


def test_window_narrowed_and_auto_queue_rules():
    assert window_narrowed(None, None, "2026-01-01", None) is True
    assert window_narrowed("2026-01-01", None, None, None) is False
    assert should_auto_queue(
        is_new=True, params_changed=False, range_changed=False, mode=RANGE_FROM_CREATED
    ) is False
    assert should_auto_queue(
        is_new=True, params_changed=False, range_changed=False, mode=RANGE_ALL_ATTENDANCE
    ) is True
    assert should_auto_queue(
        is_new=False, params_changed=False, range_changed=False, mode=RANGE_CUSTOM
    ) is False
    assert should_auto_queue(
        is_new=False, params_changed=True, range_changed=False, mode=RANGE_FROM_CREATED
    ) is True


def test_backfill_range_mode_sql_and_idempotent_rerun():
    cur = MagicMock()
    cur.rowcount = 4
    assert backfill_range_mode(cur, "f3test") == 4
    sql = cur.execute.call_args[0][0]
    assert "range_mode IS NULL" in sql
    assert RANGE_ALL_ATTENDANCE in cur.execute.call_args[0][1]
    assert RANGE_CUSTOM in cur.execute.call_args[0][1]
    cur.rowcount = 0
    assert backfill_range_mode(cur, "f3test") == 0


def test_ensure_columns_alters_when_missing():
    cur = MagicMock()
    cur.fetchone.return_value = {"c": 0}
    added = ensure_achievement_range_columns(cur, "f3test")
    assert added["range_mode"] is True
    assert added["reeval_queued_at"] is True
    joined = " ".join(str(c) for c in cur.execute.call_args_list)
    assert "ADD COLUMN `range_mode`" in joined
    assert "ADD COLUMN `reeval_queued_at`" in joined


def test_try_acquire_rejects_fresh_lock_and_overrides_stale():
    cur = MagicMock()
    now = datetime(2026, 8, 19, 12, 0, 0)
    cur.fetchone.return_value = {"reeval_queued_at": now - timedelta(minutes=5)}
    ok, msg = try_acquire_reeval_lock(cur, "f3test", 3, now=now)
    assert ok is False
    assert "already running" in (msg or "")

    cur.fetchone.return_value = {
        "reeval_queued_at": now - REEVAL_STALE_AFTER - timedelta(minutes=1)
    }
    ok, msg = try_acquire_reeval_lock(cur, "f3test", 3, now=now)
    assert ok is True
    assert msg is None
