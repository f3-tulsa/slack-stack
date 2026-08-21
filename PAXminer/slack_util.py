"""Shared Slack helpers for PAXMiner Kotter and achievements."""

from __future__ import annotations

import logging
import math
import os
import re
import ssl
import time
from collections.abc import Sequence
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


DEFAULT_ARCHIVE_BASE = "https://slack.com"
_ARCHIVE_BASE_CACHE: dict[str, str] = {}


def workspace_archive_base(client: WebClient | None) -> str:
    """Workspace archive host, e.g. ``https://f3ttown-test.slack.com``.

    Permalinks built on the bare ``slack.com`` host open in the browser but do
    not deep-link in the Slack mobile app, so every message link has to carry
    the workspace subdomain. ``auth.test`` returns it; cached per token because
    it never changes for the life of a container.
    """
    if client is None:
        return DEFAULT_ARCHIVE_BASE
    key = str(getattr(client, "token", "") or "")
    cached = _ARCHIVE_BASE_CACHE.get(key)
    if cached:
        return cached
    base = DEFAULT_ARCHIVE_BASE
    try:
        url = ((client.auth_test() or {}).get("url") or "").strip()
        if url.startswith("http"):
            base = url.rstrip("/")
    except Exception:
        logging.debug("auth.test failed; using default archive host", exc_info=True)
    _ARCHIVE_BASE_CACHE[key] = base
    return base


def clear_archive_base_cache() -> None:
    _ARCHIVE_BASE_CACHE.clear()


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
    2. Inline-code display name when we cannot mention
    3. `` `unknown PAX` `` when neither is usable

    Never puts a Slack user ID in code ticks.
    """
    uid = _clean_str(user_id)
    display = _clean_str(name)
    if uid and is_slack_user_id(uid):
        if known_ids is None or uid in known_ids:
            return f"<@{uid}>"
    if display:
        return f"`{display}`"
    if uid and not is_slack_user_id(uid):
        return f"`{uid}`"
    return "`unknown PAX`"


def ticked_display_name(name: str | None, *, fallback: str = "admin") -> str:
    """Wrap a display name in code ticks. Never ticks a Slack user ID."""
    cleaned = _clean_str(name) or ""
    if not cleaned or is_slack_user_id(cleaned):
        cleaned = fallback
    return f"`{cleaned}`"


def resolve_display_name(client, user_id: str, *, fallback: str = "admin") -> str:
    """Current Slack display name for logs. Never returns a Slack user ID."""
    uid = (user_id or "").strip()
    if not uid:
        return fallback
    if not is_slack_user_id(uid):
        return uid
    if client is None:
        return fallback
    name = slack_display_name(client, uid)
    if not isinstance(name, str) or not name.strip() or is_slack_user_id(name):
        return fallback
    return name.strip()


def strip_leading_log_dashes(text: str) -> str:
    """Drop the legacy `` - `` / ``- `` prefix from each line of a log blob."""
    lines = []
    for line in (text or "").splitlines():
        if line.startswith(" - "):
            line = line[3:]
        elif line.startswith("- "):
            line = line[2:]
        lines.append(line)
    return "\n".join(lines)


def log_header(title: str, *, noun: str = "job") -> str:
    """The one header grammar for paxminer_logs: ``The *Name* job was run.``

    What triggered the run belongs in the fenced ``Mode`` field, not the header,
    so every producer reads the same whether it was scheduled or hand-run.
    """
    return f"The *{title}* {noun} was run."


def format_log_message(
    header: str,
    *,
    status: str,
    duration_s: float | None = None,
    detail: str | None = None,
    fields: list[tuple[str, str]] | None = None,
    body: str | None = None,
    message_count: int | None = None,
    destinations: str | None = None,
    code_block: bool = True,
) -> str:
    """Shared paxminer_logs envelope: header, Status, optional fields, optional body, optional footer.

    Every job outcome in the log channel uses this shape: the header stays outside
    so its mrkdwn renders, and Status joins the fields inside a fenced block.
    ``body`` is free-form per-event detail and stays *below* the fence, because it
    carries links and ``<#channel>`` tags that Slack renders literally inside one.

    Pass ``code_block=False`` only to keep a legacy plain-text shape.
    """
    status_bit = str(status)
    if duration_s is not None:
        status_bit = f"{status} ({float(duration_s):.1f}s)"
    labeled = [(k, v) for k, v in (fields or []) if v]
    extra = strip_leading_log_dashes(body).strip() if body else ""
    footer: list[str] = []
    if message_count is not None:
        footer.append(f"Number of Messages: {message_count}")
    if destinations is not None:
        footer.append(f"Destination(s): {destinations}")
    if code_block:
        has_status = any(k.lower() == "status" for k, _ in labeled)
        if not has_status:
            insert_at = 0
            for i, (k, _) in enumerate(labeled):
                if k in ("Author", "Action", "Mode"):
                    insert_at = i + 1
                else:
                    break
            labeled = labeled[:insert_at] + [("Status", status_bit)] + labeled[insert_at:]
        inner = [f"{k}: {v}" for k, v in labeled]
        if detail and status != "success":
            inner.append(f"Error: {detail}")
        inner.extend(footer)
        fenced = "```\n" + "\n".join(inner) + "\n```" if inner else "```\n```"
        return f"{header}\n{fenced}\n{extra}" if extra else f"{header}\n{fenced}"
    lines = [header, f"Status: {status_bit}"]
    if detail and status != "success":
        lines.append(f"Error: {detail}")
    if labeled:
        lines.append("")
        lines.extend(f"{k}: {v}" for k, v in labeled)
    if extra:
        lines.append("")
        lines.append(extra)
    if footer:
        lines.append("")
        lines.extend(footer)
    return "\n".join(lines)


def plain_name(user_id: Any = None, name: Any = None) -> str:
    """Display name for operational logs: never a mention, never a Slack user id suffix."""
    display = _clean_str(name)
    if display:
        return display
    uid = _clean_str(user_id)
    if uid and not is_slack_user_id(uid):
        return uid
    return "PAX"


def _reaction_names(reaction: str | Sequence[str] | None) -> list[str]:
    if not reaction:
        return []
    if isinstance(reaction, str):
        names = [reaction]
    else:
        names = [str(r) for r in reaction]
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = str(raw).strip().strip(":")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _apply_reactions(client: WebClient, channel: str, ts: str, names: list[str]) -> None:
    """Best-effort reactions. Isolated per emoji; never raises; never retries the post."""
    for name in names:
        try:
            client.reactions_add(channel=channel, name=name, timestamp=ts)
        except SlackApiError as e:
            err = None
            try:
                err = e.response.get("error") if e.response is not None else None
            except Exception:
                err = None
            logging.info(
                "reaction %s skipped channel=%s error=%s",
                name,
                channel,
                err or e,
            )
        except Exception:
            logging.debug("reaction %s failed channel=%s", name, channel, exc_info=True)


def post_message(
    client: WebClient,
    channel: str,
    text: str,
    *,
    blocks: list | None = None,
    add_reaction: bool = False,
    reaction: str | Sequence[str] | None = "fire",
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
    response = None
    for attempt in range(max_retries):
        try:
            response = client.chat_postMessage(**kwargs)
            break
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
    else:
        if last_error:
            raise last_error
        raise RuntimeError(f"chat_postMessage failed after {max_retries} attempts channel={channel}")
    names = _reaction_names(reaction) if add_reaction else []
    ts = (response or {}).get("ts")
    if names and ts:
        _apply_reactions(client, channel, ts, names)


def post_messages(
    client: WebClient,
    channel: str,
    items: list[tuple],
    *,
    delay_s: float = 0.4,
    add_reaction_first: bool = False,
    add_reaction: bool = False,
    reaction: str | Sequence[str] | None = "fire",
) -> None:
    """Post successive messages with a pause between them; retries 429s more than once.

    Each item is ``(text, blocks)`` or ``(text, blocks, reactions)``. A 3-tuple's
    reactions list is applied to that message only and does not re-enter the post retry.
    """
    for i, item in enumerate(items):
        if isinstance(item, tuple):
            text = item[0]
            blocks = item[1] if len(item) > 1 else None
            item_reactions = item[2] if len(item) > 2 else None
        else:
            text, blocks, item_reactions = str(item), None, None
        if i:
            time.sleep(delay_s)
        if item_reactions is not None:
            names = _reaction_names(item_reactions)
            post_message(
                client,
                channel,
                text,
                blocks=blocks,
                add_reaction=bool(names),
                reaction=names or reaction,
            )
            continue
        post_message(
            client,
            channel,
            text,
            blocks=blocks,
            add_reaction=add_reaction or (add_reaction_first and i == 0),
            reaction=reaction,
        )


def open_dm_channel(client: WebClient, user_id: str) -> str:
    resp = client.conversations_open(users=user_id)
    return resp["channel"]["id"]


DEFAULT_LOG_CHANNEL = "paxminer_logs"


def resolve_log_channel(region: dict | None = None, *, channel: str | None = None) -> str:
    """Stored Slack channel ID, else an explicit override, else the #paxminer_logs name."""
    if channel and str(channel).strip() and str(channel).strip() != DEFAULT_LOG_CHANNEL:
        return str(channel).strip()
    stored = ((region or {}).get("log_channel") or "").strip()
    if stored:
        return stored
    if channel and str(channel).strip():
        return str(channel).strip()
    return DEFAULT_LOG_CHANNEL


def post_log(
    client: WebClient,
    text: str,
    *,
    blocks: list | None = None,
    channel: str | None = None,
    region: dict | None = None,
) -> None:
    """Best-effort operational log to the region's configured log channel. Never raises."""
    dest = resolve_log_channel(region, channel=channel)
    try:
        post_message(client, dest, text, blocks=blocks)
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
