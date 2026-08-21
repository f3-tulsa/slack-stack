"""Per-achievement reaction emoji picker. Cosmetic and unversioned."""

from __future__ import annotations

import logging
import time
from typing import Any

LOG = logging.getLogger(__name__)

NONE_EMOJI_VALUE = "_none"
MAX_EMOJI_OPTIONS = 100
_EMOJI_TTL_S = 60.0
_EMOJI_CACHE: dict[str, tuple[float, list[str]]] = {}

# Award-flavored standard emoji. Option labels render via Slack's :name: conversion.
CURATED_AWARD_EMOJI = (
    "fire",
    "trophy",
    "medal",
    "sports_medal",
    "first_place_medal",
    "second_place_medal",
    "third_place_medal",
    "crown",
    "star",
    "star2",
    "sparkles",
    "100",
    "clap",
    "raised_hands",
    "muscle",
    "punch",
    "thumbsup",
    "tada",
    "confetti_ball",
    "balloon",
    "bell",
    "mega",
    "loudspeaker",
    "eyes",
    "heart",
    "boom",
    "zap",
    "rocket",
    "checkered_flag",
    "dart",
    "golf",
    "weight_lifter",
    "running",
    "dash",
    "sunny",
    "rainbow",
    "gem",
    "tophat",
    "saluting_face",
    "pray",
    "ok_hand",
    "handshake",
)


def clear_emoji_cache() -> None:
    _EMOJI_CACHE.clear()


def normalize_emoji_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    name = str(raw).strip().strip(":")
    if not name or name == NONE_EMOJI_VALUE:
        return None
    return name[:64]


def emoji_option(name: str) -> dict:
    label = f":{name}: {name}"
    return {
        "text": {"type": "plain_text", "text": label[:75], "emoji": True},
        "value": name[:75],
    }


def list_custom_emoji(client: Any, *, team_id: str = "") -> list[str]:
    """Workspace Slackmojis from emoji.list. Empty on failure. Cached per team."""
    key = (team_id or "").strip() or "default"
    now = time.time()
    hit = _EMOJI_CACHE.get(key)
    if hit and now - hit[0] < _EMOJI_TTL_S:
        return list(hit[1])
    names: list[str] = []
    if client is None:
        _EMOJI_CACHE[key] = (now, names)
        return names
    try:
        resp = client.emoji_list()
        custom = (resp or {}).get("emoji") or {}
        names = sorted(str(n) for n in custom if n)
    except Exception:
        LOG.debug("emoji.list failed team=%s", key, exc_info=True)
        names = []
    _EMOJI_CACHE[key] = (now, names)
    return list(names)


def emoji_select_options(
    custom_names: list[str] | None = None,
    *,
    stored: str | None = None,
) -> list[dict]:
    """None + curated standard + workspace Slackmojis, capped at 100. Stored always present."""
    stored_name = normalize_emoji_name(stored)
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(name: str | None) -> None:
        n = normalize_emoji_name(name)
        if not n or n in seen:
            return
        seen.add(n)
        ordered.append(n)

    for name in CURATED_AWARD_EMOJI:
        _add(name)
    for name in custom_names or []:
        _add(name)
    if stored_name and stored_name not in seen:
        ordered.insert(0, stored_name)
        seen.add(stored_name)
    room = MAX_EMOJI_OPTIONS - 1  # None occupies one slot
    if stored_name and stored_name in ordered[room:]:
        ordered = [stored_name, *[n for n in ordered if n != stored_name]]
    ordered = ordered[:room]
    options = [
        {"text": {"type": "plain_text", "text": "None"}, "value": NONE_EMOJI_VALUE},
        *[emoji_option(n) for n in ordered],
    ]
    return options[:MAX_EMOJI_OPTIONS]
