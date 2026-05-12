from datetime import date

import polars as pl

from ..weaselbot.downrange_sync import (
    _build_report_messages,
    _chunks,
    _downrange_ao_id,
    _downrange_q_user_id,
    _source_event_id,
    _source_timestamp,
)


def test_source_event_id_contains_expected_fields():
    event_id = _source_event_id("f3other_prod", "1715205200.1234", "C123", date(2026, 5, 10))
    assert event_id == "f3other_prod:1715205200.1234:C123:2026-05-10"


def test_downrange_ids_are_stable():
    ao_id_1 = _downrange_ao_id("f3other_prod", "C123")
    ao_id_2 = _downrange_ao_id("f3other_prod", "C123")
    ao_id_3 = _downrange_ao_id("f3other_prod", "C456")
    q_id_1 = _downrange_q_user_id("event:1")
    q_id_2 = _downrange_q_user_id("event:1")

    assert ao_id_1 == ao_id_2
    assert ao_id_1 != ao_id_3
    assert q_id_1 == q_id_2
    assert ao_id_1.startswith("dr_")
    assert q_id_1.startswith("drq_")


def test_source_timestamp_is_truncated_for_long_values():
    long_ts = "x" * 120
    ts = _source_timestamp(long_ts, "f3other_prod")
    assert len(ts) <= 45
    assert ts.startswith("drts_")


def test_chunks_splits_lines():
    parts = _chunks([f"line-{n}" for n in range(6)], size=2)
    assert len(parts) == 3
    assert parts[0] == "line-0\nline-1"


def test_build_report_messages_includes_fields():
    df = pl.DataFrame(
        {
            "home_user_id": ["U1"],
            "email": ["u1@example.com"],
            "source_region": ["f3other_prod"],
            "source_ao": ["Alpha"],
            "date": [date(2026, 5, 10)],
            "q_flag": [1],
        }
    )
    messages = _build_report_messages("f3ttown_prod", df)
    assert messages
    assert "Downrange report for f3ttown_prod" in messages[0]
    assert "u1@example.com" in messages[0]
    assert "src=f3other_prod" in messages[0]
