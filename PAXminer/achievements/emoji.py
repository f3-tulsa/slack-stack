"""Per-achievement reaction emoji. Cosmetic and unversioned.

The field is a plain text input: type a name (``fire``, ``:fire:``) or paste the
character from your keyboard (🔥). Block Kit has no emoji picker, and the
``external_select`` approximation we tried first needed a round trip per
keystroke inside a 3-second budget and hung on mobile once the option payload
grew. A text input also gets the native mobile emoji keyboard for free.

Validation happens on save. ``emoji.list`` is the authority for what this
workspace accepts — it covers the standard set and the custom Slackmojis — so a
name is checked against it rather than against anything bundled here.

Characters are harder. ``reactions.add`` wants a name like ``thumbsup``, and
there is no API that maps 👍 to it. Slack's short names are their own vocabulary
(``muscle`` not ``flexed_biceps``, ``tada`` not ``party_popper``, ``100`` not
``hundred_points``), so they cannot be derived from Unicode names. The table
below covers the characters people reach for on an award; anything outside it
gets an error asking for the name instead.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

LOG = logging.getLogger(__name__)

MAX_EMOJI_NAME = 64
_EMOJI_TTL_S = 300.0
_EMOJI_CACHE: dict[str, tuple[float, set[str]]] = {}

# A Slack short name: letters, digits, underscore, dash, plus. No colons.
_NAME_RE = re.compile(r"^[a-z0-9_+-]+$")

# Unicode noise to drop before lookup: variation selectors, skin tones, ZWJ.
_MODIFIERS = re.compile("[\ufe0e\ufe0f\U0001F3FB-\U0001F3FF]")

# Fallback name set when emoji.list is unavailable, so the modal still saves.
CURATED_AWARD_EMOJI = (
    "fire", "trophy", "medal", "first_place_medal", "crown", "star", "star2",
    "sparkles", "100", "clap", "raised_hands", "muscle", "punch", "thumbsup",
    "tada", "confetti_ball", "bell", "mega", "eyes", "boom", "zap", "rocket",
    "dart", "weight_lifter", "runner", "sunny", "gem", "tophat", "handshake",
)

# Character -> Slack short name. Resolved names are still checked against
# emoji.list, so a wrong entry here surfaces as "not supported" rather than a
# reaction that silently fails at award time.
EMOJI_CHAR_TO_NAME = {
    "🔥": "fire", "💪": "muscle", "🏆": "trophy", "🏅": "medal",
    "🥇": "first_place_medal", "🥈": "second_place_medal", "🥉": "third_place_medal",
    "🎖": "military_medal", "👑": "crown", "⭐": "star", "🌟": "star2",
    "✨": "sparkles", "💯": "100", "👏": "clap", "🙌": "raised_hands",
    "👍": "thumbsup", "👎": "thumbsdown", "👊": "punch", "✊": "fist",
    "🤝": "handshake", "👌": "ok_hand", "🤙": "call_me_hand", "🫡": "saluting_face",
    "🙏": "pray", "🤘": "metal", "🎉": "tada", "🎊": "confetti_ball",
    "🎈": "balloon", "🔔": "bell", "📣": "mega", "📢": "loudspeaker",
    "👀": "eyes", "❤": "heart", "💥": "boom", "⚡": "zap", "🚀": "rocket",
    "🎯": "dart", "🏁": "checkered_flag", "🏃": "runner", "🏋": "weight_lifter",
    "🚴": "bicyclist", "🏊": "swimmer", "🧗": "person_climbing",
    "☀": "sunny", "🌈": "rainbow", "🌅": "sunrise", "🌄": "sunrise_over_mountains",
    "💎": "gem", "🎩": "tophat", "😀": "grinning", "😂": "joy", "😎": "sunglasses",
    "🥳": "partying_face", "🥶": "cold_face", "🥵": "hot_face", "💦": "sweat_drops",
    "🦾": "mechanical_arm", "🐐": "goat", "🦍": "gorilla", "🐻": "bear",
    "🦅": "eagle", "🍺": "beer", "🍻": "beers", "☕": "coffee",
    "⛰": "mountain", "🥾": "hiking_boot", "⏰": "alarm_clock",
    "📈": "chart_with_upwards_trend", "✅": "white_check_mark", "🛡": "shield",
    "⚔": "crossed_swords", "🗿": "moyai", "⚽": "soccer", "🏈": "football",
    "🏀": "basketball", "⚾": "baseball",
}


def clear_emoji_cache() -> None:
    _EMOJI_CACHE.clear()


def normalize_emoji_name(raw: str | None) -> str | None:
    """Bare stored name, or None. Accepts ``fire`` and ``:fire:`` alike."""
    if raw is None:
        return None
    name = str(raw).strip().strip(":").strip()
    if not name:
        return None
    return name[:MAX_EMOJI_NAME]


def load_valid_emoji_names(client: Any, *, team_id: str = "") -> set[str]:
    """Every emoji name this workspace accepts: custom plus Slack's standard set.

    Empty set means the lookup failed and the caller should not treat a name as
    invalid just because it could not be checked.
    """
    key = (team_id or "").strip() or "default"
    now = time.time()
    hit = _EMOJI_CACHE.get(key)
    if hit and now - hit[0] < _EMOJI_TTL_S:
        return set(hit[1])
    names: set[str] = set()
    if client is None:
        LOG.warning("emoji.list skipped team=%s: no Slack client", key)
        return names
    try:
        resp = client.emoji_list(include_categories=True) or {}
        names = {str(n) for n in (resp.get("emoji") or {}) if n}
        for category in resp.get("categories") or []:
            for name in (category or {}).get("emoji_names") or []:
                if name:
                    names.add(str(name))
        LOG.info("emoji.list team=%s names=%s", key, len(names))
    except Exception as exc:
        err = getattr(getattr(exc, "response", None), "get", lambda _k: None)("error")
        LOG.warning("emoji.list failed team=%s error=%s", key, err or exc, exc_info=True)
        return set()
    _EMOJI_CACHE[key] = (now, names)
    return set(names)


def resolve_emoji_input(
    raw: str | None, *, valid_names: set[str] | None = None
) -> tuple[str | None, str | None]:
    """Turn what the operator typed into a Slack name.

    Returns ``(name, error)``. Both None means the field was left empty, which
    is valid — the award just gets the default reaction.

    ``valid_names`` comes from :func:`load_valid_emoji_names`. When it is empty
    the check degrades to "looks like a name" rather than blocking the save,
    because a Slack API hiccup should not stop someone editing an achievement.
    """
    text = str(raw or "").strip()
    if not text:
        return None, None

    def _check(name: str) -> tuple[str | None, str | None]:
        if len(name) > MAX_EMOJI_NAME:
            return None, f"Emoji name is too long (max {MAX_EMOJI_NAME} characters)."
        if valid_names and name not in valid_names:
            return None, (
                f"`:{name}:` is not an emoji in this workspace. "
                "Check the spelling, or add it in Slack first."
            )
        return name, None

    # A name, with or without colons.
    candidate = text.strip(":").strip().lower()
    if _NAME_RE.match(candidate):
        return _check(candidate)

    # A character pasted from the keyboard.
    stripped = _MODIFIERS.sub("", text)
    if stripped in EMOJI_CHAR_TO_NAME:
        return _check(EMOJI_CHAR_TO_NAME[stripped])

    if len(stripped) <= 2 and not stripped.isascii():
        return None, (
            f"{text} is not one I can look up. Type its name instead, "
            "like `fire` — Slack's name is often not what the picture is called."
        )
    return None, (
        "Enter a single emoji name like `fire` or `:fire:`, "
        "or one emoji character."
    )
