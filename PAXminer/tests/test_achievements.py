import os
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")


def test_achievement_seeds_have_rule_columns():
    from achievements.achievement_rules import ACHIEVEMENT_SEEDS, RULE_COLUMNS

    assert len(ACHIEVEMENT_SEEDS) == 14
    for seed in ACHIEVEMENT_SEEDS:
        for col in RULE_COLUMNS:
            assert col in seed
            assert seed[col] is not None


def test_period_bucket_for_date():
    from achievements.engine import period_bucket_for_date

    assert period_bucket_for_date(date(2026, 3, 15), "month") == 3
    assert period_bucket_for_date(date(2026, 3, 15), "year") == 2026


def test_verify_achievements_webhook_secret():
    from slack_http import verify_achievements_webhook_secret

    os.environ["PM_ACHIEVEMENTS_WEBHOOK_SECRET"] = "webhook-secret-value"
    assert verify_achievements_webhook_secret(
        {"X-Paxminer-Achievements-Webhook-Secret": "webhook-secret-value"}
    )
    assert not verify_achievements_webhook_secret(
        {"X-Paxminer-Achievements-Webhook-Secret": "wrong"}
    )


def test_build_kotter_message_monthly_copy():
    from kotter.kotter_report import build_kotter_message

    msg = build_kotter_message(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert "monthly" in msg.lower()
    assert "weekly" not in msg.lower()


def test_leaderboard_tie_break_by_display_name():
    from achievements.leaderboard import build_leaderboard_message

    awarded = pd.DataFrame(
        {
            "pax_id": ["U1", "U2", "U3"],
            "id": [1, 2, 3],
        }
    )
    users = pd.DataFrame(
        {
            "user_id": ["U1", "U2", "U3"],
            "user_name": ["Zed", "Amy", "Bob"],
        }
    )
    msg = build_leaderboard_message(awarded, users)
    assert msg.index("<@U2>") < msg.index("<@U3>") < msg.index("<@U1>")


def test_almost_there_excludes_awarded_and_caps_gap():
    from achievements.leaderboard import build_almost_there_message

    nation = pd.DataFrame(
        {
            "region": ["f3test"] * 3,
            "user_id": ["U1", "U2", "U3"],
            "email": ["a", "b", "c"],
            "date": pd.to_datetime(["2026-07-01"] * 3),
            "ao_id": [1, 1, 1],
            "q_flag": [0, 0, 0],
            "activity": ["beatdown"] * 3,
        }
    )
    rules = [
        {
            "id": 1,
            "name": "Golden Boy",
            "metric": "posts",
            "activity": "beatdown",
            "period": "year",
            "threshold": 50,
        }
    ]
    awarded = pd.DataFrame(
        {
            "pax_id": ["U1"],
            "achievement_id": [1],
            "date_awarded": [date(2026, 7, 1)],
        }
    )
    users = pd.DataFrame({"user_id": ["U1", "U2", "U3"], "user_name": ["A", "B", "C"]})

    with patch("achievements.leaderboard.period_bucket_for_today", return_value=2026):
        with patch("achievements.leaderboard._progress_for_rule") as mock_prog:
            mock_prog.return_value = pd.DataFrame(
                {
                    "user_id": ["U1", "U2", "U3"],
                    "gap": [1, 2, 3],
                    "achievement_id": [1, 1, 1],
                    "name": ["Golden Boy"] * 3,
                    "threshold": [50] * 3,
                }
            )
            msg = build_almost_there_message(nation, rules, awarded, "f3test", users)

    assert "U1" not in msg
    assert "<@U2>" in msg
    assert "3 posts away" not in msg


def test_run_achievements_skips_duplicate_grants():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "testing",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 1,
    }
    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C1",
        "slack_token": "enc",
        "region": "test",
    }
    awarded_row = {
        "id": 99,
        "achievement_id": 1,
        "pax_id": "U1",
        "date_awarded": date(2026, 7, 1),
        "period": "year",
    }
    qual = pd.DataFrame(
        {
            "pax_id": ["U1"],
            "achievement_id": [1],
            "date_awarded": [date(2026, 7, 1)],
            "period_bucket": [2026],
        }
    )

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        [awarded_row],
        [{"schema_name": "f3test"}],
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=pd.DataFrame()):
                with patch("achievements.runner.attach_home_regions", side_effect=lambda _c, n, _s: n):
                    with patch("achievements.runner.evaluate_rule", return_value=qual):
                        result = run_achievements_for_region(
                            mock_conn,
                            pm_schema="paxminer_test",
                            regional_schema="f3test",
                            region_row=region_row,
                            dry_run=True,
                        )

    assert result["grants"] == 0
    assert result["revokes"] == 0


def test_run_achievements_revokes_on_daily_when_unqualified():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "testing",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 50,
    }
    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C1",
        "slack_token": "enc",
        "region": "test",
    }
    awarded_row = {
        "id": 99,
        "achievement_id": 1,
        "pax_id": "U1",
        "date_awarded": date(2026, 7, 1),
        "period": "year",
    }

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        [awarded_row],
        [{"schema_name": "f3test"}],
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=pd.DataFrame()):
                with patch("achievements.runner.attach_home_regions", side_effect=lambda _c, n, _s: n):
                    with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                        result = run_achievements_for_region(
                            mock_conn,
                            pm_schema="paxminer_test",
                            regional_schema="f3test",
                            region_row=region_row,
                            pax_user_ids=None,
                            dry_run=True,
                        )

    assert result["grants"] == 0
    assert result["revokes"] == 1


def test_run_achievements_scoped_revoke_only_for_webhook_pax():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "testing",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 50,
    }
    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C1",
        "slack_token": "enc",
        "region": "test",
    }
    awarded_rows = [
        {
            "id": 99,
            "achievement_id": 1,
            "pax_id": "U1",
            "date_awarded": date(2026, 7, 1),
            "period": "year",
        },
        {
            "id": 100,
            "achievement_id": 1,
            "pax_id": "U2",
            "date_awarded": date(2026, 7, 1),
            "period": "year",
        },
    ]

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        awarded_rows,
        [{"schema_name": "f3test"}],
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=pd.DataFrame()):
                with patch("achievements.runner.attach_home_regions", side_effect=lambda _c, n, _s: n):
                    with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                        result = run_achievements_for_region(
                            mock_conn,
                            pm_schema="paxminer_test",
                            regional_schema="f3test",
                            region_row=region_row,
                            pax_user_ids={"U1"},
                            dry_run=True,
                        )

    assert result["revokes"] == 1


def test_run_achievements_grants_and_posts():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "testing",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 1,
    }
    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C1",
        "slack_token": "enc",
        "region": "test",
    }
    qual = pd.DataFrame(
        {
            "pax_id": ["U1"],
            "achievement_id": [1],
            "date_awarded": [date(2026, 7, 1)],
            "period_bucket": [2026],
        }
    )

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        [],
        [{"schema_name": "f3test"}],
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=pd.DataFrame()):
                with patch("achievements.runner.attach_home_regions", side_effect=lambda _c, n, _s: n):
                    with patch("achievements.runner.evaluate_rule", return_value=qual):
                        with patch("achievements.runner.post_message") as mock_post:
                            with patch("achievements.runner.open_dm_channel", return_value="D1"):
                                result = run_achievements_for_region(
                                    mock_conn,
                                    pm_schema="paxminer_test",
                                    regional_schema="f3test",
                                    region_row=region_row,
                                    dry_run=False,
                                )

    assert result["grants"] == 1
    assert result["revokes"] == 0
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO" in str(c)]
    assert insert_calls
    assert mock_post.call_count >= 2  # channel + DM
    mock_conn.commit.assert_called_once()


def test_validate_achievement_code():
    from config_paxminer import _validate_achievement

    errors = _validate_achievement(
        {
            "name": "X",
            "description": "d",
            "verb": "v",
            "code": "Bad Code",
            "metric": "posts",
            "activity": "beatdown",
            "period": "year",
            "threshold": 1,
        }
    )
    assert "code" in errors


def test_config_modal_initial_options_match_option_labels():
    from config_paxminer import _config_modal

    modal = _config_modal(
        {
            "send_achievements": 1,
            "send_aoq_reports": 1,
            "send_achievement_leaderboard": 0,
            "send_pax_charts": 1,
            "send_q_charts": 0,
            "send_region_leaderboard": 1,
            "send_ao_leaderboard": 0,
            "achievement_channel": "C1",
            "kotter_channel": "C2",
            "firstf_channel": "C3",
            "team_id": "T1",
            "schema_name": "f3test",
        }
    )
    by_id = {b["block_id"]: b for b in modal["blocks"] if "block_id" in b}
    for block_id in ("features", "charts"):
        element = by_id[block_id]["element"]
        options = element["options"]
        initial = element.get("initial_options") or []
        assert initial
        for opt in initial:
            assert opt in options


def test_achievements_handler_webhook_unauthorized():
    from handlers import achievements_handler

    resp = achievements_handler(
        {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"X-Paxminer-Achievements-Webhook-Secret": "wrong"},
            "body": "{}",
        },
        None,
    )
    assert resp["statusCode"] == 401


def test_achievements_handler_webhook_success():
    import json

    os.environ["PM_ACHIEVEMENTS_WEBHOOK_SECRET"] = "webhook-secret-value"
    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C1",
        "slack_token": "enc",
        "region": "test",
        "schema_name": "f3test",
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = region_row

    with patch("handlers.connect_from_env", return_value=mock_conn):
        with patch(
            "achievements.runner.run_achievements_for_region",
            return_value={"grants": 1, "revokes": 0},
        ) as mock_run:
            from handlers import achievements_handler

            resp = achievements_handler(
                {
                    "requestContext": {"http": {"method": "POST"}},
                    "headers": {"X-Paxminer-Achievements-Webhook-Secret": "webhook-secret-value"},
                    "body": json.dumps(
                        {
                            "schema": "f3test",
                            "pax_user_ids": ["U1", "U2"],
                            "post_to_ao": True,
                            "ao_channel_id": "C_AO",
                        }
                    ),
                },
                None,
            )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["ok"] is True
    assert mock_run.call_args.kwargs["regional_schema"] == "f3test"
    assert mock_run.call_args.kwargs["pax_user_ids"] == {"U1", "U2"}
    assert mock_run.call_args.kwargs["post_to_ao"] is True
    assert mock_run.call_args.kwargs["ao_channel_id"] == "C_AO"
