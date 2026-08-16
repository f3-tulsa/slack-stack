import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from utilities.database import DbManager


def test_transaction_commits_and_closes():
    session = MagicMock()
    with (
        patch("utilities.database.get_session", return_value=session),
        patch("utilities.database.close_session") as close,
    ):
        with DbManager.transaction(schema="f3test") as s:
            assert s is session
            s.add("row")
        session.commit.assert_called_once()
        session.rollback.assert_not_called()
        session.close.assert_called_once()
        close.assert_called_once_with(session)
        assert session.expire_on_commit is False


def test_transaction_rolls_back_on_error_and_closes():
    session = MagicMock()
    with (
        patch("utilities.database.get_session", return_value=session),
        patch("utilities.database.close_session") as close,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            with DbManager.transaction():
                raise RuntimeError("boom")
        session.rollback.assert_called_once()
        session.commit.assert_not_called()
        session.close.assert_called_once()
        close.assert_called_once_with(session)
