#!/usr/bin/env python3
"""
Remap qsignups ao_channel_id and team_id on the target DB after migration.

Reads a CSV with prod vs test Slack IDs (see --csv). Run after migrate_data.py when
test workspace channel IDs differ from the national RDS (prod) values.

  python migration/remap_qsignups.py --env test --csv path/to/mapping.csv

Uses migration/.env.migration.<env> (TARGET_* and QSIGNUPS_SCHEMA), same as migrate_data.py.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Any

import pymysql
from dotenv import load_dotenv

_MIG_DIR = Path(__file__).resolve().parent
if str(_MIG_DIR) not in sys.path:
    sys.path.insert(0, str(_MIG_DIR))

from migrate_data import (  # noqa: E402
    F3STCHARLES_SOURCE,
    _connect_kwargs,
    _env_bool,
    default_schema_map,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("remap_qsignups")


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = (
            "prod_ao_channel_id",
            "prod_team_id",
            "test_ao_channel_id",
            "test_team_id",
        )
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row")
        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"CSV missing columns: {missing}; found {reader.fieldnames}")
        return [dict(r) for r in reader]


def main() -> int:
    parser = argparse.ArgumentParser(description="Remap qsignups Slack IDs on target DB from CSV")
    parser.add_argument(
        "--env",
        required=True,
        choices=["test", "prod"],
        help="Loads migration/.env.migration.<env>",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Mapping CSV path (required for env=test)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned updates without executing",
    )
    args = parser.parse_args()

    env_file = _MIG_DIR / f".env.migration.{args.env}"
    if not env_file.is_file():
        LOG.error("Missing %s", env_file)
        return 1
    load_dotenv(env_file)
    os.environ["STAGE"] = args.env

    if args.env == "prod":
        LOG.info(
            "env=prod: qsignups data should already use prod Slack IDs; remap is for test only. Exiting."
        )
        return 0

    if args.csv is None:
        LOG.error("--csv is required for env=test")
        return 1

    if not args.csv.is_file():
        LOG.error("CSV not found: %s", args.csv)
        return 1

    try:
        rows = _load_rows(args.csv)
    except ValueError as e:
        LOG.error("%s", e)
        return 1
    if not rows:
        LOG.error("CSV has no data rows")
        return 1

    prod_team_ids = {r["prod_team_id"].strip() for r in rows if r.get("prod_team_id")}
    test_team_ids = {r["test_team_id"].strip() for r in rows if r.get("test_team_id")}
    if len(prod_team_ids) != 1 or len(test_team_ids) != 1:
        LOG.error(
            "Expected exactly one prod_team_id and one test_team_id across CSV; got prod=%s test=%s",
            prod_team_ids,
            test_team_ids,
        )
        return 1
    prod_team = next(iter(prod_team_ids))
    test_team = next(iter(test_team_ids))

    schema_map = default_schema_map(args.env)
    qs_schema = schema_map.get(F3STCHARLES_SOURCE)
    if not qs_schema:
        LOG.error("Schema map missing %s", F3STCHARLES_SOURCE)
        return 1

    host = os.environ["TARGET_HOST"]
    port = int(os.environ.get("TARGET_PORT", "4000"))
    user = os.environ["TARGET_USER"]
    password = os.environ["TARGET_PASSWORD"]
    tls = _env_bool("TARGET_TLS_ENABLED", True)

    kw = _connect_kwargs(host, port, user, password, None, tls)
    # remap uses explicit schema-qualified SQL; no need for DictCursor on all ops
    kw.pop("cursorclass", None)

    LOG.info("Target schema %s (dry_run=%s)", qs_schema, args.dry_run)

    conn = pymysql.connect(**kw)
    try:
        cur = conn.cursor()

        def run(sql: str, params: tuple[Any, ...]) -> int:
            if args.dry_run:
                LOG.info("DRY-RUN: %s %s", sql, params)
                return 0
            cur.execute(sql, params)
            return int(cur.rowcount)

        # 1) team_id on qsignups_regions
        sql_r = (
            f"UPDATE `{qs_schema}`.`qsignups_regions` SET `team_id`=%s WHERE `team_id`=%s"
        )
        n = run(sql_r, (test_team, prod_team))
        if not args.dry_run:
            LOG.info("qsignups_regions: updated %s row(s) team_id %s -> %s", n, prod_team, test_team)

        ao_tables = ("qsignups_aos", "qsignups_weekly", "qsignups_master")
        for tbl in ao_tables:
            tbl_total = 0
            for r in rows:
                p_ao = r["prod_ao_channel_id"].strip()
                t_ao = r["test_ao_channel_id"].strip()
                sql_t = (
                    f"UPDATE `{qs_schema}`.`{tbl}` "
                    "SET `ao_channel_id`=%s, `team_id`=%s "
                    "WHERE `ao_channel_id`=%s AND `team_id`=%s"
                )
                n2 = run(sql_t, (t_ao, test_team, p_ao, prod_team))
                tbl_total += max(0, n2)
            LOG.info(
                "%s: %s row(s) affected (sum over CSV; dry_run=%s)",
                tbl,
                tbl_total,
                args.dry_run,
            )

        if not args.dry_run:
            conn.commit()
            LOG.info("Done. qsignups_features unchanged (region_id FK stable). Commit OK.")
        else:
            LOG.info("Dry-run complete; no changes committed.")
    except Exception:
        if not args.dry_run:
            conn.rollback()
        LOG.exception("Remap failed")
        return 1
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
