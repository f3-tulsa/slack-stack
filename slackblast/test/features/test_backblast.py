import json
import os
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are features.* and utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import backblast
from utilities.database.orm import Attendance, Backblast, PaxminerUser
from utilities.slack import actions


def _base_backblast_data():
    return {
        actions.BACKBLAST_TITLE: "DRQ beatdown",
        actions.BACKBLAST_DATE: "2026-05-18",
        actions.BACKBLAST_AO: "C_DOWNRANGE",
        actions.BACKBLAST_Q: "U_DRQ",
        actions.BACKBLAST_COQ: [],
        actions.BACKBLAST_PAX: ["U_PAX1"],
        actions.BACKBLAST_NONSLACK_PAX: None,
        actions.BACKBLAST_FNGS: None,
        actions.BACKBLAST_COUNT: None,
        actions.BACKBLAST_MOLESKIN: {"type": "section", "text": {"type": "mrkdwn", "text": "moleskin"}},
        actions.BACKBLAST_DESTINATION: "The_AO",
        actions.BACKBLAST_EMAIL_SEND: "no",
        actions.BACKBLAST_FILE: [],
        actions.BACKBLAST_FILE_IDS: [],
        actions.BACKBLAST_FILE_SLACK_URLS: [],
    }


class RecordingSession:
    """Minimal session stand-in that records delete/add/commit/rollback/close."""

    def __init__(self, fail_on_add=None):
        self.ops = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.expire_on_commit = True
        self.fail_on_add = fail_on_add

    def query(self, model):
        session = self

        class _Query:
            def filter(self, *args, **kwargs):
                return self

            def delete(self, synchronize_session=False):
                session.ops.append(("delete", model.__tablename__))
                return 1

        return _Query()

    def add(self, record):
        self.ops.append(("add", record.__tablename__, record))
        if self.fail_on_add:
            raise self.fail_on_add

    def add_all(self, records):
        self.ops.append(("add_all", [r.__tablename__ for r in records], records))

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _transaction_for(session):
    @contextmanager
    def _txn(schema=None):
        session.expire_on_commit = False
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _txn


def _region(*, schema="f3testregion", post_achievements=0):
    region_record = MagicMock()
    region_record.paxminer_schema = schema
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False
    region_record.post_achievements_to_ao = post_achievements
    region_record.team_id = "T_TEST"
    return region_record


def _names_side_effect(*_args, **_kwargs):
    if _kwargs.get("return_urls"):
        return ["DRQ"], ["https://avatar"]
    return ["PAX One"]


def _create_body():
    return {
        "view": {"id": "V_CREATE", "callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_SUBMITTER"},
        "team": {"id": "T_TEST"},
    }


def _edit_body(*, message_ts="111.222"):
    meta = {"channel_id": "C_DOWNRANGE"}
    if message_ts is not None:
        meta["message_ts"] = message_ts
    return {
        "view": {
            "id": "V_EDIT",
            "callback_id": actions.BACKBLAST_EDIT_CALLBACK_ID,
            "private_metadata": json.dumps(meta),
        },
        "user": {"id": "U_SUBMITTER"},
        "team": {"id": "T_TEST"},
    }


def _user_dms(client):
    return [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "U_SUBMITTER"]


def _find_records(user_records=None, prior_attendance=None, prior_backblast=None):
    def _inner(cls, filters=None, schema=None):
        if cls is Attendance:
            return prior_attendance or []
        if cls is Backblast:
            return prior_backblast or []
        return user_records or []

    return _inner


def _run_handle(*, body, session, region=None, client=None, find_records=None, extra_patches=None):
    region = region or _region()
    client = client or MagicMock()
    if body["view"]["callback_id"] == actions.BACKBLAST_CALLBACK_ID:
        client.chat_postMessage.return_value = {"ts": "123.456", "message": {"edited": {"ts": "123.457"}}}
    else:
        client.chat_update.return_value = {"ts": "111.222", "message": {"edited": {"ts": "111.223"}}}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()
    patches = [
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_names_side_effect),
        patch("features.backblast.DbManager.find_records", side_effect=find_records or _find_records()),
        patch("features.backblast.DbManager.transaction", _transaction_for(session)),
        patch("features.backblast.ensure_users_in_db", return_value=None),
        patch("features.backblast.get_channel_id", return_value=None),
        patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names"),
        patch("features.backblast.parse_rich_block", return_value="moleskin text"),
        patch("features.backblast.get_channel_name", return_value="downrange"),
        patch("features.backblast.claim_interaction", return_value=True),
        patch("features.backblast.check_for_duplicate", return_value=False),
    ]
    if extra_patches:
        patches.extend(extra_patches)
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=region,
        )
    return client, region


@patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names")
@patch("features.backblast.parse_rich_block", return_value="moleskin text")
@patch("features.backblast.get_channel_name", return_value="downrange")
def test_handle_backblast_post_uses_empty_icon_url_when_q_url_missing(
    _mock_channel_name,
    _mock_parse,
    _mock_replace,
):
    """Missing Q profile URL should not crash backblast submission."""

    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    def _get_user_names(_users, _logger, _client, return_urls=False, user_records=None):
        if return_urls:
            return ["DRQ"], []
        return ["PAX One"]

    body = {
        "view": {"id": "V_ICON", "callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_OP"},
        "team": {"id": "T_TEST"},
    }
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456"}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}

    region_record = MagicMock()
    region_record.paxminer_schema = None
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False
    region_record.team_id = "T_TEST"

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_get_user_names),
        patch("features.backblast.claim_interaction", return_value=True),
        patch("features.backblast.check_for_duplicate", return_value=False),
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_OP"},
            region_record=region_record,
        )

    assert client.chat_postMessage.called
    assert client.chat_postMessage.call_args.kwargs["icon_url"] == ""


def test_handle_backblast_post_app_q_uses_submitter_for_db_q_user_id():
    """App/bot Q identities (e.g. DRQ) should not collide on AO/date PK constraints."""
    q_user_record = MagicMock(spec=PaxminerUser)
    q_user_record.user_id = "U_DRQ"
    q_user_record.app = 1
    session = RecordingSession()
    _run_handle(
        body=_create_body(),
        session=session,
        find_records=_find_records(user_records=[q_user_record]),
    )
    added = [op for op in session.ops if op[0] == "add"]
    assert len(added) == 1
    assert added[0][2].q_user_id == "U_SUBMITTER"
    assert session.committed
    assert not session.rolled_back


def test_handle_backblast_post_triggers_achievement_webhook_when_coupled():
    session = RecordingSession()
    mock_webhook = MagicMock()
    region = _region(post_achievements=1)
    _run_handle(
        body=_create_body(),
        session=session,
        region=region,
        extra_patches=[patch("features.backblast.trigger_achievement_webhook", mock_webhook)],
    )
    mock_webhook.assert_called_once()
    kwargs = mock_webhook.call_args.kwargs
    assert kwargs["region_record"] is region
    assert "U_DRQ" in kwargs["pax_user_ids"]
    assert "U_PAX1" in kwargs["pax_user_ids"]
    assert kwargs["post_to_ao"] is True
    assert kwargs["ao_channel_id"] == "C_DOWNRANGE"


@patch("features.backblast.trigger_achievement_webhook")
@patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names")
@patch("features.backblast.parse_rich_block", return_value="moleskin text")
@patch("features.backblast.get_channel_name", return_value="downrange")
def test_handle_backblast_post_skips_webhook_when_uncoupled(
    _mock_channel_name,
    _mock_parse,
    _mock_replace,
    mock_webhook,
):
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    body = {
        "view": {"callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_OP"},
    }
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456"}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/backblast"}

    region_record = MagicMock()
    region_record.paxminer_schema = None
    region_record.strava_enabled = False
    region_record.workspace_name = "test-workspace"
    region_record.email_enabled = 0
    region_record.postie_format = False

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch(
            "features.backblast.get_user_names",
            side_effect=lambda *_args, **_kwargs: (["DRQ"], []) if _kwargs.get("return_urls") else ["PAX One"],
        ),
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_OP"},
            region_record=region_record,
        )

    mock_webhook.assert_not_called()


def test_edit_insert_failure_rolls_back_prior_rows():
    dup = IntegrityError("INSERT", {}, Exception("Duplicate entry"))
    session = RecordingSession(fail_on_add=dup)
    mock_webhook = MagicMock()
    prior = MagicMock()
    prior.user_id = "U_REMOVED"
    client, _ = _run_handle(
        body=_edit_body(),
        session=session,
        find_records=_find_records(prior_attendance=[prior]),
        extra_patches=[patch("features.backblast.trigger_achievement_webhook", mock_webhook)],
    )
    assert [("delete", "beatdowns"), ("delete", "bd_attendance"), ("add", "beatdowns")] == [
        (op[0], op[1]) for op in session.ops
    ]
    assert session.rolled_back
    assert not session.committed
    assert session.closed
    mock_webhook.assert_not_called()
    dms = _user_dms(client)
    assert len(dms) == 1
    text = dms[0].kwargs["text"]
    assert "Your edit was not applied" in text
    assert "previous backblast is still in the database" in text
    assert "was updated but the database still has the previous version" in text


def test_successful_edit_deletes_and_inserts_on_one_session_one_commit():
    session = RecordingSession()
    mock_webhook = MagicMock()
    prior = MagicMock()
    prior.user_id = "U_REMOVED"
    _run_handle(
        body=_edit_body(),
        session=session,
        find_records=_find_records(prior_attendance=[prior]),
        extra_patches=[patch("features.backblast.trigger_achievement_webhook", mock_webhook)],
    )
    assert [op[0] for op in session.ops] == ["delete", "delete", "add", "add_all"]
    assert [op[1] for op in session.ops[:3]] == ["beatdowns", "bd_attendance", "beatdowns"]
    assert session.ops[3][1] == ["bd_attendance", "bd_attendance"]
    assert session.committed
    assert not session.rolled_back
    assert session.closed
    mock_webhook.assert_called_once()
    pax_ids = mock_webhook.call_args.kwargs["pax_user_ids"]
    assert "U_REMOVED" in pax_ids
    assert "U_DRQ" in pax_ids
    assert "U_PAX1" in pax_ids


def test_create_does_not_delete_and_commits_once():
    session = RecordingSession()
    _run_handle(body=_create_body(), session=session)
    assert [op[0] for op in session.ops] == ["add", "add_all"]
    assert session.committed
    assert not session.rolled_back


def test_webhook_not_called_when_transaction_raises():
    session = RecordingSession(fail_on_add=RuntimeError("db down"))
    mock_webhook = MagicMock()
    _run_handle(
        body=_create_body(),
        session=session,
        extra_patches=[patch("features.backblast.trigger_achievement_webhook", mock_webhook)],
    )
    mock_webhook.assert_not_called()
    assert session.rolled_back
    assert not session.committed


def test_create_failure_dm_keeps_not_saved_wording():
    dup = IntegrityError("INSERT", {}, Exception("Duplicate entry"))
    session = RecordingSession(fail_on_add=dup)
    client, _ = _run_handle(body=_create_body(), session=session)
    dms = _user_dms(client)
    assert len(dms) == 1
    text = dms[0].kwargs["text"]
    assert "was not saved to the database" in text
    assert "Your edit was not applied" not in text
    assert "previous backblast is still in the database" not in text


def test_edit_without_message_ts_does_not_raise_nameerror():
    session = RecordingSession()
    mock_webhook = MagicMock()
    _run_handle(
        body=_edit_body(message_ts=None),
        session=session,
        extra_patches=[patch("features.backblast.trigger_achievement_webhook", mock_webhook)],
    )
    assert [op[0] for op in session.ops] == ["add", "add_all"]
    assert session.committed
    mock_webhook.assert_called_once()
    pax_ids = mock_webhook.call_args.kwargs["pax_user_ids"]
    assert "U_DRQ" in pax_ids
    assert "U_PAX1" in pax_ids


def test_persist_backblast_replace_then_insert_order():
    session = RecordingSession()
    bb = MagicMock()
    bb.__tablename__ = "beatdowns"
    att = MagicMock()
    att.__tablename__ = "bd_attendance"
    backblast.persist_backblast(
        session,
        backblast=bb,
        attendance_records=[att],
        replace_timestamp="111.222",
    )
    assert [op[0] for op in session.ops] == ["delete", "delete", "add", "add_all"]
    assert session.ops[0][1] == "beatdowns"
    assert session.ops[1][1] == "bd_attendance"


def test_backblast_create_logs_import_without_unfurl():
    session = RecordingSession()
    client, _ = _run_handle(
        body=_create_body(),
        session=session,
        extra_patches=[
            patch("features.backblast.resolve_paxminer_log_channel", return_value="CLOG"),
        ],
    )
    log_calls = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "CLOG"]
    assert len(log_calls) == 1
    kwargs = log_calls[0].kwargs
    assert kwargs["unfurl_links"] is False
    assert kwargs["unfurl_media"] is False
    assert "Backblast successfully imported for <#C_DOWNRANGE> on" in kwargs["text"]
    assert "```" not in kwargs["text"]


def test_backblast_edit_logs_summary_and_skips_unchanged_block():
    prior = MagicMock()
    prior.q_user_id = "U_OLDQ"
    prior.coq_user_id = None
    prior.pax_count = 2
    prior.fng_count = 0
    prior.bd_date = "2026-05-18"
    prior.ao_id = "C_DOWNRANGE"
    prior.backblast = "old body"
    prior_att = [MagicMock(user_id="U_OLDQ"), MagicMock(user_id="U_PAX1")]

    def _names(users, *_a, return_urls=False, **_k):
        if return_urls:
            return ["DRQ"], [""]
        mapping = {"U_OLDQ": "Old Q", "U_DRQ": "DRQ", "U_PAX1": "PAX One"}
        return [mapping.get(u, u) for u in users]

    session = RecordingSession()
    client, _ = _run_handle(
        body=_edit_body(),
        session=session,
        find_records=_find_records(prior_attendance=prior_att, prior_backblast=[prior]),
        extra_patches=[
            patch("features.backblast.resolve_paxminer_log_channel", return_value="CLOG"),
            patch("features.backblast.get_user_names", side_effect=_names),
        ],
    )
    log_calls = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "CLOG"]
    assert log_calls
    text = log_calls[0].kwargs["text"]
    assert "Backblast successfully edited for <#C_DOWNRANGE> on" in text
    assert log_calls[0].kwargs["unfurl_links"] is False
    assert "```" in text
    assert "Q: Old Q → DRQ" in text
    assert "PAX added:" in text or "PAX removed:" in text
    assert "Backblast body was edited" in text


def test_retried_backblast_create_posts_once():
    """Slack view_submission retries share view.id — only the first claim may post."""
    claimed = set()

    def claim(key, kind, team_id):
        token = (key, kind)
        if token in claimed:
            return False
        claimed.add(token)
        return True

    session = RecordingSession()
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456"}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/bb"}
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()
    body = _create_body()
    region = _region()

    patches = [
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_names_side_effect),
        patch("features.backblast.DbManager.find_records", side_effect=_find_records()),
        patch("features.backblast.DbManager.transaction", _transaction_for(session)),
        patch("features.backblast.ensure_users_in_db", return_value=None),
        patch("features.backblast.replace_user_channel_ids", return_value="moleskin with names"),
        patch("features.backblast.parse_rich_block", return_value="moleskin text"),
        patch("features.backblast.get_channel_name", return_value="downrange"),
        patch("features.backblast.claim_interaction", side_effect=claim),
        patch("features.backblast.check_for_duplicate", return_value=False),
        patch("features.backblast.trigger_achievement_webhook"),
        patch("features.backblast.resolve_paxminer_log_channel", return_value=None),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        for _ in range(2):
            backblast.handle_backblast_post(
                body=body,
                client=client,
                logger=MagicMock(),
                context={"user_id": "U_SUBMITTER"},
                region_record=region,
            )

    ao_posts = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "C_DOWNRANGE"]
    assert len(ao_posts) == 1


def test_backblast_create_releases_claim_when_slack_post_fails():
    session = RecordingSession()
    client = MagicMock()
    client.chat_postMessage.side_effect = RuntimeError("Slack unavailable")
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_names_side_effect),
        patch("features.backblast.DbManager.find_records", side_effect=_find_records()),
        patch("features.backblast.DbManager.transaction", _transaction_for(session)),
        patch("features.backblast.replace_user_channel_ids", return_value="moleskin"),
        patch("features.backblast.parse_rich_block", return_value="moleskin"),
        patch("features.backblast.get_channel_name", return_value="downrange"),
        patch("features.backblast.claim_interaction", return_value=True),
        patch("features.backblast.check_for_duplicate", return_value=False),
        patch("features.backblast.release_interaction") as release,
        pytest.raises(RuntimeError, match="Slack unavailable"),
    ):
        backblast.handle_backblast_post(
            body=_create_body(),
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=_region(),
        )

    release.assert_called_once()
    assert release.call_args.args[0] == "V_CREATE"


def test_backblast_create_duplicate_check_skips_post_and_releases_claim():
    session = RecordingSession()
    client = MagicMock()
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_names_side_effect),
        patch("features.backblast.DbManager.find_records", side_effect=_find_records()),
        patch("features.backblast.DbManager.transaction", _transaction_for(session)),
        patch("features.backblast.replace_user_channel_ids", return_value="moleskin"),
        patch("features.backblast.parse_rich_block", return_value="moleskin"),
        patch("features.backblast.get_channel_name", return_value="downrange"),
        patch("features.backblast.claim_interaction", return_value=True),
        patch("features.backblast.check_for_duplicate", return_value=True),
        patch("features.backblast.release_interaction") as release,
    ):
        backblast.handle_backblast_post(
            body=_create_body(),
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=_region(),
        )

    release.assert_called_once()
    ao_posts = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "C_DOWNRANGE"]
    assert ao_posts == []
    # Warning DM to submitter
    assert any(c.kwargs.get("channel") == "U_SUBMITTER" for c in client.chat_postMessage.call_args_list)


def test_backblast_refuses_post_without_claim_key():
    session = RecordingSession()
    client = MagicMock()
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()
    body = {"view": {"callback_id": actions.BACKBLAST_CALLBACK_ID}, "user": {"id": "U_SUBMITTER"}}

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_names_side_effect),
        patch("features.backblast.DbManager.find_records", side_effect=_find_records()),
        patch("features.backblast.replace_user_channel_ids", return_value="moleskin"),
        patch("features.backblast.parse_rich_block", return_value="moleskin"),
        patch("features.backblast.get_channel_name", return_value="downrange"),
        patch("features.backblast.claim_interaction") as claim,
    ):
        backblast.handle_backblast_post(
            body=body,
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=_region(schema=None),
        )

    claim.assert_not_called()
    client.chat_postMessage.assert_not_called()


def test_already_claimed_backblast_skips_file_download():
    """Retries that lose the claim must not hit Slack file/S3 work."""
    data = _base_backblast_data()
    data[actions.BACKBLAST_FILE] = [
        {
            "id": "F1",
            "filetype": "png",
            "url_private_download": "https://files.slack.com/full",
            "mimetype": "image/png",
            "original_w": 100,
            "original_h": 100,
            "thumb_1024": "https://files.slack.com/thumb",
            "permalink": "https://slack.com/p",
        }
    ]
    form = MagicMock()
    form.get_selected_values.return_value = data
    client = MagicMock()

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.claim_interaction", return_value=False),
        patch("features.backblast.requests.get") as download,
        patch("features.backblast.boto3.client") as s3,
    ):
        backblast.handle_backblast_post(
            body=_create_body(),
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=_region(schema=None),
        )

    download.assert_not_called()
    s3.assert_not_called()
    client.chat_postMessage.assert_not_called()


def test_backblast_create_keeps_claim_when_permalink_fails():
    session = RecordingSession()
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456", "message": {"edited": {"ts": "123.457"}}}
    client.chat_getPermalink.side_effect = RuntimeError("permalink failed")
    form = MagicMock()
    form.get_selected_values.return_value = _base_backblast_data()

    with (
        patch("features.backblast.copy.deepcopy", return_value=form),
        patch("features.backblast.add_custom_field_blocks", side_effect=lambda f, _r: f),
        patch("features.backblast.get_user_names", side_effect=_names_side_effect),
        patch("features.backblast.DbManager.find_records", side_effect=_find_records()),
        patch("features.backblast.DbManager.transaction", _transaction_for(session)),
        patch("features.backblast.ensure_users_in_db", return_value=None),
        patch("features.backblast.replace_user_channel_ids", return_value="moleskin"),
        patch("features.backblast.parse_rich_block", return_value="moleskin"),
        patch("features.backblast.get_channel_name", return_value="downrange"),
        patch("features.backblast.claim_interaction", return_value=True),
        patch("features.backblast.check_for_duplicate", return_value=False),
        patch("features.backblast.trigger_achievement_webhook"),
        patch("features.backblast.resolve_paxminer_log_channel", return_value=None),
        patch("features.backblast.release_interaction") as release,
    ):
        backblast.handle_backblast_post(
            body=_create_body(),
            client=client,
            logger=MagicMock(),
            context={"user_id": "U_SUBMITTER"},
            region_record=_region(),
        )

    release.assert_not_called()
    ao_posts = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "C_DOWNRANGE"]
    assert len(ao_posts) == 1
    assert any(op[0] == "add" and op[1] == "beatdowns" for op in session.ops)
