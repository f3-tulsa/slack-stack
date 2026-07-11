import os

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
    from datetime import date

    from achievements.engine import period_bucket_for_date

    assert period_bucket_for_date(date(2026, 3, 15), "month") == 3
    assert period_bucket_for_date(date(2026, 3, 15), "year") == 2026


def test_verify_sweep_secret():
    from slack_http import verify_sweep_secret

    os.environ["PAXMINER_SWEEP_SECRET"] = "sweep-secret-value"
    assert verify_sweep_secret({"X-Paxminer-Sweep-Secret": "sweep-secret-value"})
    assert not verify_sweep_secret({"X-Paxminer-Sweep-Secret": "wrong"})


def test_build_kotter_message_monthly_copy():
    import pandas as pd

    from kotter.kotter_report import build_kotter_message

    msg = build_kotter_message(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert "monthly" in msg.lower()
    assert "weekly" not in msg.lower()
