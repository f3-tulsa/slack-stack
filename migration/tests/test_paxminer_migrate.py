"""Unit tests for paxminer_migrate orchestrator."""

from __future__ import annotations

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
        return {"timezone_added": False, "tables_created": []}

    def _drop_legacy(cur, stage):
        call_order.append("drop-legacy-columns")
        return {"dropped": [], "skipped": list(DROPPED_COLUMNS)}

    with (
        patch("paxminer_migrate.run_weaselbot", side_effect=_weaselbot),
        patch("paxminer_migrate.run_scheduler", side_effect=_scheduler),
        patch("paxminer_migrate.run_drop_legacy_columns", side_effect=_drop_legacy),
        patch("paxminer_migrate._load_env"),
        patch("paxminer_migrate._write_receipt", return_value=Path("/tmp/receipt.txt")),
    ):
        from paxminer_migrate import main

        rc = main(["--env", "test", "--all"])

    assert rc == 0
    assert call_order == ["weaselbot", "scheduler", "drop-legacy-columns"]
    assert conn.commit.call_count == 3
    conn.close.assert_called_once()


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
