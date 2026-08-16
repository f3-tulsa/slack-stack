import json
from unittest.mock import MagicMock, patch

import os

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")


def test_achievements_reconcile_mode_is_silent():
    import json
    from unittest.mock import MagicMock, patch

    from handlers import achievements_handler

    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("achievements.runner.run_daily", return_value=[{"grants": 3}]) as mock_run:
            resp = achievements_handler({"mode": "reconcile"}, None)
    body = json.loads(resp["body"])
    assert body["ok"] is True
    assert body["mode"] == "reconcile"
    assert mock_run.call_args.kwargs.get("announce") is False
    assert mock_run.call_args.kwargs.get("dry_run") is not True


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


def test_schedule_tick_isolates_per_item_failures():
    import json
    from unittest.mock import MagicMock, patch

    from handlers import schedule_handler

    due = [
        {"id": 1, "report_definition_id": 10},
        {"id": 2, "report_definition_id": 11},
    ]
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.side_effect = [
        {"report_type": "kotter"},
        {"report_type": "kotter"},
    ]

    def run_one(_conn, _pm, row, **_kwargs):
        if row["id"] == 1:
            raise RuntimeError("boom item 1")
        return {"schedule_id": 2, "ok": True}

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("schedule_runner.list_due_schedules", return_value=due):
                    with patch(
                        "schedule_runner.run_one_schedule_item", side_effect=run_one
                    ):
                        resp = schedule_handler({}, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["due"] == 2
    assert body["results"][0]["ok"] is False
    assert "boom item 1" in body["results"][0]["error"]
    assert body["results"][1]["ok"] is True


def test_schedule_tick_fans_out_award_achievements():
    import json
    from unittest.mock import MagicMock, patch

    from handlers import schedule_handler

    due = [{"id": 9, "report_definition_id": 4}]
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {"report_type": "award_achievements"}

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("schedule_runner.list_due_schedules", return_value=due):
                    with patch.dict(
                        "os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "paxminer-test-schedule"}
                    ):
                        with patch(
                            "schedule_runner.async_invoke_schedule_item"
                        ) as mock_fan:
                            with patch("schedule_runner.run_one_schedule_item") as mock_run:
                                resp = schedule_handler({}, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["results"][0]["queued"] is True
    mock_fan.assert_called_once_with(9, force=False)
    mock_run.assert_not_called()


def test_achievements_leaderboard_smoke():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("achievements.leaderboard.run_leaderboard", return_value=[{"dry_run": True}]) as mock_run:
            from handlers import achievements_handler

            resp = achievements_handler({"source": "smoke", "feature": "achievement_leaderboard"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True
            assert mock_run.call_args.kwargs.get("dry_run") is True
