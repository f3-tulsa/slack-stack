import json
from unittest.mock import MagicMock, patch

import os

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")


def test_kotter_handler_smoke_dry_run():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.cursor.return_value.__enter__ = MagicMock(
            return_value=MagicMock(fetchall=lambda: [])
        )
        mock_conn.return_value.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value.close = MagicMock()
        with patch("kotter.kotter_report.run_kotter", return_value=[]):
            from handlers import kotter_handler

            resp = kotter_handler({"source": "smoke"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True


def test_achievements_leaderboard_smoke():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("achievements.leaderboard.run_leaderboard", return_value=[{"dry_run": True}]):
            from handlers import achievements_handler

            resp = achievements_handler({"source": "smoke", "feature": "achievement_leaderboard"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True
