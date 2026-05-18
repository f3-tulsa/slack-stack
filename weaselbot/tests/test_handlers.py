import hashlib
import hmac
import importlib.util
import json
import sys
import types
from pathlib import Path
from time import time
from urllib.parse import urlencode


class _Ctx:
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:weaselbot-test-weaselbot-kotter"


def _load_handlers(monkeypatch):
    monkeypatch.setenv("DB_ENCRYPTION_KEY", "ci-test-encryption-key-32chars!")
    monkeypatch.setenv("WB_SLACK_SIGNING_SECRET", "wb-signing-secret-for-tests-123")
    module_name = "weaselbot_handlers_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).resolve().parents[1] / "handlers.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _install_fake_kotter(monkeypatch):
    calls = []
    fake_module = types.ModuleType("weaselbot.kotter_report")

    def fake_main():
        calls.append("called")

    fake_module.main = fake_main
    monkeypatch.setitem(sys.modules, "weaselbot.kotter_report", fake_module)
    return calls


def _signed_headers(secret: str, body: str):
    ts = str(int(time()))
    signature = hmac.new(secret.encode("utf-8"), f"v0:{ts}:{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": f"v0={signature}"}


def test_kotter_handler_direct_invoke_still_runs(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    calls = _install_fake_kotter(monkeypatch)
    response = handlers.kotter_handler({}, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["mode"] == "kotter"
    assert calls == ["called"]


def test_kotter_handler_rejects_unsigned_http_requests(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    calls = _install_fake_kotter(monkeypatch)
    response = handlers.kotter_handler({"requestContext": {"http": {}}, "headers": {}, "body": "command=/kotter-report"}, _Ctx())
    body = json.loads(response["body"])
    assert response["statusCode"] == 401
    assert body["error"] == "Unauthorized request"
    assert calls == []


def test_kotter_handler_slash_command_returns_gui_for_admin(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    monkeypatch.setattr(handlers, "_is_slack_admin", lambda _: True)
    body = urlencode({"command": "/kotter-report", "team_id": "T123", "user_id": "U123", "trigger_id": "1337.42"})
    response = handlers.kotter_handler(
        {
            "requestContext": {"http": {}},
            "headers": _signed_headers("wb-signing-secret-for-tests-123", body),
            "body": body,
        },
        _Ctx(),
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert payload["response_type"] == "ephemeral"
    assert payload["blocks"][1]["elements"][0]["action_id"] == "weaselbot_kotter_send_now"


def test_kotter_handler_slash_command_blocks_non_admin(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    monkeypatch.setattr(handlers, "_is_slack_admin", lambda _: False)
    body = urlencode({"command": "/kotter-report", "team_id": "T123", "user_id": "U123", "trigger_id": "1337.42"})
    response = handlers.kotter_handler(
        {
            "requestContext": {"http": {}},
            "headers": _signed_headers("wb-signing-secret-for-tests-123", body),
            "body": body,
        },
        _Ctx(),
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert "must be a Slack workspace admin" in payload["text"]


def test_kotter_handler_interactive_send_queues_manual_run(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    queued = []
    monkeypatch.setattr(handlers, "_is_slack_admin", lambda _: True)
    monkeypatch.setattr(handlers, "_queue_manual_kotter_send", lambda ctx, user_id, team_id: queued.append((user_id, team_id)))
    body = urlencode(
        {
            "payload": json.dumps(
                {
                    "team": {"id": "T123"},
                    "user": {"id": "U123"},
                    "actions": [{"action_id": "weaselbot_kotter_send_now", "value": "send"}],
                }
            )
        }
    )
    response = handlers.kotter_handler(
        {
            "requestContext": {"http": {}},
            "headers": _signed_headers("wb-signing-secret-for-tests-123", body),
            "body": body,
        },
        _Ctx(),
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert "queued" in payload["text"]
    assert queued == [("U123", "T123")]
