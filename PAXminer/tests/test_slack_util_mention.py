"""Unit tests for slack_util mention helpers."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

from slack_util import (
    format_log_message,
    is_slack_user_id,
    mention,
    resolve_display_name,
    strip_leading_log_dashes,
    ticked_display_name,
    workspace_user_ids,
)


def test_is_slack_user_id_accepts_valid():
    assert is_slack_user_id("U0ANGDEBSKE")
    assert is_slack_user_id("W01234567")
    assert is_slack_user_id("U01ABCDEF23")


def test_is_slack_user_id_rejects_junk():
    assert not is_slack_user_id(None)
    assert not is_slack_user_id("")
    assert not is_slack_user_id("nan")
    assert not is_slack_user_id("none")
    assert not is_slack_user_id(math.nan)
    # Synthetic seed IDs are Slack-shaped; roster / known_ids rejects them at mention time.
    assert is_slack_user_id("USEEDPAX12XXXX")
    assert not is_slack_user_id("SEEDPAX01")
    assert not is_slack_user_id("C01234567")  # channel
    assert not is_slack_user_id(12345)


def test_mention_real_id():
    assert mention("U0ANGDEBSKE", "Ben Baldwin") == "<@U0ANGDEBSKE>"


def test_mention_fallback_name_when_not_in_roster():
    # Prefer display name in backticks; never tick a Slack user ID.
    assert (
        mention("USEEDPAX12XXXX", "[SEED] PAX 12", known_ids={"U0ANGDEBSKE"})
        == "`[SEED] PAX 12`"
    )


def test_mention_fallback_unknown_when_id_only():
    assert mention("USEEDPAX12XXXX", known_ids=set()) == "`unknown PAX`"


def test_mention_fallback_name_when_no_id():
    assert mention(None, "Ben Baldwin") == "`Ben Baldwin`"


def test_mention_unknown():
    assert mention(None) == "`unknown PAX`"
    assert mention(math.nan) == "`unknown PAX`"
    assert mention("nan", "none") == "`unknown PAX`"


def test_mention_without_roster_still_tags_shaped_ids():
    # known_ids=None → format-only validation (Slack hiccup path)
    assert mention("U0ANGDEBSKE", "Ben") == "<@U0ANGDEBSKE>"


def test_workspace_user_ids_paginates():
    client = MagicMock()
    client.users_list.side_effect = [
        {
            "members": [{"id": "U0ANGDEBSKE"}, {"id": "USLACKBOT"}],
            "response_metadata": {"next_cursor": "page2"},
        },
        {
            "members": [{"id": "U0ANAPT3F2S"}],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    ids = workspace_user_ids(client)
    assert ids == {"U0ANGDEBSKE", "USLACKBOT", "U0ANAPT3F2S"}
    assert client.users_list.call_count == 2


def test_workspace_user_ids_returns_none_on_failure():
    client = MagicMock()
    client.users_list.side_effect = RuntimeError("boom")
    assert workspace_user_ids(client) is None


def test_workspace_user_ids_terminates_on_mock_client():
    """Bare MagicMock returns a truthy non-str next_cursor; must not loop forever."""
    client = MagicMock()
    assert workspace_user_ids(client) == set()
    assert client.users_list.call_count == 1


def test_workspace_user_ids_stops_on_repeated_cursor():
    client = MagicMock()
    page = {
        "members": [{"id": "U0ANGDEBSKE"}],
        "response_metadata": {"next_cursor": "stuck"},
    }
    client.users_list.return_value = page
    ids = workspace_user_ids(client)
    assert ids == {"U0ANGDEBSKE"}
    # First page + second page sees the same cursor and stops.
    assert client.users_list.call_count == 2


def test_ticked_display_name_never_ticks_slack_id():
    assert ticked_display_name("Klint") == "`Klint`"
    assert ticked_display_name("UADMIN1234") == "`admin`"
    assert ticked_display_name("UADMIN1234", fallback="PAX") == "`PAX`"
    assert ticked_display_name("") == "`admin`"


def test_resolve_display_name_uses_profile_and_skips_ids():
    client = MagicMock()
    client.users_info.return_value = {
        "user": {"profile": {"display_name": "Klint"}, "real_name": "Klint Van Tassel"}
    }
    assert resolve_display_name(client, "UADMIN1234") == "Klint"
    client.users_info.side_effect = RuntimeError("nope")
    assert resolve_display_name(client, "UADMIN1234") == "admin"
    assert resolve_display_name(None, "Klint") == "Klint"
    assert resolve_display_name(None, "UADMIN1234") == "admin"


def test_log_header_is_one_grammar_for_jobs_and_reports():
    from slack_util import log_header

    assert log_header("Achievements") == "The *Achievements* job was run."
    assert log_header("Kotter", noun="report") == "The *Kotter* report was run."


def test_format_log_message_matches_schedule_envelope():
    text = format_log_message(
        "The *Award Achievements* report was run.",
        status="success",
        duration_s=11.0,
        fields=[
            ("Results", "13 rules, 0 granted, 0 revoked, 0 held"),
            ("Period", "2026-01-01 to 2026-08-19"),
        ],
        message_count=0,
        destinations="none",
    )
    assert text.startswith("The *Award Achievements* report was run.")
    assert "Status: success (11.0s)" in text
    assert "Results: 13 rules, 0 granted, 0 revoked, 0 held" in text
    assert "Period: 2026-01-01 to 2026-08-19" in text
    assert "Number of Messages: 0" in text
    assert "Destination(s): none" in text


def test_format_log_message_reeval_omits_destination_footer():
    text = format_log_message(
        "Achievement re-evaluate triggered by `Klint`",
        status="success",
        fields=[
            ("Achievement", "6 pack"),
            ("Results", "0 granted, 0 revoked, 27 unchanged"),
            ("Period", "2022-08-01 to 2026-08-19"),
        ],
    )
    assert "Achievement re-evaluate triggered by `Klint`" in text
    assert "Status: success" in text
    assert "Achievement: 6 pack" in text
    assert "Results: 0 granted, 0 revoked, 27 unchanged" in text
    assert "Period: 2022-08-01 to 2026-08-19" in text
    assert "Number of Messages" not in text
    assert "Destination(s)" not in text
    assert "U0AMXC36W4X" not in text


def test_strip_leading_log_dashes():
    raw = " - Backblast imported\n- Channel missing\nplain line"
    cleaned = strip_leading_log_dashes(raw)
    assert not any(line.startswith("-") or line.startswith(" -") for line in cleaned.splitlines())
    assert "Backblast imported" in cleaned
    assert "Channel missing" in cleaned
    assert "plain line" in cleaned


def test_format_log_message_body_and_failed_status():
    """The miner opts out of the fence because its body carries <#channel> tags."""
    text = format_log_message(
        "The *PAXminer hourly* job was run.",
        status="success",
        duration_s=1.5,
        body=" - Backblast imported for AO: <#C1>",
        code_block=False,
    )
    assert text.startswith("The *PAXminer hourly* job was run.")
    assert "Status: success (1.5s)" in text
    assert "Backblast imported for AO: <#C1>" in text
    assert " - Backblast" not in text
    assert "```" not in text

    failed = format_log_message(
        "The *Achievements* job was run.",
        status="failed",
        detail="boom",
    )
    assert failed.startswith("The *Achievements* job was run.")
    assert "Status: failed" in failed
    assert "Error: boom" in failed
    assert failed.splitlines()[-1] == "```"


def test_format_log_message_fences_by_default():
    """Every job outcome in paxminer_logs shares one envelope."""
    text = format_log_message("The *User sync* job was run.", status="success")
    header, _, rest = text.partition("\n")
    assert "Status:" not in header
    assert rest.startswith("```")
    assert rest.rstrip().endswith("```")
    assert "Status: success" in rest
