"""Unit tests for the dev-only test-region reset script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "reset_test_region", _SCRIPTS / "reset_test_region.py"
)
assert _SPEC and _SPEC.loader
reset = importlib.util.module_from_spec(_SPEC)
sys.modules["reset_test_region"] = reset
_SPEC.loader.exec_module(reset)


class FakeCursor:
    """Minimal cursor: queues fetchall results, records executed SQL."""

    def __init__(self, fetch_queue=None, rowcount=0):
        self.queries: list[tuple[str, object]] = []
        self._fetch_queue = list(fetch_queue or [])
        self._next_fetch = None
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if self._fetch_queue:
            self._next_fetch = self._fetch_queue.pop(0)
        else:
            self._next_fetch = []

    def fetchall(self):
        return self._next_fetch or []

    def fetchone(self):
        rows = self._next_fetch or []
        return rows[0] if rows else None


def test_wipe_tables_cover_attendance_and_awards():
    assert set(reset.WIPE_TABLES) == {
        "bd_attendance",
        "beatdowns",
        "achievements_awarded",
    }


def test_achievements_list_is_never_wiped():
    assert "achievements_list" in reset.KEEP_TABLES
    assert "achievements_list" not in reset.WIPE_TABLES


def test_wipe_attendance_deletes_each_table_once():
    cur = FakeCursor(rowcount=7)
    deleted = reset.wipe_attendance(cur, "f3ttown_test")
    assert deleted == {"bd_attendance": 7, "beatdowns": 7, "achievements_awarded": 7}
    sqls = [q for q, _ in cur.queries]
    assert len(sqls) == 3
    for table in reset.WIPE_TABLES:
        assert any(f"`{table}`" in s and s.strip().startswith("DELETE") for s in sqls)
    assert not any("achievements_list" in s for s in sqls)


def test_wipe_attendance_scopes_to_given_schema():
    cur = FakeCursor(rowcount=1)
    reset.wipe_attendance(cur, "f3ttown_test")
    assert all("`f3ttown_test`" in q for q, _ in cur.queries)


def test_prune_roster_only_deletes_ids_missing_from_workspace():
    cur = FakeCursor(
        fetch_queue=[
            [{"user_id": "UKEEP"}, {"user_id": "USTALE"}],
            [],  # delete users
            [{"channel_id": "CKEEP"}, {"channel_id": "CSTALE"}],
            [],  # delete aos
        ],
        rowcount=1,
    )
    reset.prune_roster(
        cur,
        "f3ttown_test",
        live_user_ids={"UKEEP"},
        live_channel_ids={"CKEEP"},
    )
    delete_params = [p for q, p in cur.queries if q.strip().startswith("DELETE")]
    assert delete_params == [["USTALE"], ["CSTALE"]]


def test_prune_roster_no_deletes_when_all_live():
    cur = FakeCursor(
        fetch_queue=[
            [{"user_id": "UKEEP"}],
            [{"channel_id": "CKEEP"}],
        ]
    )
    pruned = reset.prune_roster(
        cur,
        "f3ttown_test",
        live_user_ids={"UKEEP"},
        live_channel_ids={"CKEEP"},
    )
    assert pruned == {"users": 0, "aos": 0}
    assert not [q for q, _ in cur.queries if q.strip().startswith("DELETE")]


def test_delete_in_batches_chunks_large_id_lists():
    cur = FakeCursor(rowcount=2)
    ids = [f"U{i}" for i in range(450)]
    total = reset._delete_in_batches(cur, "DELETE FROM `s`.`users` WHERE user_id IN", ids)
    assert len(cur.queries) == 3  # 200 + 200 + 50
    assert total == 6
    assert sum(len(p) for _, p in cur.queries) == 450


def test_delete_in_batches_noop_on_empty():
    cur = FakeCursor(rowcount=5)
    assert reset._delete_in_batches(cur, "DELETE FROM x WHERE id IN", []) == 0
    assert cur.queries == []


def test_table_counts_reads_each_table():
    cur = FakeCursor(fetch_queue=[[{"n": 3}], [{"n": 0}]])
    counts = reset.table_counts(cur, "f3ttown_test", ("beatdowns", "users"))
    assert counts == {"beatdowns": 3, "users": 0}


def test_reset_reuses_seeder_test_only_guard():
    with pytest.raises(SystemExit, match="non-test regional"):
        reset.seeder.assert_test_only("f3ttown_prod", "paxminer_test")
