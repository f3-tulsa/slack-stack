import json
import os
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are features.* and utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))

from features import backblast
from utilities.database.orm import Attendance, PaxminerUser
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
        "view": {"callback_id": actions.BACKBLAST_CALLBACK_ID},
        "user": {"id": "U_SUBMITTER"},
    }


def _edit_body(*, message_ts="111.222"):
    meta = {"channel_id": "C_DOWNRANGE"}
    if message_ts is not None:
        meta["message_ts"] = message_ts
    return {
        "view": {
            "callback_id": actions.BACKBLAST_EDIT_CALLBACK_ID,
            "private_metadata": json.dumps(meta),
        },
        "user": {"id": "U_SUBMITTER"},
    }


def _user_dms(client):
    return [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "U_SUBMITTER"]


def _find_records(user_records=None, prior_attendance=None):
    def _inner(cls, filters=None, schema=None):
        if cls is Attendance:
            return prior_attendance or []
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
        patch("features.backblast.get_user_names", side_effect=_get_user_names),
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

