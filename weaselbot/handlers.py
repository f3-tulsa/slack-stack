"""
AWS Lambda entry points for Weaselbot (container image).

Delegates to existing ``main()`` routines that read DATABASE_* and schema env vars.
"""

from __future__ import annotations

import json
import logging
import traceback


def achievements_handler(event, context):
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s]:%(message)s", level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S"
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
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s]:%(message)s", level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S"
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
