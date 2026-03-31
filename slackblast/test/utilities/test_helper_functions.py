import os
import sys
from unittest.mock import MagicMock, patch

# Match Lambda layout (CodeUri = slackblast/slackblast): imports are utilities.* not slackblast.utilities.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "slackblast"))
from utilities.helper_functions import get_oauth_flow, safe_get


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
