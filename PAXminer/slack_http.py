"""HTTP helpers for Function URL handlers (achievements webhook + Slack keep-warm)."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os

LOG = logging.getLogger(__name__)


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


def verify_achievements_webhook_secret(headers: dict) -> bool:
    expected = os.environ.get("PM_ACHIEVEMENTS_WEBHOOK_SECRET", "").strip()
    if not expected:
        LOG.error("PM_ACHIEVEMENTS_WEBHOOK_SECRET is not configured")
        return False
    got = header_value(headers, "X-Paxminer-Achievements-Webhook-Secret")
    return hmac.compare_digest(expected, got)


def is_slack_admin(user_id: str, client=None) -> bool:
    """Return True if the user is a workspace admin/owner.

    Prefer a Bolt-injected ``client`` when available so we reuse one WebClient.
    On Slack/API errors, fail closed (deny) and log — never raise into listeners.
    """
    try:
        if client is not None:
            user = client.users_info(user=user_id).get("user", {})
            return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))
        token = os.environ.get("PM_SLACK_TOKEN", "").strip()
        if not token:
            return False
        from slack_sdk import WebClient

        user = WebClient(token=token).users_info(user=user_id).get("user", {})
        return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))
    except Exception:
        LOG.exception("is_slack_admin failed user_id=%s", user_id)
        return False


ADMIN_REQUIRED_TEXT = "You must be a Slack admin to access your PAXMiner settings."


def notify_admin_required(client, body, *, text: str = ADMIN_REQUIRED_TEXT) -> None:
    """Best-effort ephemeral or DM when a non-admin hits an admin-only control."""
    user_id = (body.get("user") or {}).get("id") or body.get("user_id")
    channel_id = (body.get("channel") or {}).get("id") or body.get("channel_id")
    if not user_id or client is None:
        return
    try:
        if channel_id:
            client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
            return
        opened = client.conversations_open(users=user_id)
        dm = (opened.get("channel") or {}).get("id")
        if dm:
            client.chat_postMessage(channel=dm, text=text)
    except Exception:
        LOG.debug("notify_admin_required failed", exc_info=True)
