"""Report scheduler migration phase."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "PAXminer"))

from schedule_schema import (  # noqa: E402
    ensure_is_customized_column,
    ensure_scheduler_tables,
    ensure_timezone_column,
)

from paxminer_phases.db import _pm_schema  # noqa: E402

LOG = logging.getLogger(__name__)


def run_scheduler(cur, stage: str) -> dict:
    """
    Ensure regions.timezone and scheduler tables (DDL only).

    report_defaults.json is NOT loaded here. Admins load or restore defaults
    from the PAX Reports / Schedule modals (Load defaults / Restore Defaults).
    seed_all_regions remains available in schedule_schema for one-time
    onboarding scripts, but is not invoked by this migration phase.
    """
    pm_schema = _pm_schema(stage)

    added_tz = ensure_timezone_column(cur, pm_schema)
    LOG.info(
        "%s.regions.timezone: %s",
        pm_schema,
        "added" if added_tz else "already present",
    )
    tables_created = ensure_scheduler_tables(cur, pm_schema)
    LOG.info("Scheduler tables created this run: %s", tables_created or "(none)")
    added_customized = ensure_is_customized_column(cur, pm_schema)
    LOG.info(
        "%s.region_report_definitions.is_customized: %s",
        pm_schema,
        "added" if added_customized else "already present",
    )
    LOG.info(
        "Defaults not seeded by migration — use Load defaults / Restore Defaults in Slack"
    )

    return {
        "pm_schema": pm_schema,
        "timezone_added": added_tz,
        "tables_created": tables_created,
        "is_customized_added": added_customized,
        "defaults_seeded": False,
        "note": "report_defaults.json loads only via Slack Load/Restore defaults",
    }
