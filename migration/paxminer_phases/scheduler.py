"""Report scheduler migration phase."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "PAXminer"))

from schedule_schema import (  # noqa: E402
    ensure_scheduler_tables,
    ensure_timezone_column,
    seed_all_regions,
)

from paxminer_phases.db import _pm_schema  # noqa: E402

LOG = logging.getLogger(__name__)


def run_scheduler(cur, stage: str) -> dict:
    """
    Ensure regions.timezone, scheduler tables, and seed default schedules.

    Seeds report definitions and schedules from report_defaults.json (via
    schedule_schema), not from legacy send_* region flags.
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
    counts = seed_all_regions(cur, pm_schema)

    return {
        "pm_schema": pm_schema,
        "timezone_added": added_tz,
        "tables_created": tables_created,
        "regions": counts["regions"],
        "regions_with_schema": counts["regions_with_schema"],
        "definitions": counts["definitions"],
        "schedules": counts["schedules"],
    }
