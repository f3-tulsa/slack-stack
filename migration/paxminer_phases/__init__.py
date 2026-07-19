"""PAXMiner migration phases (weaselbot, scheduler, drop-legacy-columns)."""

from paxminer_phases.drop_legacy import DROPPED_COLUMNS, run_drop_legacy_columns
from paxminer_phases.scheduler import run_scheduler
from paxminer_phases.weaselbot import PM_REGION_COLS, run_weaselbot

__all__ = [
    "DROPPED_COLUMNS",
    "PM_REGION_COLS",
    "run_drop_legacy_columns",
    "run_scheduler",
    "run_weaselbot",
]
