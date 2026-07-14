import json
from unittest.mock import MagicMock, patch

import os

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")


def test_kotter_handler_smoke_dry_run():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("kotter.kotter_report.run_kotter", return_value=[]) as mock_run:
            from handlers import kotter_handler

            resp = kotter_handler({"source": "smoke"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True
            assert mock_run.call_args.kwargs.get("dry_run") is True


def test_achievements_daily_smoke_dry_run():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("achievements.runner.run_daily", return_value=[]) as mock_run:
            from handlers import achievements_handler

            resp = achievements_handler({"source": "smoke"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True
            assert resp["statusCode"] == 200
            assert mock_run.call_args.kwargs.get("dry_run") is True


def test_achievements_leaderboard_smoke():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("achievements.leaderboard.run_leaderboard", return_value=[{"dry_run": True}]) as mock_run:
            from handlers import achievements_handler

            resp = achievements_handler({"source": "smoke", "feature": "achievement_leaderboard"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True
            assert mock_run.call_args.kwargs.get("dry_run") is True
