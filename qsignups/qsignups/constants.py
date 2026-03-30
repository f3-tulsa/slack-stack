import os

import pytz

SCHEDULE_CREATE_LENGTH_DAYS = int(os.environ.get("SCHEDULE_CREATE_LENGTH_DAYS", "365"))
GCAL_LOOKAHEAD_DAYS = int(os.environ.get("GCAL_LOOKAHEAD_DAYS", "30"))
GCAL_REMINDER_MINUTES = int(os.environ.get("GCAL_REMINDER_MINUTES", "1440"))
EVENT_PICKER_WEEKS = int(os.environ.get("EVENT_PICKER_WEEKS", "12"))
UPCOMING_DAYS = int(os.environ.get("UPCOMING_DAYS", "7"))
DEFAULT_EVENT_START = os.environ.get("DEFAULT_EVENT_START", "05:30")
DEFAULT_EVENT_END = os.environ.get("DEFAULT_EVENT_END", "06:15")
DEFAULT_EVENT_DURATION_MINUTES = int(os.environ.get("DEFAULT_EVENT_DURATION_MINUTES", "45"))

SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
# True only when running outside Lambda (local dev). In Lambda, AWS_LAMBDA_FUNCTION_NAME is always set.
# Previously this checked SLACK_BOT_TOKEN != "123", which was True in Lambda -- backwards.
LOCAL_DEVELOPMENT = not os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

SLACK_CLIENT_ID = "ENV_SLACK_CLIENT_ID"
SLACK_CLIENT_SECRET = "ENV_SLACK_CLIENT_SECRET"
SLACK_SCOPES = "ENV_SLACK_SCOPES"


def app_timezone():
    return pytz.timezone(os.environ.get("TIMEZONE", "US/Central"))
