#!/usr/bin/env python3
"""
Orchestrate PAXMiner DB migrations: weaselbot fold-in, report scheduler, drop legacy columns.

Deploy updated Slackblast + PAXMiner application code BEFORE running ``--all`` (or the
``drop-legacy-columns`` phase alone). After those columns are dropped, ORM queries that still
SELECT them will fail.

Usage:
  python migration/paxminer_migrate.py --env test --phase weaselbot
  python migration/paxminer_migrate.py --env test --phase scheduler
  python migration/paxminer_migrate.py --env test --phase drop-legacy-columns
  python migration/paxminer_migrate.py --env test --all
  python migration/paxminer_migrate.py --env test --phase weaselbot --force
  python migration/paxminer_migrate.py --env prod --phase weaselbot --drop-weaselbot-schema

Loads TARGET_* credentials from migration/.env.migration.<stage>.
Writes a receipt under migration/receipts/ (gitignored).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_MIGRATION_DIR = Path(__file__).resolve().parent
if str(_MIGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATION_DIR))

from paxminer_phases.db import _ListHandler, _connect, _load_env, _write_receipt  # noqa: E402
from paxminer_phases.drop_legacy import run_drop_legacy_columns  # noqa: E402
from paxminer_phases.scheduler import run_scheduler  # noqa: E402
from paxminer_phases.weaselbot import run_weaselbot  # noqa: E402

LOG = logging.getLogger(__name__)

PHASE_ORDER = ("weaselbot", "scheduler", "drop-legacy-columns")
PHASE_CHOICES = PHASE_ORDER


def _setup_logging() -> _ListHandler:
    log_capture = _ListHandler()
    log_capture.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)
    root.addHandler(log_capture)
    return log_capture


def _format_phase_summary(phase: str, result: dict) -> list[str]:
    if phase == "weaselbot":
        return [
            f"  pm_columns_added: {result.get('pm_columns_added') or '(none)'}",
            f"  sb_post_achievements_added: {result.get('sb_post_achievements_added')}",
            f"  config_rows_copied: {result.get('config_rows_copied', 0)}",
            f"  regional_schemas: {result.get('regional_schemas', 0)}",
            f"  weaselbot_schema_dropped: {result.get('weaselbot_schema_dropped', False)}",
        ]
    if phase == "scheduler":
        return [
            f"  timezone_added: {result.get('timezone_added')}",
            f"  tables_created: {', '.join(result.get('tables_created') or []) or '(none)'}",
            f"  active regions: {result.get('regions', 0)}",
            f"  regions_with_schema: {result.get('regions_with_schema', 0)}",
            f"  definitions upserted: {result.get('definitions', 0)}",
            f"  schedules inserted: {result.get('schedules', 0)}",
        ]
    if phase == "drop-legacy-columns":
        return [
            f"  dropped: {', '.join(result.get('dropped') or []) or '(none)'}",
            f"  already absent: {', '.join(result.get('skipped') or []) or '(none)'}",
        ]
    return [f"  {result}"]


def _run_phase(
    phase: str,
    cur,
    stage: str,
    *,
    force: bool,
    drop_weaselbot_schema: bool,
) -> dict:
    if phase == "weaselbot":
        return run_weaselbot(
            cur,
            stage,
            force=force,
            drop_weaselbot_schema=drop_weaselbot_schema,
        )
    if phase == "scheduler":
        return run_scheduler(cur, stage)
    if phase == "drop-legacy-columns":
        return run_drop_legacy_columns(cur, stage)
    raise ValueError(f"Unknown phase: {phase}")


def main(argv: list[str] | None = None) -> int:
    log_capture = _setup_logging()

    parser = argparse.ArgumentParser(description="PAXMiner DB migration orchestrator")
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    phase_group = parser.add_mutually_exclusive_group(required=True)
    phase_group.add_argument("--phase", choices=PHASE_CHOICES)
    phase_group.add_argument("--all", action="store_true", help="Run weaselbot → scheduler → drop-legacy-columns")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Weaselbot: re-upsert achievement seeds and re-copy config when source exists",
    )
    parser.add_argument(
        "--drop-weaselbot-schema",
        action="store_true",
        help="Weaselbot: drop weaselbot_<stage> after a successful run (irreversible)",
    )
    args = parser.parse_args(argv)

    stage = args.env
    phases = list(PHASE_ORDER) if args.all else [args.phase]

    _load_env(stage)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    LOG.info(
        "Starting PAXMiner migration stage=%s phases=%s force=%s drop_weaselbot=%s (TARGET_HOST=%s)",
        stage,
        " → ".join(phases),
        args.force,
        args.drop_weaselbot_schema,
        os.environ.get("TARGET_HOST", ""),
    )

    header = [
        "=== PAXMiner migration receipt ===",
        f"Started (UTC): {started}",
        f"Stage: {stage}",
        f"Phases: {' → '.join(phases)}",
        f"Force (weaselbot): {args.force}",
        f"Drop weaselbot schema: {args.drop_weaselbot_schema}",
        f"TARGET_HOST: {os.environ.get('TARGET_HOST', '')}",
    ]

    phase_outcomes: list[dict] = []
    overall_ok = True
    conn = _connect()
    try:
        for phase in phases:
            LOG.info("--- Phase: %s ---", phase)
            try:
                with conn.cursor() as cur:
                    result = _run_phase(
                        phase,
                        cur,
                        stage,
                        force=args.force,
                        drop_weaselbot_schema=args.drop_weaselbot_schema,
                    )
                conn.commit()
                LOG.info("Phase %s committed", phase)
                phase_outcomes.append({"phase": phase, "status": "ok", "result": result})
            except Exception:
                conn.rollback()
                LOG.exception("Phase %s failed (rolled back; prior phases remain committed)", phase)
                phase_outcomes.append({"phase": phase, "status": "failed"})
                overall_ok = False
                break
    finally:
        conn.close()

    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    LOG.info("Finished (UTC): %s", finished)

    summary = [
        f"Result: {'OK' if overall_ok else 'FAILED'}",
        f"Finished (UTC): {finished}",
        "",
    ]
    for outcome in phase_outcomes:
        summary.append(f"Phase {outcome['phase']}: {outcome['status'].upper()}")
        if outcome["status"] == "ok":
            summary.extend(_format_phase_summary(outcome["phase"], outcome["result"]))
        summary.append("")

    receipt_prefix = "paxminer-migrate-all" if args.all else f"paxminer-migrate-{phases[0]}"
    path = _write_receipt(receipt_prefix, stage, header, list(log_capture.lines), summary=summary)
    print(f"Receipt written to {path}", flush=True)

    if overall_ok:
        LOG.info("Migration complete for stage=%s", stage)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
