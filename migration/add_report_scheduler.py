#!/usr/bin/env python3
"""
Idempotent migration: add PAXMiner report scheduler tables + regions.timezone,
seed builtin report definitions and default schedules from legacy send_* flags.

Usage:
  python add_report_scheduler.py --env test
  python add_report_scheduler.py --env prod

Loads TARGET_* credentials from .env.migration.<stage>.
Writes a receipt under migration/receipts/ (gitignored).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "PAXminer"))

from schedule_schema import (  # noqa: E402
    ensure_scheduler_tables,
    ensure_timezone_column,
    seed_all_regions,
)

LOG = logging.getLogger(__name__)
_RECEIPTS_DIR = Path(__file__).parent / "receipts"


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


def _load_env(stage: str) -> None:
    from dotenv import load_dotenv

    env_file = Path(__file__).parent / f".env.migration.{stage}"
    if env_file.exists():
        load_dotenv(env_file)


def _connect():
    import pymysql

    return pymysql.connect(
        host=os.environ["TARGET_HOST"],
        port=int(os.environ.get("TARGET_PORT", "4000")),
        user=os.environ["TARGET_USER"],
        password=os.environ["TARGET_PASSWORD"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        ssl={"ssl": {}} if os.environ.get("TARGET_TLS_ENABLED", "true").lower() in ("1", "true", "yes") else None,
    )


def _pm_schema(stage: str) -> str:
    return os.environ.get("TARGET_PAXMINER_SCHEMA") or f"paxminer_{stage}"


def _write_receipt(stage: str, header: list[str], summary: list[str], log_lines: list[str]) -> Path:
    _RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = _RECEIPTS_DIR / f"add-report-scheduler-{stage}-{stamp}.txt"
    body_lines = [
        *header,
        "",
        "=== Summary ===",
        *summary,
        "",
        "=== Console log ===",
        *log_lines,
        "",
    ]
    path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    list_handler = _ListHandler()
    list_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)
    root.addHandler(list_handler)

    parser = argparse.ArgumentParser(description="Add PAXMiner report scheduler tables and seed defaults")
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    args = parser.parse_args()

    _load_env(args.env)
    pm_schema = _pm_schema(args.env)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    LOG.info(
        "Starting report scheduler migration stage=%s pm_schema=%s (TARGET_HOST=%s)",
        args.env,
        pm_schema,
        os.environ.get("TARGET_HOST", ""),
    )
    header = [
        "=== PAXMiner report scheduler migration receipt ===",
        f"Started (UTC): {started}",
        f"Stage: {args.env}",
        f"TARGET_HOST: {os.environ.get('TARGET_HOST', '')}",
        f"PAXMiner schema: {pm_schema}",
    ]

    added_tz = False
    tables_created: list[str] = []
    counts: dict[str, int] = {"regions": 0, "regions_with_schema": 0, "definitions": 0, "schedules": 0}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            added_tz = ensure_timezone_column(cur, pm_schema)
            LOG.info(
                "%s.regions.timezone: %s",
                pm_schema,
                "added" if added_tz else "already present",
            )
            tables_created = ensure_scheduler_tables(cur, pm_schema)
            LOG.info("Scheduler tables created this run: %s", tables_created or "(none)")
            counts = seed_all_regions(cur, pm_schema)
        conn.commit()
    except Exception:
        conn.rollback()
        LOG.exception("Migration failed")
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = [
            "Result: FAILED",
            f"Finished (UTC): {finished}",
        ]
        path = _write_receipt(args.env, header, summary, list(list_handler.lines))
        print(f"Receipt written to {path}", flush=True)
        return 1
    finally:
        conn.close()

    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    LOG.info("Finished (UTC): %s", finished)
    LOG.info("Migration complete for stage=%s", args.env)

    summary = [
        "Result: OK",
        f"Finished (UTC): {finished}",
        f"regions.timezone added: {added_tz}",
        f"Tables created this run: {', '.join(tables_created) if tables_created else '(none)'}",
        f"Active regions: {counts['regions']}",
        f"Regions with schema_name: {counts['regions_with_schema']}",
        f"Report definitions upserted: {counts['definitions']}",
        f"Schedule rows inserted: {counts['schedules']}",
    ]
    path = _write_receipt(args.env, header, summary, list(list_handler.lines))
    print(f"Receipt written to {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
