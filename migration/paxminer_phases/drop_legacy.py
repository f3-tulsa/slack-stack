"""Drop legacy send_* / channel columns from paxminer.regions."""

from __future__ import annotations

import logging

from paxminer_phases.db import _column_exists, _pm_schema

LOG = logging.getLogger(__name__)

DROPPED_COLUMNS = (
    "send_pax_charts",
    "send_q_charts",
    "send_region_leaderboard",
    "send_ao_leaderboard",
    "send_aoq_reports",
    "send_achievement_leaderboard",
    "firstf_channel",
    "kotter_channel",
)


def run_drop_legacy_columns(cur, stage: str) -> dict:
    """Idempotent DROP COLUMN for each missing-or-present column. Log each drop/skip."""
    pm_schema = _pm_schema(stage)
    dropped: list[str] = []
    skipped: list[str] = []

    for col in DROPPED_COLUMNS:
        if _column_exists(cur, pm_schema, "regions", col):
            cur.execute(f"ALTER TABLE `{pm_schema}`.`regions` DROP COLUMN `{col}`")
            LOG.info("Dropped %s.regions.%s", pm_schema, col)
            dropped.append(col)
        else:
            LOG.info("%s.regions.%s already absent (drop skipped)", pm_schema, col)
            skipped.append(col)

    return {
        "pm_schema": pm_schema,
        "dropped": dropped,
        "skipped": skipped,
    }
