#!/usr/bin/env python3
"""All-time achievements re-eval for rehearsal and prod cutover.

Cutover (``--all-attendance --allow-revoke``) calls ``reconcile_rule_awards``
once per enabled rule — the same function as a Slack admin re-eval. That
path already skips T-Claps/DMs and posts one awards-channel line plus one
``paxminer_logs`` envelope per rule (existing ``run_summary_line`` /
``format_log_message``). Fourteen rules means up to fourteen of those
summaries, not 100+ per-award T-Claps.

``--schemas`` is required so a forgotten flag cannot score incomplete
attendance. Pass only the regional schema(s) that should be scored.

A YTD run without ``--allow-revoke`` only fills the current year and
leaves existing award history in place; it does not go through
``reconcile_rule_awards``.

Not ``run_daily``: that still emits per-award Slack log lines.

Use the PAXminer venv (pandas, slack_sdk):

  PAXminer/.venv/bin/python migration/rehearsal_reconcile.py \\
      --env rehearsal --schemas <regional_schema> \\
      --all-attendance --allow-revoke

  PAXminer/.venv/bin/python migration/rehearsal_reconcile.py \\
      --env rehearsal --schemas <regional_schema> \\
      --all-attendance --allow-revoke --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

_MIGRATION_DIR = Path(__file__).resolve().parent
_REPO = _MIGRATION_DIR.parent
sys.path.insert(0, str(_MIGRATION_DIR))
sys.path.insert(0, str(_REPO / "PAXminer"))

from paxminer_phases.db import (  # noqa: E402
    ENV_STAGES,
    _connect,
    _load_env,
    _pm_schema,
    assert_regional_stage,
)

LOG = logging.getLogger(__name__)

RECONCILE_TIMEOUT_S = 7200


def parse_schemas(raw: list[str]) -> list[str]:
    """Flatten comma-separated --schemas values. Empty is a hard error."""
    names: list[str] = []
    for item in raw:
        for part in item.split(","):
            name = part.strip()
            if name:
                names.append(name)
    if not names:
        raise SystemExit("--schemas is required and must list at least one regional schema")
    return names


def validate_rejudge_flags(*, all_attendance: bool, allow_revoke: bool) -> None:
    """Cutover re-eval is all-time + revoke. YTD+revoke would leave pre-window rows."""
    if allow_revoke and not all_attendance:
        raise SystemExit(
            "Refusing --allow-revoke without --all-attendance: a year-to-date "
            "revoke only judges the current window and leaves earlier Weaselbot "
            "rows in place, so the table would not match the JSON-seeded rules"
        )


def earliest_beatdown(cur, schema: str) -> date:
    cur.execute(f"SELECT MIN(bd_date) AS d FROM `{schema}`.`beatdowns`")
    row = cur.fetchone() or {}
    raw = row.get("d")
    if raw is None:
        raise RuntimeError(f"no beatdowns in {schema}; cannot --all-attendance")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw)[:10])


def load_region_row(cur, pm_schema: str, regional_schema: str) -> dict:
    cur.execute(
        f"SELECT * FROM `{pm_schema}`.`regions` WHERE schema_name=%s LIMIT 1",
        (regional_schema,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"no {pm_schema}.regions row for schema_name={regional_schema!r}")
    return row


def summarize_result(result: dict) -> dict:
    keys = (
        "grants",
        "revokes",
        "held",
        "held_older_version",
        "held_grandfathered",
        "skipped",
        "dry_run",
        "duration_s",
        "rules",
        "results_line",
    )
    return {k: result.get(k) for k in keys if k in result or result.get(k) is not None}


def load_enabled_achievement_ids(cur, schema: str) -> list[int]:
    cur.execute(
        f"SELECT id FROM `{schema}`.`achievements_list` WHERE COALESCE(enabled, 1) = 1 ORDER BY id"
    )
    return [int(row["id"]) for row in (cur.fetchall() or [])]


def run_schemas(
    conn,
    pm_schema: str,
    schemas: list[str],
    *,
    dry_run: bool,
    all_attendance: bool,
    allow_revoke: bool,
) -> list[dict]:
    # Lazy: achievements.runner pulls pandas, which is not in migration/requirements.txt.
    from achievements.runner import reconcile_rule_awards, run_achievements_for_region

    loaded: list[tuple[str, dict, date | None, list[int]]] = []
    with conn.cursor() as cur:
        for schema in schemas:
            assert_regional_stage(schema, pm_schema)
            start = earliest_beatdown(cur, schema) if all_attendance else None
            region_row = load_region_row(cur, pm_schema, schema)
            ids = load_enabled_achievement_ids(cur, schema) if allow_revoke else []
            loaded.append((schema, region_row, start, ids))

    outcomes: list[dict] = []
    for schema, region_row, start, ids in loaded:
        LOG.info(
            "Reconciling %s (region=%s dry_run=%s all_attendance=%s start=%s allow_revoke=%s rules=%s)",
            schema,
            region_row.get("region"),
            dry_run,
            all_attendance,
            start.isoformat() if start else "YTD",
            allow_revoke,
            len(ids) if allow_revoke else "all",
        )
        if allow_revoke:
            per_rule: list[dict] = []
            grants = revokes = held = 0
            for aid in ids:
                result = reconcile_rule_awards(
                    conn,
                    pm_schema=pm_schema,
                    regional_schema=schema,
                    region_row=region_row,
                    achievement_id=aid,
                    start=start,
                    end=date.today(),
                    dry_run=dry_run,
                    action="re-evaluated",
                )
                grants += int(result.get("grants") or 0)
                revokes += int(result.get("revokes") or 0)
                held += int(result.get("held") or 0)
                per_rule.append({"achievement_id": aid, **summarize_result(result)})
            outcome = {
                "schema": schema,
                "region": region_row.get("region"),
                "start": start.isoformat() if start else None,
                "grants": grants,
                "revokes": revokes,
                "held": held,
                "rules": len(ids),
                "dry_run": dry_run,
                "per_rule": per_rule,
            }
        else:
            kwargs: dict = {
                "dry_run": dry_run,
                "announce": False,
                "emit_logs": False,
                # Not scheduled/backfill: those modes construct a Slack client and
                # call users.list even when announce/emit_logs are off.
                "log_mode": "cli",
            }
            if start is not None:
                kwargs["start"] = start
                kwargs["end"] = date.today()
            result = run_achievements_for_region(
                conn,
                pm_schema=pm_schema,
                regional_schema=schema,
                region_row=region_row,
                **kwargs,
            )
            if not dry_run:
                conn.commit()
            outcome = {
                "schema": schema,
                "region": region_row.get("region"),
                "start": start.isoformat() if start else None,
                **summarize_result(result),
            }
        LOG.info("  %s", outcome)
        outcomes.append(outcome)
    return outcomes


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="All-time achievements re-eval (existing per-rule reconcile; no T-Claps)"
    )
    parser.add_argument("--env", required=True, choices=ENV_STAGES)
    parser.add_argument(
        "--schemas",
        required=True,
        nargs="+",
        help="Regional schema(s) to score. Required; never defaults to all active regions.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--all-attendance",
        action="store_true",
        help="Evaluate from the earliest beatdown through today, not year-to-date.",
    )
    parser.add_argument(
        "--allow-revoke",
        action="store_true",
        help="Delete awards the JSON-seeded rules no longer grant. Requires --all-attendance.",
    )
    args = parser.parse_args(argv)
    schemas = parse_schemas(args.schemas)
    validate_rejudge_flags(all_attendance=args.all_attendance, allow_revoke=args.allow_revoke)

    _load_env(args.env)
    pm_schema = _pm_schema(args.env)
    conn = _connect(read_timeout=RECONCILE_TIMEOUT_S, write_timeout=RECONCILE_TIMEOUT_S)
    try:
        outcomes = run_schemas(
            conn,
            pm_schema,
            schemas,
            dry_run=args.dry_run,
            all_attendance=args.all_attendance,
            allow_revoke=args.allow_revoke,
        )
        print(json.dumps(outcomes, indent=2, default=str), flush=True)
        if any("error" in o for o in outcomes):
            return 1
        return 0
    except Exception:
        conn.rollback()
        LOG.exception("rehearsal_reconcile failed")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
