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


def test_schedule_tick_fans_out_computed_destinations():
    import json
    from unittest.mock import MagicMock, patch

    from handlers import schedule_handler

    due = [{"id": 9, "report_definition_id": 4, "destination_type": "dm_all_pax"}]
    mock_conn = MagicMock()

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


def test_schedule_tick_runs_specific_destinations_inline():
    import json
    from unittest.mock import MagicMock, patch

    from handlers import schedule_handler

    due = [{"id": 9, "report_definition_id": 4, "destination_type": "specific_channels"}]
    mock_conn = MagicMock()

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("schedule_runner.list_due_schedules", return_value=due):
                    with patch.dict(
                        "os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "paxminer-test-schedule"}
                    ):
                        with patch("schedule_runner.async_invoke_schedule_item") as mock_fan:
                            with patch(
                                "schedule_runner.run_one_schedule_item",
                                return_value={"schedule_id": 9, "ok": True},
                            ) as mock_run:
                                resp = schedule_handler({}, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["results"][0]["ok"] is True
    mock_fan.assert_not_called()
    mock_run.assert_called_once()


def test_achievement_rule_backfill_clears_lock_when_region_missing():
    from handlers import schedule_handler

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = None

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("achievements.range.clear_reeval_lock") as unlock:
                    with patch("achievements.runner.reconcile_rule_awards") as recon:
                        resp = schedule_handler(
                            {
                                "source": "achievement_rule_backfill",
                                "schema": "f3test",
                                "achievement_id": 3,
                            },
                            None,
                        )
    assert resp["statusCode"] == 404
    recon.assert_not_called()
    unlock.assert_called_once()
    assert unlock.call_args.args[1] == "f3test"
    assert unlock.call_args.args[2] == 3


def test_achievement_rule_backfill_clears_lock_on_bad_dates():
    from handlers import schedule_handler

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {"schema_name": "f3test"}

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("achievements.range.clear_reeval_lock") as unlock:
                    with patch("achievements.runner.reconcile_rule_awards") as recon:
                        resp = schedule_handler(
                            {
                                "source": "achievement_rule_backfill",
                                "schema": "f3test",
                                "achievement_id": 3,
                                "start": "not-a-date",
                            },
                            None,
                        )
    assert resp["statusCode"] == 500
    recon.assert_not_called()
    unlock.assert_called_once()
    assert unlock.call_args.args[2] == 3


def test_achievement_rule_backfill_passes_action_to_reconcile():
    from handlers import schedule_handler

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {"schema_name": "f3test"}

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch("handlers._pm_schema", return_value="paxminer_test"):
            with patch("handlers._registry_database", return_value="paxminer_test"):
                with patch("achievements.runner.reconcile_rule_awards", return_value={"grants": 0}) as recon:
                    resp = schedule_handler(
                        {
                            "source": "achievement_rule_backfill",
                            "schema": "f3test",
                            "achievement_id": 3,
                            "actor": "UADMIN1234",
                            "action": "created",
                            "automatic": True,
                        },
                        None,
                    )
    assert resp["statusCode"] == 200
    assert recon.call_args.kwargs["action"] == "created"
    assert recon.call_args.kwargs["automatic"] is True
    assert recon.call_args.kwargs["actor"] == "UADMIN1234"


def test_achievements_leaderboard_smoke():
    with patch("handlers.connect_from_env") as mock_conn:
        mock_conn.return_value.close = MagicMock()
        with patch("achievements.leaderboard.run_leaderboard", return_value=[{"dry_run": True}]) as mock_run:
            from handlers import achievements_handler

            resp = achievements_handler({"source": "smoke", "feature": "achievement_leaderboard"}, None)
            body = json.loads(resp["body"])
            assert body["ok"] is True
            assert mock_run.call_args.kwargs.get("dry_run") is True
