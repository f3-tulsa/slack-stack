"""Claim-before-post lock for Slack ``view_submission`` retries.

Why a table
-----------
Slack retries the same modal submit when the ack HTTP response takes more than
~3 seconds. Each retry is a **new Lambda invoke**. ``beatdowns`` /
``bd_attendance`` uniqueness uses Slack's message ``ts``, which only exists
*after* ``chat_postMessage``, so those PKs cannot prevent a second channel
message. An in-memory flag does not survive a second invoke.

This table is the lock: INSERT ``(claim_key, kind)`` where ``claim_key`` is
``view.id`` (else ``trigger_id``). Duplicate key 1062 means another invoke
already claimed this submit — skip Slack I/O. Same pattern as
``welcome_deliveries`` (that table is keyed on Events API ``event_id``, not
modal ``view.id``).

Rules
-----
- Claim **before** file/S3 work and **before** any Slack channel write.
- Release **only** if the Slack write itself failed, so the user can retry.
- Do not release after a successful post (permalink / email / DB errors must
  not allow Slack to post again).
- Fail closed: missing claim key refuses to post; missing table or non-1062
  DB errors raise (do not post).
- Kinds: ``backblast``, ``preblast``, ``strava``.
- Retention: keep rows for ``CLAIM_RETENTION`` (7 days). Slack retries last
  seconds to minutes; do **not** delete on success (a retry still needs the
  row). Each claim prunes older rows in a **separate** transaction so a 1062
  on INSERT does not roll back the delete.

Ops: create with ``python migration/migrate_data.py --env <stage>
--bootstrap-only`` before deploying code that writes this table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from logging import Logger
from typing import Callable

from sqlalchemy.exc import IntegrityError

from utilities.database import DbManager
from utilities.database.orm import InteractionClaim
from utilities.helper_functions import safe_get

KIND_BACKBLAST = "backblast"
KIND_PREBLAST = "preblast"
KIND_STRAVA = "strava"
# Slack's retry window is seconds–minutes. 7 days is well past that and keeps
# a delayed lazy invoke from racing a purge.
CLAIM_RETENTION = timedelta(days=7)


def _is_duplicate_key_error(error: IntegrityError) -> bool:
    return bool(error.orig and getattr(error.orig, "args", ()) and error.orig.args[0] == 1062)


def claim_key(body: dict) -> str | None:
    """Stable key across Slack retries: view.id, else trigger_id."""
    key = safe_get(body, "view", "id") or safe_get(body, "trigger_id")
    if not key:
        return None
    return str(key)


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def prune_stale_interaction_claims(*, now: datetime | None = None) -> int:
    """Delete claims older than ``CLAIM_RETENTION``. Own transaction (not the INSERT)."""
    cutoff = now or _naive_utc_now()
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    cutoff = cutoff - CLAIM_RETENTION
    with DbManager.transaction() as session:
        return session.query(InteractionClaim).filter(
            InteractionClaim.created < cutoff,
        ).delete(synchronize_session=False)


def claim_interaction(key: str, kind: str, team_id: str) -> bool:
    """Return True if this process owns the claim; False if already claimed (1062)."""
    prune_stale_interaction_claims()
    try:
        with DbManager.transaction() as session:
            session.add(
                InteractionClaim(
                    claim_key=key,
                    kind=kind,
                    team_id=team_id or "",
                )
            )
            session.flush()
        return True
    except IntegrityError as error:
        if _is_duplicate_key_error(error):
            return False
        raise


def release_interaction(key: str, kind: str) -> None:
    with DbManager.transaction() as session:
        session.query(InteractionClaim).filter(
            InteractionClaim.claim_key == key,
            InteractionClaim.kind == kind,
        ).delete(synchronize_session=False)


def run_once(
    *,
    body: dict,
    kind: str,
    team_id: str,
    logger: Logger,
    action: Callable[[], None],
) -> bool:
    """Claim then run action. Returns False if skipped (no key or already claimed).

    Releases the claim only when action raises so the user can retry.
    Fail closed: missing claim key refuses to post; non-1062 DB errors propagate.
    """
    key = claim_key(body)
    if not key:
        logger.error(
            "Refusing non-idempotent %s post: missing view.id and trigger_id team_id=%s",
            kind,
            team_id,
        )
        return False

    if not claim_interaction(key, kind, team_id):
        logger.info(
            "Skipping duplicate %s claim_key=%s team_id=%s",
            kind,
            key,
            team_id,
        )
        return False

    try:
        action()
    except Exception:
        release_interaction(key, kind)
        raise
    return True
