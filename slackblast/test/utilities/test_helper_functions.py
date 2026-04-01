import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are utilities.* not slackblast.utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))
from utilities.helper_functions import ensure_users_in_db, get_oauth_flow, safe_get


def _slack_user(
    email="a@b.com",
    display="Display",
    real="Real Name",
    phone="555-0100",
    bot=False,
):
    return {
        "user": {
            "profile": {
                "email": email,
                "display_name": display,
                "real_name": real,
                "phone": phone,
            },
            "is_bot": bot,
        }
    }


class _ExecResult:
    def __init__(self, rows=(), one=None):
        self._rows = rows
        self._one = one

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


def _mock_engine_with_execute(side_effect_fn):
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = side_effect_fn
    cm = MagicMock()
    cm.__enter__.return_value = mock_conn
    cm.__exit__.return_value = None
    mock_engine.begin.return_value = cm
    return mock_engine, mock_conn


def test_safe_get():
    assert safe_get({"a": {"b": {"c": 1}}}, "a", "b", "c") == 1
    assert safe_get({"a": {"b": {"c": 1}}}, "a", "b", "d") == None


@patch("utilities.helper_functions.LOCAL_DEVELOPMENT", False)
@patch("utilities.helper_functions.FixedSQLAlchemyOAuthStateStore")
@patch("utilities.helper_functions.SQLAlchemyInstallationStore")
@patch("utilities.helper_functions.get_engine")
def test_get_oauth_flow_skips_create_tables_when_disabled(
    _mock_engine, mock_inst_cls, mock_state_cls
):
    installation_store = MagicMock()
    state_store = MagicMock()
    mock_inst_cls.return_value = installation_store
    mock_state_cls.return_value = state_store
    env = {
        "ENV_SLACK_CLIENT_ID": "test-client-id",
        "ENV_SLACK_CLIENT_SECRET": "test-secret",
        "ENV_SLACK_SCOPES": "chat:write,commands",
        "CREATE_OAUTH_TABLES": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        flow = get_oauth_flow()
    assert flow is not None
    installation_store.create_tables.assert_not_called()
    state_store.create_tables.assert_not_called()


@patch("utilities.helper_functions.LOCAL_DEVELOPMENT", False)
@patch("utilities.helper_functions.FixedSQLAlchemyOAuthStateStore")
@patch("utilities.helper_functions.SQLAlchemyInstallationStore")
@patch("utilities.helper_functions.get_engine")
def test_get_oauth_flow_calls_create_tables_when_enabled(
    _mock_engine, mock_inst_cls, mock_state_cls
):
    installation_store = MagicMock()
    state_store = MagicMock()
    mock_inst_cls.return_value = installation_store
    mock_state_cls.return_value = state_store
    env = {
        "ENV_SLACK_CLIENT_ID": "test-client-id",
        "ENV_SLACK_CLIENT_SECRET": "test-secret",
        "ENV_SLACK_SCOPES": "chat:write,commands",
        "CREATE_OAUTH_TABLES": "true",
    }
    with patch.dict(os.environ, env, clear=False):
        flow = get_oauth_flow()
    assert flow is not None
    installation_store.create_tables.assert_called_once()
    state_store.create_tables.assert_called_once()


@patch("utilities.helper_functions.get_engine")
def test_ensure_users_in_db_no_merge_needed(mock_ge):
    """Email present, no other row with same email: SELECT empty, INSERT only."""
    calls = []

    def execute(stmt, params=None):
        calls.append((str(stmt), params))
        sql = str(stmt)
        if "FROM users WHERE email" in sql and "user_id !=" in sql:
            return _ExecResult([])
        if "INSERT INTO users" in sql:
            return _ExecResult()
        raise AssertionError(f"Unexpected SQL in no-merge test: {sql[:120]}")

    mock_ge.return_value, _ = _mock_engine_with_execute(execute)

    client = MagicMock()
    client.users_info.return_value = _slack_user()
    logger = MagicMock()

    ensure_users_in_db(["U_NEW"], client, logger, "region_schema")

    assert len(calls) == 2
    assert "INSERT INTO users" in calls[1][0]
    assert calls[1][1]["uid"] == "U_NEW"
    logger.warning.assert_not_called()


@patch("utilities.helper_functions.get_engine")
def test_ensure_users_in_db_merge_old_row_only(mock_ge):
    """Stale row with same email; canonical user_id not present until INSERT."""
    calls = []

    old_row = (
        "U_OLD",
        date(2021, 6, 15),
        1,
        "111-2222",
        json.dumps({"legacy": True}),
    )

    def execute(stmt, params=None):
        calls.append((str(stmt), params))
        sql = str(stmt)
        if "FROM users WHERE email" in sql and "user_id !=" in sql:
            return _ExecResult([old_row])
        if "UPDATE beatdowns SET q_user_id" in sql:
            return _ExecResult()
        if "UPDATE beatdowns SET coq_user_id" in sql:
            return _ExecResult()
        if "UPDATE bd_attendance SET user_id" in sql:
            return _ExecResult()
        if "UPDATE bd_attendance SET q_user_id" in sql:
            return _ExecResult()
        if "UPDATE achievements_awarded SET pax_id" in sql:
            return _ExecResult()
        if "DELETE FROM users WHERE user_id" in sql:
            assert params["old"] == "U_OLD"
            return _ExecResult()
        if "INSERT INTO users" in sql:
            return _ExecResult()
        if sql.strip().startswith("SELECT json FROM users"):
            return _ExecResult(one=(None,))
        if "start_date = COALESCE" in sql:
            merged = json.loads(params["merged_json"])
            assert merged["legacy"] is True
            assert merged["old_phone"] == "111-2222"
            assert params["old_start"] == date(2021, 6, 15)
            assert params["old_app"] == 1
            assert params["uid"] == "U_NEW"
            return _ExecResult()
        raise AssertionError(f"Unexpected SQL in merge-old test: {sql[:120]}")

    mock_ge.return_value, _ = _mock_engine_with_execute(execute)

    client = MagicMock()
    client.users_info.return_value = _slack_user(email="same@x.com", phone="999")
    logger = MagicMock()

    ensure_users_in_db(["U_NEW"], client, logger, "region_schema")

    delete_calls = [c for c in calls if "DELETE FROM users" in c[0]]
    assert len(delete_calls) == 1


@patch("utilities.helper_functions.get_engine")
def test_ensure_users_in_db_merge_when_canonical_row_exists(mock_ge):
    """Old + new rows share email (duplicate PK scenario): repoint, delete old, merge json."""
    calls = []

    old_row = (
        "U_OLD",
        date(2020, 1, 1),
        0,
        "000-OLD",
        json.dumps({"is_admin": True}),
    )

    def execute(stmt, params=None):
        calls.append((str(stmt), params))
        sql = str(stmt)
        if "FROM users WHERE email" in sql and "user_id !=" in sql:
            return _ExecResult([old_row])
        if "UPDATE beatdowns SET q_user_id" in sql:
            return _ExecResult()
        if "UPDATE beatdowns SET coq_user_id" in sql:
            return _ExecResult()
        if "UPDATE bd_attendance SET user_id" in sql:
            return _ExecResult()
        if "UPDATE bd_attendance SET q_user_id" in sql:
            return _ExecResult()
        if "UPDATE achievements_awarded SET pax_id" in sql:
            return _ExecResult()
        if "DELETE FROM users WHERE user_id" in sql:
            assert params["old"] == "U_OLD"
            return _ExecResult()
        if "INSERT INTO users" in sql:
            return _ExecResult()
        if sql.strip().startswith("SELECT json FROM users"):
            return _ExecResult(one=('{"from_canonical": 1}',))
        if "start_date = COALESCE" in sql:
            merged = json.loads(params["merged_json"])
            assert merged["is_admin"] is True
            assert merged["from_canonical"] == 1
            assert merged["old_phone"] == "000-OLD"
            assert params["old_start"] == date(2020, 1, 1)
            assert params["old_app"] == 0
            return _ExecResult()
        raise AssertionError(f"Unexpected SQL in both-rows test: {sql[:120]}")

    mock_ge.return_value, _ = _mock_engine_with_execute(execute)

    client = MagicMock()
    client.users_info.return_value = _slack_user(email="dup@x.com")
    logger = MagicMock()

    ensure_users_in_db(["U_NEW"], client, logger, "region_schema")

    assert any("DELETE FROM users" in c[0] for c in calls)


@patch("utilities.helper_functions.get_engine")
def test_ensure_users_in_db_no_email_logs_warning(mock_ge):
    calls = []

    def execute(stmt, params=None):
        calls.append((str(stmt), params))
        sql = str(stmt)
        if "INSERT INTO users" in sql:
            return _ExecResult()
        raise AssertionError(f"Unexpected SQL in no-email test: {sql[:120]}")

    mock_ge.return_value, _ = _mock_engine_with_execute(execute)

    client = MagicMock()
    client.users_info.return_value = {
        "user": {"profile": {"display_name": "X", "real_name": "X"}, "is_bot": False}
    }
    logger = MagicMock()

    ensure_users_in_db(["U1"], client, logger, "region_schema")

    assert len(calls) == 1
    logger.warning.assert_called_once()
    assert "users:read.email" in logger.warning.call_args[0][0]
