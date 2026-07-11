#!/usr/bin/env python3
"""
Idempotent migration: fold weaselbot_{stage}.regions config into paxminer_{stage}.regions,
add achievements_list rule columns to regional schemas, optionally drop weaselbot schema.

Usage:
  python migrate_weaselbot_to_paxminer.py --env test
  python migrate_weaselbot_to_paxminer.py --env prod --drop-weaselbot-schema
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "PAXminer"))

from achievements.achievement_rules import (  # noqa: E402
    ACHIEVEMENT_SEEDS,
    ACHIEVEMENTS_AWARDED_DDL,
    ACHIEVEMENTS_LIST_DDL,
    ACHIEVEMENTS_VIEW_DDL,
    RULE_COLUMNS,
)

LOG = logging.getLogger(__name__)


def _load_env(stage: str) -> None:
    from dotenv import load_dotenv

    env_file = Path(__file__).parent / f".env.migration.{stage}"
    if env_file.exists():
        load_dotenv(env_file)
    elif Path(__file__).parent / ".env.migration" in []:
        pass


def _connect():
    import pymysql

    return pymysql.connect(
        host=os.environ["DATABASE_HOST"],
        port=int(os.environ.get("DATABASE_PORT", "4000")),
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        ssl={"ssl": {}} if os.environ.get("DATABASE_TLS_ENABLED", "true").lower() in ("1", "true", "yes") else None,
    )


def _column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """,
        (schema, table, column),
    )
    return int(cur.fetchone()["c"]) > 0


def alter_paxminer_regions(cur, pm_schema: str) -> None:
    alters = [
        ("send_achievements", "TINYINT DEFAULT 1"),
        ("send_aoq_reports", "TINYINT DEFAULT 1"),
        ("send_achievement_leaderboard", "TINYINT DEFAULT 1"),
        ("achievement_channel", "VARCHAR(100) DEFAULT NULL"),
        ("kotter_channel", "VARCHAR(100) DEFAULT NULL"),
        ("NO_POST_THRESHOLD", "INT DEFAULT 2"),
        ("REMINDER_WEEKS", "INT DEFAULT 2"),
        ("HOME_AO_CAPTURE", "INT DEFAULT 8"),
        ("NO_Q_THRESHOLD_WEEKS", "INT DEFAULT 4"),
        ("NO_Q_THRESHOLD_POSTS", "INT DEFAULT 4"),
    ]
    for col, typedef in alters:
        if not _column_exists(cur, pm_schema, "regions", col):
            cur.execute(f"ALTER TABLE `{pm_schema}`.`regions` ADD COLUMN `{col}` {typedef}")
            LOG.info("Added paxminer.regions.%s", col)


def alter_slackblast_regions(cur, sb_schema: str) -> None:
    if not _column_exists(cur, sb_schema, "regions", "post_achievements_to_ao"):
        cur.execute(
            f"ALTER TABLE `{sb_schema}`.`regions` ADD COLUMN `post_achievements_to_ao` TINYINT DEFAULT 0"
        )
        LOG.info("Added slackblast.regions.post_achievements_to_ao")


def copy_weaselbot_config(cur, pm_schema: str, wb_schema: str) -> None:
    cur.execute(f"SELECT * FROM `{wb_schema}`.`regions` WHERE paxminer_schema IS NOT NULL")
    wb_rows = cur.fetchall()
    for wb in wb_rows:
        pax_schema = wb.get("paxminer_schema")
        if not pax_schema:
            continue
        cur.execute(
            f"SELECT region FROM `{pm_schema}`.`regions` WHERE schema_name=%s LIMIT 1",
            (pax_schema,),
        )
        pm_row = cur.fetchone()
        if not pm_row:
            LOG.warning("No paxminer.regions row for schema %s; skip weaselbot config copy", pax_schema)
            continue
        region_key = pm_row["region"]
        kotter = wb.get("default_siteq")
        if kotter and not (str(kotter).startswith("C") or str(kotter).startswith("G")):
            kotter = None
        updates = {
            "send_achievements": wb.get("send_achievements"),
            "send_aoq_reports": wb.get("send_aoq_reports"),
            "achievement_channel": wb.get("achievement_channel"),
            "kotter_channel": kotter,
            "NO_POST_THRESHOLD": wb.get("NO_POST_THRESHOLD"),
            "REMINDER_WEEKS": wb.get("REMINDER_WEEKS"),
            "HOME_AO_CAPTURE": wb.get("HOME_AO_CAPTURE"),
            "NO_Q_THRESHOLD_WEEKS": wb.get("NO_Q_THRESHOLD_WEEKS"),
            "NO_Q_THRESHOLD_POSTS": wb.get("NO_Q_THRESHOLD_POSTS"),
        }
        sets = ", ".join(f"`{k}`=%s" for k in updates)
        cur.execute(
            f"UPDATE `{pm_schema}`.`regions` SET {sets} WHERE region=%s",
            (*updates.values(), region_key),
        )
        LOG.info("Copied weaselbot config into paxminer.regions for %s", region_key)


def ensure_regional_achievements(cur, regional_schema: str) -> None:
    cur.execute(ACHIEVEMENTS_LIST_DDL.format(schema=regional_schema))
    cur.execute(ACHIEVEMENTS_AWARDED_DDL.format(schema=regional_schema))
    for col in RULE_COLUMNS:
        if not _column_exists(cur, regional_schema, "achievements_list", col):
            default = "1" if col == "threshold" else ("'posts'" if col == "metric" else ("'beatdown'" if col == "activity" else "'year'"))
            cur.execute(
                f"ALTER TABLE `{regional_schema}`.`achievements_list` "
                f"ADD COLUMN `{col}` varchar(32) NOT NULL DEFAULT {default}"
                if col != "threshold"
                else f"ALTER TABLE `{regional_schema}`.`achievements_list` ADD COLUMN `{col}` int NOT NULL DEFAULT 1"
            )
    for seed in ACHIEVEMENT_SEEDS:
        cur.execute(
            f"SELECT id FROM `{regional_schema}`.`achievements_list` WHERE code=%s",
            (seed["code"],),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                f"""
                UPDATE `{regional_schema}`.`achievements_list`
                SET name=%s, description=%s, verb=%s, metric=%s, activity=%s, period=%s, threshold=%s
                WHERE code=%s
                """,
                (
                    seed["name"],
                    seed["description"],
                    seed["verb"],
                    seed["metric"],
                    seed["activity"],
                    seed["period"],
                    seed["threshold"],
                    seed["code"],
                ),
            )
        else:
            cur.execute(
                f"""
                INSERT INTO `{regional_schema}`.`achievements_list`
                (name, description, verb, code, metric, activity, period, threshold)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    seed["name"],
                    seed["description"],
                    seed["verb"],
                    seed["code"],
                    seed["metric"],
                    seed["activity"],
                    seed["period"],
                    seed["threshold"],
                ),
            )
    try:
        cur.execute(ACHIEVEMENTS_VIEW_DDL.format(schema=regional_schema))
    except Exception as e:
        LOG.warning("achievements_view create skipped for %s: %s", regional_schema, e)


def drop_weaselbot_schema(cur, wb_schema: str) -> None:
    cur.execute(f"DROP DATABASE IF EXISTS `{wb_schema}`")
    LOG.info("Dropped schema %s", wb_schema)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    parser.add_argument("--drop-weaselbot-schema", action="store_true")
    args = parser.parse_args()
    stage = args.env
    _load_env(stage)
    pm_schema = f"paxminer_{stage}"
    wb_schema = f"weaselbot_{stage}"
    sb_schema = f"slackblast_{stage}"

    conn = _connect()
    try:
        with conn.cursor() as cur:
            alter_paxminer_regions(cur, pm_schema)
            alter_slackblast_regions(cur, sb_schema)
            copy_weaselbot_config(cur, pm_schema, wb_schema)
            cur.execute(f"SELECT schema_name FROM `{pm_schema}`.`regions` WHERE active=1 AND schema_name IS NOT NULL")
            for row in cur.fetchall():
                schema = row["schema_name"]
                if schema:
                    ensure_regional_achievements(cur, schema)
            if args.drop_weaselbot_schema:
                drop_weaselbot_schema(cur, wb_schema)
        conn.commit()
    finally:
        conn.close()
    LOG.info("Migration complete for stage=%s", stage)


if __name__ == "__main__":
    main()
