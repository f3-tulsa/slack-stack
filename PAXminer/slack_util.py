"""Shared Slack helpers for PAXMiner Kotter and achievements."""

from __future__ import annotations

import logging
import math
import os
import re
import ssl
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# Slack user IDs are U/W + alphanumeric; length varies (classic ~9, Enterprise often longer).
SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{7,}$")
_MISSING_NAMES = frozenset({"", "nan", "none", "null", "nat"})

# Cap Slack API pagination. MagicMock clients return a truthy non-str next_cursor;
# without a type check + page cap the loop grows unbounded and exhausts memory.
MAX_SLACK_PAGES = 50


def next_slack_cursor(resp: Any, seen: set[str]) -> str:
    """Extract the next pagination cursor, or "" to stop.

    Stops when the cursor is missing, empty, non-str (e.g. MagicMock), or repeated.
    """
    meta = resp.get("response_metadata") if hasattr(resp, "get") else None
    cursor = (meta or {}).get("next_cursor") if hasattr(meta, "get") else None
    if not isinstance(cursor, str) or not cursor or cursor in seen:
        return ""
    seen.add(cursor)
    return cursor



def home_region_date_tiers() -> tuple[int, int, int, int]:
    default = (30, 60, 90, 120)
    raw = (os.environ.get("HOME_REGION_DATE_TIERS") or "").strip()
    if not raw:
        return default
    parts = [int(x.strip()) for x in raw.split(",") if x.strip()]
    if len(parts) < 4:
        parts = list(parts) + list(default[len(parts) :])
    return (parts[0], parts[1], parts[2], parts[3])


def slack_client(token: str) -> WebClient:
    client = WebClient(token=token, ssl=ssl.create_default_context())
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    return client


def slack_display_name(client: WebClient, user_id: str) -> str:
    """Best-effort Slack display name for operational logs. Never a mention.

    Falls back to the raw user id when lookup fails so the log still names someone.
    """
    uid = (user_id or "").strip()
    if not uid:
        return ""
    try:
        user = (client.users_info(user=uid) or {}).get("user") or {}
        profile = user.get("profile") or {}
        name = (
            (profile.get("display_name") or "").strip()
            or (profile.get("real_name") or "").strip()
            or (user.get("real_name") or "").strip()
            or (user.get("name") or "").strip()
        )
        if name:
            return name
    except Exception:
        logging.debug("users_info failed user=%s", uid, exc_info=True)
    return uid


def ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = ["th", "st", "nd", "rd", "th"][min(n % 10, 4)]
    return suffix


def _clean_str(value: Any) -> str | None:
    """Normalize pandas / DB junk to a usable string, or None if missing."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    # pandas NA / NaT without importing pandas at module level
    type_name = type(value).__name__
    if type_name in ("NAType", "NaTType"):
        return None
    text = str(value).strip()
    if not text or text.lower() in _MISSING_NAMES:
        return None
    return text


def is_slack_user_id(value: Any) -> bool:
    """True when value looks like a Slack user ID (U… / W…)."""
    cleaned = _clean_str(value)
    if not cleaned:
        return False
    return bool(SLACK_USER_ID_RE.match(cleaned))


def workspace_user_ids(client: WebClient) -> set[str] | None:
    """Return the set of human user IDs in the workspace, or None on failure.

    None means "roster unavailable" — callers should fall back to format-only
    validation rather than treating every ID as unknown.
    """
    ids: set[str] = set()
    cursor = ""
    seen: set[str] = set()
    try:
        for _ in range(MAX_SLACK_PAGES):
            kwargs: dict[str, Any] = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.users_list(**kwargs)
            for member in resp.get("members") or []:
                uid = member.get("id")
                if uid and is_slack_user_id(uid):
                    ids.add(str(uid))
            cursor = next_slack_cursor(resp, seen)
            if not cursor:
                break
        return ids
    except Exception:
        logging.debug("workspace_user_ids failed", exc_info=True)
        return None


def mention(
    user_id: Any,
    name: Any = None,
    *,
    known_ids: set[str] | None = None,
) -> str:
    """Format a PAX for Slack mrkdwn.

    Preference order:
    1. ``<@U…>`` when the ID is Slack-shaped and (if known_ids given) in-roster
    2. Inline-code user ID when we cannot mention but have an ID
    3. Inline-code display name when that is all we have
    4. `` `unknown PAX` `` when neither is usable
    """
    uid = _clean_str(user_id)
    display = _clean_str(name)
    if uid and is_slack_user_id(uid):
        if known_ids is None or uid in known_ids:
            return f"<@{uid}>"
    if uid:
        return f"`{uid}`"
    if display:
        return f"`{display}`"
    return "`unknown PAX`"


def plain_name(user_id: Any = None, name: Any = None) -> str:
    """Display name for operational logs: never a mention, never a Slack user id suffix."""
    display = _clean_str(name)
    if display:
        return display
    uid = _clean_str(user_id)
    if uid and not is_slack_user_id(uid):
        return uid
    return "PAX"


def post_message(
    client: WebClient,
    channel: str,
    text: str,
    *,
    blocks: list | None = None,
    add_reaction: bool = False,
    reaction: str = "fire",
    unfurl_links: bool = False,
    unfurl_media: bool = False,
    max_retries: int = 5,
) -> None:
    kwargs: dict = {
        "channel": channel,
        "text": text,
        "link_names": True,
        "unfurl_links": unfurl_links,
        "unfurl_media": unfurl_media,
    }
    if blocks:
        kwargs["blocks"] = blocks
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.chat_postMessage(**kwargs)
            if add_reaction and response.get("ts"):
                client.reactions_add(channel=channel, name=reaction, timestamp=response["ts"])
            return
        except SlackApiError as e:
            last_error = e
            if e.response is not None and e.response.status_code == 429:
                delay = int(e.response.headers.get("Retry-After", "1"))
                logging.info("Slack rate limit; sleeping %ss (attempt %s)", delay, attempt + 1)
                time.sleep(delay)
                continue
            if e.response.get("error") == "not_in_channel":
                try:
                    client.conversations_join(channel=channel)
                    continue
                except Exception:
                    logging.exception("Failed to join/post channel=%s", channel)
                    raise
            raise
    if last_error:
        raise last_error
    raise RuntimeError(f"chat_postMessage failed after {max_retries} attempts channel={channel}")


def post_messages(
    client: WebClient,
    channel: str,
    items: list[tuple[str, list | None]],
    *,
    delay_s: float = 0.4,
    add_reaction_first: bool = False,
    reaction: str = "fire",
) -> None:
    """Post successive messages with a pause between them; retries 429s more than once."""
    for i, item in enumerate(items):
        if isinstance(item, tuple):
            text, blocks = item[0], item[1] if len(item) > 1 else None
        else:
            text, blocks = str(item), None
        if i:
            time.sleep(delay_s)
        post_message(
            client,
            channel,
            text,
            blocks=blocks,
            add_reaction=add_reaction_first and i == 0,
            reaction=reaction,
        )


def open_dm_channel(client: WebClient, user_id: str) -> str:
    resp = client.conversations_open(users=user_id)
    return resp["channel"]["id"]


def post_log(
    client: WebClient,
    text: str,
    *,
    blocks: list | None = None,
    channel: str = "paxminer_logs",
) -> None:
    """Best-effort operational log to the region's paxminer_logs channel. Never raises."""
    try:
        post_message(client, channel, text, blocks=blocks)
    except Exception:
        logging.debug("paxminer_logs post failed: %s", text, exc_info=True)


def upload_file(
    client: WebClient,
    channel: str,
    file_path: str,
    *,
    initial_comment: str = "",
    title: str | None = None,
    max_retries: int = 5,
) -> None:
    """Upload a file with 429 retry and not_in_channel join fallback."""
    kwargs: dict = {
        "channel": channel,
        "file": file_path,
        "initial_comment": initial_comment or "",
    }
    if title:
        kwargs["title"] = title
    for attempt in range(max_retries):
        try:
            client.files_upload_v2(**kwargs)
            return
        except SlackApiError as e:
            err = e.response.get("error") if e.response else None
            if e.response is not None and e.response.status_code == 429:
                delay = int(e.response.headers.get("Retry-After", "1"))
                logging.info("Slack rate limit on upload; sleeping %ss", delay)
                time.sleep(delay)
                continue
            if err == "not_in_channel":
                try:
                    client.conversations_join(channel=channel)
                    continue
                except Exception:
                    logging.exception("Failed to join channel=%s for upload", channel)
                    raise
            raise
    raise RuntimeError(f"files_upload_v2 failed after {max_retries} attempts channel={channel}")
