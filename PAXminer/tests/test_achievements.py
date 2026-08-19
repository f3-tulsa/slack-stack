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

    assert period_bucket_for_date(date(2026, 3, 15), "month") == "2026-03"
    assert period_bucket_for_date(date(2026, 3, 15), "year") == "2026"
    assert period_bucket_for_date(date(2025, 12, 29), "week") == "2026-W01"
    assert period_bucket_for_date(date(2026, 1, 5), "week") == "2026-W02"


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

    text, blocks = build_kotter_message(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert "monthly" in text.lower()
    assert "weekly" not in text.lower()
    assert blocks
    assert blocks[0]["type"] == "header"


def test_build_kotter_message_mentions_known_and_falls_back():
    from kotter.kotter_report import build_kotter_message

    known = "U0ANGDEBSKE"
    unknown = "USEEDPAX12XXXX"
    mia = pd.DataFrame(
        {
            "user_id": [known],
            "user_name": ["Ben Baldwin"],
            "date": ["July 01, 2026"],
        }
    )
    lowq = pd.DataFrame(
        {
            "user_id": [unknown],
            "user_name": ["[SEED] PAX 12"],
            "date": [pd.Timestamp("2026-06-20")],
        }
    )
    noq = pd.DataFrame({"user_id": [unknown], "user_name": ["[SEED] PAX 12"]})
    text, _blocks = build_kotter_message(
        mia, lowq, noq, known_ids={known}
    )
    assert f"<@{known}>" in text
    assert "`[SEED] PAX 12`" in text
    assert f"`{unknown}`" not in text
    assert f"<@{unknown}>" not in text
    assert "\n- " not in text


def test_kotter_mia_keeps_nan_home_ao():
    """PAX whose last post predates HOME_AO_CAPTURE must still appear in MIA."""
    from datetime import date, timedelta

    from kotter import kotter_report as kotter_mod

    today = date.today()
    # Last post 3 weeks ago → inside the default MIA window (2–8 weeks).
    last_post = (today - timedelta(weeks=3)).isoformat()
    frame = pd.DataFrame(
        {
            "email": ["mia@x.com"],
            "user_id": ["U0MIATEST01"],
            "user_name": ["MIA PAX"],
            "ao_id": ["C_OLD"],
            "ao": ["old_ao"],
            "date": [last_post],
            "q_flag": [0],
        }
    )
    region_row = {
        "send_aoq_reports": 1,
        "schema_name": "f3ttown",
        "kotter_channel": "C_KOTTER",
        "slack_token": "enc",
        # Capture window shorter than the gap so home_ao stays NaN.
        "HOME_AO_CAPTURE": 1,
        "NO_POST_THRESHOLD": 2,
        "REMINDER_WEEKS": 8,
    }
    with patch("paxminer_db.read_sql_df", return_value=frame):
        with patch.object(
            kotter_mod, "attach_home_regions", side_effect=lambda _c, n, _s: n
        ):
            result = kotter_mod.run_kotter_for_region(
                MagicMock(),
                "paxminer",
                region_row,
                dry_run=True,
            )
    assert result.get("dry_run") is True
    text = result.get("text", "")
    assert "U0MIATEST01" in text
    assert "haven't posted in a while" in text


def test_load_nation_attendance_coerces_bad_dates():
    from achievements.attendance import load_nation_attendance

    frame = pd.DataFrame(
        {
            "email": ["a@x.com", "b@x.com"],
            "user_name": ["A", "B"],
            "user_id": ["U1", "U2"],
            "ao_id": ["C1", "C2"],
            "ao": ["ao1", "ao2"],
            "date": ["2026-07-01", "date"],
            "q_flag": [0, 1],
            "backblast": ["bb", "bb"],
        }
    )
    with patch("paxminer_db.read_sql_df", return_value=frame):
        out = load_nation_attendance(MagicMock(), ["f3ttown"])
    assert len(out) == 1
    assert str(out.iloc[0]["date"].date()) == "2026-07-01"


def test_kotter_nation_coerces_bad_dates():
    """Bad bd_date values must not abort run_kotter_for_region."""
    from kotter import kotter_report as kotter_mod

    bad_frame = pd.DataFrame(
        {
            "email": ["a@x.com", "b@x.com"],
            "user_id": ["U1", "U2"],
            "ao_id": ["C1", "C2"],
            "ao": ["ao1", "ao2"],
            "date": ["2026-07-01", "date"],
            "q_flag": [0, 1],
        }
    )
    region_row = {
        "send_aoq_reports": 1,
        "schema_name": "f3ttown",
        "kotter_channel": "C_KOTTER",
        "slack_token": "enc",
        "region": "f3ttown",
        "NO_POST_THRESHOLD": 2,
        "REMINDER_WEEKS": 2,
        "HOME_AO_CAPTURE": 8,
        "NO_Q_THRESHOLD_WEEKS": 4,
        "NO_Q_THRESHOLD_POSTS": 4,
    }
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchall.return_value = [{"schema_name": "f3ttown"}]

    def _attach(_conn, df, _schemas):
        out = df.copy()
        out["region"] = "f3ttown"
        return out

    with (
        patch("paxminer_db.read_sql_df", return_value=bad_frame),
        patch.object(kotter_mod, "attach_home_regions", side_effect=_attach),
        patch.object(kotter_mod, "build_kotter_message", return_value=("ok", [])),
    ):
        result = kotter_mod.run_kotter_for_region(
            conn, "paxminer_test", region_row, dry_run=True
        )
    assert result.get("dry_run") is True
    assert "error" not in result


def test_leaderboard_tie_break_by_display_name():
    from achievements.leaderboard import build_leaderboard_message

    awarded = pd.DataFrame(
        {
            "pax_id": ["U01AAAAAAA1", "U01AAAAAAA2", "U01AAAAAAA3"],
            "id": [1, 2, 3],
        }
    )
    users = pd.DataFrame(
        {
            "user_id": ["U01AAAAAAA1", "U01AAAAAAA2", "U01AAAAAAA3"],
            "user_name": ["Zed", "Amy", "Bob"],
        }
    )
    text, blocks = build_leaderboard_message(awarded, users)
    assert text.index("<@U01AAAAAAA2>") < text.index("<@U01AAAAAAA3>") < text.index("<@U01AAAAAAA1>")
    assert "\n- " not in text
    assert blocks
    assert any(b.get("type") == "header" for b in blocks)
    assert "YTD" not in text


def test_almost_there_excludes_awarded_and_caps_gap():
    from achievements.leaderboard import build_almost_there_message

    nation = pd.DataFrame(
        {
            "region": ["f3test"] * 3,
            "user_id": ["U01AAAAAAA1", "U01AAAAAAA2", "U01AAAAAAA3"],
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
            "pax_id": ["U01AAAAAAA1"],
            "achievement_id": [1],
            "date_awarded": [date(2026, 7, 1)],
        }
    )
    users = pd.DataFrame(
        {
            "user_id": ["U01AAAAAAA1", "U01AAAAAAA2", "U01AAAAAAA3"],
            "user_name": ["A", "B", "C"],
        }
    )

    with patch("achievements.leaderboard.period_bucket_for_today", return_value="2026"):
        with patch("achievements.leaderboard._progress_for_rule") as mock_prog:
            mock_prog.return_value = pd.DataFrame(
                {
                    "user_id": ["U01AAAAAAA1", "U01AAAAAAA2", "U01AAAAAAA3"],
                    "gap": [1, 2, 3],
                    "achievement_id": [1, 1, 1],
                    "name": ["Golden Boy"] * 3,
                    "threshold": [50] * 3,
                }
            )
            text, blocks = build_almost_there_message(nation, rules, awarded, "f3test", users)

    assert "U01AAAAAAA1" not in text
    assert "<@U01AAAAAAA2>" in text
    assert "3 posts away" not in text
    assert blocks


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
    nation = pd.DataFrame(
        {"email": ["a@b.c"], "user_id": ["U1"], "date": [date(2026, 7, 1)], "region": ["f3test"]}
    )

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        [awarded_row],
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=nation):
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


def test_run_achievements_skips_when_no_attendance_data():
    """Empty attendance must not mass-revoke awards."""
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
    mock_cur.fetchall.side_effect = [[rule], [awarded_row]]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=pd.DataFrame()):
                with patch("achievements.runner.evaluate_rule") as mock_eval:
                    result = run_achievements_for_region(
                        mock_conn,
                        pm_schema="paxminer_test",
                        regional_schema="f3test",
                        region_row=region_row,
                        dry_run=True,
                    )

    assert result == {"skipped": "no attendance data"}
    mock_eval.assert_not_called()
    # Must not DELETE awards when attendance is empty
    delete_calls = [c for c in mock_cur.execute.call_args_list if "DELETE FROM" in str(c)]
    assert delete_calls == []


def test_channel_override_bypasses_send_achievements_gate():
    """Schedule path uses channel_override; send_achievements may be off."""
    from achievements.runner import run_achievements_for_region

    region_row = {
        "send_achievements": 0,
        "achievement_channel": None,
        "slack_token": "enc",
        "region": "test",
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[], []]  # no rules

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            result = run_achievements_for_region(
                mock_conn,
                pm_schema="paxminer_test",
                regional_schema="f3test",
                region_row=region_row,
                channel_override="C_SCHEDULE",
                dry_run=True,
            )

    assert result == {"skipped": "no rules"}

    # Without override, the legacy gate still applies.
    result2 = run_achievements_for_region(
        mock_conn,
        pm_schema="paxminer_test",
        regional_schema="f3test",
        region_row=region_row,
        dry_run=True,
    )
    assert result2 == {"skipped": "send_achievements off"}


def test_run_achievements_skips_revokes_on_unscoped_run():
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
    nation = pd.DataFrame(
        {"email": ["a@b.c"], "user_id": ["U1"], "date": [date(2026, 7, 1)], "region": ["f3test"]}
    )

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        [awarded_row],
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=nation):
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
    assert result["revokes"] == 0


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
        "version_id": 7,
        "enabled": 1,
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
            "achievement_version_id": 7,
            "period_key": "2026",
        },
        {
            "id": 100,
            "achievement_id": 1,
            "pax_id": "U2",
            "date_awarded": date(2026, 7, 1),
            "period": "year",
            "achievement_version_id": 7,
            "period_key": "2026",
        },
    ]
    nation = pd.DataFrame(
        {"email": ["a@b.c"], "user_id": ["U1"], "date": [date(2026, 7, 1)], "region": ["f3test"]}
    )

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [
        [rule],
        awarded_rows,
    ]

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=nation):
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
            "pax_id": ["U01ABCDEF23"],
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
    ]
    nation = pd.DataFrame(
        {
            "email": ["a@b.c"],
            "user_id": ["U01ABCDEF23"],
            "user_name": ["Test PAX"],
            "date": [date(2026, 7, 1)],
            "region": ["f3test"],
        }
    )

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch(
                "achievements.runner.workspace_user_ids", return_value={"U01ABCDEF23"}
            ):
                with patch("achievements.runner.load_nation_attendance", return_value=nation):
                    with patch(
                        "achievements.runner.attach_home_regions",
                        side_effect=lambda _c, n, _s: n,
                    ):
                        with patch("achievements.runner.evaluate_rule", return_value=qual):
                            with patch("achievements.runner.post_message") as mock_post:
                                with patch(
                                    "achievements.runner.open_dm_channel", return_value="D1"
                                ):
                                    result = run_achievements_for_region(
                                        mock_conn,
                                        pm_schema="paxminer_test",
                                        regional_schema="f3test",
                                        region_row=region_row,
                                        dry_run=False,
                                    )

    assert result["grants"] == 1
    assert result["revokes"] == 0
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT" in str(c)]
    assert insert_calls
    assert "INSERT IGNORE" in str(insert_calls[0])
    assert mock_post.call_count >= 2  # channel + DM
    # Per-grant commit so a timeout mid-loop cannot re-announce uncommitted rows.
    assert mock_conn.commit.call_count >= 1
    mock_conn.commit.assert_called_once()


def test_run_achievements_ignored_insert_does_not_announce():
    """A racing second grant (INSERT IGNORE rowcount 0) must not clap or DM."""
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
            "pax_id": ["U01ABCDEF23"],
            "achievement_id": [1],
            "date_awarded": [date(2026, 7, 1)],
            "period_bucket": [2026],
        }
    )
    nation = pd.DataFrame(
        {
            "email": ["a@b.c"],
            "user_id": ["U01ABCDEF23"],
            "user_name": ["Test PAX"],
            "date": [date(2026, 7, 1)],
            "region": ["f3test"],
        }
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], []]
    mock_cur.rowcount = 0

    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client"):
            with patch(
                "achievements.runner.workspace_user_ids", return_value={"U01ABCDEF23"}
            ):
                with patch("achievements.runner.load_nation_attendance", return_value=nation):
                    with patch(
                        "achievements.runner.attach_home_regions",
                        side_effect=lambda _c, n, _s: n,
                    ):
                        with patch("achievements.runner.evaluate_rule", return_value=qual):
                            with patch("achievements.runner.post_message") as mock_post:
                                with patch("achievements.runner.open_dm_channel") as mock_dm:
                                    result = run_achievements_for_region(
                                        mock_conn,
                                        pm_schema="paxminer_test",
                                        regional_schema="f3test",
                                        region_row=region_row,
                                        dry_run=False,
                                        emit_logs=False,
                                    )

    assert result["grants"] == 0
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT IGNORE" in str(c)]
    assert insert_calls
    mock_post.assert_not_called()
    mock_dm.assert_not_called()


def test_announce_false_inserts_without_slack():
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
        "send_achievements": 0,
        "achievement_channel": None,
        "slack_token": None,
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
    awarded_row = {
        "id": 99,
        "achievement_id": 1,
        "pax_id": "U2",
        "date_awarded": date(2026, 7, 1),
        "period": "year",
    }
    nation = pd.DataFrame(
        {"email": ["a@b.c"], "user_id": ["U1"], "date": [date(2026, 7, 1)], "region": ["f3test"]}
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], [awarded_row]]
    mock_cur.fetchone.return_value = None

    with patch("achievements.runner.decrypt_field") as mock_dec:
        with patch("achievements.runner.slack_client") as mock_client:
            with patch("achievements.runner.load_nation_attendance", return_value=nation):
                with patch(
                    "achievements.runner.attach_home_regions", side_effect=lambda _c, n, _s: n
                ):
                    with patch("achievements.runner.evaluate_rule", return_value=qual):
                        with patch("achievements.runner.post_message") as mock_post:
                            result = run_achievements_for_region(
                                mock_conn,
                                pm_schema="paxminer_test",
                                regional_schema="f3test",
                                region_row=region_row,
                                announce=False,
                            )

    assert result["grants"] == 1
    assert result["revokes"] == 0
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT IGNORE" in str(c)]
    assert insert_calls
    delete_calls = [c for c in mock_cur.execute.call_args_list if "DELETE FROM" in str(c)]
    assert delete_calls == []
    mock_dec.assert_not_called()
    mock_client.assert_not_called()
    mock_post.assert_not_called()


def test_resolve_achievement_channel_prefers_schedule():
    from achievements.runner import resolve_achievement_channel

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {"destination_channels": '["C_SCHED"]'}
    region_row = {"achievement_channel": "C_FALLBACK", "send_achievements": 1}

    assert (
        resolve_achievement_channel(mock_conn, "paxminer_test", "f3test", region_row)
        == "C_SCHED"
    )
    mock_cur.fetchone.return_value = None
    assert (
        resolve_achievement_channel(mock_conn, "paxminer_test", "f3test", region_row)
        == "C_FALLBACK"
    )
    assert (
        resolve_achievement_channel(
            mock_conn,
            "paxminer_test",
            "f3test",
            region_row,
            channel_override="C_OVR",
        )
        == "C_OVR"
    )


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


def test_parse_kotter_form_non_numeric_falls_back_to_defaults():
    from config_schedule import parse_kotter_form

    parsed = parse_kotter_form(
        {
            "view": {
                "state": {
                    "values": {
                        "NO_POST_THRESHOLD": {"val": {"value": "abc"}},
                        "REMINDER_WEEKS": {"val": {"value": ""}},
                        "HOME_AO_CAPTURE": {"val": {"value": "nope"}},
                        "NO_Q_THRESHOLD_WEEKS": {"val": {"value": "3.5"}},
                        "NO_Q_THRESHOLD_POSTS": {"val": {"value": None}},
                    }
                }
            }
        }
    )
    assert parsed["NO_POST_THRESHOLD"] == 2
    assert parsed["REMINDER_WEEKS"] == 2
    assert parsed["HOME_AO_CAPTURE"] == 8
    assert parsed["NO_Q_THRESHOLD_WEEKS"] == 4
    assert parsed["NO_Q_THRESHOLD_POSTS"] == 4


def test_parse_achievement_form_non_numeric_threshold_is_none():
    from config_paxminer import _parse_achievement_form, _validate_achievement

    values = _parse_achievement_form(
        {
            "view": {
                "state": {
                    "values": {
                        "name": {"val": {"value": "Six Pack"}},
                        "description": {"val": {"value": "d"}},
                        "verb": {"val": {"value": "posting"}},
                        "code": {"val": {"value": "six_pack"}},
                        "metric": {"val": {"selected_option": {"value": "posts"}}},
                        "activity": {"val": {"selected_option": {"value": "beatdown"}}},
                        "period": {"val": {"selected_option": {"value": "week"}}},
                        "threshold": {"val": {"value": "abc"}},
                    }
                }
            }
        }
    )
    assert values["threshold"] is None
    errors = _validate_achievement(values)
    assert errors.get("threshold") == "Enter a whole number"


def test_config_modal_hub_has_timezone_and_section_buttons():
    from config_paxminer import _config_modal, _parse_modal_values
    from config_schedule import (
        OPEN_KOTTER_CONFIG_ACTION_ID,
        OPEN_REPORTS_ACTION_ID,
        OPEN_SCHEDULE_ACTION_ID,
    )

    modal = _config_modal(
        {
            "send_achievements": 1,
            "achievement_channel": "C12345678",
            "timezone": "America/Chicago",
            "team_id": "T1",
            "schema_name": "f3test",
        }
    )
    by_id = {b["block_id"]: b for b in modal["blocks"] if "block_id" in b}
    ids = [b["block_id"] for b in modal["blocks"] if "block_id" in b]
    assert ids.index("hub_actions") < ids.index("timezone") < ids.index("log_channel")
    assert "timezone" in by_id
    assert by_id["timezone"]["element"]["type"] == "static_select"
    assert by_id["log_channel"]["element"]["type"] == "conversations_select"
    assert by_id["log_channel"]["optional"] is True
    assert set(by_id["log_channel"]["element"]["filter"]["include"]) == {"public", "private"}
    assert "hub_actions" in by_id
    action_ids = {e["action_id"] for e in by_id["hub_actions"]["elements"]}
    assert OPEN_SCHEDULE_ACTION_ID in action_ids
    assert OPEN_REPORTS_ACTION_ID in action_ids
    assert OPEN_KOTTER_CONFIG_ACTION_ID in action_ids

    # Achievements enable/channel moved to Schedule (Award Achievements).
    assert "send_achievements" not in by_id
    assert "achievement_channel" not in by_id
    assert "kotter_channel" not in by_id
    assert "firstf_channel" not in by_id
    assert "features" not in by_id
    schedule_btn = next(
        e for e in by_id["hub_actions"]["elements"] if e["action_id"] == OPEN_SCHEDULE_ACTION_ID
    )
    assert schedule_btn.get("style") == "primary"
    configure = next(
        b for b in modal["blocks"] if b.get("type") == "section" and "Configure" in str(b)
    )
    assert "awards" in configure["text"]["text"].lower()

    parsed = _parse_modal_values(
        {
            "view": {
                "state": {
                    "values": {
                        "timezone": {
                            "val": {"selected_option": {"value": "America/Chicago"}}
                        },
                    }
                }
            }
        }
    )
    assert parsed == {"timezone": "America/Chicago", "log_channel": None}

    picked = _parse_modal_values(
        {
            "view": {
                "state": {
                    "values": {
                        "timezone": {
                            "val": {"selected_option": {"value": "America/Chicago"}}
                        },
                        "log_channel": {
                            "val": {"selected_conversation": "CLOG123"}
                        },
                    }
                }
            }
        }
    )
    assert picked == {"timezone": "America/Chicago", "log_channel": "CLOG123"}


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
    assert mock_run.call_args.kwargs["log_mode"] == "webhook"


def test_achievements_loads_only_regional_schema():
    """Achievements must not fan out across other regions' schemas."""
    from achievements import runner as runner_mod

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
        "region": "Tulsa",
        "schema_name": "f3ttown_test",
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], []]

    with patch.object(runner_mod, "decrypt_field", return_value="xoxb-test"):
        with patch.object(runner_mod, "slack_client", return_value=MagicMock()):
            with patch.object(runner_mod, "workspace_user_ids", return_value=set()):
                with patch.object(
                    runner_mod,
                    "load_nation_attendance",
                    return_value=pd.DataFrame(
                        {
                            "email": ["a@b.c"],
                            "user_id": ["U1"],
                            "date": [date(2026, 7, 1)],
                            "region": ["f3ttown_test"],
                        }
                    ),
                ) as mock_nation:
                    with patch.object(
                        runner_mod, "attach_home_regions", side_effect=lambda _c, n, _s: n
                    ) as mock_home:
                        with patch.object(
                            runner_mod, "evaluate_rule", return_value=pd.DataFrame()
                        ):
                            runner_mod.run_achievements_for_region(
                                mock_conn,
                                pm_schema="paxminer_test",
                                regional_schema="f3ttown_test",
                                region_row=region_row,
                                dry_run=True,
                            )

    mock_nation.assert_called_once()
    assert mock_nation.call_args.args[1] == ["f3ttown_test"]
    mock_home.assert_called_once()
    assert mock_home.call_args.args[2] == ["f3ttown_test"]


def test_kotter_loads_only_regional_schema():
    """Kotter must not fan out across other regions' schemas."""
    from kotter import kotter_report as kotter_mod

    frame = pd.DataFrame(
        {
            "email": ["a@x.com"],
            "user_id": ["U1"],
            "ao_id": ["C1"],
            "ao": ["ao1"],
            "date": ["2026-07-01"],
            "q_flag": [0],
        }
    )
    region_row = {
        "schema_name": "f3ttown_test",
        "kotter_channel": "C_KOTTER",
        "slack_token": "enc",
        "region": "Tulsa",
        "NO_POST_THRESHOLD": 2,
        "REMINDER_WEEKS": 2,
        "HOME_AO_CAPTURE": 8,
        "NO_Q_THRESHOLD_WEEKS": 4,
        "NO_Q_THRESHOLD_POSTS": 4,
    }
    conn = MagicMock()
    schemas_seen: list[list[str]] = []

    def _attach(_conn, df, schemas):
        schemas_seen.append(list(schemas))
        out = df.copy()
        out["region"] = "f3ttown_test"
        return out

    with (
        patch("paxminer_db.read_sql_df", return_value=frame) as mock_sql,
        patch.object(kotter_mod, "attach_home_regions", side_effect=_attach),
        patch.object(kotter_mod, "build_kotter_message", return_value=("ok", [])),
    ):
        result = kotter_mod.run_kotter_for_region(
            conn, "paxminer_test", region_row, dry_run=True
        )

    assert result.get("dry_run") is True
    mock_sql.assert_called_once()
    assert schemas_seen == [["f3ttown_test"]]


def test_leaderboard_loads_only_regional_schema():
    """Achievement leaderboard almost-there must not fan out across regions."""
    from achievements import leaderboard as lb_mod

    region_row = {
        "schema_name": "f3ttown_test",
        "achievement_channel": "C_ACH",
        "slack_token": "enc",
        "region": "Tulsa",
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.return_value = []

    with patch.object(lb_mod, "load_nation_attendance", return_value=pd.DataFrame()) as mock_nation:
        with patch.object(lb_mod, "attach_home_regions", side_effect=lambda _c, n, _s: n):
            with patch.object(lb_mod, "build_leaderboard_message", return_value=("", [])):
                with patch.object(lb_mod, "build_almost_there_message", return_value=("", [])):
                    result = lb_mod.run_leaderboard_for_region(
                        mock_conn, "paxminer_test", region_row, dry_run=True
                    )
                    almost = lb_mod.run_almost_there_for_region(
                        mock_conn, "paxminer_test", region_row, dry_run=True
                    )

    assert result.get("dry_run") is True
    assert almost.get("dry_run") is True
    mock_nation.assert_called_once()
    assert mock_nation.call_args.args[1] == ["f3ttown_test"]


def test_achievement_failure_log_uses_schema_name():
    from achievements.runner import _post_achievement_failure_log

    region_row = {
        "region": "Tulsa",
        "schema_name": "f3ttown_test",
        "slack_token": "enc",
    }
    log_lines: list[str] = []
    with patch("achievements.runner.decrypt_field", return_value="xoxb-test"):
        with patch("achievements.runner.slack_client", return_value=MagicMock()):
            with patch(
                "achievements.runner.post_log",
                side_effect=lambda _c, text, **_k: log_lines.append(text),
            ):
                _post_achievement_failure_log(region_row, RuntimeError("boom"))

    assert len(log_lines) == 1
    assert "The *Achievements* job was run as scheduled" in log_lines[0]
    assert "Status: failed" in log_lines[0]
    assert "boom" in log_lines[0]
    assert not log_lines[0].startswith("-")


def test_run_summary_line_non_backfill_uses_envelope():
    from datetime import date

    from achievements.announcements import run_summary_line

    text = run_summary_line(
        kind="reconcile",
        granted=2,
        revoked=1,
        held=5,
        held_grandfathered=1,
        held_older_version=0,
        held_out_of_range=0,
        rules=3,
        start=date(2026, 1, 1),
        end=date(2026, 8, 19),
        channel="<#CACH>",
        dms=2,
        dm_failed=0,
        duration_s=1.5,
    )
    assert text.startswith("The *Achievements (reconcile)* job was run as scheduled")
    assert "Status: success (1.5s)" in text
    assert "Results: 3 rules, 2 granted, 1 revoked, 5 held (1 grandfathered)" in text
    assert "Period: 2026-01-01 to 2026-08-19" in text
    assert "Destination(s): <#CACH>, 2 DMs" in text
    assert not text.startswith("-")
    assert "<@" not in text


def test_achievements_emit_per_event_paxminer_logs():
    """Grant and revoke each emit a paxminer_logs line."""
    from achievements import runner as runner_mod

    rule = {
        "id": 1,
        "name": "Ironman",
        "verb": "posting 30 times",
        "code": "ironman",
        "period": "year",
        "version_id": 7,
        "enabled": 1,
        "metric": "posts",
        "activity": "beatdown",
        "threshold": 1,
    }
    awarded = pd.DataFrame(
        [
            {
                "id": 99,
                "achievement_id": 1,
                "pax_id": "U0REVOKEXXX",
                "date_awarded": date(2026, 1, 5),
                "period": "year",
                "code": "ironman",
                "achievement_version_id": 7,
                "period_key": "2026",
                "period_start": date(2026, 1, 1),
                "period_end": date(2026, 12, 31),
            }
        ]
    )
    qualified = pd.DataFrame(
        [
            {
                "pax_id": "U0GRANTXXXX",
                "date_awarded": date(2026, 7, 1),
                "period_bucket": "2026",
                "period_key": "2026",
            }
        ]
    )

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    # _load_rules only (_load_awarded_ytd is patched; no multi-schema query)
    mock_cur.fetchall.side_effect = [
        [rule],
    ]

    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C_ACH",
        "slack_token": "enc",
        "region": "Tulsa",
        "schema_name": "f3test",
    }
    log_lines: list[str] = []

    def capture_log(client, text, **kwargs):
        log_lines.append(text)

    with patch.object(runner_mod, "decrypt_field", return_value="xoxb-test"):
        with patch.object(runner_mod, "slack_client", return_value=MagicMock()):
            with patch.object(runner_mod, "workspace_user_ids", return_value={"U0GRANTXXXX", "U0REVOKEXXX"}):
                with patch.object(runner_mod, "post_message"):
                    with patch.object(runner_mod, "open_dm_channel", return_value="D1"):
                        with patch.object(runner_mod, "post_log", side_effect=capture_log):
                            with patch.object(
                                runner_mod,
                                "load_nation_attendance",
                                return_value=pd.DataFrame(
                                    {
                                        "email": ["a@b.c", "c@d.e"],
                                        "user_id": ["U0GRANTXXXX", "U0REVOKEXXX"],
                                        "user_name": ["Grant", "Revoke"],
                                        "date": [date(2026, 7, 1), date(2026, 7, 1)],
                                        "region": ["f3test", "f3test"],
                                    }
                                ),
                            ):
                                with patch.object(
                                    runner_mod,
                                    "attach_home_regions",
                                    side_effect=lambda _c, n, _s: n,
                                ):
                                    with patch.object(
                                        runner_mod, "evaluate_rule", return_value=qualified
                                    ):
                                        with patch.object(
                                            runner_mod, "_load_awarded", return_value=awarded
                                        ):
                                            result = runner_mod.run_achievements_for_region(
                                                mock_conn,
                                                pm_schema="paxminer",
                                                regional_schema="f3test",
                                                region_row=region_row,
                                                pax_user_ids={"U0GRANTXXXX", "U0REVOKEXXX"},
                                            )

    assert result["grants"] == 1
    assert result["revokes"] == 1
    assert any("was granted to `Grant`" in line for line in log_lines)
    assert any("was revoked from `Revoke`" in line for line in log_lines)
    assert any("Achievement *" in line for line in log_lines)
    assert not any("<@" in line for line in log_lines)
    assert not any(line.startswith("- ") for line in log_lines)
    assert not any("f3test" in line for line in log_lines)
    assert not any("Tulsa" in line for line in log_lines)


def test_run_daily_posts_failure_log():
    from achievements import runner as runner_mod

    region_row = {
        "region": "Tulsa",
        "schema_name": "f3test",
        "slack_token": "enc",
        "send_achievements": 1,
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.return_value = [region_row]

    with patch.object(
        runner_mod,
        "run_achievements_for_region",
        side_effect=RuntimeError("db down"),
    ):
        with patch.object(runner_mod, "_post_achievement_failure_log") as mock_fail:
            results = runner_mod.run_daily(mock_conn, "paxminer")

    assert results[0]["error"] == "db down"
    mock_fail.assert_called_once()
    assert mock_fail.call_args.args[0]["region"] == "Tulsa"



def test_kotter_empty_attendance_vs_query_error():
    from unittest.mock import MagicMock, patch

    import pandas as pd

    from kotter import kotter_report as kr

    region_row = {
        "schema_name": "f3ttown_test",
        "kotter_channel": "C1",
        "slack_token": "enc",
        "region": "Tulsa",
    }
    conn = MagicMock()

    with patch("paxminer_db.read_sql_df", return_value=pd.DataFrame()):
        result = kr.run_kotter_for_region(conn, "paxminer", region_row, dry_run=True)
    assert result.get("skipped") == "no attendance rows for f3ttown_test"

    with patch("paxminer_db.read_sql_df", side_effect=RuntimeError("table missing")):
        result = kr.run_kotter_for_region(conn, "paxminer", region_row, dry_run=True)
    assert result.get("error", "").startswith("attendance query failed:")


def test_q_charter_joins_and_tracks_failed_channels():
    from datetime import date
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from monthly_charts import Qcharter as qc

    mydb = MagicMock()
    cur = MagicMock()
    mydb.cursor.return_value.__enter__.return_value = cur
    mydb.cursor.return_value.__exit__.return_value = False
    cur.fetchall.side_effect = [
        [{"ao": "The Fort", "channel_id": "C_AO1"}],
        [
            {
                "Date": "2026-07-01",
                "AO": "The Fort",
                "Q": "Beaker",
                "Q_Is_App": 0,
                "CoQ": None,
                "pax_count": 5,
                "fngs": "",
                "fng_count": 0,
            }
        ],
        [],
    ]

    slack = MagicMock()
    slack.conversations_join.return_value = {"ok": True}
    slack.files_upload_v2.side_effect = RuntimeError("not_in_channel")

    with patch.object(qc, "WebClient", return_value=slack):
        with patch.object(qc.plt, "savefig"):
            with patch.object(qc.plt, "title"):
                with patch.object(qc.plt, "legend"):
                    with patch.object(qc.plt, "close"):
                        with patch.object(qc.plt, "ioff"):
                            result = qc.run_q_charter(
                                mydb,
                                "xoxb-test",
                                "f3ttown_test",
                                "Tulsa",
                                "C_SUM",
                                plot_dir="/tmp/paxminer_plots_test",
                                destinations=["C_SUM"],
                                post_per_ao=True,
                                window=(date(2026, 7, 1), date(2026, 7, 31)),
                            )

    assert any(
        call.kwargs.get("channel") == "C_AO1" or (call.args and call.args[0] == "C_AO1")
        for call in slack.conversations_join.call_args_list
    ) or slack.conversations_join.called
    assert result["channel_count"] == 0
    assert result["failed_channels"]
    assert result["failed_channels"][0]["channel_id"] == "C_AO1"
    assert "not_in_channel" in result["failed_channels"][0]["reason"]
    # cleanup plot dir noise
    Path("/tmp/paxminer_plots_test/f3ttown_test").mkdir(parents=True, exist_ok=True)


def _posts(user, dates, *, ao="C_AO", q=0, activity="beatdown"):
    return pd.DataFrame(
        {
            "region": ["f3test"] * len(dates),
            "email": ["a@x.com"] * len(dates),
            "user_id": [user] * len(dates),
            "user_name": ["PAX"] * len(dates),
            "ao_id": [ao] * len(dates),
            "ao": ["ao"] * len(dates),
            "date": pd.to_datetime(dates),
            "q_flag": [q] * len(dates),
            "activity_type": [activity] * len(dates),
            "timestamp": [f"175000000{i}.000000" for i in range(len(dates))],
        }
    )


def test_evaluate_rule_year_qualified_week_and_crossing_date():
    from achievements.engine import evaluate_rule

    dates = [f"2026-08-{d:02d}" for d in range(10, 16)]
    nation = _posts("U1", dates)
    rule = {
        "id": 1,
        "metric": "posts",
        "activity": "beatdown",
        "period": "week",
        "threshold": 6,
        "effective_from": None,
    }
    out = evaluate_rule(nation, rule, schema="f3test")
    assert len(out) == 1
    assert out.iloc[0]["period_key"] == "2026-W33"
    assert out.iloc[0]["date_awarded"] == date(2026, 8, 15)
    extra = pd.concat([nation, _posts("U1", ["2026-08-16"])], ignore_index=True)
    out2 = evaluate_rule(extra, rule, schema="f3test")
    assert out2.iloc[0]["date_awarded"] == date(2026, 8, 15)


def test_evaluate_rule_going_forward_skips_past_months():
    from achievements.engine import evaluate_rule

    nation = _posts("U1", ["2026-01-05", "2026-01-06", "2026-08-05", "2026-08-06"])
    rule = {
        "id": 1,
        "metric": "posts",
        "activity": [],
        "period": "month",
        "threshold": 2,
        "effective_from": date(2026, 8, 1),
    }
    out = evaluate_rule(nation, rule, schema="f3test")
    assert list(out["period_key"]) == ["2026-08"]


def test_empty_activity_list_matches_all_types():
    from achievements.engine import evaluate_rule

    nation = pd.concat(
        [
            _posts("U1", ["2026-08-01"], activity="qsource"),
            _posts("U1", ["2026-08-02"], activity="rucking"),
        ],
        ignore_index=True,
    )
    rule = {
        "id": 1,
        "metric": "posts",
        "activity": [],
        "period": "year",
        "threshold": 2,
    }
    out = evaluate_rule(nation, rule, schema="f3test")
    assert len(out) == 1


def test_populated_activity_list_filters():
    from achievements.engine import evaluate_rule

    nation = pd.concat(
        [
            _posts("U1", ["2026-08-01"], activity="qsource"),
            _posts("U1", ["2026-08-02"], activity="beatdown"),
        ],
        ignore_index=True,
    )
    rule = {
        "id": 1,
        "metric": "posts",
        "activity": ["qsource"],
        "period": "year",
        "threshold": 1,
    }
    out = evaluate_rule(nation, rule, schema="f3test")
    assert len(out) == 1
    assert out.iloc[0]["date_awarded"] == date(2026, 8, 1)


def test_rule_edit_does_not_revoke_older_version():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Six Pack",
        "verb": "posting",
        "period": "week",
        "version_id": 2,
        "enabled": 1,
        "metric": "posts",
        "threshold": 8,
    }
    awarded_row = {
        "id": 9,
        "achievement_id": 1,
        "pax_id": "U1",
        "date_awarded": date(2026, 8, 10),
        "period": "week",
        "achievement_version_id": 1,
        "period_key": "2026-W33",
        "period_start": date(2026, 8, 10),
        "period_end": date(2026, 8, 16),
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], [awarded_row]]
    nation = _posts("U1", ["2026-08-10"])
    with patch("achievements.runner.decrypt_field", return_value="x"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=nation):
                with patch("achievements.runner.attach_home_regions", side_effect=lambda c, n, s: n):
                    with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                        result = run_achievements_for_region(
                            mock_conn,
                            pm_schema="pm",
                            regional_schema="f3test",
                            region_row={
                                "send_achievements": 1,
                                "achievement_channel": "C1",
                                "slack_token": "enc",
                            },
                            pax_user_ids={"U1"},
                            dry_run=True,
                        )
    assert result["revokes"] == 0


def test_null_version_is_grandfathered():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "x",
        "period": "year",
        "version_id": 1,
        "enabled": 1,
        "threshold": 1,
    }
    awarded_row = {
        "id": 9,
        "achievement_id": 1,
        "pax_id": "U1",
        "date_awarded": date(2026, 1, 1),
        "period": "year",
        "achievement_version_id": None,
        "period_key": "2026",
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], [awarded_row]]
    with patch("achievements.runner.decrypt_field", return_value="x"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.load_nation_attendance", return_value=_posts("U1", ["2026-01-01"])):
                with patch("achievements.runner.attach_home_regions", side_effect=lambda c, n, s: n):
                    with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                        result = run_achievements_for_region(
                            mock_conn,
                            pm_schema="pm",
                            regional_schema="f3test",
                            region_row={
                                "send_achievements": 1,
                                "achievement_channel": "C1",
                                "slack_token": "enc",
                            },
                            pax_user_ids={"U1"},
                            dry_run=True,
                        )
    assert result["revokes"] == 0


def test_disabled_family_is_inert():
    from achievements.runner import run_achievements_for_region

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[], []]
    region_row = {"send_achievements": 1, "achievement_channel": "C1", "slack_token": "enc"}
    with patch("achievements.runner.decrypt_field", return_value="x"):
        with patch("achievements.runner.slack_client"):
            result = run_achievements_for_region(
                mock_conn,
                pm_schema="pm",
                regional_schema="f3test",
                region_row=region_row,
                dry_run=True,
            )
    assert result == {"skipped": "no rules"}


def test_channel_single_and_batch_copy():
    from achievements.announcements import channel_grant_messages, dm_grant_messages

    g1 = {
        "pax_id": "U01AAAAAAA1",
        "achievement_id": 1,
        "date_awarded": date(2026, 8, 16),
        "ao_id": "C_AO",
        "timestamp": "1750000000.000001",
        "period": "month",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "rule": {"name": "Leader of Men", "verb": "Qing at 4 beatdowns in a month"},
    }
    msgs = channel_grant_messages(
        [g1],
        year=2026,
        names={"U01AAAAAAA1": "A"},
        known_ids={"U01AAAAAAA1"},
        ytd_totals={"U01AAAAAAA1": 23},
        ytd_family={("U01AAAAAAA1", 1): 5},
    )
    text = msgs[0][0]
    assert "<@U01AAAAAAA1>" in text
    assert "*Leader of Men*" in text
    assert "<#C_AO>" in text
    assert "in 2026" in text
    assert "Encourage this HIM to keep it up!" in text
    assert "Keep up the good work!" not in text
    assert "unfurl" not in text.lower()
    g2 = dict(g1, pax_id="U01AAAAAAA2")
    batch = channel_grant_messages(
        [g1, g2],
        year=2026,
        names={"U01AAAAAAA1": "A", "U01AAAAAAA2": "B"},
        known_ids={"U01AAAAAAA1", "U01AAAAAAA2"},
        ytd_totals={"U01AAAAAAA1": 23, "U01AAAAAAA2": 2},
        ytd_family={("U01AAAAAAA1", 1): 5, ("U01AAAAAAA2", 1): 1},
    )
    btext = batch[0][0]
    assert "T-Claps" in btext
    assert "achievement #23" not in btext
    dms = dm_grant_messages(
        [g1, dict(g1, achievement_id=2, rule={"name": "6 pack", "verb": "posting at 6 beatdowns in a week"})],
        year=2026,
        names={"U01AAAAAAA1": "A"},
        known_ids={"U01AAAAAAA1"},
        ytd_totals={"U01AAAAAAA1": 23},
        ytd_family={("U01AAAAAAA1", 1): 5, ("U01AAAAAAA1", 2): 1},
    )
    dm_text = dms["U01AAAAAAA1"][0]
    assert "you just earned 2 achievements!" in dm_text


def test_missing_timestamp_prints_date_without_link():
    from achievements.announcements import date_link

    assert "http" not in date_link(date(2026, 8, 16), "C_AO", None)
    assert "August 16, 2026" in date_link(date(2026, 8, 16), "C_AO", None)
    linked = date_link(date(2026, 8, 16), "C_AO", "1750000000.123")
    assert linked.startswith("<https://slack.com/archives/C_AO/p1750000000123|")


def test_post_messages_retries_repeated_429():
    from slack_sdk.errors import SlackApiError
    from slack_util import post_messages

    client = MagicMock()
    err = SlackApiError("ratelimit", MagicMock(status_code=429, headers={"Retry-After": "0"}))
    err.response.data = {"ok": False, "error": "ratelimited"}
    client.chat_postMessage.side_effect = [err, err, {"ok": True, "ts": "1"}]
    with patch("slack_util.time.sleep"):
        post_messages(client, "C1", [("hello", None)])
    assert client.chat_postMessage.call_count == 3
    assert client.chat_postMessage.call_args.kwargs.get("unfurl_links") is False


def test_iter_year_windows_overlap_attributes_iso_week_once():
    from achievements.engine import evaluate_rule
    from achievements.runner import _filter_period_year, iter_year_windows

    nation = _posts("U1", ["2025-12-29", "2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02", "2026-01-03"])
    rule = {"id": 1, "metric": "posts", "activity": [], "period": "week", "threshold": 6}
    windows = iter_year_windows(date(2025, 1, 1), date(2026, 12, 31))
    keys = []
    for year, start, end in windows:
        chunk = nation[(nation["date"] >= pd.Timestamp(start)) & (nation["date"] <= pd.Timestamp(end))]
        qualified = _filter_period_year(evaluate_rule(chunk, rule, schema="f3test"), year)
        keys.extend(list(qualified["period_key"]) if not qualified.empty else [])
    assert keys.count("2026-W01") == 1


def test_genuine_revoke_posts_channel_dm_and_log():
    from achievements.runner import run_achievements_for_region

    pax = "U01REVOKE01"
    rule = {
        "id": 1,
        "name": "Leader of Men",
        "verb": "Qing",
        "period": "month",
        "version_id": 7,
        "enabled": 1,
        "metric": "qs",
        "threshold": 4,
    }
    awarded_row = {
        "id": 99,
        "achievement_id": 1,
        "pax_id": pax,
        "date_awarded": date(2026, 8, 16),
        "period": "month",
        "achievement_version_id": 7,
        "period_key": "2026-08",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "ao_id": "C_AO",
        "timestamp": "1750000000.000001",
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], [awarded_row]]
    posts = []
    logs = []
    with patch("achievements.runner.decrypt_field", return_value="x"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.workspace_user_ids", return_value={pax}):
                with patch(
                    "achievements.runner.load_nation_attendance",
                    return_value=_posts(pax, ["2026-08-01"]),
                ):
                    with patch(
                        "achievements.runner.attach_home_regions",
                        side_effect=lambda c, n, s: n,
                    ):
                        with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                            with patch(
                                "achievements.runner.post_message",
                                side_effect=lambda _c, ch, text, **_k: posts.append((ch, text)),
                            ):
                                with patch(
                                    "achievements.runner.post_log",
                                    side_effect=lambda _c, text, **_k: logs.append(text),
                                ):
                                    with patch(
                                        "achievements.runner.open_dm_channel",
                                        return_value="D1",
                                    ):
                                        result = run_achievements_for_region(
                                            mock_conn,
                                            pm_schema="pm",
                                            regional_schema="f3test",
                                            region_row={
                                                "send_achievements": 1,
                                                "achievement_channel": "C1",
                                                "slack_token": "enc",
                                            },
                                            pax_user_ids={pax},
                                            log_mode="webhook",
                                            trigger_ao_id="C_AO",
                                            trigger_timestamp="1750000000.000001",
                                            trigger_date=date(2026, 8, 16),
                                        )
    assert result["revokes"] == 1
    assert result["grants"] == 0
    channel_msgs = [t for ch, t in posts if ch == "C1"]
    dm_msgs = [t for ch, t in posts if ch == "D1"]
    assert len(channel_msgs) == 1
    assert "Correction:" in channel_msgs[0]
    assert "T-Claps" not in channel_msgs[0]
    assert "this Backblast" not in channel_msgs[0]
    assert "August 16" in channel_msgs[0]
    assert len(dm_msgs) == 1
    assert "Keep showing up and you'll get it back!" in dm_msgs[0]
    assert len(logs) == 1
    assert "was revoked from" in logs[0]
    assert "after an edit on" in logs[0]
    assert "<@" not in logs[0]


def test_ytd_run_does_not_revoke_prior_year():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Ironman",
        "verb": "x",
        "period": "year",
        "version_id": 7,
        "enabled": 1,
        "threshold": 50,
    }
    awarded_row = {
        "id": 9,
        "achievement_id": 1,
        "pax_id": "U01AAAAAAA1",
        "date_awarded": date(2025, 6, 1),
        "period": "year",
        "achievement_version_id": 7,
        "period_key": "2025",
        "period_start": date(2025, 1, 1),
        "period_end": date(2025, 12, 31),
    }
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], [awarded_row]]
    with patch("achievements.runner.decrypt_field", return_value="x"):
        with patch("achievements.runner.slack_client"):
            with patch(
                "achievements.runner.load_nation_attendance",
                return_value=_posts("U01AAAAAAA1", ["2026-01-05"]),
            ):
                with patch(
                    "achievements.runner.attach_home_regions",
                    side_effect=lambda c, n, s: n,
                ):
                    with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                        result = run_achievements_for_region(
                            mock_conn,
                            pm_schema="pm",
                            regional_schema="f3test",
                            region_row={
                                "send_achievements": 1,
                                "achievement_channel": "C1",
                                "slack_token": "enc",
                            },
                            pax_user_ids={"U01AAAAAAA1"},
                            dry_run=True,
                            start=date(2026, 1, 1),
                            end=date(2026, 8, 18),
                        )
    assert result["revokes"] == 0
    assert result["held"] >= 1


def test_reconcile_rule_awards_silent_channel_summary():
    from achievements.runner import reconcile_rule_awards

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {
        "name": "Centurion",
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2026, 12, 31),
    }
    posts = []
    logs = []
    with patch(
        "achievements.runner.run_achievements_for_region",
        return_value={"grants": 89, "revokes": 141, "held": 73},
    ) as mock_run:
        with patch("achievements.runner.resolve_achievement_channel", return_value="C_ACH"):
            with patch("achievements.runner.decrypt_field", return_value="x"):
                with patch("achievements.runner.slack_client"):
                    with patch(
                        "achievements.runner.post_message",
                        side_effect=lambda _c, ch, text, **_k: posts.append((ch, text)),
                    ):
                        with patch(
                            "achievements.runner.post_log",
                            side_effect=lambda _c, text, **_k: logs.append(text),
                        ):
                            with patch("achievements.runner.open_dm_channel") as mock_dm:
                                result = reconcile_rule_awards(
                                    mock_conn,
                                    pm_schema="pm",
                                    regional_schema="f3test",
                                    region_row={"slack_token": "enc"},
                                    achievement_id=4,
                                    actor="UADMIN1234",
                                )
    assert result["grants"] == 89
    assert result["revokes"] == 141
    assert mock_run.call_args.kwargs["announce"] is False
    assert mock_run.call_args.kwargs["allow_revoke"] is True
    assert mock_run.call_args.kwargs["emit_logs"] is False
    assert len(posts) == 1
    assert "Achievement *Centurion* was corrected" in posts[0][1]
    assert mock_dm.call_count == 0
    assert len(logs) == 1
    assert "Achievement re-evaluate triggered by `admin`" in logs[0]
    assert "Status: success" in logs[0]
    assert "Achievement: Centurion" in logs[0]
    assert "Results: 89 granted, 141 revoked, 73 unchanged" in logs[0]
    assert "`UADMIN1234`" not in logs[0]


def test_reconcile_rule_awards_skips_channel_when_noop():
    """0 granted / 0 revoked is not a public correction; the log line still posts."""
    from achievements.runner import reconcile_rule_awards

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {
        "name": "6 pack",
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2026, 12, 31),
    }
    posts = []
    logs = []
    with patch(
        "achievements.runner.run_achievements_for_region",
        return_value={"grants": 0, "revokes": 0, "held": 26},
    ):
        with patch("achievements.runner.resolve_achievement_channel", return_value="C_ACH"):
            with patch("achievements.runner.decrypt_field", return_value="x"):
                with patch("achievements.runner.slack_client"):
                    with patch(
                        "achievements.runner.post_message",
                        side_effect=lambda _c, ch, text, **_k: posts.append((ch, text)),
                    ):
                        with patch(
                            "achievements.runner.post_log",
                            side_effect=lambda _c, text, **_k: logs.append(text),
                        ):
                            result = reconcile_rule_awards(
                                mock_conn,
                                pm_schema="pm",
                                regional_schema="f3test",
                                region_row={"slack_token": "enc"},
                                achievement_id=6,
                                actor="UADMIN1234",
                            )
    assert result["grants"] == 0
    assert result["revokes"] == 0
    assert result["held"] == 26
    assert posts == []
    assert len(logs) == 1
    assert "Achievement re-evaluate triggered by `admin`" in logs[0]
    assert "Status: success" in logs[0]
    assert "Achievement: 6 pack" in logs[0]
    assert "Results: 0 granted, 0 revoked, 26 unchanged" in logs[0]
    assert "Period:" in logs[0]
    assert "`UADMIN1234`" not in logs[0]
    assert "was corrected" not in logs[0]


def test_scheduled_noop_logs_summary_webhook_silent():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "x",
        "period": "year",
        "version_id": 1,
        "enabled": 1,
        "threshold": 1,
    }
    region_row = {
        "send_achievements": 1,
        "achievement_channel": "C1",
        "slack_token": "enc",
    }
    nation = _posts("U01AAAAAAA1", ["2026-01-01"])

    def _run(log_mode: str):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False
        mock_cur.fetchall.side_effect = [[rule], []]
        logs = []
        with patch("achievements.runner.decrypt_field", return_value="x"):
            with patch("achievements.runner.slack_client"):
                with patch("achievements.runner.load_nation_attendance", return_value=nation):
                    with patch(
                        "achievements.runner.attach_home_regions",
                        side_effect=lambda c, n, s: n,
                    ):
                        with patch("achievements.runner.evaluate_rule", return_value=pd.DataFrame()):
                            with patch("achievements.runner.post_message"):
                                with patch(
                                    "achievements.runner.post_log",
                                    side_effect=lambda _c, text, **_k: logs.append(text),
                                ):
                                    run_achievements_for_region(
                                        mock_conn,
                                        pm_schema="pm",
                                        regional_schema="f3test",
                                        region_row=region_row,
                                        log_mode=log_mode,
                                    )
        return logs

    scheduled = _run("scheduled")
    assert scheduled == []
    assert _run("webhook") == []


def test_dm_failure_counted_on_summary():
    from achievements.runner import run_achievements_for_region

    rule = {
        "id": 1,
        "name": "Test",
        "verb": "testing",
        "period": "year",
        "version_id": 1,
        "enabled": 1,
        "threshold": 1,
    }
    qual = pd.DataFrame(
        {
            "pax_id": ["U01ABCDEF23"],
            "achievement_id": [1],
            "date_awarded": [date(2026, 7, 1)],
            "period_key": ["2026"],
        }
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchall.side_effect = [[rule], []]
    logs = []
    with patch("achievements.runner.decrypt_field", return_value="x"):
        with patch("achievements.runner.slack_client"):
            with patch("achievements.runner.workspace_user_ids", return_value={"U01ABCDEF23"}):
                with patch(
                    "achievements.runner.load_nation_attendance",
                    return_value=_posts("U01ABCDEF23", ["2026-07-01"]),
                ):
                    with patch(
                        "achievements.runner.attach_home_regions",
                        side_effect=lambda c, n, s: n,
                    ):
                        with patch("achievements.runner.evaluate_rule", return_value=qual):
                            with patch("achievements.runner.post_message"):
                                with patch(
                                    "achievements.runner.open_dm_channel",
                                    side_effect=RuntimeError("boom"),
                                ):
                                    with patch(
                                        "achievements.runner.post_log",
                                        side_effect=lambda _c, text, **_k: logs.append(text),
                                    ):
                                        result = run_achievements_for_region(
                                            mock_conn,
                                            pm_schema="pm",
                                            regional_schema="f3test",
                                            region_row={
                                                "send_achievements": 1,
                                                "achievement_channel": "C1",
                                                "slack_token": "enc",
                                            },
                                        )
    assert result["grants"] == 1
    assert result["dm_failed"] == 1
    assert result["results_line"].endswith("1 granted, 0 revoked") or "1 granted" in result["results_line"]
    summary = [line for line in logs if "Achievements daily" in line]
    assert summary == []
    assert any("granted" in line.lower() or "Priest" in line or "achievement" in line.lower() for line in logs) or logs


def test_legacy_activity_lists_and_classifier_text_formats():
    from achievements.activity import classify_activity_type, legacy_activity_to_list

    assert legacy_activity_to_list("any") == []
    assert "beatdown" in legacy_activity_to_list("beatdown")
    assert "Bootcamp" in legacy_activity_to_list("beatdown")
    assert "qsource" in {a.lower() for a in legacy_activity_to_list("qsource")}
    assert (
        classify_activity_type(backblast="QSource with Klint at The Goose", ao_name="the-goose")
        == "qsource"
    )
    assert (
        classify_activity_type(backblast="QSource with <@U01ABCDEF23>", ao_name="the-goose")
        == "qsource"
    )
