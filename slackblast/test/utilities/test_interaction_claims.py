import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from utilities.interaction_claims import (
    CLAIM_RETENTION,
    KIND_BACKBLAST,
    KIND_PREBLAST,
    KIND_STRAVA,
    claim_interaction,
    claim_key,
    prune_stale_interaction_claims,
    run_once,
)


def test_claim_key_prefers_view_id():
    assert claim_key({"view": {"id": "V1"}, "trigger_id": "T1"}) == "V1"
    assert claim_key({"trigger_id": "T1"}) == "T1"
    assert claim_key({}) is None


def _duplicate_error(code):
    return IntegrityError("INSERT", {}, Exception(code, "Duplicate entry"))


def test_claim_interaction_returns_false_on_duplicate_key():
    session = MagicMock()
    session.flush.side_effect = _duplicate_error(1062)

    @contextmanager
    def transaction():
        yield session

    with patch("utilities.interaction_claims.DbManager.transaction", transaction):
        assert claim_interaction("V1", KIND_BACKBLAST, "T1") is False


def test_claim_interaction_non_duplicate_propagates():
    session = MagicMock()
    session.flush.side_effect = _duplicate_error(1213)

    @contextmanager
    def transaction():
        yield session

    with (
        patch("utilities.interaction_claims.DbManager.transaction", transaction),
        pytest.raises(IntegrityError),
    ):
        claim_interaction("V1", KIND_BACKBLAST, "T1")


def test_claim_interaction_prunes_before_insert():
    session = MagicMock()

    @contextmanager
    def transaction():
        yield session

    with (
        patch("utilities.interaction_claims.prune_stale_interaction_claims") as prune,
        patch("utilities.interaction_claims.DbManager.transaction", transaction),
    ):
        assert claim_interaction("V1", KIND_BACKBLAST, "T1") is True
    prune.assert_called_once_with()
    session.add.assert_called_once()


def test_prune_stale_deletes_rows_older_than_retention():
    session = MagicMock()
    session.query.return_value.filter.return_value.delete.return_value = 4

    @contextmanager
    def transaction():
        yield session

    now = datetime(2026, 9, 15, 18, 0, 0)
    with patch("utilities.interaction_claims.DbManager.transaction", transaction):
        assert prune_stale_interaction_claims(now=now) == 4

    expr = session.query.return_value.filter.call_args[0][0]
    cutoff = expr.right.value
    assert cutoff == now - CLAIM_RETENTION
    session.query.return_value.filter.return_value.delete.assert_called_once_with(
        synchronize_session=False
    )


def test_run_once_skips_without_claim_key():
    action = MagicMock()
    assert (
        run_once(body={}, kind=KIND_PREBLAST, team_id="T1", logger=MagicMock(), action=action) is False
    )
    action.assert_not_called()


def test_run_once_releases_on_action_failure():
    with (
        patch("utilities.interaction_claims.claim_interaction", return_value=True),
        patch("utilities.interaction_claims.release_interaction") as release,
        pytest.raises(RuntimeError, match="boom"),
    ):
        run_once(
            body={"view": {"id": "V1"}},
            kind=KIND_STRAVA,
            team_id="T1",
            logger=MagicMock(),
            action=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    release.assert_called_once_with("V1", KIND_STRAVA)


def test_run_once_skips_when_already_claimed():
    action = MagicMock()
    with patch("utilities.interaction_claims.claim_interaction", return_value=False):
        assert (
            run_once(
                body={"view": {"id": "V1"}},
                kind=KIND_PREBLAST,
                team_id="T1",
                logger=MagicMock(),
                action=action,
            )
            is False
        )
    action.assert_not_called()


def test_root_handler_restore_pattern_after_bolt_clear():
    """Bolt clear_all_log_handlers + 'if not handlers: add StreamHandler' (app.py)."""
    from slack_bolt.adapter.aws_lambda import SlackRequestHandler

    root = logging.getLogger()
    prior = list(root.handlers)
    try:
        root.handlers.clear()
        root.addHandler(logging.StreamHandler())
        SlackRequestHandler.clear_all_log_handlers()
        if not root.handlers:
            root.addHandler(logging.StreamHandler())
        assert root.handlers
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    finally:
        root.handlers[:] = prior
