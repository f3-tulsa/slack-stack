"""Shared Slack helpers for PAXMiner Kotter and achievements."""

from __future__ import annotations

import logging
import os
import ssl
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler


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


def ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = ["th", "st", "nd", "rd", "th"][min(n % 10, 4)]
    return suffix


def post_message(
    client: WebClient,
    channel: str,
    text: str,
    *,
    blocks: list | None = None,
    add_reaction: bool = False,
    reaction: str = "fire",
) -> None:
    kwargs: dict = {"channel": channel, "text": text, "link_names": True}
    if blocks:
        kwargs["blocks"] = blocks
    try:
        response = client.chat_postMessage(**kwargs)
        if add_reaction and response.get("ts"):
            client.reactions_add(channel=channel, name=reaction, timestamp=response["ts"])
    except SlackApiError as e:
        if e.response.status_code == 429:
            delay = int(e.response.headers.get("Retry-After", "1"))
            logging.info("Slack rate limit; sleeping %ss", delay)
            time.sleep(delay)
            response = client.chat_postMessage(**kwargs)
            if add_reaction and response.get("ts"):
                client.reactions_add(channel=channel, name=reaction, timestamp=response["ts"])
        elif e.response.get("error") == "not_in_channel":
            try:
                client.conversations_join(channel=channel)
                post_message(
                    client,
                    channel,
                    text,
                    blocks=blocks,
                    add_reaction=add_reaction,
                    reaction=reaction,
                )
            except Exception:
                logging.exception("Failed to join/post channel=%s", channel)
        else:
            raise


def open_dm_channel(client: WebClient, user_id: str) -> str:
    resp = client.conversations_open(users=user_id)
    return resp["channel"]["id"]
