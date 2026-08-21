"""Effective-date range modes for achievement versions.

Four stored modes (varchar 24). Empty/NULL ``effective_from`` means all
attendance dates — resolved to the earliest beatdown at evaluation time.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

RANGE_FROM_CREATED = "from_created"
RANGE_SINCE_RULES_CHANGED = "since_rules_changed"
RANGE_ALL_ATTENDANCE = "all_attendance"
RANGE_CUSTOM = "custom"

LOG = logging.getLogger(__name__)

RANGE_MODES = (
    RANGE_FROM_CREATED,
    RANGE_SINCE_RULES_CHANGED,
    RANGE_ALL_ATTENDANCE,
    RANGE_CUSTOM,
)

RANGE_MODE_LABELS = {
    RANGE_FROM_CREATED: "From when the achievement was created",
    RANGE_SINCE_RULES_CHANGED: "Since the earning rules last changed",
    RANGE_ALL_ATTENDANCE: "All attendance dates",
    RANGE_CUSTOM: "Custom",
}

_LEGACY_RANGE_MODES = {
    "going_forward": RANGE_FROM_CREATED,
    "all_previous": RANGE_ALL_ATTENDANCE,
}

REEVAL_STALE_AFTER = timedelta(minutes=15)

_RANGE_MODE_COL = "varchar(24) DEFAULT NULL"
_REEVAL_QUEUED_COL = "datetime DEFAULT NULL"


def normalize_range_mode(mode: str | None, *, effective_from=None) -> str:
    """Map stored/submitted mode onto one of the four canonical values."""
    raw = (mode or "").strip()
    if raw in RANGE_MODES:
        return raw
    if raw in _LEGACY_RANGE_MODES:
        return _LEGACY_RANGE_MODES[raw]
    if effective_from is None or effective_from == "":
        return RANGE_ALL_ATTENDANCE
    return RANGE_CUSTOM


def iso_date(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _today() -> str:
    return date.today().isoformat()


def computed_start(
    mode: str,
    *,
    first_created=None,
    version_created=None,
    custom_from=None,
    today: str | None = None,
) -> str | None:
    """Stored ``effective_from`` for *mode* (NULL = all attendance)."""
    mode = normalize_range_mode(mode)
    today = today or _today()
    if mode == RANGE_ALL_ATTENDANCE:
        return None
    if mode == RANGE_FROM_CREATED:
        return iso_date(first_created) or today
    if mode == RANGE_SINCE_RULES_CHANGED:
        return iso_date(version_created) or today
    return iso_date(custom_from)


def display_start(
    mode: str,
    *,
    first_created=None,
    version_created=None,
    custom_from=None,
    earliest_beatdown=None,
    today: str | None = None,
) -> str | None:
    """Human-facing start for helper text. Pickers stay empty unless Custom."""
    stored = computed_start(
        mode,
        first_created=first_created,
        version_created=version_created,
        custom_from=custom_from,
        today=today,
    )
    if stored:
        return stored
    if mode == RANGE_ALL_ATTENDANCE:
        return iso_date(earliest_beatdown)
    return today or _today()


def range_mode_hint(
    mode: str,
    *,
    first_created=None,
    version_created=None,
    earliest_beatdown=None,
    today: str | None = None,
) -> str:
    """Context under the mode picker. Dates themselves live in Custom pickers only."""
    mode = normalize_range_mode(mode)
    shown = display_start(
        mode,
        first_created=first_created,
        version_created=version_created,
        earliest_beatdown=earliest_beatdown,
        today=today,
    )
    if mode == RANGE_CUSTOM:
        return "Start date is required. Leave End date empty for no end date."
    if mode == RANGE_ALL_ATTENDANCE:
        if shown:
            return f"Awards count from all attendance dates (earliest on record: {shown})."
        return "Awards count from all attendance dates."
    if mode == RANGE_SINCE_RULES_CHANGED:
        return f"Awards count from {shown} (when the earning rules last changed)."
    return f"Awards count from {shown} (when this achievement was created)."


def range_mode_options() -> list[dict]:
    return [
        {
            "text": {"type": "plain_text", "text": RANGE_MODE_LABELS[mode]},
            "value": mode,
        }
        for mode in RANGE_MODES
    ]


def range_validation_errors(
    values: dict,
    *,
    first_created=None,
    version_created=None,
    earliest_beatdown=None,
    today: str | None = None,
) -> dict[str, str]:
    """Validate Custom pickers. Non-custom modes ignore leftover picker values."""
    mode = normalize_range_mode(values.get("range_mode"), effective_from=values.get("effective_from"))
    errors: dict[str, str] = {}
    if mode not in RANGE_MODES:
        errors["range_mode"] = "Choose an effective date range"
        return errors
    submitted_from = iso_date(values.get("effective_from"))
    submitted_to = iso_date(values.get("effective_to"))
    if mode != RANGE_CUSTOM:
        return errors
    if not submitted_from:
        errors["effective_from"] = "Start date is required for a custom range"
    if submitted_from and submitted_to and submitted_to < submitted_from:
        errors["effective_to"] = "End date must be on or after the start date"
    return errors


def resolve_stored_range(
    values: dict,
    *,
    first_created=None,
    version_created=None,
    minting: bool = False,
    today: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Return ``(range_mode, effective_from, effective_to)`` to persist."""
    today = today or _today()
    mode = normalize_range_mode(values.get("range_mode"), effective_from=values.get("effective_from"))
    if mode == RANGE_CUSTOM:
        to_date = iso_date(values.get("effective_to"))
    else:
        to_date = None
    if mode == RANGE_SINCE_RULES_CHANGED and minting:
        from_date = today
    else:
        from_date = computed_start(
            mode,
            first_created=first_created,
            version_created=version_created,
            custom_from=values.get("effective_from"),
            today=today,
        )
    return mode, from_date, to_date


def range_tuple(from_date, to_date, mode: str | None) -> tuple[str | None, str | None, str]:
    return (
        iso_date(from_date),
        iso_date(to_date),
        normalize_range_mode(mode, effective_from=from_date),
    )


def range_changed(existing: dict | None, mode: str, from_date, to_date) -> bool:
    if not existing:
        return True
    old = range_tuple(
        existing.get("effective_from"),
        existing.get("effective_to"),
        existing.get("range_mode"),
    )
    new = range_tuple(from_date, to_date, mode)
    return old != new


def should_auto_queue(*, is_new: bool, params_changed: bool, range_changed: bool, mode: str) -> bool:
    """New all-attendance/custom windows, or an edit that actually changed params or range."""
    if is_new:
        return normalize_range_mode(mode) in (RANGE_ALL_ATTENDANCE, RANGE_CUSTOM)
    return bool(params_changed or range_changed)


def hold_prior_version_awards(mode: str | None, *, effective_from=None) -> bool:
    """Freeze older-version awards only when the window starts at the last rule change.

    All attendance, from created, and custom re-judge history (and may revoke).
    """
    return (
        normalize_range_mode(mode, effective_from=effective_from)
        == RANGE_SINCE_RULES_CHANGED
    )


def window_narrowed(old_from, old_to, new_from, new_to) -> bool:
    """True when the new window is a strict subset of the old one."""
    old_f, old_t = iso_date(old_from), iso_date(old_to)
    new_f, new_t = iso_date(new_from), iso_date(new_to)
    start_later = bool(new_f) and (old_f is None or new_f > old_f)
    end_earlier = bool(new_t) and (old_t is None or new_t < old_t)
    return start_later or end_earlier


def _column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """,
        (schema, table, column),
    )
    row = cur.fetchone() or {}
    return int(row.get("c") or 0) > 0


def ensure_range_mode_column(cur, schema: str) -> bool:
    """Add nullable achievement_versions.range_mode if missing."""
    if _column_exists(cur, schema, "achievement_versions", "range_mode"):
        return False
    cur.execute(
        f"ALTER TABLE `{schema}`.`achievement_versions` "
        f"ADD COLUMN `range_mode` {_RANGE_MODE_COL}"
    )
    return True


def ensure_reeval_queued_at_column(cur, schema: str) -> bool:
    """Add nullable achievements_list.reeval_queued_at if missing."""
    if _column_exists(cur, schema, "achievements_list", "reeval_queued_at"):
        return False
    cur.execute(
        f"ALTER TABLE `{schema}`.`achievements_list` "
        f"ADD COLUMN `reeval_queued_at` {_REEVAL_QUEUED_COL}"
    )
    return True


def ensure_achievement_range_columns(cur, schema: str) -> dict[str, bool]:
    return {
        "range_mode": ensure_range_mode_column(cur, schema),
        "reeval_queued_at": ensure_reeval_queued_at_column(cur, schema),
    }


def backfill_range_mode(cur, schema: str) -> int:
    """NULL effective_from → all_attendance, otherwise custom. Skips rows that already have a mode."""
    cur.execute(
        f"""
        UPDATE `{schema}`.`achievement_versions`
        SET range_mode = CASE
            WHEN effective_from IS NULL THEN %s
            ELSE %s
        END
        WHERE range_mode IS NULL
        """,
        (RANGE_ALL_ATTENDANCE, RANGE_CUSTOM),
    )
    return int(cur.rowcount or 0)


def count_awards_outside_range(
    cur, schema: str, achievement_id: int, from_date, to_date
) -> tuple[int, int]:
    """Awards (and distinct PAX) whose award date falls outside ``[from, to]``."""
    start = iso_date(from_date)
    end = iso_date(to_date)
    cur.execute(
        f"""
        SELECT COUNT(*) AS awards, COUNT(DISTINCT pax_id) AS pax
        FROM `{schema}`.`achievements_awarded`
        WHERE achievement_id=%s
          AND (
            (%s IS NOT NULL AND COALESCE(period_start, date_awarded) < %s)
            OR (%s IS NOT NULL AND COALESCE(period_end, date_awarded) > %s)
          )
        """,
        (achievement_id, start, start, end, end),
    )
    row = cur.fetchone() or {}
    return int(row.get("awards") or 0), int(row.get("pax") or 0)


def try_acquire_reeval_lock(
    cur, schema: str, achievement_id: int, *, now: datetime | None = None
) -> tuple[bool, str | None]:
    """Lock this achievement's re-evaluate. Returns ``(ok, rejection_message)``."""
    ensure_reeval_queued_at_column(cur, schema)
    now = now or datetime.utcnow()
    cur.execute(
        f"""
        SELECT reeval_queued_at FROM `{schema}`.`achievements_list`
        WHERE id=%s FOR UPDATE
        """,
        (achievement_id,),
    )
    row = cur.fetchone() or {}
    queued_at = row.get("reeval_queued_at")
    if queued_at is not None:
        if isinstance(queued_at, str):
            try:
                queued_at = datetime.fromisoformat(queued_at.replace("Z", ""))
            except ValueError:
                queued_at = now
        age = now - queued_at
        if age < REEVAL_STALE_AFTER:
            return False, (
                "A re-evaluate is already running for this achievement. "
                "Try again in a few minutes."
            )
    cur.execute(
        f"UPDATE `{schema}`.`achievements_list` SET reeval_queued_at=%s WHERE id=%s",
        (now, achievement_id),
    )
    return True, None


def clear_reeval_lock(cur, schema: str, achievement_id: int) -> None:
    try:
        cur.execute(
            f"UPDATE `{schema}`.`achievements_list` SET reeval_queued_at=NULL WHERE id=%s",
            (achievement_id,),
        )
    except Exception:
        LOG.warning(
            "failed to clear reeval lock schema=%s id=%s",
            schema,
            achievement_id,
            exc_info=True,
        )


def range_confirm_text(award_count: int, pax_count: int) -> str:
    from slack_blocks import counted_noun

    awards = counted_noun(award_count, "award")
    pax = counted_noun(pax_count, "PAX", "PAX")
    return (
        f"This range is narrower than the current one, so saving will revoke "
        f"{awards} from {pax}. Re-evaluate runs with revokes enabled. Continue?"
    )
