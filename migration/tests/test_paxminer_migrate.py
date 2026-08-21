"""Unit tests for paxminer_migrate orchestrator."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_MIGRATION_DIR = Path(__file__).resolve().parent.parent
_REPO = _MIGRATION_DIR.parent
sys.path.insert(0, str(_MIGRATION_DIR))
sys.path.insert(0, str(_REPO / "PAXminer"))

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")
os.environ.setdefault("TARGET_HOST", "localhost")
os.environ.setdefault("TARGET_USER", "test")
os.environ.setdefault("TARGET_PASSWORD", "test")

from paxminer_phases.drop_legacy import DROPPED_COLUMNS  # noqa: E402
from paxminer_phases.weaselbot import PM_REGION_COLS  # noqa: E402


@pytest.fixture
def mock_connect():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    with patch("paxminer_migrate._connect", return_value=conn):
        yield conn, cursor


def test_pm_region_cols_exclude_dropped_columns():
    overlap = set(PM_REGION_COLS) & set(DROPPED_COLUMNS)
    assert not overlap, f"PM_REGION_COLS must not include dropped columns: {overlap}"


def test_all_runs_phases_in_order(mock_connect):
    conn, _cursor = mock_connect
    call_order: list[str] = []

    def _weaselbot(cur, stage, *, force=False, drop_weaselbot_schema=False):
        call_order.append("weaselbot")
        return {"pm_columns_added": []}

    def _scheduler(cur, stage):
        call_order.append("scheduler")
        return {
            "timezone_added": False,
            "tables_created": [],
            "is_customized_added": False,
            "defaults_seeded": False,
        }

    def _achievements(cur, stage):
        call_order.append("achievements")
        return {"regional_schemas": 0, "versions_seeded": 0}

    def _drop_legacy(cur, stage):
        call_order.append("drop-legacy-columns")
        return {"dropped": [], "skipped": list(DROPPED_COLUMNS)}

    with (
        patch("paxminer_migrate.run_weaselbot", side_effect=_weaselbot),
        patch("paxminer_migrate.run_scheduler", side_effect=_scheduler),
        patch("paxminer_migrate.run_achievements", side_effect=_achievements),
        patch("paxminer_migrate.run_drop_legacy_columns", side_effect=_drop_legacy),
        patch("paxminer_migrate._load_env"),
        patch("paxminer_migrate._write_receipt", return_value=Path("/tmp/receipt.txt")),
    ):
        from paxminer_migrate import main

        rc = main(["--env", "test", "--all"])

    assert rc == 0
    assert call_order == ["weaselbot", "scheduler", "achievements", "drop-legacy-columns"]
    assert conn.commit.call_count == 4
    conn.close.assert_called_once()


def test_scheduler_phase_does_not_seed_defaults(mock_connect):
    """Migration DDL only — report_defaults.json loads via Slack Load/Restore."""
    _conn, cursor = mock_connect
    cursor.fetchone.return_value = {"c": 1}  # columns/tables already exist

    with (
        patch("schedule_schema.seed_all_regions") as seed,
        patch(
            "paxminer_phases.scheduler.backfill_award_achievements_schedules",
            return_value={"definitions_ensured": 0, "schedules_inserted": 0, "skipped": 0},
        ) as backfill,
        patch("paxminer_phases.scheduler.ensure_last_run_at_column", return_value=False),
    ):
        from paxminer_phases.scheduler import run_scheduler

        result = run_scheduler(cursor, "test")

    seed.assert_not_called()
    backfill.assert_called_once()
    assert result["defaults_seeded"] is False
    assert "Load/Restore" in result["note"] or "defaults" in result["note"].lower()
    assert "is_customized_added" in result
    assert "last_run_at_added" in result
    assert "award_achievements_backfill" in result


def test_all_stops_on_first_failure(mock_connect):
    conn, _cursor = mock_connect
    call_order: list[str] = []

    def _weaselbot(cur, stage, *, force=False, drop_weaselbot_schema=False):
        call_order.append("weaselbot")
        return {"pm_columns_added": []}

    def _scheduler(cur, stage):
        call_order.append("scheduler")
        raise RuntimeError("scheduler boom")

    with (
        patch("paxminer_migrate.run_weaselbot", side_effect=_weaselbot),
        patch("paxminer_migrate.run_scheduler", side_effect=_scheduler),
        patch("paxminer_migrate.run_achievements"),
        patch("paxminer_migrate.run_drop_legacy_columns"),
        patch("paxminer_migrate._load_env"),
        patch("paxminer_migrate._write_receipt", return_value=Path("/tmp/receipt.txt")),
    ):
        from paxminer_migrate import main

        rc = main(["--env", "test", "--all"])

    assert rc == 1
    assert call_order == ["weaselbot", "scheduler"]
    assert conn.commit.call_count == 1
    conn.rollback.assert_called_once()


def test_single_phase_weaselbot(mock_connect):
    conn, _cursor = mock_connect

    with (
        patch("paxminer_migrate.run_weaselbot", return_value={"pm_columns_added": ["send_achievements"]}) as run_wb,
        patch("paxminer_migrate.run_scheduler") as run_sched,
        patch("paxminer_migrate.run_drop_legacy_columns") as run_drop,
        patch("paxminer_migrate._load_env"),
        patch("paxminer_migrate._write_receipt", return_value=Path("/tmp/receipt.txt")),
    ):
        from paxminer_migrate import main

        rc = main(["--env", "test", "--phase", "weaselbot", "--force", "--drop-weaselbot-schema"])

    assert rc == 0
    run_wb.assert_called_once()
    _cur, stage = run_wb.call_args[0]
    assert stage == "test"
    assert run_wb.call_args.kwargs == {"force": True, "drop_weaselbot_schema": True}
    run_sched.assert_not_called()
    run_drop.assert_not_called()
    conn.commit.assert_called_once()


def test_achievements_phase_seeds_version_one_and_is_idempotent():
    from paxminer_phases.achievements import _seed_version_1

    cur = MagicMock()
    family = {
        "id": 42,
        "code": "six_pack",
        "metric": "posts",
        "activity": "beatdown",
        "period": "week",
        "threshold": 6,
    }
    cur.fetchall.return_value = [family]
    cur.fetchone.return_value = None
    cur.rowcount = 1
    first = _seed_version_1(cur, "f3test")
    assert first == 1
    insert = next(c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0]))
    assert insert.args[1][0] == 42
    assert insert.args[1][3] is None
    assert "WHERE NOT EXISTS" in insert.args[0]

    cur.reset_mock()
    cur.fetchall.return_value = [family]
    cur.fetchone.return_value = {"1": 1}
    assert _seed_version_1(cur, "f3test") == 0
    assert not any("INSERT INTO" in str(c.args[0]) for c in cur.execute.call_args_list)
    assert not any(
        c.args and "UPDATE" in str(c.args[0]) and "achievements_list" in str(c.args[0])
        for c in cur.execute.call_args_list
    )


def test_seed_version_1_writes_spec_json_for_catalog_codes():
    from paxminer_phases.achievements import _seed_version_1

    cur = MagicMock()
    cur.fetchall.return_value = [
        {
            "id": 7,
            "code": "the_priest",
            "metric": "posts",
            "activity": "qsource",
            "period": "year",
            "threshold": 25,
        }
    ]
    cur.fetchone.return_value = None
    cur.rowcount = 1
    assert _seed_version_1(cur, "f3test") == 1
    insert = next(c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0]))
    payload = insert.args[1][3]
    assert payload == '["QSource"]'
    assert not payload.startswith("qsource")


def test_seed_version_1_uses_catalog_rules_for_builtin_alter_defaults():
    from paxminer_phases.achievements import _seed_version_1

    cur = MagicMock()
    cur.fetchall.return_value = [
        {
            "id": 7,
            "code": "the_priest",
            "metric": "posts",
            "activity": "beatdown",
            "period": "year",
            "threshold": 1,
        }
    ]
    cur.fetchone.return_value = None
    cur.rowcount = 1
    assert _seed_version_1(cur, "f3test") == 1
    insert = next(c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0]))
    assert insert.args[1][2] == "posts"
    assert insert.args[1][3] == '["QSource"]'
    assert insert.args[1][4] == "year"
    assert insert.args[1][5] == 25
    update = next(
        c
        for c in cur.execute.call_args_list
        if c.args and "UPDATE" in str(c.args[0]) and "achievements_list" in str(c.args[0])
    )
    assert update.args[1][0] == "posts"
    assert update.args[1][1] == "qsource"
    assert update.args[1][2] == "year"
    assert update.args[1][3] == 25


def test_seed_version_1_non_catalog_derives_from_stored_row():
    from paxminer_phases.achievements import _seed_version_1

    cur = MagicMock()
    cur.fetchall.return_value = [
        {
            "id": 9,
            "code": "custom_ao",
            "metric": "qs",
            "activity": "Bootcamp",
            "period": "month",
            "threshold": 4,
        }
    ]
    cur.fetchone.return_value = None
    cur.rowcount = 1
    assert _seed_version_1(cur, "f3test") == 1
    insert = next(c for c in cur.execute.call_args_list if "INSERT INTO" in str(c.args[0]))
    assert insert.args[1][2] == "qs"
    assert insert.args[1][3] == '["Bootcamp"]'
    assert insert.args[1][4] == "month"
    assert insert.args[1][5] == 4
    assert not any(
        c.args and "UPDATE" in str(c.args[0]) and "achievements_list" in str(c.args[0])
        for c in cur.execute.call_args_list
    )


def test_achievements_phase_classifies_null_activity_types():
    from datetime import date

    from paxminer_phases.achievements import _classify_activity_types

    cur = MagicMock()
    cur.fetchall.side_effect = [
        [
            {
                "ao_id": "C1",
                "bd_date": date(2026, 1, 1),
                "q_user_id": "U1",
                "backblast": "QSource lesson",
                "json": None,
                "ao": "the-goose",
            }
        ],
        [],
    ]
    cur.rowcount = 1
    with patch("paxminer_phases.achievements._column_exists", return_value=True):
        updated = _classify_activity_types(cur, "f3test")
    assert updated == 1
    update = next(c for c in cur.execute.call_args_list if "SET activity_type" in str(c.args[0]))
    assert update.args[1][0] == "qsource"


def test_achievements_ddl_stays_additive_for_slackblast_orm():
    from achievements.achievement_rules import (
        ACHIEVEMENTS_AWARDED_DDL,
        ACHIEVEMENTS_LIST_DDL,
        AWARDED_PERIOD_COLUMNS,
    )

    for col in (
        "id",
        "name",
        "description",
        "verb",
        "code",
        "metric",
        "activity",
        "period",
        "threshold",
    ):
        assert f"`{col}`" in ACHIEVEMENTS_LIST_DDL
    assert "`enabled`" in ACHIEVEMENTS_LIST_DDL
    for col in ("id", "achievement_id", "pax_id", "date_awarded", "created", "updated"):
        assert f"`{col}`" in ACHIEVEMENTS_AWARDED_DDL
    for col in AWARDED_PERIOD_COLUMNS:
        assert f"`{col}`" in ACHIEVEMENTS_AWARDED_DDL
    assert "UNIQUE KEY `uniq_award_period`" in ACHIEVEMENTS_AWARDED_DDL
    sql_path = (
        _REPO
        / "slackblast"
        / "slackblast"
        / "utilities"
        / "database"
        / "create_clear_local_db.sql"
    )
    local = sql_path.read_text(encoding="utf-8")
    beatdowns = local.split("CREATE TABLE f3devregion.`beatdowns`")[1].split("CREATE TABLE")[0]
    assert "activity_type" not in beatdowns


def test_award_unique_dedupes_keeping_lowest_id(caplog):
    from paxminer_phases.achievements import _enforce_award_period_unique

    cur = MagicMock()
    cur.fetchone.return_value = {"c": 0}
    cur.rowcount = 2

    def index_exists(_c, _s, _t, name):
        return name == "awarded_period_lookup"

    with (
        patch("paxminer_phases.achievements._index_exists", side_effect=index_exists),
        caplog.at_level(logging.INFO),
    ):
        result = _enforce_award_period_unique(cur, "f3test")

    sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
    delete_sql = next(s for s in sqls if "DELETE a FROM" in s)
    assert "a.id > b.id" in delete_sql
    assert "a.period_key = b.period_key" in delete_sql
    alter_sql = next(s for s in sqls if "ADD UNIQUE KEY" in s)
    assert "`uniq_award_period`" in alter_sql
    assert "`achievement_id`, `pax_id`, `period_key`" in alter_sql
    drop_sql = next(s for s in sqls if "DROP KEY" in s)
    assert "awarded_period_lookup" in drop_sql
    assert result["duplicates_deleted"] == 2
    assert result["unique_added"] is True
    assert result["null_period_key"] == 0
    assert "Deleted 2 duplicate award row(s)" in caplog.text


def test_award_unique_alter_skipped_when_key_exists():
    from paxminer_phases.achievements import _enforce_award_period_unique

    cur = MagicMock()
    with patch("paxminer_phases.achievements._index_exists", return_value=True):
        result = _enforce_award_period_unique(cur, "f3test")

    assert result["unique_already_present"] is True
    assert result["unique_added"] is False
    cur.execute.assert_not_called()


def test_award_unique_surfaces_null_period_key(caplog):
    from paxminer_phases.achievements import _enforce_award_period_unique

    cur = MagicMock()
    cur.fetchone.return_value = {"c": 4}
    cur.rowcount = 1

    with (
        patch("paxminer_phases.achievements._index_exists", return_value=False),
        caplog.at_level(logging.WARNING),
    ):
        result = _enforce_award_period_unique(cur, "f3test")

    sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
    assert any("period_key IS NULL" in s for s in sqls)
    assert any("DELETE a FROM" in s for s in sqls)
    assert not any("ADD UNIQUE KEY" in s for s in sqls)
    assert result["null_period_key"] == 4
    assert result["duplicates_deleted"] == 1
    assert result["unique_added"] is False
    assert "NULL period_key" in caplog.text


def test_weaselbot_seed_update_is_cosmetic_only():
    from paxminer_phases.weaselbot import ensure_regional_achievements

    cur = MagicMock()
    cur.fetchone.side_effect = [{"id": 1}] * 20
    with (
        patch("paxminer_phases.weaselbot._column_exists", return_value=True),
        patch("paxminer_phases.weaselbot.ACHIEVEMENTS_VIEW_DDL", "SELECT 1"),
    ):
        ensure_regional_achievements(cur, "f3test", upsert_seeds=True)
    updates = [
        str(c.args[0])
        for c in cur.execute.call_args_list
        if c.args and str(c.args[0]).lstrip().startswith("UPDATE")
    ]
    assert updates
    assert "SET name=%s, description=%s, verb=%s" in updates[0]
    assert "metric=%s" not in updates[0]


def test_weaselbot_seed_insert_writes_varchar_mirror():
    from paxminer_phases.weaselbot import ensure_regional_achievements

    cur = MagicMock()
    cur.fetchone.return_value = None
    with (
        patch("paxminer_phases.weaselbot._column_exists", return_value=True),
        patch("paxminer_phases.weaselbot.ACHIEVEMENTS_VIEW_DDL", "SELECT 1"),
    ):
        ensure_regional_achievements(cur, "f3test", upsert_seeds=True)
    inserts = [
        c
        for c in cur.execute.call_args_list
        if c.args
        and "INSERT INTO" in str(c.args[0])
        and "achievements_list" in str(c.args[0])
    ]
    assert inserts
    for call in inserts:
        activity = call.args[1][5]
        assert isinstance(activity, str)
        assert not isinstance(activity, dict)


def test_achievements_phase_adds_range_mode_and_backfills():
    from paxminer_phases import achievements as ach

    cur = MagicMock()
    with (
        patch.object(ach, "_column_exists", return_value=False),
        patch.object(ach, "_index_exists", return_value=True),
        patch.object(ach, "_table_exists", return_value=True),
        patch.object(ach, "_seed_version_1", return_value=0),
        patch.object(ach, "_backfill_award_periods", return_value=0),
        patch.object(
            ach,
            "_enforce_award_period_unique",
            return_value={
                "duplicates_deleted": 0,
                "null_period_key": 0,
                "unique_added": False,
                "unique_already_present": True,
            },
        ),
        patch.object(ach, "_classify_activity_types", return_value=0),
        patch.object(ach, "_refresh_view", return_value=True),
        patch("achievements.range.backfill_range_mode", return_value=7) as bf,
    ):
        result = ach.migrate_regional_schema(cur, "f3test")
    sql = " ".join(str(c) for c in cur.execute.call_args_list)
    assert "range_mode" in sql
    assert "reeval_queued_at" in sql
    assert "emoji" in sql
    bf.assert_called_once()
    assert result["range_modes_backfilled"] == 7
