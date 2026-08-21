"""Phase 5g messaging, log envelope, award openers, emoji picker, and reaction isolation."""

from __future__ import annotations

import random
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError


def _slack_err(error: str, status: int = 200, headers: dict | None = None) -> SlackApiError:
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.get = lambda k, d=None: error if k == "error" else d
    err = SlackApiError(error, resp)
    return err


def test_admin_channel_line_created_changed_deleted():
    from achievements.copy import admin_channel_line

    created = admin_channel_line("created", "Six Pack", "<@UADMIN1234>", granted=0)
    assert created == "Achievement *Six Pack* was created by <@UADMIN1234> (0 granted)."
    changed = admin_channel_line(
        "changed", "The Priest", "<@UADMIN1234>", granted=3, revoked=1, unchanged=26
    )
    assert changed == (
        "Achievement *The Priest* was changed by <@UADMIN1234> "
        "(3 granted, 1 revoked, 26 unchanged)."
    )
    assert (
        admin_channel_line("changed", "The Priest", "<@UADMIN1234>", granted=0, revoked=0)
        is None
    )
    deleted = admin_channel_line("deleted", "Centurion", "<@UADMIN1234>", revoked=10)
    assert deleted == "Achievement *Centurion* was deleted by <@UADMIN1234> (10 revoked)."


def test_achievement_admin_log_omits_empty_fields_and_fences_status():
    from achievements.copy import achievement_admin_log_line

    created = achievement_admin_log_line(
        name="Six Pack",
        action="created",
        author="Klint",
        code="six_pack",
        version="v1",
        rules="after: posts/week ≥ 6 · beatdown",
        period="2026-01-01 to 2026-12-31",
        granted=2,
        duration_s=1.5,
        affected_pax=2,
    )
    assert created.startswith("Achievement *Six Pack* was processed.")
    assert "```" in created
    assert "Author: Klint" in created
    assert "Action: created" in created
    assert "Status: success (1.5s)" in created
    assert "before:" not in created
    assert "Rules: after: posts/week ≥ 6 · beatdown" in created
    header, _, rest = created.partition("\n")
    assert "Status:" not in header
    assert rest.strip().startswith("```")

    changed = achievement_admin_log_line(
        name="The Priest",
        action="changed",
        author="Klint",
        rules="before: posts/year ≥ 25 · QSource · after: posts/year ≥ 25 · QSource (excluding Rucking)",
        granted=3,
        revoked=1,
        unchanged=26,
        affected_pax=4,
    )
    assert "before:" in changed
    assert "after:" in changed
    assert "Affected PAX: 4" in changed


def test_format_log_message_code_block_inserts_status_after_mode():
    from slack_util import format_log_message

    text = format_log_message(
        "The *Golden Boy YTD* report was run.",
        status="success",
        duration_s=5.1,
        fields=[("Mode", "manually by Klint")],
        message_count=12,
        destinations="#ao-the-goose, #ao-ruck-club",
        code_block=True,
    )
    assert text.startswith("The *Golden Boy YTD* report was run.")
    inner = text.split("```")[1]
    lines = [ln for ln in inner.strip().splitlines()]
    assert lines[0] == "Mode: manually by Klint"
    assert lines[1] == "Status: success (5.1s)"
    assert "Number of Messages: 12" in inner
    assert "Destination(s): #ao-the-goose, #ao-ruck-club" in inner
    assert "<#" not in text


def test_award_openers_are_distinct_and_pinnable():
    from achievements.announcements import AWARD_OPENERS, channel_grant_messages, pick_award_openers

    assert all(opener.endswith("our man") for opener in AWARD_OPENERS)
    assert "Congrats to our man" not in AWARD_OPENERS
    rng = random.Random(1)
    three = pick_award_openers(3, rng)
    assert len(three) == 3
    assert len(set(three)) == 3
    huge = pick_award_openers(len(AWARD_OPENERS) + 5, random.Random(2))
    assert len(huge) == len(AWARD_OPENERS) + 5
    g = {
        "pax_id": "U01AAAAAAA1",
        "achievement_id": 1,
        "date_awarded": date(2026, 8, 16),
        "ao_id": "C_AO",
        "timestamp": "1750000000.000001",
        "rule": {"name": "Leader of Men", "verb": "Qing at 4 beatdowns in a month"},
    }
    pinned = channel_grant_messages(
        [g],
        year=2026,
        names={"U01AAAAAAA1": "A"},
        known_ids={"U01AAAAAAA1"},
        ytd_totals={"U01AAAAAAA1": 7},
        rng=random.Random(0),
    )
    assert pinned[0][0].startswith(pick_award_openers(1, random.Random(0))[0])
    assert "who just unlocked the achievement" in pinned[0][0]
    assert "This is achievement #7 this year." in pinned[0][0]
    assert "and the" not in pinned[0][0]


def test_award_log_line_grant_and_revoke_share_suffix():
    from achievements.announcements import award_log_line, grant_log_line, revoke_log_line

    row = {
        "pax_id": "U01AAAAAAA1",
        "period": "month",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "date_awarded": date(2026, 8, 16),
        "ao_id": "C_AO",
        "timestamp": "1750000000.000001",
        "rule": {"name": "Leader of Men"},
    }
    grant = grant_log_line(row, "Nacho")
    revoke = revoke_log_line(row, "Nacho", webhook=True)
    assert grant == award_log_line(row, "Nacho", granted=True)
    assert revoke == award_log_line(row, "Nacho", granted=False)
    assert "granted to `Nacho`" in grant
    assert "revoked from `Nacho`" in revoke
    assert "after evaluating" in grant
    assert "after evaluating" in revoke
    assert "the Backblast from <" in grant
    assert "|August 16, 2026>" in grant
    bare = award_log_line({**row, "ao_id": None, "timestamp": None}, "Nacho", granted=False)
    assert "after evaluating" not in bare
    assert bare.endswith("for period August 2026.")

    scoped = award_log_line(
        row, "Nacho", granted=True, archive_base="https://f3ttown-test.slack.com"
    )
    assert "https://f3ttown-test.slack.com/archives/C_AO/p1750000000000001" in scoped


def test_emoji_list_supplies_custom_and_standard_names():
    from achievements.emoji import CURATED_AWARD_EMOJI, clear_emoji_cache, load_emoji_names

    clear_emoji_cache()
    client = MagicMock()
    client.emoji_list.return_value = {
        "emoji": {"f3_logo": "https://x/1.png", "shipit": "alias:squirrel"},
        "categories": [
            {"name": "Smileys", "emoji_names": ["grinning", "joy"]},
            {"name": "Activities", "emoji_names": ["soccer", "joy"]},
        ],
    }
    custom, standard = load_emoji_names(client, team_id="T1")
    assert custom == ["f3_logo", "shipit"]
    assert standard == ["grinning", "joy", "soccer"]
    assert client.emoji_list.call_args.kwargs["include_categories"] is True

    load_emoji_names(client, team_id="T1")
    assert client.emoji_list.call_count == 1

    clear_emoji_cache()
    broken = MagicMock()
    broken.emoji_list.side_effect = RuntimeError("missing_scope")
    custom, standard = load_emoji_names(broken, team_id="T2")
    assert custom == []
    assert standard == list(CURATED_AWARD_EMOJI)


def test_emoji_search_ranks_matches_and_respects_the_option_cap():
    from achievements.emoji import (
        MAX_EMOJI_OPTIONS,
        NONE_EMOJI_VALUE,
        search_emoji_options,
    )

    custom = ["f3_ruck", "f3_logo"]
    standard = ["fire", "firecracker", "campfire", "joy"]

    opts = search_emoji_options("fire", custom=custom, standard=standard)
    values = [o["value"] for o in opts]
    assert values[0] == NONE_EMOJI_VALUE
    assert values[1:] == ["fire", "firecracker", "campfire"]

    scoped = search_emoji_options(":f3_", custom=custom, standard=standard)
    assert [o["value"] for o in scoped][1:] == ["f3_ruck", "f3_logo"]

    empty = search_emoji_options("", custom=custom, standard=standard)
    assert [o["value"] for o in empty][1] == "fire"
    assert "f3_ruck" in [o["value"] for o in empty]

    huge = search_emoji_options(
        None, custom=[f"custom_{i}" for i in range(400)], standard=list(standard)
    )
    assert len(huge) == MAX_EMOJI_OPTIONS

    assert search_emoji_options("zzzz", custom=custom, standard=standard) == [
        {"text": {"type": "plain_text", "text": "None"}, "value": NONE_EMOJI_VALUE}
    ]


def test_emoji_options_handler_acks_within_the_options_contract():
    from slack_app import handle_emoji_options

    from achievements.emoji import clear_emoji_cache

    clear_emoji_cache()
    ack = MagicMock()
    client = MagicMock()
    client.emoji_list.return_value = {
        "emoji": {"f3_ruck": "https://x/1.png"},
        "categories": [{"name": "Smileys", "emoji_names": ["fire"]}],
    }
    handle_emoji_options(
        ack, {"team": {"id": "T1"}, "value": "f3"}, client, MagicMock()
    )
    assert [o["value"] for o in ack.call_args.kwargs["options"]][1:] == ["f3_ruck"]

    clear_emoji_cache()
    ack2 = MagicMock()
    broken = MagicMock()
    broken.emoji_list.side_effect = RuntimeError("missing_scope")
    handle_emoji_options(ack2, {"team": {"id": "T2"}, "value": "fir"}, broken, MagicMock())
    # emoji.list failed, so only the curated fallback is searchable.
    assert [o["value"] for o in ack2.call_args.kwargs["options"]][1:] == [
        "fire",
        "first_place_medal",
    ]


def test_stored_emoji_survives_when_missing_from_options():
    from config_paxminer import (
        EMOJI_OPTIONS_ACTION_ID,
        _achievement_edit_modal,
        _parse_achievement_form,
    )

    row = {
        "id": 3,
        "name": "Six Pack",
        "code": "six_pack",
        "description": "d",
        "verb": "posting",
        "metric": "posts",
        "period": "week",
        "threshold": 6,
        "enabled": 1,
        "emoji": "gone_custom",
    }
    modal = _achievement_edit_modal("T1", "f3test", row)
    emoji_block = next(b for b in modal["blocks"] if b.get("block_id") == "emoji")
    element = emoji_block["element"]
    assert element["type"] == "external_select"
    assert element["action_id"] == EMOJI_OPTIONS_ACTION_ID
    assert element["min_query_length"] == 0
    assert "options" not in element
    # An emoji deleted from the workspace still round-trips instead of clearing.
    assert element["initial_option"]["value"] == "gone_custom"
    assert emoji_block["optional"] is True

    parsed = _parse_achievement_form(
        {
            "view": {
                "state": {
                    "values": {
                        "name": {"val": {"value": "Six Pack"}},
                        "description": {"val": {"value": "d"}},
                        "verb": {"val": {"value": "posting"}},
                        "metric": {"val": {"selected_option": {"value": "posts"}}},
                        "period": {"val": {"selected_option": {"value": "week"}}},
                        "threshold": {"val": {"value": "6"}},
                        "emoji": {
                            EMOJI_OPTIONS_ACTION_ID: {
                                "selected_option": {"value": "gone_custom"}
                            }
                        },
                    }
                }
            }
        }
    )
    assert parsed["emoji"] == "gone_custom"

    blank = _achievement_edit_modal("T1", "f3test", {**row, "emoji": None})
    blank_el = next(b for b in blank["blocks"] if b.get("block_id") == "emoji")["element"]
    assert "initial_option" not in blank_el


def test_backblast_links_use_the_workspace_host():
    """Bare slack.com archive links do not deep-link in the Slack mobile app."""
    from achievements.period import backblast_archive_url
    from slack_util import clear_archive_base_cache, workspace_archive_base

    assert backblast_archive_url("C0APCSRGBB9", "1787335245.777559") == (
        "https://slack.com/archives/C0APCSRGBB9/p1787335245777559"
    )
    assert backblast_archive_url(
        "C0APCSRGBB9", "1787335245.777559", archive_base="https://f3ttown-test.slack.com/"
    ) == "https://f3ttown-test.slack.com/archives/C0APCSRGBB9/p1787335245777559"
    assert backblast_archive_url(None, "1787335245.777559") is None
    assert backblast_archive_url("C1", "not-a-ts") is None

    clear_archive_base_cache()
    client = MagicMock(token="xoxb-1")
    client.auth_test.return_value = {"url": "https://f3ttown-test.slack.com/"}
    assert workspace_archive_base(client) == "https://f3ttown-test.slack.com"
    assert workspace_archive_base(client) == "https://f3ttown-test.slack.com"
    assert client.auth_test.call_count == 1

    clear_archive_base_cache()
    broken = MagicMock(token="xoxb-2")
    broken.auth_test.side_effect = RuntimeError("nope")
    assert workspace_archive_base(broken) == "https://slack.com"
    assert workspace_archive_base(None) == "https://slack.com"


def test_award_messages_carry_the_workspace_host_and_full_date():
    from achievements.announcements import channel_grant_messages

    g = {
        "pax_id": "U01AAAAAAA1",
        "achievement_id": 1,
        "date_awarded": date(2026, 8, 21),
        "ao_id": "C0APCSRGBB9",
        "timestamp": "1787335245.777559",
        "rule": {"name": "Beast", "verb": "posting"},
    }
    msgs = channel_grant_messages(
        [g],
        year=2026,
        names={"U01AAAAAAA1": "A"},
        known_ids={"U01AAAAAAA1"},
        ytd_totals={"U01AAAAAAA1": 1},
        rng=random.Random(0),
        archive_base="https://f3ttown-test.slack.com",
    )
    assert (
        "<https://f3ttown-test.slack.com/archives/C0APCSRGBB9/p1787335245777559"
        "|August 21, 2026>" in msgs[0][0]
    )


def test_shared_rules_sql_omits_emoji_so_leaderboard_never_needs_it():
    from achievements.runner import RULES_ONE_SQL, RULES_SQL, _RULES_SQL_EMOJI

    assert "a.emoji" not in RULES_SQL
    assert "a.emoji" not in RULES_ONE_SQL
    assert "a.emoji" in _RULES_SQL_EMOJI
    for sql in (RULES_SQL, RULES_ONE_SQL, _RULES_SQL_EMOJI):
        assert "v.range_mode" in sql
        assert "superseded_at IS NULL" in sql


def test_load_rules_falls_back_when_emoji_column_is_missing():
    """A schema that predates the 5g migration still evaluates."""
    from achievements.runner import _load_rules

    happy = MagicMock()
    happy.fetchall.return_value = [{"id": 3, "name": "Beast", "emoji": "muscle"}]
    loaded = _load_rules(happy, "f3test")
    assert loaded[0]["emoji"] == "muscle"
    assert happy.execute.call_count == 1
    assert "a.emoji" in str(happy.execute.call_args.args[0])

    pre_5g = MagicMock()
    pre_5g.execute.side_effect = [
        Exception("1054 (42S22): Unknown column 'a.emoji' in 'field list'"),
        None,
    ]
    pre_5g.fetchall.return_value = [{"id": 3, "name": "Beast"}]
    loaded = _load_rules(pre_5g, "f3test")
    assert loaded[0]["emoji"] is None
    assert pre_5g.execute.call_count == 2
    assert "a.emoji" not in str(pre_5g.execute.call_args.args[0])

    other = MagicMock()
    other.execute.side_effect = Exception("connection lost")
    with pytest.raises(Exception, match="connection lost"):
        _load_rules(other, "f3test")


def test_emoji_only_edit_does_not_mint_or_queue():
    from achievements.range import RANGE_FROM_CREATED, should_auto_queue
    from achievements.versions import params_changed

    existing = {
        "metric": "posts",
        "period": "week",
        "threshold": 6,
        "activity": ["beatdown"],
    }
    values = {
        "metric": "posts",
        "period": "week",
        "threshold": 6,
        "activity_filter": {"include": ["beatdown"], "exclude": []},
        "emoji": "trophy",
    }
    assert not params_changed(existing, values)
    assert not should_auto_queue(
        is_new=False, params_changed=False, range_changed=False, mode=RANGE_FROM_CREATED
    )


def test_post_message_reaction_failures_never_repost_or_raise():
    from slack_util import post_message

    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1.0"}
    client.reactions_add.side_effect = [
        _slack_err("invalid_name"),
        None,
    ]
    post_message(client, "C1", "hi", add_reaction=True, reaction=["gone", "fire"])
    assert client.chat_postMessage.call_count == 1
    assert client.reactions_add.call_count == 2

    client2 = MagicMock()
    client2.chat_postMessage.return_value = {"ok": True, "ts": "2.0"}
    client2.reactions_add.side_effect = [
        _slack_err("invalid_name"),
        None,
    ]
    post_message(client2, "C1", "hi", add_reaction=True, reaction=["fire", "muscle"])
    names = [c.kwargs["name"] for c in client2.reactions_add.call_args_list]
    assert names == ["fire", "muscle"]

    client3 = MagicMock()
    client3.chat_postMessage.return_value = {"ok": True, "ts": "3.0"}
    client3.reactions_add.side_effect = _slack_err("ratelimited", status=429, headers={"Retry-After": "1"})
    post_message(client3, "C1", "hi", add_reaction=True, reaction="fire")
    assert client3.chat_postMessage.call_count == 1

    client4 = MagicMock()
    client4.chat_postMessage.return_value = {"ok": True, "ts": "4.0"}
    client4.reactions_add.side_effect = _slack_err("already_reacted")
    post_message(client4, "C1", "hi", add_reaction=True)
    assert client4.chat_postMessage.call_count == 1


def test_reconcile_created_posts_zero_grant_line_changed_does_not():
    from datetime import date

    from achievements.runner import reconcile_rule_awards

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cur.fetchone.return_value = {
        "name": "Six Pack",
        "code": "six_pack",
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2026, 12, 31),
    }
    mock_cur.fetchall.return_value = []
    posts = []
    logs = []
    with patch(
        "achievements.runner.run_achievements_for_region",
        return_value={"grants": 0, "revokes": 0, "held": 0},
    ):
        with patch("achievements.runner.resolve_achievement_channel", return_value="C_ACH"):
            with patch("achievements.runner.decrypt_field", return_value="x"):
                with patch("achievements.runner.slack_client"):
                    with patch(
                        "achievements.runner.post_message",
                        side_effect=lambda _c, ch, text, **_k: posts.append(text),
                    ):
                        with patch(
                            "achievements.runner.post_log",
                            side_effect=lambda _c, text, **_k: logs.append(text),
                        ):
                            with patch("achievements.runner.open_dm_channel") as mock_dm:
                                reconcile_rule_awards(
                                    mock_conn,
                                    pm_schema="pm",
                                    regional_schema="f3test",
                                    region_row={"slack_token": "enc"},
                                    achievement_id=6,
                                    actor="UADMIN1234",
                                    action="created",
                                )
                                mock_dm.assert_not_called()
    assert len(posts) == 1
    assert "was created by" in posts[0]
    assert "(0 granted)" in posts[0]
    assert "T-Claps" not in posts[0]
    assert logs and "Action: created" in logs[0]


def test_reconcile_never_opens_dms_or_tclaps_when_granting():
    from datetime import date

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
    mock_cur.fetchall.return_value = []
    posts = []
    with patch(
        "achievements.runner.run_achievements_for_region",
        return_value={"grants": 4, "revokes": 1, "held": 2, "grant_pax_ids": ["U1"], "revoke_pax_ids": ["U2"]},
    ) as mock_run:
        with patch("achievements.runner.resolve_achievement_channel", return_value="C_ACH"):
            with patch("achievements.runner.decrypt_field", return_value="x"):
                with patch("achievements.runner.slack_client"):
                    with patch(
                        "achievements.runner.post_message",
                        side_effect=lambda _c, ch, text, **_k: posts.append(text),
                    ):
                        with patch("achievements.runner.post_log"):
                            with patch("achievements.runner.open_dm_channel") as mock_dm:
                                reconcile_rule_awards(
                                    mock_conn,
                                    pm_schema="pm",
                                    regional_schema="f3test",
                                    region_row={"slack_token": "enc"},
                                    achievement_id=4,
                                    actor="UADMIN1234",
                                    action="changed",
                                )
    assert mock_run.call_args.kwargs["announce"] is False
    assert mock_run.call_args.kwargs["emit_logs"] is False
    mock_dm.assert_not_called()
    assert all("who just unlocked" not in t for t in posts)
    assert all("T-Claps" not in t for t in posts)
    assert any("was changed by" in t for t in posts)


def test_schedule_skipped_run_posts_nothing():
    from schedule_runner import _post_schedule_outcome_log, format_schedule_log_line

    assert (
        format_schedule_log_line(
            "f3test",
            {"definition_name": "Kotter", "ok": True, "skipped": "already ran today"},
        )
        is None
    )
    log_lines: list[str] = []
    with patch("schedule_runner.decrypt_field", return_value="xoxb-test"):
        with patch("schedule_runner.slack_client", return_value=MagicMock()):
            with patch(
                "schedule_runner.post_log",
                side_effect=lambda _c, text, **_k: log_lines.append(text),
            ):
                _post_schedule_outcome_log(
                    {"schema_name": "f3test", "slack_token": "enc"},
                    {"definition_name": "Kotter", "ok": True, "skipped": "already ran today"},
                )
    assert log_lines == []


def test_list_rows_fold_subline_for_all_three_lists():
    from config_paxminer import _achievements_list_modal
    from config_schedule import _reports_list_modal, _schedules_list_modal

    ach = _achievements_list_modal(
        "T1",
        "f3test",
        [{"id": 1, "name": "Six Pack", "code": "six_pack", "enabled": 1, "period": "week", "metric": "posts", "threshold": 6, "emoji": "trophy"}],
    )
    overflow = next(b for b in ach["blocks"] if (b.get("accessory") or {}).get("type") == "overflow")
    assert "*Six Pack*" in overflow["text"]["text"]
    assert "_Enabled | Week - 6 posts | :trophy:_" in overflow["text"]["text"]
    idx = ach["blocks"].index(overflow)
    assert ach["blocks"][idx + 1].get("type") != "context"

    reports = _reports_list_modal(
        "T1",
        "f3test",
        [{"id": 2, "name": "Kotter", "code": "kotter", "enabled": 1, "is_builtin": 1, "report_type": "kotter"}],
    )
    ro = next(b for b in reports["blocks"] if (b.get("accessory") or {}).get("type") == "overflow")
    assert "*Kotter*" in ro["text"]["text"]
    assert "_" in ro["text"]["text"]
    ridx = reports["blocks"].index(ro)
    assert reports["blocks"][ridx + 1].get("type") != "context"

    schedules = _schedules_list_modal(
        "T1",
        "f3test",
        [
            {
                "id": 9,
                "definition_name": "Kotter",
                "enabled": 1,
                "destination_type": "specific_channels",
                "frequency_type": "weekly",
                "time_of_day": "07:00:00",
            }
        ],
    )
    so = next(b for b in schedules["blocks"] if (b.get("accessory") or {}).get("type") == "overflow")
    assert "*Kotter*" in so["text"]["text"]
    sidx = schedules["blocks"].index(so)
    assert schedules["blocks"][sidx + 1].get("type") != "context"


def test_channel_grant_messages_include_custom_reaction():
    from achievements.announcements import channel_grant_messages

    g = {
        "pax_id": "U01AAAAAAA1",
        "achievement_id": 1,
        "date_awarded": date(2026, 8, 16),
        "ao_id": "C_AO",
        "timestamp": "1750000000.000001",
        "rule": {"name": "Beast", "verb": "posting", "emoji": "muscle"},
    }
    msgs = channel_grant_messages(
        [g],
        year=2026,
        names={"U01AAAAAAA1": "A"},
        known_ids={"U01AAAAAAA1"},
        ytd_totals={"U01AAAAAAA1": 1},
        rng=random.Random(0),
    )
    assert msgs[0][2] == ["fire", "muscle"]
