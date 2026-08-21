"""Unit tests for the interactive test-only seeder (pure plan + guards)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "seed_test_region", _SCRIPTS / "seed_test_region.py"
)
assert _SPEC and _SPEC.loader
seeder = importlib.util.module_from_spec(_SPEC)
sys.modules["seed_test_region"] = seeder
_SPEC.loader.exec_module(seeder)

from achievements.achievement_rules import ACHIEVEMENT_SEEDS  # noqa: E402

TODAY = date(2026, 7, 15)  # mid-week, mid-month, mid-year
AOS = [(f"AO{i}", f"C{i:04d}") for i in range(1, 10)]
THRESHOLDS = {
    "NO_POST_THRESHOLD": 2,
    "REMINDER_WEEKS": 4,
    "HOME_AO_CAPTURE": 8,
    "NO_Q_THRESHOLD_WEEKS": 4,
    "NO_Q_THRESHOLD_POSTS": 3,
}


def _seed(code: str) -> dict:
    for s in ACHIEVEMENT_SEEDS:
        if s["code"] == code:
            return dict(s)
    raise KeyError(code)


# ---- guards -----------------------------------------------------------------


def test_assert_test_only_accepts_test_schemas():
    seeder.assert_test_only("f3ttown_test", "paxminer_test")


def test_assert_test_only_rejects_prod_regional():
    with pytest.raises(SystemExit, match="non-test regional"):
        seeder.assert_test_only("f3ttown_prod", "paxminer_test")


def test_assert_test_only_rejects_prod_in_name():
    with pytest.raises(SystemExit, match="prod"):
        seeder.assert_test_only("f3prod_test", "paxminer_test")


def test_assert_test_only_rejects_prod_registry():
    with pytest.raises(SystemExit, match="registry"):
        seeder.assert_test_only("f3ttown_test", "paxminer_prod")


def test_resolve_schemas_appends_test(monkeypatch):
    monkeypatch.setenv("PAXMINER_SCHEMA", "paxminer")
    monkeypatch.delenv("PM_REGIONAL_SCHEMA", raising=False)
    regional, registry = seeder.resolve_schemas("f3ttown_test")
    assert regional == "f3ttown_test"
    assert registry == "paxminer_test"


def test_resolve_schemas_rejects_prod_paxminer_schema(monkeypatch):
    monkeypatch.setenv("PAXMINER_SCHEMA", "paxminer_prod")
    with pytest.raises(SystemExit, match="prod registry"):
        seeder.resolve_schemas("f3ttown_test")


def test_assert_slack_team_mismatch():
    class FakeClient:
        def auth_test(self):
            return {"team_id": "TPROD", "team": "Prod"}

    with pytest.raises(SystemExit, match="does not match"):
        seeder.assert_slack_team(FakeClient(), "TTEST")


def test_assert_slack_team_ok():
    class FakeClient:
        def auth_test(self):
            return {"team_id": "TTEST", "team": "Test"}

    assert seeder.assert_slack_team(FakeClient(), "TTEST") == "TTEST"


# ---- min aos / co-triggered -------------------------------------------------


def test_min_aos_cadre_is_threshold():
    assert seeder.min_aos_required("achievement", achievement=_seed("cadre")) == 7


def test_min_aos_posts_is_one():
    assert seeder.min_aos_required("achievement", achievement=_seed("golden_boy")) == 1


def test_min_aos_kotter_is_one():
    assert seeder.min_aos_required("kotter", kotter_kind="mia") == 1


def test_co_triggered_golden_boy_includes_el_quatro():
    names = seeder.co_triggered_achievements(_seed("golden_boy"), ACHIEVEMENT_SEEDS)
    assert "El Quatro" in names
    assert "Golden Boy" not in names


# ---- achievement plans ------------------------------------------------------


def test_plan_posts_year_uses_explicit_q():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("el_quatro"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        catalog=ACHIEVEMENT_SEEDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert len(plan.beatdowns) == 25
    assert all(b.q_user_id == "UQ" for b in plan.beatdowns)
    assert all(b.attendee_ids == ["U1"] for b in plan.beatdowns)
    assert all(seeder.SEED_SENTINEL in b.backblast for b in plan.beatdowns)
    assert all(b.bd_date.year == TODAY.year for b in plan.beatdowns)
    assert "El Quatro" in plan.expected_outcome or "el_quatro" in plan.goal_label


def test_plan_qs_month_target_is_q():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("leader_of_men"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
    )
    assert len(plan.beatdowns) == 4
    assert all(b.q_user_id == "U1" for b in plan.beatdowns)
    assert all(b.bd_date.month == plan.beatdowns[0].bd_date.month for b in plan.beatdowns)


def test_plan_posts_week_six_pack():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("6_pack"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert len(plan.beatdowns) == 6
    weeks = {b.bd_date.isocalendar().week for b in plan.beatdowns}
    assert len(weeks) == 1


def test_plan_qsource_backblast_tagged():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("the_monk"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert len(plan.beatdowns) == 4
    assert all("qsource" in b.backblast.lower() for b in plan.beatdowns)


def test_plan_cadre_needs_seven_aos():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("cadre"),
        target_user_id="U1",
        aos=AOS[:7],
        thresholds=THRESHOLDS,
        today=TODAY,
    )
    assert len(plan.beatdowns) == 7
    assert len({b.ao_id for b in plan.beatdowns}) == 7
    assert len({b.bd_date for b in plan.beatdowns}) == 7
    assert all(b.q_user_id == "U1" for b in plan.beatdowns)


def test_plan_cadre_rejects_too_few_aos():
    with pytest.raises(ValueError, match="at least 7"):
        seeder.build_seed_plan(
            goal_type="achievement",
            achievement=_seed("cadre"),
            target_user_id="U1",
            aos=AOS[:3],
            thresholds=THRESHOLDS,
            today=TODAY,
        )


def test_plan_posts_at_single_ao():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("holding_down_the_fort"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert len(plan.beatdowns) == 50
    assert len({b.ao_id for b in plan.beatdowns}) == 1


# ---- kotter plans -----------------------------------------------------------


def test_plan_kotter_mia_in_window():
    plan = seeder.build_seed_plan(
        goal_type="kotter",
        kotter_kind="mia",
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert len(plan.beatdowns) == 1
    bd = plan.beatdowns[0].bd_date
    lo = TODAY - timedelta(weeks=THRESHOLDS["REMINDER_WEEKS"])
    hi = TODAY - timedelta(weeks=THRESHOLDS["NO_POST_THRESHOLD"])
    assert lo <= bd <= hi
    assert plan.beatdowns[0].q_user_id == "UQ"
    assert "MIA" in plan.expected_outcome


def test_plan_kotter_low_q():
    plan = seeder.build_seed_plan(
        goal_type="kotter",
        kotter_kind="low-q",
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert len(plan.beatdowns) >= 1
    q_rows = [b for b in plan.beatdowns if b.q_user_id == "U1"]
    assert len(q_rows) == 1
    lo = TODAY - timedelta(weeks=THRESHOLDS["REMINDER_WEEKS"])
    hi = TODAY - timedelta(weeks=THRESHOLDS["NO_Q_THRESHOLD_POSTS"])
    if hi < lo:
        lo, hi = hi, lo
    assert lo <= q_rows[0].bd_date <= hi
    assert "low-Q" in plan.expected_outcome


def test_plan_kotter_never_q():
    plan = seeder.build_seed_plan(
        goal_type="kotter",
        kotter_kind="never-q",
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        filler_q_id="UQ",
    )
    assert plan.beatdowns
    assert all(b.q_user_id == "UQ" for b in plan.beatdowns)
    assert all(b.attendee_ids == ["U1"] for b in plan.beatdowns)
    assert "never-Q" in plan.expected_outcome


def test_plan_unknown_goal_raises():
    with pytest.raises(ValueError, match="Unknown goal_type"):
        seeder.build_seed_plan(
            goal_type="nope",
            target_user_id="U1",
            aos=AOS[:1],
            thresholds=THRESHOLDS,
            today=TODAY,
        )


def test_awarded_ddl_no_fk_omits_references():
    ddl = seeder._ACHIEVEMENTS_AWARDED_DDL_NO_FK.format(schema="f3ttown_test")
    assert "FOREIGN KEY" not in ddl.upper()
    assert "REFERENCES" not in ddl.upper()
    assert "achievements_awarded" in ddl


# ---- DDL privilege tolerance -------------------------------------------------


class _DenyingCursor:
    """Cursor that reports objects exist, and denies any DDL."""

    def __init__(self, *, exists: bool = True, errno: int = 1142):
        self.exists = exists
        self.errno = errno
        self.ddl_attempts: list[str] = []

    def execute(self, sql, params=None):
        upper = sql.strip().upper()
        if upper.startswith("SELECT 1 FROM INFORMATION_SCHEMA"):
            self._row = {"1": 1} if self.exists else None
            return
        if upper.startswith(("CREATE", "ALTER", "DROP")):
            self.ddl_attempts.append(sql)
            import pymysql

            raise pymysql.err.OperationalError(
                self.errno, "CREATE VIEW command denied to user 'app'@'%'"
            )
        self._row = None

    def fetchone(self):
        return getattr(self, "_row", None)


# ---- AO index parsing / picker ----------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,3,5", [0, 2, 4]),
        ("1 3 5", [0, 2, 4]),
        ("1, 3  5", [0, 2, 4]),
        ("1-5", [0, 1, 2, 3, 4]),
        ("1-3, 7", [0, 1, 2, 6]),
        ("  2  ", [1]),
        ("", []),
    ],
)
def test_parse_index_list_accepts_commas_spaces_and_ranges(raw, expected):
    assert seeder._parse_index_list(raw, 10) == expected


@pytest.mark.parametrize("raw", ["0", "11", "abc", "1-11", "5-2", "1;2", "-3", "1-"])
def test_parse_index_list_rejects_bad_input(raw):
    assert seeder._parse_index_list(raw, 10) is None


def test_pick_aos_simple_enter_accepts_preselection(monkeypatch):
    aos = [(f"AO{i}", f"C{i}") for i in range(5)]
    monkeypatch.setattr(seeder, "_prompt", lambda *_a, **_k: "")
    assert seeder.pick_aos_simple(aos, minimum=3) == aos[:3]


def test_pick_aos_simple_range_selection(monkeypatch):
    aos = [(f"AO{i}", f"C{i}") for i in range(5)]
    monkeypatch.setattr(seeder, "_prompt", lambda *_a, **_k: "2-4")
    assert seeder.pick_aos_simple(aos, minimum=3) == aos[1:4]


def test_pick_aos_simple_reprompts_below_minimum(monkeypatch):
    aos = [(f"AO{i}", f"C{i}") for i in range(5)]
    replies = iter(["1", "1,2,5"])
    monkeypatch.setattr(seeder, "_prompt", lambda *_a, **_k: next(replies))
    assert seeder.pick_aos_simple(aos, minimum=3) == [aos[0], aos[1], aos[4]]


def test_pick_aos_simple_dedupes_repeats(monkeypatch):
    aos = [(f"AO{i}", f"C{i}") for i in range(5)]
    monkeypatch.setattr(seeder, "_prompt", lambda *_a, **_k: "1,1,2,3-3")
    assert seeder.pick_aos_simple(aos, minimum=3) == aos[:3]


# ---- AO source selection ----------------------------------------------------

QS_AOS = [("The Goose", "CGOOSE"), ("COPA", "CCOPA")]
DB_AOS = [("paxminer_logs", "CLOGS"), ("the-goose", "CGOOSE")]
CHANNELS = [("paxminer_logs", "CLOGS"), ("ao-the-goose", "CGOOSE"), ("social", "CSOC")]


def test_ao_source_prefers_qsignups_display_names():
    aos, source, missing = seeder.select_ao_candidates(QS_AOS, DB_AOS, CHANNELS)
    assert source == "qsignups"
    assert aos == [("The Goose", "CGOOSE")]
    assert missing == [("COPA", "CCOPA")]


def test_ao_source_excludes_non_ao_channels():
    aos, _, _ = seeder.select_ao_candidates(QS_AOS, DB_AOS, CHANNELS)
    picked = {cid for _, cid in aos}
    assert "CLOGS" not in picked
    assert "CSOC" not in picked


def test_ao_source_falls_back_to_paxminer_aos():
    aos, source, missing = seeder.select_ao_candidates([], DB_AOS, CHANNELS)
    assert source == "paxminer_aos"
    assert aos == [("paxminer_logs", "CLOGS"), ("the-goose", "CGOOSE")]
    assert missing == []


def test_ao_source_falls_back_to_channels_when_no_ao_lists():
    aos, source, _ = seeder.select_ao_candidates([], [], CHANNELS)
    assert source == "slack_channels"
    assert aos == CHANNELS


def test_ao_source_falls_back_when_no_qsignups_ao_is_live():
    aos, source, _ = seeder.select_ao_candidates(
        [("Ghost", "CGONE")], DB_AOS, CHANNELS
    )
    assert source == "paxminer_aos"
    assert all(cid != "CGONE" for _, cid in aos)


def test_resolve_qsignups_schema_appends_test(monkeypatch):
    monkeypatch.setenv("QSIGNUPS_SCHEMA", "qsignups")
    assert seeder.resolve_qsignups_schema() == "qsignups_test"


def test_resolve_qsignups_schema_rejects_prod(monkeypatch):
    monkeypatch.setenv("QSIGNUPS_SCHEMA", "qsignups_prod")
    with pytest.raises(SystemExit, match="prod QSignups"):
        seeder.resolve_qsignups_schema()


def test_load_qsignups_aos_returns_empty_on_error():
    class Boom:
        def cursor(self):
            raise RuntimeError("no access")

    assert seeder.load_qsignups_aos(Boom(), "qsignups_test", "T1") == []


def test_attendance_view_ddl_matches_migrated_shape():
    """Consumers do SELECT * (PAXcharter), so columns/joins must match prod."""
    ddl = seeder.ATTENDANCE_VIEW_DDL.format(schema="f3ttown_test")
    for col in ("`Date`", "`AO`", "`PAX`", "`Q`"):
        assert col in ddl
    # Q comes from a second users join; inner joins would drop rows prod keeps.
    assert ddl.upper().count("LEFT JOIN") == 3
    assert "bd.q_user_id = q.user_id" in ddl


def test_is_permission_denied_detects_1142_and_message():
    import pymysql

    assert seeder._is_permission_denied(
        pymysql.err.OperationalError(1142, "CREATE VIEW command denied to user")
    )
    assert seeder._is_permission_denied(Exception("REFERENCES command denied to user"))
    assert not seeder._is_permission_denied(Exception("some other failure"))


def test_try_ddl_returns_false_when_denied():
    cur = _DenyingCursor()
    assert seeder._try_ddl(cur, "CREATE VIEW x AS SELECT 1", "x") is False


def test_try_ddl_reraises_non_permission_errors():
    class Boom:
        def execute(self, sql, params=None):
            raise RuntimeError("syntax error")

    with pytest.raises(RuntimeError, match="syntax error"):
        seeder._try_ddl(Boom(), "CREATE VIEW x AS SELECT 1", "x")


def test_existing_views_are_not_recreated():
    cur = _DenyingCursor(exists=True)
    assert seeder._view_exists(cur, "f3ttown_test", "attendance_view") is True
    assert cur.ddl_attempts == []


def test_missing_view_attempt_is_tolerated():
    cur = _DenyingCursor(exists=False)
    assert seeder._view_exists(cur, "f3ttown_test", "attendance_view") is False
    assert seeder._try_ddl(cur, "CREATE VIEW v AS SELECT 1", "v") is False
    assert len(cur.ddl_attempts) == 1


# ---- pool / baseline / overlay ------------------------------------------------


POOL = [(f"P{i}", f"U{i}") for i in range(1, 15)]


def test_build_pax_pool_real_users_only():
    real = [("Alice", "UA"), ("Bob", "UB")]
    pool = seeder.build_pax_pool(real)
    assert pool == real
    assert all(not uid.startswith(seeder.LEGACY_SYNTHETIC_USER_PREFIX) for _, uid in pool)


def test_pick_filler_q_never_returns_target():
    assert seeder.pick_filler_q(POOL, "U1") == "U10"  # sorted: U10 before U2…
    assert seeder.pick_filler_q(POOL, {"U1", "U10"}) != "U1"
    with pytest.raises(ValueError, match="No filler Q"):
        seeder.pick_filler_q([("Solo", "U1")], "U1")


def test_build_baseline_plan_multi_attendees_and_q_in_attendees():
    plan = seeder.build_baseline_plan(
        aos=AOS[:2],
        pool=POOL,
        days=28,
        today=TODAY,
    )
    assert plan.beatdowns
    for spec in plan.beatdowns:
        assert spec.q_user_id in spec.attendee_ids
        assert 2 <= len(spec.attendee_ids) <= 4
        assert len(spec.attendee_ids) == len(set(spec.attendee_ids))


def test_build_seed_plan_with_pool_adds_multi_attendees():
    plan = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("el_quatro"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        pool=POOL,
    )
    assert len(plan.beatdowns) == 25
    assert all(len(b.attendee_ids) >= 2 for b in plan.beatdowns)
    assert all(b.q_user_id != seeder.LEGACY_FILLER_Q_USER_ID for b in plan.beatdowns)
    assert all(b.q_user_id in {uid for _, uid in POOL} for b in plan.beatdowns)
    assert all("U1" in b.attendee_ids for b in plan.beatdowns)


def test_write_beatdowns_rejects_off_roster_id():
    class RecordingCursor:
        def execute(self, sql, params=None):
            pass

    spec = seeder.BeatdownSpec(
        ao_id="C1",
        ao_name="AO1",
        bd_date=TODAY,
        q_user_id="U1",
        attendee_ids=["U1", "USEEDPAX01XXXX"],
        backblast=f"{seeder.SEED_SENTINEL} x",
    )
    with pytest.raises(ValueError, match="off-roster"):
        seeder.write_beatdowns(
            RecordingCursor(),
            "f3ttown_test",
            [spec],
            allowed_ids={"U1", "U2"},
        )


def test_upsert_user_name_none_preserves_existing():
    class MockCursor:
        def __init__(self):
            self.last_sql = ""
            self.last_params = None

        def execute(self, sql, params=None):
            self.last_sql = sql
            self.last_params = params

    cur = MockCursor()
    seeder.upsert_user(cur, "f3ttown_test", "U1", name=None, allowed_ids={"U1"})
    assert "user_name=IF(%s, VALUES(user_name), user_name)" in cur.last_sql
    # set_name flags are 0,0 at end of params
    assert cur.last_params[-2:] == (0, 0)

    cur2 = MockCursor()
    seeder.upsert_user(cur2, "f3ttown_test", "U1", name="Alice", allowed_ids={"U1"})
    assert cur2.last_params[-2:] == (1, 1)


def test_upsert_user_rejects_off_roster():
    class MockCursor:
        def execute(self, sql, params=None):
            raise AssertionError("should not execute")

    with pytest.raises(ValueError, match="off-roster"):
        seeder.upsert_user(MockCursor(), "f3ttown_test", "UBAD", "X", allowed_ids={"U1"})


def test_apply_overlay_writes_passes_user_names():
    """Regression: overlay writes must forward user_names (no name clobber)."""
    recorded: dict = {}

    def fake_write(cur, schema, beatdowns, *, user_names=None, allowed_ids=None):
        recorded["user_names"] = user_names
        recorded["allowed_ids"] = allowed_ids
        return {"beatdowns": 0, "bd_attendance": 0}

    overlay = seeder.OverlayResult(
        beatdowns_to_upsert=[
            seeder.BeatdownSpec(
                ao_id="C1",
                ao_name="AO1",
                bd_date=TODAY,
                q_user_id="U1",
                attendee_ids=["U1"],
                backblast="x",
            )
        ]
    )
    names = {"U1": "Alice"}
    allowed = {"U1"}
    orig = seeder.write_beatdowns
    seeder.write_beatdowns = fake_write  # type: ignore[assignment]
    try:
        seeder.apply_overlay_writes(
            object(),
            "f3ttown_test",
            overlay,
            user_names=names,
            allowed_ids=allowed,
        )
    finally:
        seeder.write_beatdowns = orig  # type: ignore[assignment]
    assert recorded["user_names"] == names
    assert recorded["allowed_ids"] == allowed


def test_purge_targets_legacy_prefixes():
    class MockCursor:
        def __init__(self):
            self.statements: list[tuple[str, tuple | None]] = []
            self.rowcount = 0
            self._fetch: list = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            # count_region_rows SELECTs
            if "COUNT(*)" in sql:
                self._fetch = [{"c": 0}]
            elif "SELECT user_id" in sql:
                self._fetch = []
            else:
                self._fetch = []

        def fetchone(self):
            return self._fetch[0] if self._fetch else {"c": 0}

        def fetchall(self):
            return list(self._fetch)

    cur = MockCursor()
    result = seeder.purge_synthetic(cur, "f3ttown_test", slack_roster={"U1", "U2"})
    joined = " ".join(sql for sql, _ in cur.statements)
    assert seeder.LEGACY_SYNTHETIC_USER_PREFIX in str(cur.statements)
    assert seeder.LEGACY_FILLER_Q_USER_ID in str(cur.statements)
    assert "bd_attendance" in joined
    assert "beatdowns" in joined
    assert "achievements_awarded" in joined
    assert "users" in joined
    assert "deleted" in result
    assert result["before"] is not None
    assert result["after"] is not None


def test_plan_realistic_overlay_joins_existing_calendar():
    ideal = seeder.build_seed_plan(
        goal_type="achievement",
        achievement=_seed("6_pack"),
        target_user_id="U1",
        aos=AOS[:1],
        thresholds=THRESHOLDS,
        today=TODAY,
        pool=POOL,
    )
    match_date = ideal.beatdowns[0].bd_date
    existing = seeder.CalendarEvent(
        ao_id="C0001",
        ao_name="AO1",
        bd_date=match_date,
        q_user_id="U2",
        coq_user_id=None,
        attendee_ids=["U2", "U3"],
        pax_count=2,
        is_seed=True,
    )
    overlay = seeder.plan_realistic_overlay(
        calendar=[existing],
        goal_type="achievement",
        achievement=_seed("6_pack"),
        target_user_id="U1",
        aos=AOS[:1],
        pool=POOL,
        thresholds=THRESHOLDS,
        today=TODAY,
    )
    assert overlay.beatdowns_to_upsert
    joined = next(
        b for b in overlay.beatdowns_to_upsert if b.bd_date == match_date
    )
    assert "U1" in joined.attendee_ids
    assert "U2" in joined.attendee_ids
    assert any("Joined" in n for n in overlay.notes)


def test_plan_realistic_overlay_creates_when_no_calendar_match():
    overlay = seeder.plan_realistic_overlay(
        calendar=[],
        goal_type="achievement",
        achievement=_seed("6_pack"),
        target_user_id="U1",
        aos=AOS[:1],
        pool=POOL,
        thresholds=THRESHOLDS,
        today=TODAY,
    )
    assert len(overlay.beatdowns_to_upsert) == 6
    assert not overlay.attendance_deletes


def test_pick_new_q_reassigns_after_q_removed():
    assert seeder.pick_new_q(["U2", "U3"], "U1", "U1") == "U2"
    assert seeder.pick_new_q([], "U1", "U1") is None


def test_ensure_achievement_tables_insert_writes_varchar_mirror():
    from unittest.mock import MagicMock, patch

    cur = MagicMock()
    cur.fetchone.return_value = None
    with (
        patch.object(seeder, "_table_exists", return_value=True),
        patch.object(seeder, "_view_exists", return_value=True),
        patch("paxminer_phases.achievements.migrate_regional_schema"),
    ):
        seeder.ensure_achievement_tables(cur, "f3ttown_test")
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
