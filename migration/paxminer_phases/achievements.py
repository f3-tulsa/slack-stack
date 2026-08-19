"""Additive achievements versioning / period / activity_type migration phase."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "PAXminer"))

from achievements.achievement_rules import (  # noqa: E402
    ACHIEVEMENT_VERSIONS_DDL,
    ACHIEVEMENTS_AWARDED_DDL,
    ACHIEVEMENTS_LIST_DDL,
    ACHIEVEMENTS_VIEW_DDL,
    AWARDED_PERIOD_COLUMNS,
)
from achievements.activity import classify_activity_type, legacy_activity_to_list  # noqa: E402
from achievements.period import period_bounds, period_key_for_date  # noqa: E402

from paxminer_phases.db import (  # noqa: E402
    _column_exists,
    _index_exists,
    _pm_schema,
    _table_exists,
)

LOG = logging.getLogger(__name__)

ACTIVITY_TYPE_BATCH = 500
AWARD_PERIOD_BATCH = 500


def _regional_schemas(cur, pm_schema: str) -> list[str]:
    cur.execute(
        f"SELECT schema_name FROM `{pm_schema}`.`regions` "
        "WHERE active=1 AND schema_name IS NOT NULL"
    )
    return [row["schema_name"] for row in cur.fetchall() if row.get("schema_name")]


def _add_column(cur, schema: str, table: str, column: str, typedef: str) -> bool:
    if _column_exists(cur, schema, table, column):
        return False
    cur.execute(f"ALTER TABLE `{schema}`.`{table}` ADD COLUMN `{column}` {typedef}")
    LOG.info("Added %s.%s.%s", schema, table, column)
    return True


def _ensure_index(cur, schema: str, table: str, index_name: str, cols: str) -> bool:
    if _index_exists(cur, schema, table, index_name):
        return False
    cur.execute(f"ALTER TABLE `{schema}`.`{table}` ADD KEY `{index_name}` ({cols})")
    LOG.info("Added index %s.%s.%s", schema, table, index_name)
    return True


def _ensure_tables(cur, schema: str) -> None:
    if not _table_exists(cur, schema, "achievements_list"):
        cur.execute(ACHIEVEMENTS_LIST_DDL.format(schema=schema))
        LOG.info("Created %s.achievements_list", schema)
    if not _table_exists(cur, schema, "achievements_awarded"):
        cur.execute(ACHIEVEMENTS_AWARDED_DDL.format(schema=schema))
        LOG.info("Created %s.achievements_awarded", schema)
    if not _table_exists(cur, schema, "achievement_versions"):
        try:
            cur.execute(ACHIEVEMENT_VERSIONS_DDL.format(schema=schema))
            LOG.info("Created %s.achievement_versions", schema)
        except Exception:
            # App users may lack REFERENCES; retry without the FK.
            LOG.warning("achievement_versions FK create failed for %s; retrying without FK", schema)
            ddl = ACHIEVEMENT_VERSIONS_DDL.format(schema=schema)
            ddl = ddl.replace(
                f",\n  CONSTRAINT `achievement_versions_ibfk_1` FOREIGN KEY (`achievement_id`)\n"
                f"    REFERENCES `{schema}`.`achievements_list` (`id`)",
                "",
            )
            cur.execute(ddl)
            LOG.info("Created %s.achievement_versions (no FK)", schema)


def _ensure_list_and_awarded_columns(cur, schema: str) -> dict:
    added = {
        "enabled": _add_column(
            cur, schema, "achievements_list", "enabled", "TINYINT NOT NULL DEFAULT 1"
        ),
        "reeval_queued_at": _add_column(
            cur, schema, "achievements_list", "reeval_queued_at", "DATETIME DEFAULT NULL"
        ),
        "range_mode": _add_column(
            cur, schema, "achievement_versions", "range_mode", "VARCHAR(24) DEFAULT NULL"
        ),
    }
    typedefs = {
        "achievement_version_id": "INT DEFAULT NULL",
        "period": "VARCHAR(16) DEFAULT NULL",
        "period_key": "VARCHAR(16) DEFAULT NULL",
        "period_start": "DATE DEFAULT NULL",
        "period_end": "DATE DEFAULT NULL",
        "qualifying_count": "INT DEFAULT NULL",
    }
    for col in AWARDED_PERIOD_COLUMNS:
        added[col] = _add_column(cur, schema, "achievements_awarded", col, typedefs[col])
    # Prefer the unique period key; keep the old lookup index only until it exists.
    if _index_exists(cur, schema, "achievements_awarded", "uniq_award_period"):
        added["awarded_period_lookup"] = False
    else:
        added["awarded_period_lookup"] = _ensure_index(
            cur,
            schema,
            "achievements_awarded",
            "awarded_period_lookup",
            "`achievement_id`, `pax_id`, `period_key`",
        )
    try:
        added["activity_type"] = _add_column(
            cur, schema, "beatdowns", "activity_type", "VARCHAR(64) DEFAULT NULL"
        )
    except Exception:
        LOG.warning("Could not add %s.beatdowns.activity_type", schema, exc_info=True)
        added["activity_type"] = False
    return added


def _seed_version_1(cur, schema: str) -> int:
    cur.execute(
        f"""
        SELECT id, code, metric, activity, period, threshold
        FROM `{schema}`.`achievements_list`
        """
    )
    rows = cur.fetchall() or []
    inserted = 0
    for row in rows:
        cur.execute(
            f"""
            SELECT 1 FROM `{schema}`.`achievement_versions`
            WHERE achievement_id=%s AND version=1
            LIMIT 1
            """,
            (row["id"],),
        )
        if cur.fetchone():
            continue
        activity = legacy_activity_to_list(row.get("activity"))
        version_key = f"{row.get('code') or 'achievement'}_v1"
        cur.execute(
            f"""
            INSERT INTO `{schema}`.`achievement_versions`
            (achievement_id, version, version_key, metric, activity, period, threshold,
             effective_from, effective_to, superseded_at, created_by)
            SELECT %s, 1, %s, %s, %s, %s, %s, NULL, NULL, NULL, 'migration'
            FROM DUAL
            WHERE NOT EXISTS (
                SELECT 1 FROM `{schema}`.`achievement_versions`
                WHERE achievement_id=%s AND version=1
            )
            """,
            (
                row["id"],
                version_key,
                row.get("metric") or "posts",
                json.dumps(activity),
                row.get("period") or "year",
                int(row.get("threshold") or 1),
                row["id"],
            ),
        )
        if cur.rowcount:
            inserted += 1
            LOG.info("Seeded version 1 for %s achievement id=%s code=%s", schema, row["id"], row.get("code"))
    return inserted


def _backfill_award_periods(cur, schema: str) -> int:
    updated = 0
    while True:
        cur.execute(
            f"""
            SELECT aa.id, aa.date_awarded, aa.achievement_id,
                   COALESCE(aa.period, al.period) AS period
            FROM `{schema}`.`achievements_awarded` aa
            JOIN `{schema}`.`achievements_list` al ON al.id = aa.achievement_id
            WHERE aa.period_key IS NULL
            LIMIT {AWARD_PERIOD_BATCH}
            """
        )
        rows = cur.fetchall() or []
        if not rows:
            break
        for row in rows:
            awarded = row["date_awarded"]
            if isinstance(awarded, str):
                awarded = date.fromisoformat(awarded[:10])
            elif hasattr(awarded, "date") and callable(awarded.date):
                awarded = awarded.date()
            period = row.get("period") or "year"
            key = period_key_for_date(awarded, period)
            start, end = period_bounds(awarded, period)
            cur.execute(
                f"""
                SELECT id FROM `{schema}`.`achievement_versions`
                WHERE achievement_id=%s AND superseded_at IS NULL
                ORDER BY version DESC LIMIT 1
                """,
                (row["achievement_id"],),
            )
            ver = cur.fetchone()
            version_id = (ver or {}).get("id")
            cur.execute(
                f"""
                UPDATE `{schema}`.`achievements_awarded`
                SET period=%s, period_key=%s, period_start=%s, period_end=%s,
                    achievement_version_id=COALESCE(achievement_version_id, %s)
                WHERE id=%s AND period_key IS NULL
                """,
                (period, key, start, end, version_id, row["id"]),
            )
            updated += cur.rowcount
    if updated:
        LOG.info("Backfilled period columns on %s award(s) in %s", updated, schema)
    return updated


def _classify_activity_types(cur, schema: str) -> int:
    if not _column_exists(cur, schema, "beatdowns", "activity_type"):
        return 0
    has_json = _column_exists(cur, schema, "beatdowns", "json")
    json_sel = "b.json" if has_json else "NULL AS json"
    updated = 0
    while True:
        cur.execute(
            f"""
            SELECT b.ao_id, b.bd_date, b.q_user_id, b.backblast, {json_sel}, ao.ao
            FROM `{schema}`.`beatdowns` b
            LEFT JOIN `{schema}`.`aos` ao ON ao.channel_id = b.ao_id
            WHERE b.activity_type IS NULL
            LIMIT {ACTIVITY_TYPE_BATCH}
            """
        )
        rows = cur.fetchall() or []
        if not rows:
            break
        for row in rows:
            atype = classify_activity_type(
                json_blob=row.get("json"),
                ao_name=row.get("ao"),
                backblast=row.get("backblast"),
            )
            cur.execute(
                f"""
                UPDATE `{schema}`.`beatdowns`
                SET activity_type=%s
                WHERE ao_id=%s AND bd_date=%s AND q_user_id=%s AND activity_type IS NULL
                """,
                (atype, row["ao_id"], row["bd_date"], row["q_user_id"]),
            )
            updated += cur.rowcount
    if updated:
        LOG.info("Classified activity_type on %s beatdown(s) in %s", updated, schema)
    return updated


def _enforce_award_period_unique(cur, schema: str) -> dict:
    """Collapse duplicate awards, then add UNIQUE (achievement_id, pax_id, period_key).

    MySQL unique indexes allow multiple NULLs, so a NULL period_key would escape
    the constraint. Count those rows and skip the ALTER when any remain.
    """
    result = {
        "duplicates_deleted": 0,
        "null_period_key": 0,
        "unique_added": False,
        "unique_already_present": False,
    }
    if _index_exists(cur, schema, "achievements_awarded", "uniq_award_period"):
        result["unique_already_present"] = True
        return result

    cur.execute(
        f"SELECT COUNT(*) AS c FROM `{schema}`.`achievements_awarded` WHERE period_key IS NULL"
    )
    result["null_period_key"] = int((cur.fetchone() or {}).get("c") or 0)

    cur.execute(
        f"""
        DELETE a FROM `{schema}`.`achievements_awarded` a
        JOIN `{schema}`.`achievements_awarded` b
          ON a.achievement_id = b.achievement_id
         AND a.pax_id = b.pax_id
         AND a.period_key = b.period_key
         AND a.id > b.id
        """
    )
    result["duplicates_deleted"] = int(cur.rowcount or 0)
    if result["duplicates_deleted"]:
        LOG.info(
            "Deleted %s duplicate award row(s) in %s (kept lowest id per period)",
            result["duplicates_deleted"],
            schema,
        )

    if result["null_period_key"]:
        LOG.warning(
            "%s has %s achievements_awarded row(s) with NULL period_key; "
            "UNIQUE uniq_award_period was not added (MySQL allows multiple NULLs)",
            schema,
            result["null_period_key"],
        )
        return result

    cur.execute(
        f"""
        ALTER TABLE `{schema}`.`achievements_awarded`
          ADD UNIQUE KEY `uniq_award_period` (`achievement_id`, `pax_id`, `period_key`)
        """
    )
    result["unique_added"] = True
    LOG.info("Added unique key uniq_award_period on %s.achievements_awarded", schema)
    if _index_exists(cur, schema, "achievements_awarded", "awarded_period_lookup"):
        cur.execute(
            f"ALTER TABLE `{schema}`.`achievements_awarded` DROP KEY `awarded_period_lookup`"
        )
        LOG.info("Dropped redundant index awarded_period_lookup on %s.achievements_awarded", schema)
    return result


def _refresh_view(cur, schema: str) -> bool:
    try:
        cur.execute(ACHIEVEMENTS_VIEW_DDL.format(schema=schema))
        LOG.info("Created/replaced %s.achievements_view", schema)
        return True
    except Exception as e:
        LOG.warning("achievements_view create skipped for %s: %s", schema, e)
        return False


def migrate_regional_schema(cur, schema: str) -> dict:
    _ensure_tables(cur, schema)
    added = _ensure_list_and_awarded_columns(cur, schema)
    from achievements.range import backfill_range_mode

    versions = _seed_version_1(cur, schema)
    range_modes = backfill_range_mode(cur, schema)
    awards = _backfill_award_periods(cur, schema)
    unique = _enforce_award_period_unique(cur, schema)
    classified = _classify_activity_types(cur, schema)
    view_ok = _refresh_view(cur, schema)
    return {
        "schema": schema,
        "columns_added": {k: v for k, v in added.items() if v},
        "versions_seeded": versions,
        "awards_backfilled": awards,
        "award_unique": unique,
        "activity_type_classified": classified,
        "view_ok": view_ok,
        "range_modes_backfilled": range_modes,
    }


def run_achievements(cur, stage: str) -> dict:
    """Idempotent achievements versioning phase. Does not touch Slackblast tables."""
    pm_schema = _pm_schema(stage)
    schemas = _regional_schemas(cur, pm_schema)
    LOG.info("Achievements phase: %s regional schema(s)", len(schemas))
    results = []
    for schema in schemas:
        LOG.info("Migrating achievements for %s", schema)
        results.append(migrate_regional_schema(cur, schema))
    return {
        "regional_schemas": len(results),
        "versions_seeded": sum(r["versions_seeded"] for r in results),
        "awards_backfilled": sum(r["awards_backfilled"] for r in results),
        "duplicates_deleted": sum(r["award_unique"]["duplicates_deleted"] for r in results),
        "null_period_key": sum(r["award_unique"]["null_period_key"] for r in results),
        "unique_keys_added": sum(1 for r in results if r["award_unique"]["unique_added"]),
        "activity_type_classified": sum(r["activity_type_classified"] for r in results),
        "results": results,
    }
