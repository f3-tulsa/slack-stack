"""Report scheduler migration phase."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "PAXminer"))

from schedule_schema import (  # noqa: E402
    backfill_award_achievements_schedules,
    ensure_is_customized_column,
    ensure_report_enabled_column,
    ensure_last_run_at_column,
    ensure_log_channel_column,
    ensure_scheduler_tables,
    ensure_timezone_column,
)

from paxminer_phases.db import _pm_schema  # noqa: E402

LOG = logging.getLogger(__name__)


def run_scheduler(cur, stage: str) -> dict:
    """
    Ensure regions.timezone, regions.log_channel, scheduler tables, last_run_at, and backfill
    Award Achievements schedules for regions that previously used the daily
    EventBridge path (send_achievements + achievement_channel).

    report_defaults.json is NOT loaded wholesale here. Admins load or restore
    defaults from the PAX Reports / Schedule modals (Load defaults / Restore
    Defaults). seed_all_regions remains available in schedule_schema for
    one-time onboarding scripts, but is not invoked by this migration phase.
    """
    pm_schema = _pm_schema(stage)

    added_tz = ensure_timezone_column(cur, pm_schema)
    LOG.info(
        "%s.regions.timezone: %s",
        pm_schema,
        "added" if added_tz else "already present",
    )
    added_log = ensure_log_channel_column(cur, pm_schema)
    LOG.info(
        "%s.regions.log_channel: %s",
        pm_schema,
        "added" if added_log else "already present",
    )
    tables_created = ensure_scheduler_tables(cur, pm_schema)
    LOG.info("Scheduler tables created this run: %s", tables_created or "(none)")
    added_customized = ensure_is_customized_column(cur, pm_schema)
    LOG.info(
        "%s.region_report_definitions.is_customized: %s",
        pm_schema,
        "added" if added_customized else "already present",
    )
    added_enabled = ensure_report_enabled_column(cur, pm_schema)
    LOG.info(
        "%s.region_report_definitions.enabled: %s",
        pm_schema,
        "added" if added_enabled else "already present",
    )
    added_last_run_at = ensure_last_run_at_column(cur, pm_schema)
    LOG.info(
        "%s.region_schedules.last_run_at: %s",
        pm_schema,
        "added" if added_last_run_at else "already present",
    )
    backfill = backfill_award_achievements_schedules(cur, pm_schema)
    LOG.info(
        "Award Achievements backfill: definitions=%s schedules=%s skipped=%s",
        backfill.get("definitions_ensured"),
        backfill.get("schedules_inserted"),
        backfill.get("skipped"),
    )
    LOG.info(
        "Defaults not seeded by migration — use Add Missing Defaults in Slack"
    )

    return {
        "pm_schema": pm_schema,
        "timezone_added": added_tz,
        "log_channel_added": added_log,
        "tables_created": tables_created,
        "is_customized_added": added_customized,
        "report_enabled_added": added_enabled,
        "last_run_at_added": added_last_run_at,
        "award_achievements_backfill": backfill,
        "defaults_seeded": False,
        "note": "report_defaults.json loads only via Slack Load/Restore defaults",
    }
