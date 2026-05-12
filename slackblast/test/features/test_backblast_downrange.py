import os
import sys

# Match Lambda layout (CodeUri = slackblast/slackblast)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features.backblast import (
    _downrange_ao_id,
    _downrange_q_user_id,
    _downrange_source_event_id,
    _safe_source_timestamp,
)


def test_downrange_id_helpers_are_stable():
    ao_1 = _downrange_ao_id("f3other_prod", "C111")
    ao_2 = _downrange_ao_id("f3other_prod", "C111")
    q_1 = _downrange_q_user_id("event-id-123")
    q_2 = _downrange_q_user_id("event-id-123")

    assert ao_1 == ao_2
    assert q_1 == q_2
    assert ao_1.startswith("dr_")
    assert q_1.startswith("drq_")


def test_source_event_id_contains_key_parts():
    event_id = _downrange_source_event_id("f3other_prod", "123.45", "C111", "2026-05-10")
    assert event_id == "f3other_prod:123.45:C111:2026-05-10"


def test_safe_source_timestamp_caps_length():
    ts = _safe_source_timestamp("f3other_prod", "x" * 120)
    assert len(ts) <= 45
