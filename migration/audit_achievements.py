#!/usr/bin/env python3
"""Read-only achievements + token-storage audit.

Snapshots ``achievement_versions`` (rule of record since 5c) plus the
``achievements_list`` mirror. Exits non-zero on any finding. Never prints
token values.

Usage:
  python migration/audit_achievements.py --env rehearsal --snapshot migration/rehearsal/sittings/audit-before.json
  python migration/audit_achievements.py --env rehearsal --compare migration/rehearsal/sittings/audit-before.json
  python migration/audit_achievements.py --env rehearsal --snapshot migration/rehearsal/sittings/audit-after.json \\
      --compare migration/rehearsal/sittings/audit-before.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_MIGRATION_DIR = Path(__file__).resolve().parent
_REPO = _MIGRATION_DIR.parent
sys.path.insert(0, str(_MIGRATION_DIR))
sys.path.insert(0, str(_REPO / "PAXminer"))

from achievements.achievement_rules import ACHIEVEMENT_SEEDS  # noqa: E402
from achievements.activity import (  # noqa: E402
    activity_filter_from_rule,
    activity_legacy_mirror,
)

from paxminer_phases.db import (  # noqa: E402
    ENV_STAGES,
    _column_exists,
    _connect,
    _load_env,
    _pm_schema,
    _sb_schema,
    _table_exists,
    _wb_schema,
)

LOG = logging.getLogger(__name__)

VERSION_COLS = (
    "id",
    "achievement_id",
    "version",
    "version_key",
    "metric",
    "activity",
    "period",
    "threshold",
    "effective_from",
    "effective_to",
    "range_mode",
    "superseded_at",
)
LIST_COLS = (
    "id",
    "code",
    "name",
    "description",
    "verb",
    "metric",
    "activity",
    "period",
    "threshold",
    "enabled",
)
LIST_COMPARE_KEYS = ("code", "name", "metric", "activity", "period", "threshold")
VERSION_COMPARE_KEYS = (
    "achievement_id",
    "version",
    "version_key",
    "metric",
    "activity",
    "period",
    "threshold",
    "effective_from",
    "effective_to",
    "range_mode",
    "superseded_at",
)

SEED_CODES = {str(s["code"]) for s in ACHIEVEMENT_SEEDS}
SEEDS_BY_CODE = {str(s["code"]): s for s in ACHIEVEMENT_SEEDS}

# Compare notes catalog-alignment updates from _seed_version_1; they do not fail the audit.
FAIL_CODES = frozenset(
    {
        "unknown_list_code",
        "orphaned_award",
        "version_mirror_mismatch",
        "plaintext_token",
        "unknown_token_storage",
        "deleted_list_row",
        "changed_list_row",
        "deleted_version_row",
        "changed_version_row",
    }
)


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def row_jsonable(row: dict, cols: tuple[str, ...]) -> dict:
    return {c: jsonable(row.get(c)) for c in cols}


def token_storage_state(value: str | None) -> str:
    if value is None or value == "":
        return "empty"
    if value.startswith("gAAAAA"):
        return "fernet"
    if value.lower().startswith("xox"):
        return "plaintext"
    return "unknown"


def _select_columns(cur, schema: str, table: str, wanted: tuple[str, ...]) -> list[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        """,
        (schema, table),
    )
    present = {row["COLUMN_NAME"] for row in cur.fetchall()}
    return [c for c in wanted if c in present]


def _fetch_table(cur, schema: str, table: str, cols: list[str]) -> list[dict]:
    if not cols:
        return []
    quoted = ", ".join(f"`{c}`" for c in cols)
    cur.execute(f"SELECT {quoted} FROM `{schema}`.`{table}`")
    return [row_jsonable(row, tuple(cols)) for row in cur.fetchall()]


def snapshot_region(cur, schema: str) -> dict:
    """Read-only snapshot of one regional schema. Missing tables are empty lists."""
    out: dict[str, Any] = {
        "schema": schema,
        "achievements_list": [],
        "achievement_versions": [],
        "unknown_codes": [],
        "orphaned_award_ids": [],
        "version_mirror_mismatches": [],
        "tables_present": [],
    }
    if not _table_exists(cur, schema, "achievements_list"):
        return out
    out["tables_present"].append("achievements_list")
    list_cols = _select_columns(cur, schema, "achievements_list", LIST_COLS)
    list_rows = _fetch_table(cur, schema, "achievements_list", list_cols)
    out["achievements_list"] = list_rows
    list_ids = {int(r["id"]) for r in list_rows if r.get("id") is not None}
    out["unknown_codes"] = sorted(
        {str(r.get("code")) for r in list_rows if r.get("code") and str(r.get("code")) not in SEED_CODES}
    )

    if _table_exists(cur, schema, "achievements_awarded"):
        out["tables_present"].append("achievements_awarded")
        cur.execute(
            f"""
            SELECT DISTINCT a.achievement_id
            FROM `{schema}`.`achievements_awarded` a
            LEFT JOIN `{schema}`.`achievements_list` l ON l.id = a.achievement_id
            WHERE l.id IS NULL
            """
        )
        out["orphaned_award_ids"] = sorted(
            int(r["achievement_id"]) for r in cur.fetchall() if r.get("achievement_id") is not None
        )

    if _table_exists(cur, schema, "achievement_versions"):
        out["tables_present"].append("achievement_versions")
        ver_cols = _select_columns(cur, schema, "achievement_versions", VERSION_COLS)
        versions = _fetch_table(cur, schema, "achievement_versions", ver_cols)
        out["achievement_versions"] = versions
        list_by_id = {int(r["id"]): r for r in list_rows if r.get("id") is not None}
        for v in versions:
            if v.get("superseded_at"):
                continue
            aid = v.get("achievement_id")
            if aid is None:
                continue
            mirror = list_by_id.get(int(aid))
            if not mirror:
                out["version_mirror_mismatches"].append(
                    {"achievement_id": int(aid), "reason": "version_without_list_row"}
                )
                continue
            mismatches: dict[str, Any] = {}
            for col in ("metric", "period", "threshold"):
                if jsonable(v.get(col)) != jsonable(mirror.get(col)):
                    mismatches[col] = {"version": jsonable(v.get(col)), "list": jsonable(mirror.get(col))}
            if mismatches:
                out["version_mirror_mismatches"].append(
                    {
                        "achievement_id": int(aid),
                        "code": mirror.get("code"),
                        "fields": mismatches,
                    }
                )
    return out


def snapshot_tokens(cur, stage: str) -> list[dict]:
    """Storage state of every Slack token across registries. Never includes values."""
    reports: list[dict] = []
    targets = [
        (_pm_schema(stage), "regions", "slack_token", "region"),
        (_wb_schema(stage), "regions", "slack_token", "team_id"),
        (_sb_schema(stage), "regions", "bot_token", "id"),
    ]
    for schema, table, column, label_col in targets:
        if not _table_exists(cur, schema, table) or not _column_exists(cur, schema, table, column):
            reports.append(
                {
                    "schema": schema,
                    "table": table,
                    "column": column,
                    "present": False,
                    "counts": {},
                    "rows": [],
                }
            )
            continue
        extra = label_col if _column_exists(cur, schema, table, label_col) else None
        extra_sql = f", `{extra}` AS label" if extra else ", NULL AS label"
        schema_name_sql = ""
        if _column_exists(cur, schema, table, "schema_name"):
            schema_name_sql = ", `schema_name`"
        elif _column_exists(cur, schema, table, "paxminer_schema"):
            schema_name_sql = ", `paxminer_schema` AS schema_name"
        else:
            schema_name_sql = ", NULL AS schema_name"
        cur.execute(
            f"SELECT `{column}` AS token{extra_sql}{schema_name_sql} FROM `{schema}`.`{table}`"
        )
        counts = {"empty": 0, "fernet": 0, "plaintext": 0, "unknown": 0}
        rows = []
        for row in cur.fetchall():
            state = token_storage_state(row.get("token"))
            counts[state] = counts.get(state, 0) + 1
            rows.append(
                {
                    "label": jsonable(row.get("label")),
                    "schema_name": jsonable(row.get("schema_name")),
                    "state": state,
                }
            )
        reports.append(
            {
                "schema": schema,
                "table": table,
                "column": column,
                "present": True,
                "counts": counts,
                "rows": rows,
            }
        )
    return reports


def findings_from_snapshot(snapshot: dict) -> list[dict]:
    findings: list[dict] = []
    for schema, region in (snapshot.get("schemas") or {}).items():
        for code in region.get("unknown_codes") or []:
            findings.append(
                {
                    "code": "unknown_list_code",
                    "schema": schema,
                    "detail": f"achievements_list.code={code!r} is not in ACHIEVEMENT_SEEDS",
                }
            )
        for aid in region.get("orphaned_award_ids") or []:
            findings.append(
                {
                    "code": "orphaned_award",
                    "schema": schema,
                    "detail": f"achievements_awarded.achievement_id={aid} has no achievements_list row",
                }
            )
        for mismatch in region.get("version_mirror_mismatches") or []:
            findings.append(
                {
                    "code": "version_mirror_mismatch",
                    "schema": schema,
                    "detail": mismatch,
                }
            )
    for token_report in snapshot.get("tokens") or []:
        if not token_report.get("present"):
            continue
        schema = token_report["schema"]
        for row in token_report.get("rows") or []:
            if row.get("state") == "plaintext":
                findings.append(
                    {
                        "code": "plaintext_token",
                        "schema": schema,
                        "detail": (
                            f"{token_report['table']}.{token_report['column']} "
                            f"label={row.get('label')!r} schema_name={row.get('schema_name')!r} "
                            "is stored as plaintext xox*"
                        ),
                    }
                )
            elif row.get("state") == "unknown":
                findings.append(
                    {
                        "code": "unknown_token_storage",
                        "schema": schema,
                        "detail": (
                            f"{token_report['table']}.{token_report['column']} "
                            f"label={row.get('label')!r} schema_name={row.get('schema_name')!r} "
                            "is neither empty, Fernet, nor xox plaintext"
                        ),
                    }
                )
    return findings


def catalog_list_target(code: str | None) -> dict | None:
    """Expected achievements_list mirror after weaselbot + _seed_version_1."""
    if not code or code not in SEEDS_BY_CODE:
        return None
    seed = SEEDS_BY_CODE[code]
    spec = activity_filter_from_rule(seed)
    return {
        "name": seed["name"],
        "metric": seed["metric"],
        "activity": activity_legacy_mirror(spec, version=1),
        "period": seed["period"],
        "threshold": int(seed["threshold"]),
    }


def _index_rows(rows: list[dict], key: str = "id") -> dict[Any, dict]:
    return {r.get(key): r for r in rows if r.get(key) is not None}


def _changed_fields(before: dict, after: dict, keys: tuple[str, ...]) -> dict:
    changed = {}
    for k in keys:
        if jsonable(before.get(k)) != jsonable(after.get(k)):
            changed[k] = {"before": jsonable(before.get(k)), "after": jsonable(after.get(k))}
    return changed


def compare_snapshots(before: dict, after: dict) -> list[dict]:
    """Flag deleted or changed list/version rows. Added rows (new versions) are expected."""
    findings: list[dict] = []
    before_schemas = before.get("schemas") or {}
    after_schemas = after.get("schemas") or {}
    for schema, before_region in before_schemas.items():
        after_region = after_schemas.get(schema) or {}
        before_list = _index_rows(before_region.get("achievements_list") or [])
        after_list = _index_rows(after_region.get("achievements_list") or [])
        for row_id, row in before_list.items():
            if row_id not in after_list:
                findings.append(
                    {
                        "code": "deleted_list_row",
                        "schema": schema,
                        "detail": {"id": row_id, "code": row.get("code")},
                    }
                )
                continue
            changed = _changed_fields(row, after_list[row_id], LIST_COMPARE_KEYS)
            if changed:
                target = catalog_list_target(row.get("code"))
                after_row = after_list[row_id]
                aligned = bool(target) and "code" not in changed and all(
                    k in target and jsonable(after_row.get(k)) == jsonable(target[k])
                    for k in changed
                )
                findings.append(
                    {
                        "code": "catalog_alignment" if aligned else "changed_list_row",
                        "schema": schema,
                        "detail": {"id": row_id, "code": row.get("code"), "fields": changed},
                    }
                )
        before_vers = _index_rows(before_region.get("achievement_versions") or [])
        after_vers = _index_rows(after_region.get("achievement_versions") or [])
        for row_id, row in before_vers.items():
            if row_id not in after_vers:
                findings.append(
                    {
                        "code": "deleted_version_row",
                        "schema": schema,
                        "detail": {
                            "id": row_id,
                            "achievement_id": row.get("achievement_id"),
                            "version_key": row.get("version_key"),
                        },
                    }
                )
                continue
            changed = _changed_fields(row, after_vers[row_id], VERSION_COMPARE_KEYS)
            if changed:
                findings.append(
                    {
                        "code": "changed_version_row",
                        "schema": schema,
                        "detail": {
                            "id": row_id,
                            "version_key": row.get("version_key"),
                            "fields": changed,
                        },
                    }
                )
    return findings


def take_snapshot(cur, stage: str) -> dict:
    pm_schema = _pm_schema(stage)
    schemas: dict[str, dict] = {}
    if _table_exists(cur, pm_schema, "regions"):
        cur.execute(
            f"SELECT schema_name FROM `{pm_schema}`.`regions` "
            "WHERE schema_name IS NOT NULL"
        )
        names = [row["schema_name"] for row in cur.fetchall() if row.get("schema_name")]
    else:
        names = []
    # Always include the expected regional schemas for this stage, even if
    # the registry is empty (pre-copy) or a row is inactive.
    expected = [f"f3ttown_{stage}", f"f3scissortail_{stage}"]
    for name in expected:
        if name not in names:
            names.append(name)
    for name in names:
        schemas[name] = snapshot_region(cur, name)
    return {
        "stage": stage,
        "pm_schema": pm_schema,
        "schemas": schemas,
        "tokens": snapshot_tokens(cur, stage),
    }


def _print_findings(title: str, findings: list[dict]) -> None:
    print(f"{title}: {len(findings)}", flush=True)
    for f in findings:
        print(f"  [{f['code']}] {f.get('schema')}: {f['detail']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Read-only achievements audit")
    parser.add_argument("--env", required=True, choices=ENV_STAGES)
    parser.add_argument("--snapshot", type=Path, help="Write current snapshot JSON to this path")
    parser.add_argument(
        "--compare",
        type=Path,
        help="Compare current snapshot against this earlier JSON (deleted/changed rows)",
    )
    args = parser.parse_args(argv)
    if not args.snapshot and not args.compare:
        parser.error("provide --snapshot and/or --compare")

    _load_env(args.env)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            current = take_snapshot(cur, args.env)
    finally:
        conn.close()

    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(current, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"Wrote snapshot {args.snapshot}", flush=True)

    snapshot_findings = findings_from_snapshot(current)
    _print_findings("Snapshot findings", snapshot_findings)
    for report in current.get("tokens") or []:
        if not report.get("present"):
            print(
                f"Token registry {report['schema']}.{report['table']}.{report['column']}: absent",
                flush=True,
            )
            continue
        counts = report.get("counts") or {}
        print(
            f"Token registry {report['schema']}.{report['table']}.{report['column']}: "
            f"empty={counts.get('empty', 0)} fernet={counts.get('fernet', 0)} "
            f"plaintext={counts.get('plaintext', 0)} unknown={counts.get('unknown', 0)}",
            flush=True,
        )

    compare_findings: list[dict] = []
    if args.compare:
        before = json.loads(args.compare.read_text(encoding="utf-8"))
        compare_findings = compare_snapshots(before, current)
        _print_findings("Compare findings", compare_findings)

    all_findings = snapshot_findings + compare_findings
    failing = [f for f in all_findings if f["code"] in FAIL_CODES]
    notes = [f for f in all_findings if f["code"] not in FAIL_CODES]
    if notes:
        _print_findings("Informational notes", notes)
    if failing:
        print(f"AUDIT FAILED: {len(failing)} finding(s)", flush=True)
        return 1
    print("AUDIT OK: no failing findings", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
