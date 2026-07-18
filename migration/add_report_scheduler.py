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


def main() -> int:
    parser = argparse.ArgumentParser(description="Add PAXMiner report scheduler tables and seed defaults")
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    args = parser.parse_args()

    _load_env(args.env)
    list_handler = _ListHandler()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), list_handler],
    )

    pm_schema = _pm_schema(args.env)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    LOG.info("add_report_scheduler start env=%s pm_schema=%s", args.env, pm_schema)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            added_tz = ensure_timezone_column(cur, pm_schema)
            ensure_scheduler_tables(cur, pm_schema)
            LOG.info(
                "Tables ready (timezone_added=%s): region_report_definitions, region_schedules",
                added_tz,
            )
            counts = seed_all_regions(cur, pm_schema)
            LOG.info("Seeded regions=%s schedules=%s", counts["regions"], counts["schedules"])
        conn.commit()
    except Exception:
        conn.rollback()
        LOG.exception("Migration failed")
        return 1
    finally:
        conn.close()

    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    receipt = _RECEIPTS_DIR / f"add-report-scheduler-{args.env}-{stamp}.txt"
    receipt.write_text(
        "\n".join(
            [
                f"started={started}",
                f"finished={finished}",
                f"env={args.env}",
                f"pm_schema={pm_schema}",
                "",
                *list_handler.lines,
                "",
            ]
        ),
        encoding="utf-8",
    )
    LOG.info("Wrote receipt %s", receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
