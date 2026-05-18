import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_handlers(monkeypatch):
    monkeypatch.setenv("DB_ENCRYPTION_KEY", "ci-test-encryption-key-32chars!")
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


def test_kotter_handler_status_action(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    calls = _install_fake_kotter(monkeypatch)
    response = handlers.kotter_handler(
        {"requestContext": {"http": {}}, "queryStringParameters": {"action": "status"}},
        None,
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["mode"] == "kotter-status"
    assert calls == []


def test_kotter_handler_requires_send_for_http(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    calls = _install_fake_kotter(monkeypatch)
    response = handlers.kotter_handler({"requestContext": {"http": {}}, "queryStringParameters": {}}, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert "action=send" in body["error"]
    assert calls == []


def test_kotter_handler_manual_send_http(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    calls = _install_fake_kotter(monkeypatch)
    response = handlers.kotter_handler(
        {"requestContext": {"http": {}}, "queryStringParameters": {"action": "send"}},
        None,
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["mode"] == "kotter-manual"
    assert calls == ["called"]


def test_kotter_handler_direct_invoke_still_runs(monkeypatch):
    handlers = _load_handlers(monkeypatch)
    calls = _install_fake_kotter(monkeypatch)
    response = handlers.kotter_handler({}, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["mode"] == "kotter"
    assert calls == ["called"]
