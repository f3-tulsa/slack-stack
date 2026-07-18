"""DDL and seed helpers for the report scheduler (PAXMiner-owned tables)."""

from __future__ import annotations

import json
import logging
from typing import Any

from scheduling import (
    BUILTIN_DEFINITIONS,
    DEFAULT_TIMEZONE,
    LEGACY_FLAG_MAP,
)

LOG = logging.getLogger(__name__)

DDL_REGION_REPORT_DEFINITIONS = """
CREATE TABLE IF NOT EXISTS `{schema}`.`region_report_definitions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `schema_name` varchar(45) NOT NULL,
  `code` varchar(64) NOT NULL,
  `name` varchar(120) NOT NULL,
  `report_type` varchar(40) NOT NULL,
  `is_builtin` tinyint NOT NULL DEFAULT 0,
  `kind` varchar(20) DEFAULT NULL,
  `source` varchar(40) DEFAULT NULL,
  `fields` json DEFAULT NULL,
  `metric` varchar(40) DEFAULT NULL,
  `aggregation` varchar(40) DEFAULT NULL,
  `group_by` varchar(80) DEFAULT NULL,
  `top_n` int DEFAULT NULL,
  `time_window_type` varchar(20) DEFAULT NULL,
  `window_days` int DEFAULT NULL,
  `window_start` date DEFAULT NULL,
  `window_end` date DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rrd_schema_code` (`schema_name`, `code`),
  KEY `idx_rrd_schema` (`schema_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DDL_REGION_SCHEDULES = """
CREATE TABLE IF NOT EXISTS `{schema}`.`region_schedules` (
  `id` int NOT NULL AUTO_INCREMENT,
  `schema_name` varchar(45) NOT NULL,
  `report_definition_id` int NOT NULL,
  `destination_type` varchar(40) NOT NULL,
  `destination_channels` json DEFAULT NULL,
  `destination_users` json DEFAULT NULL,
  `frequency_type` varchar(20) NOT NULL DEFAULT 'monthly',
  `day_of_week` tinyint DEFAULT NULL,
  `month_day_mode` varchar(20) DEFAULT 'first',
  `day_of_month` tinyint DEFAULT NULL,
  `time_of_day` time NOT NULL DEFAULT '07:00:00',
  `custom_spec` json DEFAULT NULL,
  `enabled` tinyint NOT NULL DEFAULT 1,
  `last_run_on` date DEFAULT NULL,
  `last_run_status` varchar(20) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_rs_schema` (`schema_name`),
  KEY `idx_rs_enabled` (`enabled`),
  CONSTRAINT `fk_rs_definition`
    FOREIGN KEY (`report_definition_id`)
    REFERENCES `{schema}`.`region_report_definitions` (`id`)
    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def ensure_timezone_column(cur, pm_schema: str) -> bool:
    """Add regions.timezone if missing. Returns True when a column was added."""
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME='regions' AND COLUMN_NAME='timezone'
        """,
        (pm_schema,),
    )
    if int(cur.fetchone()["c"]) > 0:
        return False
    cur.execute(
        f"""
        ALTER TABLE `{pm_schema}`.`regions`
        ADD COLUMN `timezone` varchar(45) NOT NULL DEFAULT %s
        """,
        (DEFAULT_TIMEZONE,),
    )
    LOG.info("Added %s.regions.timezone DEFAULT %s", pm_schema, DEFAULT_TIMEZONE)
    return True


def ensure_scheduler_tables(cur, pm_schema: str) -> None:
    """Create scheduler tables if absent."""
    cur.execute(DDL_REGION_REPORT_DEFINITIONS.format(schema=pm_schema))
    cur.execute(DDL_REGION_SCHEDULES.format(schema=pm_schema))


def upsert_builtin_definitions(cur, pm_schema: str, regional_schema: str) -> dict[str, int]:
    """Upsert builtin definitions by (schema_name, code). Return code -> id map."""
    code_to_id: dict[str, int] = {}
    for d in BUILTIN_DEFINITIONS:
        cur.execute(
            f"""
            INSERT INTO `{pm_schema}`.`region_report_definitions`
              (schema_name, code, name, report_type, is_builtin, time_window_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              name=VALUES(name),
              report_type=VALUES(report_type),
              is_builtin=VALUES(is_builtin),
              time_window_type=VALUES(time_window_type)
            """,
            (
                regional_schema,
                d["code"],
                d["name"],
                d["report_type"],
                d["is_builtin"],
                d.get("time_window_type"),
            ),
        )
        cur.execute(
            f"""
            SELECT id FROM `{pm_schema}`.`region_report_definitions`
            WHERE schema_name=%s AND code=%s
            """,
            (regional_schema, d["code"]),
        )
        row = cur.fetchone()
        code_to_id[d["code"]] = int(row["id"])
    return code_to_id


def _channel_list(channel: str | None) -> str | None:
    ch = (channel or "").strip()
    if not ch:
        return None
    return json.dumps([ch])


def seed_default_schedules(
    cur,
    pm_schema: str,
    region: dict[str, Any],
    *,
    merge_only: bool = True,
    skip_if_any_schedules: bool = False,
) -> int:
    """
    Seed default schedules for one region from legacy flags/channels.

    When merge_only=True (Restore Defaults), always INSERT new schedule rows
    for each builtin (duplicates allowed). Definitions are upserted by code.

    When skip_if_any_schedules=True (initial migration), skip inserting schedules
    if the region already has any rows (definitions are still upserted).

    Returns number of schedule rows inserted.
    """
    regional = region.get("schema_name") or ""
    if not regional:
        return 0
    code_to_id = upsert_builtin_definitions(cur, pm_schema, regional)
    if skip_if_any_schedules:
        cur.execute(
            f"SELECT COUNT(*) AS c FROM `{pm_schema}`.`region_schedules` WHERE schema_name=%s",
            (regional,),
        )
        if int(cur.fetchone()["c"]) > 0:
            LOG.info(
                "Skip schedule seed schema=%s — schedules already present",
                regional,
            )
            return 0
    inserted = 0
    for item in LEGACY_FLAG_MAP:
        def_id = code_to_id[item["code"]]
        enabled = 1 if region.get(item["flag"]) else 0
        channel_col = item["channel_col"]
        channel = region.get(channel_col)
        dest_type = item["destination_type"]
        dest_channels = None
        if dest_type == "specific_channels":
            dest_channels = _channel_list(channel)
        # Monthly first-of-month @ 07:00 local (matches legacy cron(0 12 1 * ? *) ≈ early Central)
        cur.execute(
            f"""
            INSERT INTO `{pm_schema}`.`region_schedules`
              (schema_name, report_definition_id, destination_type, destination_channels,
               frequency_type, month_day_mode, time_of_day, enabled)
            VALUES (%s, %s, %s, %s, 'monthly', 'first', '07:00:00', %s)
            """,
            (regional, def_id, dest_type, dest_channels, enabled),
        )
        inserted += 1
        LOG.info(
            "Seeded schedule schema=%s code=%s enabled=%s dest=%s",
            regional,
            item["code"],
            enabled,
            dest_type,
        )
    return inserted


def seed_all_regions(cur, pm_schema: str) -> dict[str, int]:
    """Seed builtins + default schedules for every active region. Returns counts."""
    ensure_timezone_column(cur, pm_schema)
    ensure_scheduler_tables(cur, pm_schema)
    cur.execute(f"SELECT * FROM `{pm_schema}`.`regions` WHERE active = 1")
    regions = list(cur.fetchall() or [])
    total_schedules = 0
    for region in regions:
        total_schedules += seed_default_schedules(
            cur, pm_schema, region, skip_if_any_schedules=True
        )
    return {"regions": len(regions), "schedules": total_schedules}


def delete_all_schedules(cur, pm_schema: str, regional_schema: str) -> int:
    """Delete all schedule rows for a regional schema. Definitions are kept."""
    cur.execute(
        f"DELETE FROM `{pm_schema}`.`region_schedules` WHERE schema_name=%s",
        (regional_schema,),
    )
    return int(cur.rowcount or 0)


def restore_defaults(cur, pm_schema: str, region: dict[str, Any]) -> int:
    """Upsert builtin definitions and merge/add default schedule rows."""
    return seed_default_schedules(cur, pm_schema, region, merge_only=True)


def count_schedules_for_definition(cur, pm_schema: str, definition_id: int) -> int:
    cur.execute(
        f"SELECT COUNT(*) AS c FROM `{pm_schema}`.`region_schedules` WHERE report_definition_id=%s",
        (definition_id,),
    )
    return int(cur.fetchone()["c"])
