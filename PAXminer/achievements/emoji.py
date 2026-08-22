"""Per-achievement reaction emoji. Cosmetic and unversioned.

The picker is a ``static_select`` whose options are grouped. Block Kit has no
emoji picker element, but a ``plain_text`` option with ``emoji: true`` renders
``:fire:`` as the image, so a select of names doubles as one.

Grouping is what makes the whole set fit. A flat options array caps at 100;
``option_groups`` allows 100 groups of 100, so all ~1,900 names go in at once.

**Static, not external.** ``external_select`` looked like the obvious choice and
was wrong twice over: it needs a round trip to the Lambda on every keystroke
inside a 3-second budget, and the payload big enough to hold every emoji is the
payload that hung the mobile client. A ``static_select`` ships the list inside
the view and Slack filters it **client-side** with no request at all
(``min_query_length`` is an ``external_select`` concept — it is the server
dispatch threshold, and does not apply here).

The list comes from ``emoji.list`` with ``include_categories``, which returns the
workspace's own emoji plus Slack's standard set already grouped by category. No
bundled emoji data to drift out of date.
"""

from __future__ import annotations

import logging
import time
from typing import Any

LOG = logging.getLogger(__name__)

NONE_EMOJI_VALUE = "_none"
MAX_EMOJI_NAME = 64
MAX_OPTIONS_PER_GROUP = 100
MAX_OPTION_GROUPS = 100
WORKSPACE_GROUP_LABEL = "This workspace"
_EMOJI_TTL_S = 300.0
_EMOJI_CACHE: dict[str, tuple[float, list[str], list[tuple[str, list[str]]]]] = {}

# Categories worth reaching first. Anything Slack returns that is not listed
# keeps its place after these; Flags is last because nobody picks one for an
# award and it is 270 options of scrolling.
_CATEGORY_ORDER = (
    "Activities",
    "Smileys & People",
    "Objects",
    "Symbols",
    "Animals & Nature",
    "Food & Drink",
    "Travel & Places",
)
_CATEGORY_LAST = ("Flags", "Component")

# Only used when emoji.list is unavailable, so the modal still opens.
CURATED_AWARD_EMOJI = (
    "fire", "trophy", "medal", "first_place_medal", "crown", "star", "star2",
    "sparkles", "100", "clap", "raised_hands", "muscle", "punch", "thumbsup",
    "tada", "confetti_ball", "bell", "mega", "eyes", "boom", "zap", "rocket",
    "dart", "weight_lifter", "runner", "sunny", "gem", "tophat", "handshake",
)


def clear_emoji_cache() -> None:
    _EMOJI_CACHE.clear()


def normalize_emoji_name(raw: str | None) -> str | None:
    """Bare stored name, or None. Accepts ``fire`` and ``:fire:`` alike."""
    if raw is None:
        return None
    name = str(raw).strip().strip(":").strip()
    if not name or name == NONE_EMOJI_VALUE:
        return None
    return name[:MAX_EMOJI_NAME]


def emoji_option(name: str) -> dict:
    """``:fire: fire`` — the plain_text object renders the image before the name."""
    label = f":{name}: {name}"
    return {
        "text": {"type": "plain_text", "text": label[:75], "emoji": True},
        "value": name[:75],
    }


def load_emoji_catalog(
    client: Any, *, team_id: str = ""
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """``(workspace_emoji, [(category, names), ...])``. Cached per team.

    Slack's own categories are kept rather than flattened; they are what turns
    ~1,900 names into a browsable menu.
    """
    key = (team_id or "").strip() or "default"
    now = time.time()
    hit = _EMOJI_CACHE.get(key)
    if hit and now - hit[0] < _EMOJI_TTL_S:
        return list(hit[1]), [(label, list(names)) for label, names in hit[2]]

    custom: list[str] = []
    categories: list[tuple[str, list[str]]] = []
    if client is None:
        LOG.warning("emoji.list skipped team=%s: no Slack client", key)
    else:
        try:
            resp = client.emoji_list(include_categories=True) or {}
            custom = sorted(str(n) for n in (resp.get("emoji") or {}) if n)
            seen: set[str] = set()
            for category in resp.get("categories") or []:
                label = str((category or {}).get("name") or "Emoji")
                names = []
                for name in (category or {}).get("emoji_names") or []:
                    text = str(name)
                    if text and text not in seen:
                        seen.add(text)
                        names.append(text)
                if names:
                    categories.append((label, names))
            LOG.info(
                "emoji.list team=%s workspace=%s categories=%s standard=%s",
                key, len(custom), len(categories), sum(len(n) for _, n in categories),
            )
        except Exception as exc:
            err = getattr(getattr(exc, "response", None), "get", lambda _k: None)("error")
            LOG.warning("emoji.list failed team=%s error=%s", key, err or exc, exc_info=True)
            custom, categories = [], []

    if not categories:
        categories = [("Awards", list(CURATED_AWARD_EMOJI))]
    _EMOJI_CACHE[key] = (now, custom, categories)
    return list(custom), [(label, list(names)) for label, names in categories]


def _ordered_categories(
    categories: list[tuple[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    by_label = {label: names for label, names in categories}
    ordered = [(l, by_label.pop(l)) for l in _CATEGORY_ORDER if l in by_label]
    last = [(l, by_label.pop(l)) for l in _CATEGORY_LAST if l in by_label]
    return ordered + list(by_label.items()) + last


def _chunk(label: str, names: list[str]) -> list[dict]:
    """One group per 100 names, numbered when a category needs more than one."""
    total = (len(names) + MAX_OPTIONS_PER_GROUP - 1) // MAX_OPTIONS_PER_GROUP
    groups = []
    for i in range(total):
        window = names[i * MAX_OPTIONS_PER_GROUP : (i + 1) * MAX_OPTIONS_PER_GROUP]
        title = label if total == 1 else f"{label} {i + 1}/{total}"
        groups.append(
            {
                "label": {"type": "plain_text", "text": title[:75]},
                "options": [emoji_option(n) for n in window],
            }
        )
    return groups


def emoji_option_groups(
    custom: list[str] | None = None,
    categories: list[tuple[str, list[str]]] | None = None,
    *,
    stored: str | None = None,
) -> list[dict]:
    """Every emoji the workspace can use, grouped for a static_select.

    ``stored`` is unioned in when it is not otherwise present. A ``static_select``
    requires ``initial_option`` to match an option that exists, so an emoji that
    was deleted from the workspace would otherwise make the view invalid and be
    silently cleared on the next save — the same round-trip hole 5f closed for
    activity types.
    """
    stored_name = normalize_emoji_name(stored)
    seen: set[str] = set()
    groups: list[dict] = [
        {
            "label": {"type": "plain_text", "text": "None"},
            "options": [
                {
                    "text": {"type": "plain_text", "text": "No extra reaction"},
                    "value": NONE_EMOJI_VALUE,
                }
            ],
        }
    ]

    workspace = []
    for name in custom or []:
        clean = normalize_emoji_name(name)
        if clean and clean not in seen:
            seen.add(clean)
            workspace.append(clean)
    if workspace:
        groups.extend(_chunk(WORKSPACE_GROUP_LABEL, workspace))

    for label, names in _ordered_categories(list(categories or [])):
        kept = []
        for name in names:
            clean = normalize_emoji_name(name)
            if clean and clean not in seen:
                seen.add(clean)
                kept.append(clean)
        if kept:
            groups.extend(_chunk(label, kept))

    if stored_name and stored_name not in seen:
        groups.insert(1, _chunk("Currently set", [stored_name])[0])

    return groups[:MAX_OPTION_GROUPS]


def initial_emoji_option(groups: list[dict], stored: str | None) -> dict:
    """``initial_option`` for the select, only when the value really is present."""
    stored_name = normalize_emoji_name(stored)
    if not stored_name:
        return {}
    for group in groups:
        for option in group["options"]:
            if option["value"] == stored_name:
                return {"initial_option": option}
    return {}
