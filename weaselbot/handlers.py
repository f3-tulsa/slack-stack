"""
AWS Lambda entry points for Weaselbot (container image).

Delegates to existing ``main()`` routines that read DATABASE_* and schema env vars.
"""

from __future__ import annotations

import json
import logging
import os
import traceback

from common.encryption import require_encryption_key

# Configure root logging before cold-start bootstrap (token_bootstrap uses LOG.info).
logging.basicConfig(
    format="%(asctime)s [%(levelname)s]:%(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

require_encryption_key()


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
        from weaselbot.kotter_report import main

        main()
        return {"statusCode": 200, "body": json.dumps({"ok": True, "mode": "kotter"})}
    except Exception:
        logging.exception("Weaselbot kotter failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"ok": False, "error": traceback.format_exc()}),
        }


def downrange_handler(event, context):
    logging.info(
        "Weaselbot downrange_handler start request_id=%s",
        getattr(context, "aws_request_id", None) if context else None,
    )
    try:
        from weaselbot.downrange_sync import main

        main()
        return {"statusCode": 200, "body": json.dumps({"ok": True, "mode": "downrange"})}
    except Exception:
        logging.exception("Weaselbot downrange sync failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"ok": False, "error": traceback.format_exc()}),
        }
