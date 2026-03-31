"""
Local verification for extend-schedule (mocked DB + handler routing).

Run from repo root:
  cd qsignups/qsignups && PYTHONPATH=. python3 ../testing/test_extend_schedule_local.py
"""
from __future__ import annotations

import logging
import os
import sys

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_extend_all_schedules_no_db() -> None:
    from unittest.mock import MagicMock, patch

    from slack.handlers.weekly import extend_all_schedules

    mock_session = MagicMock()
    query_mock = MagicMock()
    mock_session.query.return_value = query_mock
    query_mock.all.return_value = []

    with patch("slack.handlers.weekly.get_session", return_value=mock_session):
        with patch("slack.handlers.weekly.close_session"):
            extend_all_schedules(logging.getLogger("test"))


def test_handler_routes_extend_schedule() -> None:
    """Import app without Slack auth.test (Bolt calls it in _init_middleware_list)."""
    import importlib
    import sys
    from unittest.mock import patch

    with patch("slack_bolt.app.app.App._init_middleware_list", lambda *args, **kwargs: None):
        with patch("slack.handlers.weekly.extend_all_schedules") as ext:
            sys.modules.pop("app", None)
            import app as app_mod

            out = app_mod.handler({"source": "qsignups.extend-schedule"}, None)
            ext.assert_called_once()
            assert out.get("statusCode") == 200


def main() -> None:
    # Before any import of app.py (require_encryption_key + Bolt App)
    os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-min-16")
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-for-local-extend-test")
    os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

    test_extend_all_schedules_no_db()
    print("extend_all_schedules (empty weeklies, mocked session) OK")
    test_handler_routes_extend_schedule()
    print("handler extend-schedule routing OK")


if __name__ == "__main__":
    main()
