"""Claim-before-post receipts for Slack view_submission retries.

Mirrors welcome_deliveries: INSERT claim → Slack I/O → release only if Slack fails.
"""

from __future__ import annotations

from logging import Logger
from typing import Callable

from sqlalchemy.exc import IntegrityError

from utilities.database import DbManager
from utilities.database.orm import InteractionClaim
from utilities.helper_functions import safe_get

KIND_BACKBLAST = "backblast"
KIND_PREBLAST = "preblast"
KIND_STRAVA = "strava"


def _is_duplicate_key_error(error: IntegrityError) -> bool:
    return bool(error.orig and getattr(error.orig, "args", ()) and error.orig.args[0] == 1062)


def claim_key(body: dict) -> str | None:
    """Stable key across Slack retries: view.id, else trigger_id."""
    key = safe_get(body, "view", "id") or safe_get(body, "trigger_id")
    if not key:
        return None
    return str(key)


def claim_interaction(key: str, kind: str, team_id: str) -> bool:
    """Return True if this process owns the claim; False if already claimed (1062)."""
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
