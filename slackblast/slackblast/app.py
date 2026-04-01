# import json
import json
import logging
import os
import re
from typing import Callable, Tuple

from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from sqlalchemy import text

from features import strava
from utilities.builders import add_loading_form, send_error_response
from utilities.constants import LOCAL_DEVELOPMENT
from utilities.database.orm import Region
from utilities.field_encryption import require_encryption_key
from utilities.helper_functions import (
    REGION_RECORDS,
    get_oauth_flow,
    get_region_record,
    get_request_type,
    safe_get,
    update_local_region_records,
)
from utilities.routing import MAIN_MAPPER
from utilities.slack.actions import LOADING_ID

try:
    from snapshot_restore_py import register_after_restore
except ImportError:

    def register_after_restore(func):
        return func


require_encryption_key()

# Avoid duplicate CloudWatch lines: Lambda already attaches a root handler; Bolt can add more.
SlackRequestHandler.clear_all_log_handlers()
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# Local / non-Lambda runs need an explicit handler; Lambda must not get a second StreamHandler.
if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    _stream_handler = logging.StreamHandler()
    logger.addHandler(_stream_handler)

app = App(
    process_before_response=not LOCAL_DEVELOPMENT,
    oauth_flow=get_oauth_flow(),
)


def _warmup(log: logging.Logger) -> None:
    """Pre-warm DB pool, Fernet derivation, and region cache for EventBridge keep-warm."""
    from utilities.database import get_engine
    from utilities.field_encryption import _get_fernet, require_encryption_key

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("Keep-warm: DB connection verified")
    except Exception:
        log.warning("Keep-warm: DB ping failed", exc_info=True)
    try:
        _get_fernet(require_encryption_key())
        log.info("Keep-warm: Fernet key derived")
    except Exception:
        log.warning("Keep-warm: Fernet derivation failed", exc_info=True)
    try:
        update_local_region_records()
        log.info("Keep-warm: region records loaded (%d)", len(REGION_RECORDS))
    except Exception:
        log.warning("Keep-warm: region records load failed", exc_info=True)


@register_after_restore
def _on_snapstart_restore() -> None:
    """Dispose stale DB pool from snapshot and re-warm connections."""
    from utilities.database import get_engine

    try:
        get_engine().dispose()
    except Exception:
        pass
    _warmup(logger)


def _lambda_invocation_kind(event: dict) -> str:
    """Best-effort label for CloudWatch filtering (API vs scheduled vs Strava)."""
    if not isinstance(event, dict):
        return "non_dict_event"
    if event.get("path") == "/exchange_token":
        return "strava_exchange_token"
    if event.get("source") == "aws.events" or event.get("detail-type"):
        return "eventbridge_scheduled"
    if event.get("requestContext") or event.get("httpMethod") or event.get("rawPath"):
        return "api_gateway"
    return "unknown"


def handler(event, context):
    logger.info(
        "Lambda invocation kind=%s path=%s request_id=%s",
        _lambda_invocation_kind(event),
        event.get("path") if isinstance(event, dict) else None,
        getattr(context, "aws_request_id", None) if context else None,
    )
    if isinstance(event, dict) and (
        event.get("source") == "aws.events" or event.get("detail-type")
    ):
        _warmup(logger)
        return {"statusCode": 200, "body": "warm"}
    if event.get("path") == "/exchange_token":
        return strava.strava_exchange_token(event, context)
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def main_response(body, logger, client, ack, context):
    # Lambda lazy path: ack already sent by first invocation. Local dev: ack here.
    if LOCAL_DEVELOPMENT:
        ack()
    team_id = safe_get(body, "team_id") or safe_get(body, "team", "id")
    user_id = safe_get(body, "user_id") or safe_get(body, "user", "id")
    request_type, request_id = get_request_type(body)
    logger.info(
        "Slack request summary team_id=%s user_id=%s request_type=%s request_id=%s",
        team_id,
        user_id,
        request_type,
        request_id,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Slack request body=%s", json.dumps(body, default=str))

    region_record: Region = get_region_record(team_id, body, context, client, logger)
    logger.info(
        "Region record team_id=%s workspace_name=%s",
        getattr(region_record, "team_id", None) or team_id,
        getattr(region_record, "workspace_name", None) or "(none)",
    )

    lookup: Tuple[Callable, bool] = safe_get(safe_get(MAIN_MAPPER, request_type), request_id)
    if lookup:
        run_function, add_loading = lookup
        fn_name = getattr(run_function, "__name__", str(run_function))
        logger.info(
            "Dispatching handler=%s add_loading=%s request_id=%s",
            fn_name,
            add_loading,
            request_id,
        )
        if add_loading:
            body[LOADING_ID] = add_loading_form(body=body, client=client)
        try:
            run_function(
                body=body,
                client=client,
                logger=logger,
                context=context,
                region_record=region_record,
            )
            logger.info("Handler complete request_id=%s handler=%s", request_id, fn_name)
        except Exception as exc:
            logger.info("sending error response")
            send_error_response(body=body, client=client, error=str(exc)[:3000])
            logger.error("Handler error: %s", exc, exc_info=True)
    else:
        logger.error(
            f"no handler for path: "
            f"{safe_get(safe_get(MAIN_MAPPER, request_type), request_id) or request_type+', '+request_id}"
        )


if LOCAL_DEVELOPMENT:
    ARGS = [main_response]
    LAZY_KWARGS = {}
else:
    ARGS = []
    LAZY_KWARGS = {
        "ack": lambda ack: ack(),
        "lazy": [main_response],
    }


MATCH_ALL_PATTERN = re.compile(".*")
app.action(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)
app.view(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)
app.command(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)
app.view_closed(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)
app.event(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)

if __name__ == "__main__":
    app.start(3000)
    update_local_region_records()
