"""
AWS Lambda entry points for Weaselbot (container image).

Delegates to existing ``main()`` routines that read DATABASE_* and schema env vars.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import traceback
from datetime import datetime, timezone
from urllib.parse import parse_qs

# Configure root logging before cold-start bootstrap (token_bootstrap uses LOG.info).
logging.basicConfig(
    format="%(asctime)s [%(levelname)s]:%(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

from common.encryption import require_encryption_key

require_encryption_key()


_KOTTER_SEND_ACTION_ID = "weaselbot_kotter_send_now"


def _try_bootstrap_weaselbot_slack_token() -> None:
    f3 = os.environ.get("F3_REGION_NAME", "").strip()
    st = os.environ.get("STAGE", "").strip()
    wb_schema = os.environ.get("WEASELBOT_SCHEMA", "").strip()
    team = os.environ.get("F3_REGION_SLACK_TEAM_ID", "").strip()
    tok = os.environ.get("WB_SLACK_TOKEN", "").strip()
    if not (f3 and st and wb_schema and team and tok):
        return
    from common.token_bootstrap import upsert_weaselbot_slack_token

    upsert_weaselbot_slack_token(
        weaselbot_schema=wb_schema,
        team_id=team,
        paxminer_regional_schema=f"{f3}_{st}",
        plaintext_token=tok,
    )


try:
    _try_bootstrap_weaselbot_slack_token()
except Exception:
    logging.exception(
        "Token bootstrap failed (non-fatal); weaselbot.regions slack_token may need manual upsert"
    )


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _http_request(event) -> bool:
    request_context = (event or {}).get("requestContext", {})
    return isinstance(request_context, dict) and "http" in request_context


def _raw_body(event) -> str:
    body = (event or {}).get("body") or ""
    if (event or {}).get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return body


def _header_value(headers: dict, key: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == key.lower():
            return v
    return ""


def _verify_slack_request(headers: dict, raw_body: str) -> bool:
    secret = os.environ.get("WB_SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        logging.error("WB_SLACK_SIGNING_SECRET is not configured")
        return False
    timestamp = _header_value(headers, "X-Slack-Request-Timestamp")
    signature = _header_value(headers, "X-Slack-Signature")
    if not timestamp or not signature:
        return False
    try:
        age = abs(int(datetime.now(timezone.utc).timestamp()) - int(timestamp))
    except ValueError:
        return False
    if age > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    expected = "v0=" + hmac.new(secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_slack_body(raw_body: str) -> tuple[str, dict]:
    params = parse_qs(raw_body, keep_blank_values=True)
    if "payload" in params:
        try:
            return "interactive", json.loads(params["payload"][0])
        except (KeyError, IndexError, json.JSONDecodeError):
            return "invalid", {}
    if "command" in params:
        return "command", {k: v[0] if v else "" for k, v in params.items()}
    return "invalid", {}


def _is_slack_admin(user_id: str) -> bool:
    token = os.environ.get("WB_SLACK_TOKEN", "").strip()
    if not token:
        return False
    from slack_sdk import WebClient

    user = WebClient(token=token).users_info(user=user_id).get("user", {})
    return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))


def _queue_manual_kotter_send(context, requested_by: str, team_id: str) -> None:
    import boto3

    function_name = getattr(context, "invoked_function_arn", "") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(
            {"source": "weaselbot.kotter.manual", "requested_by": requested_by, "team_id": team_id}
        ).encode("utf-8"),
    )


def _slash_command_response() -> dict:
    return {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": "Kotter report controls",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Kotter Reports*\nSend this month's Kotter report now."},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Send Monthly Kotter Now"},
                        "style": "primary",
                        "action_id": _KOTTER_SEND_ACTION_ID,
                        "value": "send",
                    }
                ],
            },
        ],
    }


def _interactive_response(text: str) -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


def _kotter_http_handler(event, context) -> dict:
    headers = (event or {}).get("headers") or {}
    raw_body = _raw_body(event)
    if not _verify_slack_request(headers, raw_body):
        return _response(401, {"ok": False, "error": "Unauthorized request"})

    payload_type, payload = _parse_slack_body(raw_body)
    if payload_type == "command":
        user_id = payload.get("user_id", "")
        if not _is_slack_admin(user_id):
            return _response(200, _interactive_response("You must be a Slack workspace admin to send Kotter reports."))
        return _response(200, _slash_command_response())

    if payload_type == "interactive":
        user_id = (payload.get("user") or {}).get("id", "")
        team_id = (payload.get("team") or {}).get("id", "")
        action_id = (((payload.get("actions") or [{}])[0]).get("action_id") or "").strip()
        if action_id != _KOTTER_SEND_ACTION_ID:
            return _response(400, {"ok": False, "error": "Unsupported interactive action"})
        if not _is_slack_admin(user_id):
            return _response(200, _interactive_response("You must be a Slack workspace admin to send Kotter reports."))
        _queue_manual_kotter_send(context, user_id, team_id)
        return _response(200, _interactive_response("Manual Kotter send queued. You'll see report output in Slack shortly."))

    return _response(400, {"ok": False, "error": "Unsupported Slack request payload"})


def achievements_handler(event, context):
    logging.info(
        "Weaselbot achievements_handler start request_id=%s",
        getattr(context, "aws_request_id", None) if context else None,
    )
    try:
        from weaselbot.pax_achievements import main

        main()
        return {"statusCode": 200, "body": json.dumps({"ok": True, "mode": "achievements"})}
    except Exception:
        logging.exception("Weaselbot achievements failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"ok": False, "error": traceback.format_exc()}),
        }


def kotter_handler(event, context):
    logging.info(
        "Weaselbot kotter_handler start request_id=%s",
        getattr(context, "aws_request_id", None) if context else None,
    )
    try:
        if _http_request(event):
            return _kotter_http_handler(event, context)

        mode = "kotter"
        if (event or {}).get("source") == "weaselbot.kotter.manual":
            mode = "kotter-manual"
        from weaselbot.kotter_report import main

        main()
        return _response(200, {"ok": True, "mode": mode})
    except Exception:
        logging.exception("Weaselbot kotter failed")
        return _response(500, {"ok": False, "error": traceback.format_exc()})
