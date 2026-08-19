"""Unit tests for the miner's backblast re-import decision logic.

`PAX_BD_Miner` runs these on every scraped message to decide whether a backblast
is new, an edit, or already recorded. Replaces the live-DB `test_BD_Comparer.py`,
which connected in its class body and so could never run in CI.
"""

from __future__ import annotations

from backblast_scraping.BD_Update_Utils import (
    TS_EDITED_NULL_VAL,
    DbAction,
    determine_db_action,
    find_match,
)

AO_ID = "C04807X50N4"
TS = 1704218963.541859
TS_EDITED = 1704235588.000000
TS_EDITED_OLDER = 1677222222.000000


def _msg(**overrides):
    record = {"timestamp": TS, "ts_edited": TS_EDITED, "ao_id": AO_ID}
    record.update(overrides)
    return record


def test_insert_when_no_historical_record():
    assert determine_db_action(_msg(), None) is DbAction.INSERT


def test_update_when_new_edit_is_newer():
    historical = _msg(ts_edited=TS_EDITED_OLDER)
    assert determine_db_action(_msg(), historical) is DbAction.UPDATE


def test_ignore_when_edit_timestamps_match():
    assert determine_db_action(_msg(), _msg()) is DbAction.IGNORE


def test_ignore_when_new_record_has_no_ts_edited_key():
    new = {"timestamp": TS, "ao_id": AO_ID}
    assert determine_db_action(new, _msg()) is DbAction.IGNORE


def test_ignore_when_new_record_was_never_edited():
    """Slack sends no edited.ts; the miner substitutes the "NA" sentinel."""
    new = _msg(ts_edited=TS_EDITED_NULL_VAL)
    assert determine_db_action(new, _msg()) is DbAction.IGNORE


def test_update_when_historical_was_never_edited_but_new_one_was():
    """First edit of an already-imported backblast: historical side holds "NA"."""
    historical = _msg(ts_edited=TS_EDITED_NULL_VAL)
    assert determine_db_action(_msg(), historical) is DbAction.UPDATE


def test_find_match_returns_the_row_with_the_same_timestamp():
    previous = [
        {"timestamp": 1600000000.0, "ao_id": "COTHER", "ts_edited": TS_EDITED_NULL_VAL},
        {"timestamp": TS, "ao_id": AO_ID, "ts_edited": TS_EDITED},
    ]
    match = find_match(_msg(), previous)
    assert match is not None
    assert match["ao_id"] == AO_ID


def test_find_match_returns_none_when_absent():
    previous = [{"timestamp": 1600000000.0, "ao_id": "COTHER", "ts_edited": "NA"}]
    assert find_match(_msg(), previous) is None
    assert find_match(_msg(), []) is None


def test_find_match_compares_timestamps_as_strings():
    """DB rows come back as strings; Slack gives floats. Both must still match."""
    previous = [{"timestamp": str(TS), "ao_id": AO_ID, "ts_edited": TS_EDITED}]
    assert find_match(_msg(), previous) is not None


def test_find_match_does_not_pair_a_different_ao_on_a_different_timestamp():
    previous = [{"timestamp": TS + 1, "ao_id": AO_ID, "ts_edited": TS_EDITED}]
    assert find_match(_msg(), previous) is None
