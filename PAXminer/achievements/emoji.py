"""Per-achievement reaction emoji picker. Cosmetic and unversioned.

Block Kit has no emoji picker element, so the modal uses an ``external_select``
whose option labels render the emoji (a ``plain_text`` object with ``emoji:
true`` turns ``:fire:`` into the image). External select means Slack sends the
operator's keystrokes to an options handler, so the whole workspace emoji set is
searchable instead of being truncated to the 100 options a static select allows.

``emoji.list`` with ``include_categories`` returns both halves of that set: the
workspace Slackmojis and Slack's own standard Unicode emoji names. No bundled
emoji dataset to drift out of date.
"""

from __future__ import annotations

import logging
import time
from typing import Any

LOG = logging.getLogger(__name__)

NONE_EMOJI_VALUE = "_none"
MAX_EMOJI_OPTIONS = 100
_EMOJI_TTL_S = 300.0
_EMOJI_CACHE: dict[str, tuple[float, list[str], list[str]]] = {}

# Shown before the operator types anything, so the menu opens on something useful.
CURATED_AWARD_EMOJI = (
    "fire",
    "trophy",
    "medal",
    "sports_medal",
    "first_place_medal",
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
    "bell",
    "mega",
    "eyes",
    "boom",
    "zap",
    "rocket",
    "dart",
    "weight_lifter",
    "running",
    "sunny",
    "gem",
    "tophat",
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


def none_option() -> dict:
    return {"text": {"type": "plain_text", "text": "None"}, "value": NONE_EMOJI_VALUE}


def load_emoji_names(client: Any, *, team_id: str = "") -> tuple[list[str], list[str]]:
    """``(custom, standard)`` emoji names for the workspace. Empty on failure.

    Cached per team: the modal reopens constantly via Edit and Duplicate, and
    ``emoji.list`` is Tier 2.
    """
    key = (team_id or "").strip() or "default"
    now = time.time()
    hit = _EMOJI_CACHE.get(key)
    if hit and now - hit[0] < _EMOJI_TTL_S:
        return list(hit[1]), list(hit[2])
    custom: list[str] = []
    standard: list[str] = []
    if client is not None:
        try:
            resp = client.emoji_list(include_categories=True) or {}
            custom = sorted(str(n) for n in (resp.get("emoji") or {}) if n)
            seen: set[str] = set()
            for category in resp.get("categories") or []:
                for name in (category or {}).get("emoji_names") or []:
                    text = str(name)
                    if text and text not in seen:
                        seen.add(text)
                        standard.append(text)
            LOG.info(
                "emoji.list team=%s custom=%s standard=%s", key, len(custom), len(standard)
            )
        except Exception as exc:
            # Warning, not debug: a silent fallback here looks identical to
            # "the workspace has no custom emoji", which is not a thing.
            err = getattr(getattr(exc, "response", None), "get", lambda _k: None)("error")
            LOG.warning("emoji.list failed team=%s error=%s", key, err or exc, exc_info=True)
            custom, standard = [], []
    else:
        LOG.warning("emoji.list skipped team=%s: no Slack client", key)
    if not standard:
        standard = list(CURATED_AWARD_EMOJI)
    _EMOJI_CACHE[key] = (now, custom, standard)
    return list(custom), list(standard)


def list_custom_emoji(client: Any, *, team_id: str = "") -> list[str]:
    """Workspace Slackmojis only. Kept for callers that do not need the standard set."""
    return load_emoji_names(client, team_id=team_id)[0]


def search_emoji_options(
    query: str | None,
    *,
    custom: list[str] | None = None,
    standard: list[str] | None = None,
    stored: str | None = None,
) -> list[dict]:
    """Options for the picker, filtered by what the operator typed.

    Ordering puts the closest matches first: names that start with the query,
    then names that merely contain it, with workspace emoji ahead of standard
    ones so a region's own Slackmojis are easy to reach.
    """
    text = (query or "").strip().strip(":").lower()
    stored_name = normalize_emoji_name(stored)
    pool: list[str] = []
    seen: set[str] = set()
    for name in [*(custom or []), *(standard or [])]:
        clean = normalize_emoji_name(name)
        if clean and clean not in seen:
            seen.add(clean)
            pool.append(clean)

    if not text:
        ordered = [n for n in CURATED_AWARD_EMOJI if n in seen]
        ordered += [n for n in pool if n not in set(ordered)]
    else:
        starts = [n for n in pool if n.lower().startswith(text)]
        contains = [n for n in pool if text in n.lower() and not n.lower().startswith(text)]
        ordered = starts + contains

    if stored_name and stored_name not in ordered:
        ordered.insert(0, stored_name)

    room = MAX_EMOJI_OPTIONS - 1  # None occupies one slot
    return [none_option(), *[emoji_option(n) for n in ordered[:room]]]


def emoji_select_options(
    custom_names: list[str] | None = None,
    *,
    stored: str | None = None,
) -> list[dict]:
    """Unfiltered option list. Used by the options handler's empty-query response."""
    return search_emoji_options(None, custom=custom_names, stored=stored)


def emoji_select_element(action_id: str, stored: str | None = None) -> dict:
    """Searchable picker. Options come from the options handler, not the view.

    ``initial_option`` is built from the stored name rather than looked up, so an
    emoji that was deleted from the workspace still round-trips a save instead of
    being silently cleared (the 5f f6 rule, applied here).
    """
    stored_name = normalize_emoji_name(stored)
    element: dict = {
        "type": "external_select",
        "action_id": action_id,
        "min_query_length": 0,
        "placeholder": {"type": "plain_text", "text": "Search emoji", "emoji": True},
    }
    if stored_name:
        element["initial_option"] = emoji_option(stored_name)
    return element
