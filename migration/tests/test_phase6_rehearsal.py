"""Phase 6 rehearsal tooling: suffix guard, env fail-hard, prepare refusal, audit."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
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

from paxminer_phases.db import assert_regional_stage, stage_suffix  # noqa: E402


def test_stage_suffix_splits_on_first_underscore():
    assert stage_suffix("paxminer_rehearsal") == "rehearsal"
    assert stage_suffix("f3ttown_rehearsal") == "rehearsal"
    assert stage_suffix("f3scissortail_test") == "test"


def test_stage_suffix_migrate_test_trap():
    """Last-underscore parsing would treat migrate_test as test and wave through f3ttown_test."""
    assert stage_suffix("paxminer_migrate_test") == "migrate_test"
    assert stage_suffix("f3ttown_test") == "test"
    naive_pm = "paxminer_migrate_test".rsplit("_", 1)[-1]
    naive_reg = "f3ttown_test".rsplit("_", 1)[-1]
    assert naive_pm == naive_reg == "test"
    with pytest.raises(RuntimeError, match="cross-stage"):
        assert_regional_stage("f3ttown_test", "paxminer_migrate_test")
    assert_regional_stage("f3ttown_rehearsal", "paxminer_rehearsal")


def test_assert_regional_stage_rejects_prod_pointer():
    with pytest.raises(RuntimeError, match="f3ttown_prod"):
        assert_regional_stage("f3ttown_prod", "paxminer_rehearsal")


def test_load_env_missing_file_is_hard_error():
    from paxminer_phases.db import _load_env

    with pytest.raises(FileNotFoundError, match="Missing"):
        _load_env("no_such_stage_xyz")


def test_load_env_uses_override_true():
    from paxminer_phases import db as dbmod

    captured: dict = {}

    def fake_load_dotenv(path, override=False):
        captured["override"] = override

    fake_file = MagicMock()
    fake_file.is_file.return_value = True

    class FakePath:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self):
            return self

        @property
        def parent(self):
            return self

        def __truediv__(self, other):
            return fake_file

    with patch.object(dbmod, "Path", FakePath), patch("dotenv.load_dotenv", fake_load_dotenv):
        dbmod._load_env("rehearsal")
    assert captured["override"] is True


def test_run_achievements_refuses_cross_stage_schema():
    from paxminer_phases.achievements import run_achievements

    cur = MagicMock()
    cur.fetchall.return_value = [{"schema_name": "f3ttown_prod"}]
    with patch("paxminer_phases.achievements._pm_schema", return_value="paxminer_rehearsal"):
        with pytest.raises(RuntimeError, match="cross-stage"):
            run_achievements(cur, "rehearsal")


def test_run_weaselbot_refuses_cross_stage_schema():
    from paxminer_phases import weaselbot as wb

    cur = MagicMock()
    cur.fetchall.return_value = [{"region": "ttown", "schema_name": "f3ttown_prod"}]
    with (
        patch.object(wb, "_pm_schema", return_value="paxminer_rehearsal"),
        patch.object(wb, "_wb_schema", return_value="weaselbot_rehearsal"),
        patch.object(wb, "_sb_schema", return_value="slackblast_rehearsal"),
        patch.object(wb, "_pm_columns_complete", return_value=True),
        patch.object(wb, "_sb_column_complete", return_value=True),
        patch.object(wb, "_weaselbot_source_available", return_value=False),
        patch.object(wb, "alter_paxminer_regions", return_value=[]),
        patch.object(wb, "alter_slackblast_regions", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="cross-stage"):
            wb.run_weaselbot(cur, "rehearsal")


def test_prepare_refuses_non_rehearsal_target():
    from prepare_rehearsal import validate_copy_args

    with pytest.raises(SystemExit, match="rehearsal"):
        validate_copy_args("prod", "test")
    with pytest.raises(SystemExit, match="rehearsal"):
        validate_copy_args("prod", "prod")
    with pytest.raises(SystemExit, match="prod"):
        validate_copy_args("test", "rehearsal")
    validate_copy_args("prod", "rehearsal")


def test_prepare_rewrite_view_strips_definer_and_swaps_schema():
    from prepare_rehearsal import rewrite_view_ddl, schema_mapping

    mapping = schema_mapping("prod", "rehearsal")
    ddl = (
        "CREATE ALGORITHM=UNDEFINED DEFINER=`migrator.root`@`%` "
        "SQL SECURITY DEFINER VIEW `attendance_view` (`Date`, `AO`) AS "
        "SELECT `bd`.`date` AS `Date` FROM `f3ttown_prod`.`bd_attendance` AS `bd`"
    )
    out = rewrite_view_ddl(ddl, mapping, dest_schema="f3ttown_rehearsal", view_name="attendance_view")
    assert "DEFINER=" not in out
    assert "migrator.root" not in out
    assert "f3ttown_prod" not in out
    assert "`f3ttown_rehearsal`.`attendance_view`" in out
    assert "SQL SECURITY INVOKER" in out
    assert out.upper().startswith("CREATE OR REPLACE")


def test_token_storage_state():
    from audit_achievements import token_storage_state

    assert token_storage_state(None) == "empty"
    assert token_storage_state("") == "empty"
    assert token_storage_state("gAAAAAabcdef") == "fernet"
    assert token_storage_state("xoxb-live-token") == "plaintext"
    assert token_storage_state("xoxp-legacy") == "plaintext"
    assert token_storage_state("not-a-token") == "unknown"


def test_audit_snapshot_findings_unknown_code_and_orphan():
    from audit_achievements import FAIL_CODES, findings_from_snapshot

    snap = {
        "schemas": {
            "f3ttown_rehearsal": {
                "unknown_codes": ["custom_ao"],
                "orphaned_award_ids": [99],
                "version_mirror_mismatches": [],
            }
        },
        "tokens": [
            {
                "schema": "paxminer_rehearsal",
                "table": "regions",
                "column": "slack_token",
                "present": True,
                "rows": [
                    {"label": "ttown", "schema_name": "f3ttown_rehearsal", "state": "fernet"},
                    {"label": "scissortail", "schema_name": "f3scissortail_rehearsal", "state": "empty"},
                ],
            }
        ],
    }
    findings = findings_from_snapshot(snap)
    codes = {f["code"] for f in findings}
    assert "unknown_list_code" in codes
    assert "orphaned_award" in codes
    assert "plaintext_token" not in codes
    assert codes <= FAIL_CODES


def test_audit_plaintext_token_is_a_finding():
    from audit_achievements import findings_from_snapshot

    snap = {
        "schemas": {},
        "tokens": [
            {
                "schema": "paxminer_rehearsal",
                "table": "regions",
                "column": "slack_token",
                "present": True,
                "rows": [{"label": "x", "schema_name": "f3x", "state": "plaintext"}],
            }
        ],
    }
    findings = findings_from_snapshot(snap)
    assert findings[0]["code"] == "plaintext_token"
    dumped = json.dumps(findings)
    assert "xoxb-" not in dumped
    assert "xoxp-" not in dumped


def test_audit_compare_deleted_and_changed_rows():
    from audit_achievements import FAIL_CODES, compare_snapshots

    before = {
        "schemas": {
            "f3x": {
                "achievements_list": [
                    {
                        "id": 1,
                        "code": "custom_ao",
                        "name": "A",
                        "metric": "posts",
                        "activity": "beatdown",
                        "period": "year",
                        "threshold": 1,
                    },
                    {
                        "id": 2,
                        "code": "gone",
                        "name": "G",
                        "metric": "posts",
                        "activity": "beatdown",
                        "period": "year",
                        "threshold": 1,
                    },
                ],
                "achievement_versions": [
                    {
                        "id": 10,
                        "achievement_id": 1,
                        "version": 1,
                        "version_key": "custom_ao_v1",
                        "metric": "posts",
                        "activity": None,
                        "period": "year",
                        "threshold": 1,
                        "effective_from": None,
                        "effective_to": None,
                        "range_mode": None,
                        "superseded_at": None,
                    }
                ],
            }
        }
    }
    after = {
        "schemas": {
            "f3x": {
                "achievements_list": [
                    {
                        "id": 1,
                        "code": "custom_ao",
                        "name": "A",
                        "metric": "qs",
                        "activity": "beatdown",
                        "period": "year",
                        "threshold": 1,
                    }
                ],
                "achievement_versions": [
                    {
                        "id": 10,
                        "achievement_id": 1,
                        "version": 1,
                        "version_key": "custom_ao_v1",
                        "metric": "qs",
                        "activity": None,
                        "period": "year",
                        "threshold": 1,
                        "effective_from": None,
                        "effective_to": None,
                        "range_mode": None,
                        "superseded_at": None,
                    }
                ],
            }
        }
    }
    findings = compare_snapshots(before, after)
    codes = {f["code"] for f in findings}
    assert "deleted_list_row" in codes
    assert "changed_list_row" in codes
    assert "changed_version_row" in codes
    assert codes <= FAIL_CODES


def test_audit_compare_catalog_alignment_is_informational():
    from audit_achievements import FAIL_CODES, catalog_list_target, compare_snapshots

    target = catalog_list_target("the_priest")
    assert target is not None
    before = {
        "schemas": {
            "f3x": {
                "achievements_list": [
                    {
                        "id": 7,
                        "code": "the_priest",
                        "name": target["name"],
                        "metric": "posts",
                        "activity": "beatdown",
                        "period": "year",
                        "threshold": 1,
                    }
                ],
                "achievement_versions": [],
            }
        }
    }
    after = {
        "schemas": {
            "f3x": {
                "achievements_list": [
                    {
                        "id": 7,
                        "code": "the_priest",
                        "name": target["name"],
                        "metric": target["metric"],
                        "activity": target["activity"],
                        "period": target["period"],
                        "threshold": target["threshold"],
                    }
                ],
                "achievement_versions": [
                    {
                        "id": 1,
                        "achievement_id": 7,
                        "version": 1,
                        "version_key": "the_priest_v1",
                        "metric": target["metric"],
                        "activity": '["QSource"]',
                        "period": target["period"],
                        "threshold": target["threshold"],
                        "effective_from": None,
                        "effective_to": None,
                        "range_mode": None,
                        "superseded_at": None,
                    }
                ],
            }
        }
    }
    findings = compare_snapshots(before, after)
    assert findings
    assert all(f["code"] == "catalog_alignment" for f in findings)
    assert "catalog_alignment" not in FAIL_CODES


def test_reconcile_requires_schemas():
    from rehearsal_reconcile import parse_schemas

    with pytest.raises(SystemExit, match="--schemas"):
        parse_schemas([])
    with pytest.raises(SystemExit, match="--schemas"):
        parse_schemas(["", ","])
    assert parse_schemas(["f3ttown_rehearsal"]) == ["f3ttown_rehearsal"]
    assert parse_schemas(["f3ttown_rehearsal,f3extra"]) == ["f3ttown_rehearsal", "f3extra"]


def test_reconcile_refuses_revoke_without_all_attendance():
    from rehearsal_reconcile import validate_rejudge_flags

    validate_rejudge_flags(all_attendance=False, allow_revoke=False)
    validate_rejudge_flags(all_attendance=True, allow_revoke=False)
    validate_rejudge_flags(all_attendance=True, allow_revoke=True)
    with pytest.raises(SystemExit, match="--all-attendance"):
        validate_rejudge_flags(all_attendance=False, allow_revoke=True)


def test_load_enabled_achievement_ids_orders_by_id():
    from rehearsal_reconcile import load_enabled_achievement_ids

    cur = MagicMock()
    cur.fetchall.return_value = [{"id": 2}, {"id": 5}]
    assert load_enabled_achievement_ids(cur, "f3ttown_rehearsal") == [2, 5]
    assert "achievements_list" in cur.execute.call_args[0][0]


def test_allow_revoke_dispatches_to_reconcile_rule_awards():
    from rehearsal_reconcile import run_schemas

    fake_runner = MagicMock()
    fake_runner.reconcile_rule_awards.return_value = {
        "grants": 2,
        "revokes": 1,
        "held": 3,
        "reconcile": True,
        "dry_run": False,
    }
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchone.side_effect = [
        {"d": date(2018, 3, 4)},
        {"region": "f3ttown", "schema_name": "f3ttown_rehearsal", "slack_token": "enc"},
    ]
    cur.fetchall.return_value = [{"id": 10}, {"id": 11}]
    with patch.dict(sys.modules, {"achievements.runner": fake_runner}):
        outcomes = run_schemas(
            conn,
            "paxminer_rehearsal",
            ["f3ttown_rehearsal"],
            dry_run=False,
            all_attendance=True,
            allow_revoke=True,
        )
    assert fake_runner.reconcile_rule_awards.call_count == 2
    fake_runner.run_achievements_for_region.assert_not_called()
    ids = [c.kwargs["achievement_id"] for c in fake_runner.reconcile_rule_awards.call_args_list]
    assert ids == [10, 11]
    assert all(c.kwargs["dry_run"] is False for c in fake_runner.reconcile_rule_awards.call_args_list)
    assert all(c.kwargs["action"] == "re-evaluated" for c in fake_runner.reconcile_rule_awards.call_args_list)
    assert outcomes[0]["grants"] == 4
    assert outcomes[0]["revokes"] == 2
    assert outcomes[0]["rules"] == 2


def test_allow_revoke_dry_run_passes_dry_run_into_reconcile():
    from rehearsal_reconcile import run_schemas

    fake_runner = MagicMock()
    fake_runner.reconcile_rule_awards.return_value = {
        "grants": 0,
        "revokes": 0,
        "held": 5,
        "reconcile": True,
        "dry_run": True,
    }
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchone.side_effect = [
        {"d": date(2018, 3, 4)},
        {"region": "f3ttown", "schema_name": "f3ttown_rehearsal"},
    ]
    cur.fetchall.return_value = [{"id": 1}]
    with patch.dict(sys.modules, {"achievements.runner": fake_runner}):
        run_schemas(
            conn,
            "paxminer_rehearsal",
            ["f3ttown_rehearsal"],
            dry_run=True,
            all_attendance=True,
            allow_revoke=True,
        )
    assert fake_runner.reconcile_rule_awards.call_args.kwargs["dry_run"] is True


def test_earliest_beatdown_parses_min_date():
    from rehearsal_reconcile import earliest_beatdown

    cur = MagicMock()
    cur.fetchone.return_value = {"d": datetime(2018, 3, 4)}
    assert earliest_beatdown(cur, "f3ttown_rehearsal") == date(2018, 3, 4)
    cur.fetchone.return_value = {"d": None}
    with pytest.raises(RuntimeError, match="no beatdowns"):
        earliest_beatdown(cur, "f3ttown_rehearsal")


def test_paxminer_migrate_accepts_rehearsal_env():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    with (
        patch("paxminer_migrate._connect", return_value=conn),
        patch("paxminer_migrate.run_weaselbot", return_value={"pm_columns_added": []}),
        patch("paxminer_migrate._load_env"),
        patch("paxminer_migrate._write_receipt", return_value=Path("/tmp/receipt.txt")),
    ):
        from paxminer_migrate import main

        rc = main(["--env", "rehearsal", "--phase", "weaselbot"])
    assert rc == 0
