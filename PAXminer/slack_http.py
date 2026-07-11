"""Slack HTTP verification and parsing for Function URL handlers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs

LOG = logging.getLogger(__name__)

KOTTER_SEND_ACTION_ID = "paxminer_kotter_send_now"


def http_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def is_http_request(event) -> bool:
    request_context = (event or {}).get("requestContext", {})
    return isinstance(request_context, dict) and "http" in request_context


def raw_body(event) -> str:
    body = (event or {}).get("body") or ""
    if (event or {}).get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return body


def header_value(headers: dict, key: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == key.lower():
            return v
    return ""


def verify_slack_request(headers: dict, body: str) -> bool:
    secret = os.environ.get("PM_SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        LOG.error("PM_SLACK_SIGNING_SECRET is not configured")
        return False
    timestamp = header_value(headers, "X-Slack-Request-Timestamp")
    signature = header_value(headers, "X-Slack-Signature")
    if not timestamp or not signature:
        return False
    try:
        age = abs(int(datetime.now(timezone.utc).timestamp()) - int(timestamp))
    except ValueError:
        return False
    if age > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{body}".encode("utf-8")
    expected = "v0=" + hmac.new(secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_sweep_secret(headers: dict) -> bool:
    expected = os.environ.get("PAXMINER_SWEEP_SECRET", "").strip()
    if not expected:
        LOG.error("PAXMINER_SWEEP_SECRET is not configured")
        return False
    got = header_value(headers, "X-Paxminer-Sweep-Secret")
    return hmac.compare_digest(expected, got)


def parse_slack_body(body: str) -> tuple[str, dict]:
    params = parse_qs(body, keep_blank_values=True)
    if "payload" in params:
        try:
            return "interactive", json.loads(params["payload"][0])
        except (KeyError, IndexError, json.JSONDecodeError):
            return "invalid", {}
    if "command" in params:
        return "command", {k: v[0] if v else "" for k, v in params.items()}
    return "invalid", {}


def is_slack_admin(user_id: str) -> bool:
    token = os.environ.get("PM_SLACK_TOKEN", "").strip()
    if not token:
        return False
    from slack_sdk import WebClient

    user = WebClient(token=token).users_info(user=user_id).get("user", {})
    return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))
