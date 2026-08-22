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

# Shortlist of standard emoji that suit an award, filtered against what the
# workspace actually has so a wrong name here drops out instead of breaking the
# view. Deliberately generous but nowhere near complete: the full ~1,900 fits
# inside Slack's limits and renders fine on desktop, but freezes the modal on
# mobile, so the list is sized for the smaller client. Workspace emoji are never
# filtered — those are the ones a region actually wants.
#
# About two dozen entries below never survive the filter, and that is fine.
# `emoji.list` categories return *canonical* names only, so valid aliases like
# `punch`, `+1`, and `rofl` are dropped even though reactions.add accepts them.
# Being conservative here means the picker only ever offers a name that is
# certain to render. Do not "fix" the drops by loosening the filter.
AWARD_EMOJI_CANDIDATES = (
    # Awards and milestones
    "trophy", "medal", "sports_medal", "first_place_medal", "second_place_medal",
    "third_place_medal", "military_medal", "crown", "star", "star2", "stars",
    "sparkles", "dizzy", "100", "checkered_flag", "dart", "gem", "ribbon",
    "rosette", "tada", "confetti_ball", "balloon", "partying_face", "gift",
    # Effort
    "muscle", "mechanical_arm", "weight_lifter", "runner", "running", "walking",
    "hiking_boot", "athletic_shoe", "bike", "bicyclist", "mountain_bicyclist",
    "swimmer", "rowboat", "person_climbing", "skier", "snowboarder", "surfer",
    "boxing_glove", "martial_arts_uniform", "wrestlers", "running_shirt_with_sash",
    # Hands
    "clap", "raised_hands", "thumbsup", "+1", "thumbsdown", "punch", "facepunch",
    "fist", "raised_fist", "handshake", "ok_hand", "pray", "point_up", "v",
    "metal", "call_me_hand", "wave", "saluting_face", "vulcan_salute",
    # Faces
    "grinning", "smile", "smiley", "laughing", "joy", "rofl", "sunglasses",
    "star-struck", "flushed", "cold_face", "hot_face", "sweat_smile", "triumph",
    "exploding_head", "nerd_face", "face_with_monocle", "hugging_face",
    # Energy
    "fire", "boom", "zap", "comet", "rocket", "dash", "cyclone", "sparkler",
    "firecracker", "high_brightness", "wind_blowing_face",
    # Gloom and outdoors
    "sunny", "sunrise", "sunrise_over_mountains", "city_sunrise", "partly_sunny",
    "cloud", "rain_cloud", "snowflake", "snowman", "thermometer", "umbrella",
    "umbrella_with_rain_drops", "mountain", "national_park", "camping",
    "evergreen_tree", "deciduous_tree", "sunflower", "ocean", "droplet",
    "sweat_drops", "milky_way", "night_with_stars", "first_quarter_moon",
    # Mascots
    "goat", "gorilla", "bear", "eagle", "lion_face", "tiger", "wolf", "horse",
    "ox", "ram", "boar", "shark", "dolphin", "whale", "monkey", "dog", "cat",
    "rooster", "snake", "turtle", "rabbit", "penguin", "bee", "ant",
    # Objects
    "alarm_clock", "stopwatch", "timer_clock", "watch", "hourglass", "calendar",
    "chart_with_upwards_trend", "bar_chart", "clipboard", "memo", "pushpin",
    "key", "lock", "hammer", "wrench", "toolbox", "shield", "crossed_swords",
    "dagger", "axe", "anchor", "flashlight", "bulb", "moneybag", "package",
    "bookmark", "trophy", "wastebasket", "mega", "loudspeaker", "bell",
    # Fuel
    "beer", "beers", "coffee", "tea", "cup_with_straw", "potable_water",
    "doughnut", "pizza", "hamburger", "taco", "bacon", "egg", "apple", "banana",
    "watermelon", "cookie", "cake", "birthday", "icecream", "popcorn", "pretzel",
    "meat_on_bone", "poultry_leg",
    # Symbols
    "white_check_mark", "heavy_check_mark", "ballot_box_with_check", "x",
    "heavy_plus_sign", "arrow_up", "arrow_double_up", "warning", "exclamation",
    "question", "heart", "orange_heart", "yellow_heart", "green_heart",
    "blue_heart", "purple_heart", "black_heart", "sparkling_heart", "heartpulse",
    "eyes", "brain", "skull", "ghost", "alien", "robot_face", "clown_face",
    "japanese_ogre", "moyai", "tophat", "billed_cap", "school_satchel", "shoe",
)

# Kept for the case where emoji.list is unavailable and nothing can be filtered.
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
    """Options for the award-reaction select.

    Every workspace emoji, plus the shortlist of standard ones that suit an
    award. **Not the whole standard set** — all ~1,900 fits inside Slack's
    documented limits and works on desktop, but freezes the modal on mobile, so
    the count is kept low enough for a phone.

    ``stored`` is unioned in when missing. A ``static_select`` requires
    ``initial_option`` to match an option that exists, so an emoji deleted from
    the workspace would otherwise make the view invalid rather than merely
    unselected — the round-trip hole 5f closed for activity types, sharper here.
    """
    stored_name = normalize_emoji_name(stored)
    available: set[str] = set()
    for _, names in categories or []:
        for name in names:
            clean = normalize_emoji_name(name)
            if clean:
                available.add(clean)

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

    # Without a category list there is nothing to filter against, so fall back
    # to the small hardcoded set rather than offering nothing.
    shortlist = AWARD_EMOJI_CANDIDATES if available else CURATED_AWARD_EMOJI
    standard = []
    for name in shortlist:
        clean = normalize_emoji_name(name)
        if clean and clean not in seen and (not available or clean in available):
            seen.add(clean)
            standard.append(clean)
    if standard:
        groups.extend(_chunk("Standard", standard))

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
