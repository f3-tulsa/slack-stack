#!/usr/bin/env python3
"""
Idempotent migration: fold weaselbot_{stage}.regions config into paxminer_{stage}.regions,
add achievements_list rule columns to regional schemas, optionally drop weaselbot schema.

Usage:
  python migrate_weaselbot_to_paxminer.py --env test
  python migrate_weaselbot_to_paxminer.py --env test --force
  python migrate_weaselbot_to_paxminer.py --env prod --drop-weaselbot-schema

Loads TARGET_* credentials from .env.migration.<stage> (same as migrate_data.py).
Writes a receipt under migration/receipts/ (gitignored) including the full console log.
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

from achievements.achievement_rules import (  # noqa: E402
    ACHIEVEMENT_SEEDS,
    ACHIEVEMENTS_AWARDED_DDL,
    ACHIEVEMENTS_LIST_DDL,
    ACHIEVEMENTS_VIEW_DDL,
    RULE_COLUMNS,
)

LOG = logging.getLogger(__name__)
_RECEIPTS_DIR = Path(__file__).parent / "receipts"

_PM_REGION_COLS = (
    "send_achievements",
    "send_aoq_reports",
    "send_achievement_leaderboard",
    "achievement_channel",
    "kotter_channel",
    "NO_POST_THRESHOLD",
    "REMINDER_WEEKS",
    "HOME_AO_CAPTURE",
    "NO_Q_THRESHOLD_WEEKS",
    "NO_Q_THRESHOLD_POSTS",
)


class _ListHandler(logging.Handler):
    """Capture formatted log lines for the receipt file."""

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

    # Same TARGET_* names as migrate_data.py / .env.migration.*
    return pymysql.connect(
        host=os.environ["TARGET_HOST"],
        port=int(os.environ.get("TARGET_PORT", "4000")),
        user=os.environ["TARGET_USER"],
        password=os.environ["TARGET_PASSWORD"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        ssl={"ssl": {}} if os.environ.get("TARGET_TLS_ENABLED", "true").lower() in ("1", "true", "yes") else None,
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


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        """,
        (schema, table),
    )
    return int(cur.fetchone()["c"]) > 0


def _weaselbot_source_available(cur, wb_schema: str) -> bool:
    """True when weaselbot schema still has a regions table to copy from."""
    return _table_exists(cur, wb_schema, "regions")


def _pm_columns_complete(cur, pm_schema: str) -> bool:
    return all(_column_exists(cur, pm_schema, "regions", col) for col in _PM_REGION_COLS)


def _sb_column_complete(cur, sb_schema: str) -> bool:
    return _column_exists(cur, sb_schema, "regions", "post_achievements_to_ao")


def alter_paxminer_regions(cur, pm_schema: str) -> list[str]:
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
    added: list[str] = []
    for col, typedef in alters:
        if not _column_exists(cur, pm_schema, "regions", col):
            cur.execute(f"ALTER TABLE `{pm_schema}`.`regions` ADD COLUMN `{col}` {typedef}")
            LOG.info("Added %s.regions.%s", pm_schema, col)
            added.append(col)
        else:
            LOG.info("%s.regions.%s already present (DDL not needed)", pm_schema, col)
    return added


def alter_slackblast_regions(cur, sb_schema: str) -> bool:
    if not _column_exists(cur, sb_schema, "regions", "post_achievements_to_ao"):
        cur.execute(
            f"ALTER TABLE `{sb_schema}`.`regions` ADD COLUMN `post_achievements_to_ao` TINYINT DEFAULT 0"
        )
        LOG.info("Added %s.regions.post_achievements_to_ao", sb_schema)
        return True
    LOG.info("%s.regions.post_achievements_to_ao already present (DDL not needed)", sb_schema)
    return False


def copy_weaselbot_config(cur, pm_schema: str, wb_schema: str) -> list[dict]:
    """Copy weaselbot config into paxminer.regions. Caller must ensure source exists."""
    cur.execute(f"SELECT * FROM `{wb_schema}`.`regions` WHERE paxminer_schema IS NOT NULL")
    wb_rows = cur.fetchall()
    copied: list[dict] = []
    LOG.info("Reading config from %s.regions (%s row(s) with paxminer_schema)", wb_schema, len(wb_rows))
    for wb in wb_rows:
        pax_schema = wb.get("paxminer_schema")
        if not pax_schema:
            continue
        cur.execute(
            f"SELECT region, schema_name FROM `{pm_schema}`.`regions` WHERE schema_name=%s LIMIT 1",
            (pax_schema,),
        )
        pm_row = cur.fetchone()
        if not pm_row:
            LOG.warning(
                "No %s.regions row for schema_name=%s; skip weaselbot config copy",
                pm_schema,
                pax_schema,
            )
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
        LOG.info(
            "Copying weaselbot config %s.regions → %s.regions (region=%s, schema_name=%s)",
            wb_schema,
            pm_schema,
            region_key,
            pax_schema,
        )
        for key, value in updates.items():
            LOG.info("  %s = %r", key, value)
        copied.append({"region": region_key, "schema_name": pax_schema})
    return copied


def ensure_regional_achievements(cur, regional_schema: str, *, upsert_seeds: bool) -> dict:
    LOG.info("Ensuring achievements tables/rules in %s", regional_schema)
    cur.execute(ACHIEVEMENTS_LIST_DDL.format(schema=regional_schema))
    cur.execute(ACHIEVEMENTS_AWARDED_DDL.format(schema=regional_schema))
    cols_added: list[str] = []
    for col in RULE_COLUMNS:
        if not _column_exists(cur, regional_schema, "achievements_list", col):
            if col == "threshold":
                cur.execute(
                    f"ALTER TABLE `{regional_schema}`.`achievements_list` "
                    f"ADD COLUMN `{col}` int NOT NULL DEFAULT 1"
                )
            else:
                default = (
                    "'posts'"
                    if col == "metric"
                    else ("'beatdown'" if col == "activity" else "'year'")
                )
                cur.execute(
                    f"ALTER TABLE `{regional_schema}`.`achievements_list` "
                    f"ADD COLUMN `{col}` varchar(32) NOT NULL DEFAULT {default}"
                )
            LOG.info("Added %s.achievements_list.%s", regional_schema, col)
            cols_added.append(col)

    seeds_upserted = 0
    if upsert_seeds:
        for seed in ACHIEVEMENT_SEEDS:
            cur.execute(
                f"SELECT id FROM `{regional_schema}`.`achievements_list` WHERE code=%s",
                (seed["code"],),
            )
            existing = cur.fetchone()
            action = "update" if existing else "insert"
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
            LOG.info(
                "  %s achievement code=%s name=%r metric=%s activity=%s period=%s threshold=%s",
                action,
                seed["code"],
                seed["name"],
                seed["metric"],
                seed["activity"],
                seed["period"],
                seed["threshold"],
            )
            seeds_upserted += 1
        LOG.info("Upserted %s achievement seed(s) into %s.achievements_list", seeds_upserted, regional_schema)
    else:
        LOG.info(
            "Skip achievement seed upsert for %s (already migrated; use --force to re-apply)",
            regional_schema,
        )

    view_ok = True
    try:
        cur.execute(ACHIEVEMENTS_VIEW_DDL.format(schema=regional_schema))
        LOG.info("Created/replaced %s.achievements_view", regional_schema)
    except Exception as e:
        view_ok = False
        LOG.warning("achievements_view create skipped for %s: %s", regional_schema, e)
    return {
        "schema": regional_schema,
        "rule_columns_added": cols_added,
        "seeds_upserted": seeds_upserted,
        "view_ok": view_ok,
    }


def drop_weaselbot_schema(cur, wb_schema: str) -> None:
    cur.execute(f"DROP DATABASE IF EXISTS `{wb_schema}`")
    LOG.info("Dropped schema %s", wb_schema)


def _write_receipt(stage: str, header: list[str], log_lines: list[str]) -> Path:
    _RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = _RECEIPTS_DIR / f"migrate-weaselbot-to-paxminer-{stage}-{stamp}.txt"
    body_lines = [
        *header,
        "",
        "=== Console log ===",
        *log_lines,
        "",
    ]
    path.write_text("\n".join(body_lines) + "\n")
    return path


def main() -> None:
    log_capture = _ListHandler()
    log_capture.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)
    root.addHandler(log_capture)

    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-upsert achievement seeds (and re-copy weaselbot config if the "
            "weaselbot schema still exists) even when columns already exist"
        ),
    )
    parser.add_argument(
        "--drop-weaselbot-schema",
        action="store_true",
        help="Drop weaselbot_<stage> after a successful migration (irreversible)",
    )
    args = parser.parse_args()
    stage = args.env
    _load_env(stage)
    pm_schema = f"paxminer_{stage}"
    wb_schema = f"weaselbot_{stage}"
    sb_schema = f"slackblast_{stage}"
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    LOG.info(
        "Starting weaselbot→paxminer migration stage=%s force=%s drop_weaselbot=%s "
        "schemas: %s, %s, %s (TARGET_HOST=%s)",
        stage,
        args.force,
        args.drop_weaselbot_schema,
        pm_schema,
        wb_schema,
        sb_schema,
        os.environ.get("TARGET_HOST", ""),
    )

    header = [
        "=== Weaselbot → PAXMiner migration receipt ===",
        f"Started (UTC): {started}",
        f"Stage: {stage}",
        f"Force: {args.force}",
        f"TARGET_HOST: {os.environ.get('TARGET_HOST', '')}",
        f"Schemas in scope: {pm_schema}, {sb_schema}, {wb_schema}",
        f"Drop weaselbot schema: {args.drop_weaselbot_schema}",
    ]

    conn = _connect()
    try:
        with conn.cursor() as cur:
            schema_already = _pm_columns_complete(cur, pm_schema) and _sb_column_complete(cur, sb_schema)
            wb_available = _weaselbot_source_available(cur, wb_schema)
            # Seeds can always be re-applied from code; config copy requires weaselbot source.
            apply_seeds = args.force or not schema_already
            apply_config = apply_seeds and wb_available

            if not wb_available:
                LOG.info(
                    "Weaselbot source %s.regions not present (already dropped?); "
                    "config copy will be skipped even with --force",
                    wb_schema,
                )
            if schema_already and not args.force:
                LOG.info(
                    "Schema columns already present; skipping config copy and achievement seed "
                    "upserts (pass --force to re-apply seeds; config only if weaselbot still exists)"
                )
            elif args.force and schema_already:
                if wb_available:
                    LOG.info("--force: re-applying config copy and achievement seed upserts")
                else:
                    LOG.info(
                        "--force: re-applying achievement seed upserts only "
                        "(no weaselbot source for config copy)"
                    )

            pm_cols = alter_paxminer_regions(cur, pm_schema)
            LOG.info("%s.regions columns added this run: %s", pm_schema, pm_cols or "(none)")
            sb_added = alter_slackblast_regions(cur, sb_schema)
            LOG.info(
                "%s.regions.post_achievements_to_ao: %s",
                sb_schema,
                "added" if sb_added else "already present",
            )

            if apply_config:
                copied = copy_weaselbot_config(cur, pm_schema, wb_schema)
                if not copied:
                    LOG.info("Config copy: no rows copied from %s.regions", wb_schema)
            elif not wb_available:
                LOG.info("Config copy skipped: weaselbot source unavailable")
            else:
                LOG.info("Config copy skipped (use --force to re-apply while weaselbot still exists)")

            cur.execute(
                f"SELECT region, schema_name FROM `{pm_schema}`.`regions` "
                f"WHERE active=1 AND schema_name IS NOT NULL"
            )
            region_rows = cur.fetchall()
            LOG.info("Regional schemas from %s.regions (active):", pm_schema)
            for row in region_rows:
                LOG.info("  region=%s schema_name=%s", row.get("region"), row.get("schema_name"))

            for row in region_rows:
                schema = row.get("schema_name")
                if schema:
                    # Always add missing rule columns; upsert seeds when applying or cols missing.
                    rule_cols_missing = any(
                        not _column_exists(cur, schema, "achievements_list", col) for col in RULE_COLUMNS
                    )
                    upsert_seeds = apply_seeds or rule_cols_missing
                    ensure_regional_achievements(cur, schema, upsert_seeds=upsert_seeds)

            if args.drop_weaselbot_schema:
                if not wb_available:
                    LOG.info("Drop skipped: %s already absent", wb_schema)
                else:
                    drop_weaselbot_schema(cur, wb_schema)
        conn.commit()
    finally:
        conn.close()

    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    LOG.info("Finished (UTC): %s", finished)
    LOG.info("Migration complete for stage=%s", stage)
    # Capture "Receipt written..." after file write would miss it; write with lines so far + finish note.
    path = _write_receipt(stage, header, list(log_capture.lines))
    # Append path onto console (and a one-line note; receipt already has full log up to finish)
    print(f"Receipt written to {path}", flush=True)


if __name__ == "__main__":
    main()
