#!/usr/bin/env python3
"""Test-only seeder for PAXMiner realistic attendance + goal overlays.

Always loads ``.env.deploy.test`` and hard-fails if the target does not look
like test. Pulls real Slack users and the AO list from QSignups (``qsignups_aos``).

**One-shot (default):** clear prior seed data, write ~180 days of multi-PAX
weekly beatdowns (real Slack users only), optional Kotter overlays, and
threshold-minus-one shapes for a few users. Legacy synthetic users
(``USEEDPAX*`` / ``USEEDFILLER*``) are purged at the start of every seed run.
Run the achievements job (Schedule → Run Now) afterward.

**Interactive (``--interactive``):** walk each user and assign Kotter or one
achievement; overlays join onto the existing calendar when possible.

Usage (from repo root):

  python PAXminer/scripts/seed_test_region.py
  python PAXminer/scripts/seed_test_region.py --yes --days 180 --verify
  python PAXminer/scripts/seed_test_region.py --interactive --schema f3ttown_test
  python PAXminer/scripts/seed_test_region.py --purge-synthetic --yes
  python PAXminer/scripts/seed_test_region.py --verify-only

Not wired into CI or deploy.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

_PAX_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PAX_ROOT.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
if str(_REPO_ROOT / "migration") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "migration"))

LOG = logging.getLogger("seed_test_region")

SEED_SENTINEL = "[SEED]"
SEED_JSON = '{"seed": true}'
# Legacy synthetic IDs from older seeder runs — purge-only; never create new ones.
LEGACY_SYNTHETIC_USER_PREFIX = "USEEDPAX"
LEGACY_FILLER_Q_USER_ID = "USEEDFILLER0XX"
DEPLOY_ENV_FILE = _REPO_ROOT / ".env.deploy.test"

# Mirrors the national PAXMiner attendance_view that migration copies, including
# the Q column and LEFT JOINs — consumers do SELECT * (e.g. PAXcharter.py).
ATTENDANCE_VIEW_DDL = """
CREATE OR REPLACE VIEW `{schema}`.`attendance_view` AS
SELECT
  bd.date AS `Date`,
  ao.ao AS `AO`,
  u.user_name AS `PAX`,
  q.user_name AS `Q`
FROM `{schema}`.`bd_attendance` bd
LEFT JOIN `{schema}`.`aos` ao ON bd.ao_id = ao.channel_id
LEFT JOIN `{schema}`.`users` u ON bd.user_id = u.user_id
LEFT JOIN `{schema}`.`users` q ON bd.q_user_id = q.user_id
ORDER BY bd.date DESC, ao.ao
"""


# ---------------------------------------------------------------------------
# Env + safety guards
# ---------------------------------------------------------------------------


def load_test_env(*, skip: bool = False) -> Path:
    """Always load ``.env.deploy.test``. Raises SystemExit if missing."""
    if skip:
        return DEPLOY_ENV_FILE
    if not DEPLOY_ENV_FILE.exists():
        raise SystemExit(
            f"Missing {DEPLOY_ENV_FILE}. Copy .env.deploy.example to "
            ".env.deploy.test and fill in test credentials."
        )
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise SystemExit(
            "python-dotenv is required: pip install python-dotenv"
        ) from exc
    load_dotenv(DEPLOY_ENV_FILE, override=True)
    LOG.info("Loaded env from %s", DEPLOY_ENV_FILE)
    return DEPLOY_ENV_FILE


def resolve_schemas(schema_arg: str | None) -> tuple[str, str]:
    """Return (regional_schema, registry_schema) with test-only naming."""
    regional = (
        (schema_arg or "").strip()
        or (os.environ.get("PM_REGIONAL_SCHEMA") or "").strip()
        or "f3ttown_test"
    )
    # PM_REGIONAL_SCHEMA may be comma-separated; take the first.
    regional = regional.split(",")[0].strip()
    bare_pm = (os.environ.get("PAXMINER_SCHEMA") or "paxminer").strip()
    # deploy.sh appends _<stage>; accept either bare or already-suffixed.
    if bare_pm.endswith("_test"):
        registry = bare_pm
    elif bare_pm.endswith("_prod") or "prod" in bare_pm.lower():
        raise SystemExit(
            f"Refusing prod registry schema from PAXMINER_SCHEMA={bare_pm!r}"
        )
    else:
        registry = f"{bare_pm}_test"
    return regional, registry


def resolve_qsignups_schema() -> str:
    """QSignups schema for the test stage (holds the curated AO list)."""
    bare = (os.environ.get("QSIGNUPS_SCHEMA") or "qsignups").strip()
    if bare.endswith("_test"):
        return bare
    if "prod" in bare.lower():
        raise SystemExit(f"Refusing prod QSignups schema QSIGNUPS_SCHEMA={bare!r}")
    return f"{bare}_test"


def assert_test_only(regional_schema: str, registry_schema: str) -> None:
    """Hard-fail if schemas look like production."""
    if not regional_schema.endswith("_test"):
        raise SystemExit(
            f"Refusing non-test regional schema {regional_schema!r} "
            "(must end with _test)"
        )
    if "prod" in regional_schema.lower():
        raise SystemExit(f"Refusing regional schema containing 'prod': {regional_schema!r}")
    if "prod" in registry_schema.lower() or not registry_schema.endswith("_test"):
        raise SystemExit(
            f"Refusing non-test registry schema {registry_schema!r} "
            "(must end with _test and not contain 'prod')"
        )


def assert_slack_team(client: Any, expected_team_id: str) -> str:
    """Call auth.test and require team_id == expected (from .env.deploy.test)."""
    expected = (expected_team_id or "").strip()
    if not expected:
        raise SystemExit(
            "F3_REGION_SLACK_TEAM_ID is required in .env.deploy.test "
            "(used to prove we are talking to the test workspace)."
        )
    resp = client.auth_test()
    team_id = resp.get("team_id") or ""
    if team_id != expected:
        raise SystemExit(
            f"Slack auth.test team_id={team_id!r} does not match "
            f"F3_REGION_SLACK_TEAM_ID={expected!r}. Refusing to seed "
            "(wrong workspace / possible prod)."
        )
    LOG.info("Slack team verified team_id=%s team=%s", team_id, resp.get("team"))
    return team_id


# ---------------------------------------------------------------------------
# Slack fetch
# ---------------------------------------------------------------------------


def fetch_workspace_profiles(client: Any) -> list[dict[str, str]]:
    """Return Slack human profiles: user_id, display_name, real_name."""
    from slack_util import MAX_SLACK_PAGES, next_slack_cursor

    profiles: list[dict[str, str]] = []
    cursor = ""
    seen: set[str] = set()
    for _ in range(MAX_SLACK_PAGES):
        kwargs: dict[str, Any] = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.users_list(**kwargs)
        for u in resp.get("members") or []:
            if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT":
                continue
            if u.get("is_app_user"):
                continue
            profile = u.get("profile") or {}
            real_name = str(
                profile.get("real_name") or u.get("real_name") or u.get("name") or u.get("id")
            )
            display_name = str(
                profile.get("display_name")
                or profile.get("real_name")
                or u.get("real_name")
                or u.get("name")
                or u.get("id")
            )
            profiles.append(
                {
                    "user_id": str(u["id"]),
                    "display_name": display_name,
                    "real_name": real_name,
                }
            )
        cursor = next_slack_cursor(resp, seen)
        if not cursor:
            break
    profiles.sort(key=lambda p: p["display_name"].lower())
    return profiles


def fetch_workspace_users(client: Any) -> list[tuple[str, str]]:
    """Return [(display_name, user_id), ...] excluding bots/deleted/Slackbot."""
    return [
        (p["display_name"], p["user_id"]) for p in fetch_workspace_profiles(client)
    ]


def fetch_workspace_channels(client: Any) -> list[tuple[str, str]]:
    """Return [(channel_name, channel_id), ...] for public + member private."""
    from slack_util import MAX_SLACK_PAGES, next_slack_cursor

    channels: list[tuple[str, str]] = []
    cursor = ""
    seen: set[str] = set()
    for _ in range(MAX_SLACK_PAGES):
        kwargs: dict[str, Any] = {
            "types": "public_channel,private_channel",
            "exclude_archived": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_list(**kwargs)
        for c in resp.get("channels") or []:
            if c.get("is_archived"):
                continue
            # Skip private channels the bot is not in (list may still return them).
            if c.get("is_private") and not c.get("is_member"):
                continue
            name = c.get("name") or c.get("id")
            channels.append((str(name), str(c["id"])))
        cursor = next_slack_cursor(resp, seen)
        if not cursor:
            break
    channels.sort(key=lambda x: x[0].lower())
    return channels


# ---------------------------------------------------------------------------
# Pure seed plan
# ---------------------------------------------------------------------------


@dataclass
class BeatdownSpec:
    ao_id: str
    ao_name: str
    bd_date: date
    q_user_id: str
    attendee_ids: list[str]
    backblast: str
    coq_user_id: str | None = None
    fng_count: int = 0


@dataclass
class SeedPlan:
    beatdowns: list[BeatdownSpec] = field(default_factory=list)
    expected_outcome: str = ""
    co_triggered: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    goal_label: str = ""


@dataclass
class CalendarEvent:
    ao_id: str
    ao_name: str
    bd_date: date
    q_user_id: str
    coq_user_id: str | None
    attendee_ids: list[str]
    pax_count: int
    is_seed: bool


@dataclass
class OverlayResult:
    beatdowns_to_upsert: list[BeatdownSpec] = field(default_factory=list)
    attendance_deletes: list[tuple[str, str, date, str]] = field(default_factory=list)
    q_reassigns: list[tuple[str, date, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    goal_label: str = ""
    co_triggered: list[str] = field(default_factory=list)


def min_aos_required(
    goal_type: str,
    achievement: dict | None = None,
    kotter_kind: str | None = None,
) -> int:
    del kotter_kind
    if goal_type == "achievement" and achievement:
        if achievement.get("metric") == "distinct_aos":
            return max(1, int(achievement.get("threshold") or 1))
    return 1


def _dates_in_period(today: date, period: str, n: int) -> list[date]:
    """Return ``n`` distinct dates in a single period bucket <= today."""
    if n < 1:
        return []
    if period == "week":
        cursor = today
        for _ in range(12):
            week_start = cursor - timedelta(days=cursor.weekday())
            week_num = week_start.isocalendar().week
            year = week_start.isocalendar().year
            days = [
                week_start + timedelta(days=i)
                for i in range(7)
                if (week_start + timedelta(days=i)) <= today
                and (week_start + timedelta(days=i)).isocalendar().week == week_num
                and (week_start + timedelta(days=i)).isocalendar().year == year
            ]
            if len(days) >= n:
                return days[:n]
            cursor = week_start - timedelta(days=1)
        raise ValueError(f"Not enough days in any recent ISO week for threshold={n}")
    if period == "month":
        cursor = today.replace(day=1)
        for _ in range(12):
            # last day of this month (or today if current)
            if cursor.month == 12:
                next_month = cursor.replace(year=cursor.year + 1, month=1, day=1)
            else:
                next_month = cursor.replace(month=cursor.month + 1, day=1)
            end = min(today, next_month - timedelta(days=1))
            days: list[date] = []
            d = cursor
            while d <= end:
                days.append(d)
                d += timedelta(days=1)
            if len(days) >= n:
                return days[:n]
            # step to previous month
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        raise ValueError(f"Not enough days in any recent month for threshold={n}")
    # year
    start = today.replace(month=1, day=1)
    days = []
    d = today
    while d >= start and len(days) < n:
        days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    if len(days) < n:
        raise ValueError(
            f"Not enough days in year {today.year} for threshold={n} (have {len(days)})"
        )
    return days


def _midpoint_date(lo: date, hi: date) -> date:
    if hi < lo:
        raise ValueError(f"Empty date window: {lo} .. {hi}")
    return lo + timedelta(days=(hi - lo).days // 2)


def co_triggered_achievements(achievement: dict, catalog: Iterable[dict]) -> list[str]:
    """Names of other seeds that would also grant given the same attendance shape."""
    metric = achievement.get("metric")
    activity = achievement.get("activity")
    period = achievement.get("period")
    threshold = int(achievement.get("threshold") or 0)
    code = achievement.get("code")
    out: list[str] = []
    for other in catalog:
        if other.get("code") == code:
            continue
        if (
            other.get("metric") == metric
            and other.get("activity") == activity
            and other.get("period") == period
            and int(other.get("threshold") or 0) <= threshold
        ):
            out.append(str(other.get("name") or other.get("code")))
    return out


def pick_filler_q(pool: list[tuple[str, str]], exclude: str | set[str]) -> str:
    """Pick a deterministic real user from ``pool`` excluding ``exclude``.

    Returns the first other user by sorted user_id.
    """
    excluded = {exclude} if isinstance(exclude, str) else set(exclude)
    candidates = sorted(uid for _, uid in pool if uid not in excluded)
    if not candidates:
        raise ValueError(
            "No filler Q available in pool (need at least one other real user)"
        )
    return candidates[0]


def _pick_q_from_pool(
    pool: list[tuple[str, str]],
    exclude: set[str],
    *,
    index: int = 0,
) -> str:
    candidates = [uid for _, uid in pool if uid not in exclude]
    if not candidates:
        return pick_filler_q(pool, exclude)
    return candidates[index % len(candidates)]


def _sample_pool_attendees(
    pool: list[tuple[str, str]],
    target_user_id: str,
    q_user_id: str,
    rng: random.Random,
    *,
    min_extra: int = 1,
    max_extra: int = 3,
) -> list[str]:
    """Sample 1–3 extra PAX so beatdowns typically carry 2–4 attendees."""
    others = [uid for _, uid in pool if uid not in (target_user_id, q_user_id)]
    if not others:
        return []
    n = min(rng.randint(min_extra, max_extra), len(others))
    return rng.sample(others, n)


def _ensure_q_in_attendees(q_user_id: str, attendee_ids: list[str]) -> list[str]:
    if q_user_id not in attendee_ids:
        return [q_user_id, *attendee_ids]
    return list(dict.fromkeys(attendee_ids))


def build_pax_pool(real_users: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return real Slack users only (no synthetic PAX)."""
    return list(real_users)


def build_seed_plan(
    *,
    goal_type: str,
    target_user_id: str,
    aos: list[tuple[str, str]],
    thresholds: dict,
    achievement: dict | None = None,
    kotter_kind: str | None = None,
    catalog: list[dict] | None = None,
    today: date | None = None,
    filler_q_id: str | None = None,
    pool: list[tuple[str, str]] | None = None,
) -> SeedPlan:
    """Pure planner: return beatdown/attendance specs for one user goal."""
    today = today or date.today()
    catalog = catalog or []
    if not aos:
        raise ValueError("At least one AO is required")
    needed = min_aos_required(goal_type, achievement, kotter_kind)
    if len(aos) < needed:
        raise ValueError(f"Need at least {needed} AO(s); got {len(aos)}")

    if goal_type == "achievement":
        if not achievement:
            raise ValueError("achievement dict required")
        return _plan_achievement(
            achievement=achievement,
            target_user_id=target_user_id,
            aos=aos,
            today=today,
            filler_q_id=filler_q_id,
            catalog=catalog,
            pool=pool or [],
        )
    if goal_type == "kotter":
        if kotter_kind not in ("mia", "low-q", "never-q"):
            raise ValueError(f"Unknown kotter_kind={kotter_kind!r}")
        return _plan_kotter(
            kind=kotter_kind,
            target_user_id=target_user_id,
            aos=aos,
            thresholds=thresholds,
            today=today,
            filler_q_id=filler_q_id,
            pool=pool or [],
        )
    raise ValueError(f"Unknown goal_type={goal_type!r}")


def _bb(activity: str, ao_name: str, bd_date: date, extra: str = "") -> str:
    if activity == "qsource":
        # Must match qsource_mask in achievements/attendance.py
        return f"{SEED_SENTINEL} qsource q1.1 — {ao_name} on {bd_date.isoformat()} {extra}".strip()
    return f"{SEED_SENTINEL} Backblast — {ao_name} on {bd_date.isoformat()} {extra}".strip()


def _plan_achievement(
    *,
    achievement: dict,
    target_user_id: str,
    aos: list[tuple[str, str]],
    today: date,
    filler_q_id: str | None,
    catalog: list[dict],
    pool: list[tuple[str, str]],
) -> SeedPlan:
    rng = random.Random(42)
    metric = achievement.get("metric") or "posts"
    activity = achievement.get("activity") or "beatdown"
    period = achievement.get("period") or "year"
    threshold = int(achievement.get("threshold") or 1)
    name = str(achievement.get("name") or achievement.get("code") or "achievement")
    plan = SeedPlan(
        goal_label=f"achievement:{achievement.get('code') or name}",
        expected_outcome=f"Grant '{name}' ({metric}/{activity}/{period} >= {threshold})",
        co_triggered=co_triggered_achievements(achievement, catalog),
    )

    def _attendees(q_id: str) -> list[str]:
        base = [target_user_id]
        if pool:
            base.extend(_sample_pool_attendees(pool, target_user_id, q_id, rng))
            return _ensure_q_in_attendees(q_id, base)
        return base

    def _q_for_posts() -> str:
        if pool:
            return _pick_q_from_pool(pool, {target_user_id})
        if filler_q_id:
            return filler_q_id
        raise ValueError("pool or filler_q_id required for posts metrics (no synthetic Q)")

    if metric == "distinct_aos":
        dates = _dates_in_period(today, period, threshold)
        for (ao_name, ao_id), bd_date in zip(aos[:threshold], dates):
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=bd_date,
                    q_user_id=target_user_id,
                    attendee_ids=_attendees(target_user_id),
                    backblast=_bb(activity, ao_name, bd_date),
                )
            )
        plan.notes.append(
            f"Q'd at {threshold} distinct AOs on distinct dates in period={period}"
        )
        return plan

    dates = _dates_in_period(today, period, threshold)
    ao_name, ao_id = aos[0]

    if metric in ("posts", "posts_at_single_ao"):
        q_id = _q_for_posts()
        for bd_date in dates:
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=bd_date,
                    q_user_id=q_id,
                    attendee_ids=_attendees(q_id),
                    backblast=_bb(activity, ao_name, bd_date),
                )
            )
        note_q = q_id if pool else f"Q={filler_q_id}"
        plan.notes.append(f"{threshold} posts as attendee (Q={note_q}) at {ao_name}")
        return plan

    if metric == "qs":
        for bd_date in dates:
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=bd_date,
                    q_user_id=target_user_id,
                    attendee_ids=_attendees(target_user_id),
                    backblast=_bb(activity, ao_name, bd_date),
                )
            )
        plan.notes.append(f"{threshold} Qs at {ao_name} in period={period}")
        return plan

    raise ValueError(f"Unsupported achievement metric={metric!r}")


def _plan_kotter(
    *,
    kind: str,
    target_user_id: str,
    aos: list[tuple[str, str]],
    thresholds: dict,
    today: date,
    filler_q_id: str | None,
    pool: list[tuple[str, str]],
) -> SeedPlan:
    rng = random.Random(42)
    no_post = int(thresholds.get("NO_POST_THRESHOLD") or 2)
    reminder = int(thresholds.get("REMINDER_WEEKS") or 2)
    home_ao_capture = int(thresholds.get("HOME_AO_CAPTURE") or 8)
    no_q_weeks = int(thresholds.get("NO_Q_THRESHOLD_WEEKS") or 4)
    no_q_posts = int(thresholds.get("NO_Q_THRESHOLD_POSTS") or 4)

    ao_name, ao_id = aos[0]
    plan = SeedPlan(goal_label=f"kotter:{kind}")

    def _q_id() -> str:
        if pool:
            return _pick_q_from_pool(pool, {target_user_id})
        if filler_q_id:
            return filler_q_id
        raise ValueError("pool or filler_q_id required for kotter (no synthetic Q)")

    def _attendees(q_id: str) -> list[str]:
        base = [target_user_id]
        if pool:
            base.extend(_sample_pool_attendees(pool, target_user_id, q_id, rng))
            return _ensure_q_in_attendees(q_id, base)
        return base

    def _window(weeks_older: int, weeks_newer: int) -> tuple[date, date]:
        # Match kotter_report: date.between(today - reminder, today - no_post)
        lo = today - timedelta(weeks=weeks_older)
        hi = today - timedelta(weeks=weeks_newer)
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    if kind == "mia":
        lo, hi = _window(reminder, no_post)
        # Also keep within HOME_AO_CAPTURE so home_ao resolves.
        capture_lo = today - timedelta(weeks=home_ao_capture)
        lo = max(lo, capture_lo)
        if hi < lo:
            plan.notes.append(
                f"WARNING: empty MIA window (REMINDER_WEEKS={reminder}, "
                f"NO_POST_THRESHOLD={no_post}, HOME_AO_CAPTURE={home_ao_capture}); "
                "using midpoint of reminder/no_post anyway"
            )
            lo, hi = _window(reminder, no_post)
        bd_date = _midpoint_date(lo, hi) if hi >= lo else today - timedelta(weeks=no_post)
        q_id = _q_id()
        plan.beatdowns.append(
            BeatdownSpec(
                ao_id=ao_id,
                ao_name=ao_name,
                bd_date=bd_date,
                q_user_id=q_id,
                attendee_ids=_attendees(q_id),
                backblast=_bb("beatdown", ao_name, bd_date, "mia"),
            )
        )
        plan.expected_outcome = (
            f"Appear on Kotter MIA list (last post {bd_date.isoformat()})"
        )
        plan.notes.append(f"MIA window {lo} .. {hi}")
        return plan

    if kind == "low-q":
        # Last Q in [today-reminder, today-no_q_posts] (kotter uses NO_Q_THRESHOLD_POSTS as weeks)
        lo, hi = _window(reminder, no_q_posts)
        if hi < lo:
            plan.notes.append(
                f"WARNING: empty low-q window (REMINDER_WEEKS={reminder}, "
                f"NO_Q_THRESHOLD_POSTS={no_q_posts})"
            )
            q_date = today - timedelta(weeks=max(no_q_posts, 1))
        else:
            q_date = _midpoint_date(lo, hi)
        # Recent non-Q post so they are not MIA (after today - no_post)
        recent = today - timedelta(days=2)
        mia_cutoff = today - timedelta(weeks=no_post)
        if recent <= mia_cutoff:
            recent = mia_cutoff + timedelta(days=1)
        if recent > today:
            recent = today
        plan.beatdowns.append(
            BeatdownSpec(
                ao_id=ao_id,
                ao_name=ao_name,
                bd_date=q_date,
                q_user_id=target_user_id,
                attendee_ids=_attendees(target_user_id),
                backblast=_bb("beatdown", ao_name, q_date, "low-q-as-q"),
            )
        )
        if recent != q_date:
            q_id = _q_id()
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=recent,
                    q_user_id=q_id,
                    attendee_ids=_attendees(q_id),
                    backblast=_bb("beatdown", ao_name, recent, "low-q-recent-post"),
                )
            )
        plan.expected_outcome = (
            f"Appear on Kotter low-Q list (last Q {q_date.isoformat()}, "
            f"recent post {recent.isoformat()})"
        )
        plan.notes.append(f"low-q Q window {lo} .. {hi}")
        return plan

    # never-q
    lo, hi = _window(reminder, no_q_weeks)
    if hi < lo:
        plan.notes.append(
            f"WARNING: empty never-q window (REMINDER_WEEKS={reminder}, "
            f"NO_Q_THRESHOLD_WEEKS={no_q_weeks})"
        )
        old_post = today - timedelta(weeks=max(no_q_weeks, 1))
    else:
        old_post = _midpoint_date(lo, hi)
    recent = today - timedelta(days=2)
    mia_cutoff = today - timedelta(weeks=no_post)
    if recent <= mia_cutoff:
        recent = mia_cutoff + timedelta(days=1)
    if recent > today:
        recent = today
    for bd_date, tag in ((old_post, "never-q-old"), (recent, "never-q-recent")):
        if any(b.bd_date == bd_date for b in plan.beatdowns):
            continue
        q_id = _q_id()
        plan.beatdowns.append(
            BeatdownSpec(
                ao_id=ao_id,
                ao_name=ao_name,
                bd_date=bd_date,
                q_user_id=q_id,
                attendee_ids=_attendees(q_id),
                backblast=_bb("beatdown", ao_name, bd_date, tag),
            )
        )
    plan.expected_outcome = (
        f"Appear on Kotter never-Q list (posts {old_post.isoformat()} / "
        f"{recent.isoformat()}, never Q)"
    )
    plan.notes.append(f"never-q post window {lo} .. {hi}")
    return plan


def build_baseline_plan(
    *,
    aos: list[tuple[str, str]],
    pool: list[tuple[str, str]],
    days: int = 180,
    today: date | None = None,
    rng: random.Random | None = None,
) -> SeedPlan:
    """Pure planner: weekly multi-PAX beatdowns per AO."""
    today = today or date.today()
    rng = rng or random.Random(42)
    if not pool:
        raise ValueError("pool is required for baseline")
    plan = SeedPlan(
        goal_label="baseline",
        expected_outcome=f"~{days}d of weekly multi-PAX beatdowns across {len(aos)} AO(s)",
    )
    q_index = 0
    for ao_name, ao_id in aos:
        weekday = hash(ao_id) % 5
        d = today
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        while (today - d).days <= days:
            q_uid = pool[q_index % len(pool)][1]
            q_index += 1
            others = [uid for _, uid in pool if uid != q_uid]
            hi = min(4, len(pool))
            lo = min(2, hi)
            n_attendees = rng.randint(lo, hi) if hi >= 1 else 1
            n_extra = min(n_attendees - 1, len(others))
            extras = rng.sample(others, n_extra) if n_extra > 0 else []
            attendees = _ensure_q_in_attendees(q_uid, [q_uid, *extras])

            activity = "qsource" if rng.random() < 0.10 else "beatdown"
            extra_bb = ""
            if "ruck" in ao_name.lower() or rng.random() < 0.05:
                extra_bb = "ruck"

            coq: str | None = None
            non_q = [u for u in attendees if u != q_uid]
            if non_q and rng.random() < 0.30:
                coq = rng.choice(non_q)

            fng_count = rng.randint(1, 2) if rng.random() < 0.15 else 0

            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=d,
                    q_user_id=q_uid,
                    attendee_ids=attendees,
                    backblast=_bb(activity, ao_name, d, extra_bb),
                    coq_user_id=coq,
                    fng_count=fng_count,
                )
            )
            d -= timedelta(days=7)
    plan.notes.append(f"{len(plan.beatdowns)} weekly beatdown(s) seeded")
    return plan


def _calendar_index(
    calendar: list[CalendarEvent],
) -> dict[tuple[str, date], CalendarEvent]:
    out: dict[tuple[str, date], CalendarEvent] = {}
    for ev in calendar:
        out[(ev.ao_id, ev.bd_date)] = ev
    return out


def _kotter_subtractive_deletes(
    *,
    kind: str,
    target_user_id: str,
    calendar: list[CalendarEvent],
    thresholds: dict,
    today: date,
) -> list[tuple[str, str, date, str]]:
    no_post = int(thresholds.get("NO_POST_THRESHOLD") or 2)
    mia_cutoff = today - timedelta(weeks=no_post)
    deletes: list[tuple[str, str, date, str]] = []
    for ev in calendar:
        if target_user_id not in ev.attendee_ids:
            continue
        if kind == "mia" and ev.bd_date > mia_cutoff:
            deletes.append((target_user_id, ev.ao_id, ev.bd_date, ev.q_user_id))
        elif kind in ("low-q", "never-q") and ev.q_user_id == target_user_id:
            deletes.append((target_user_id, ev.ao_id, ev.bd_date, ev.q_user_id))
    return deletes


def plan_realistic_overlay(
    *,
    calendar: list[CalendarEvent],
    goal_type: str,
    target_user_id: str,
    aos: list[tuple[str, str]],
    pool: list[tuple[str, str]],
    thresholds: dict,
    achievement: dict | None = None,
    kotter_kind: str | None = None,
    catalog: list[dict] | None = None,
    today: date | None = None,
    rng: random.Random | None = None,
) -> OverlayResult:
    """Plan joins/creates/removes against an existing calendar."""
    today = today or date.today()
    ideal = build_seed_plan(
        goal_type=goal_type,
        target_user_id=target_user_id,
        aos=aos,
        thresholds=thresholds,
        achievement=achievement,
        kotter_kind=kotter_kind,
        catalog=catalog,
        today=today,
        pool=pool,
    )
    result = OverlayResult(
        expected_outcome=ideal.expected_outcome,
        goal_label=ideal.goal_label,
        co_triggered=ideal.co_triggered,
        notes=list(ideal.notes),
    )
    cal = _calendar_index(calendar)

    if goal_type == "kotter" and kotter_kind:
        result.attendance_deletes.extend(
            _kotter_subtractive_deletes(
                kind=kotter_kind,
                target_user_id=target_user_id,
                calendar=calendar,
                thresholds=thresholds,
                today=today,
            )
        )

    for spec in ideal.beatdowns:
        existing = cal.get((spec.ao_id, spec.bd_date))
        if existing:
            merged = list(
                dict.fromkeys(existing.attendee_ids + spec.attendee_ids + [target_user_id])
            )
            q_id = existing.q_user_id
            if spec.q_user_id == target_user_id and existing.q_user_id != target_user_id:
                result.q_reassigns.append(
                    (spec.ao_id, spec.bd_date, existing.q_user_id, target_user_id)
                )
                q_id = target_user_id
            merged = _ensure_q_in_attendees(q_id, merged)
            coq = existing.coq_user_id or spec.coq_user_id
            result.beatdowns_to_upsert.append(
                BeatdownSpec(
                    ao_id=spec.ao_id,
                    ao_name=spec.ao_name or existing.ao_name,
                    bd_date=spec.bd_date,
                    q_user_id=q_id,
                    attendee_ids=merged,
                    backblast=spec.backblast,
                    coq_user_id=coq,
                    fng_count=spec.fng_count,
                )
            )
            result.notes.append(
                f"Joined {target_user_id} onto existing {spec.ao_id} {spec.bd_date}"
            )
        else:
            attendees = _ensure_q_in_attendees(spec.q_user_id, spec.attendee_ids)
            result.beatdowns_to_upsert.append(
                BeatdownSpec(
                    ao_id=spec.ao_id,
                    ao_name=spec.ao_name,
                    bd_date=spec.bd_date,
                    q_user_id=spec.q_user_id,
                    attendee_ids=attendees,
                    backblast=spec.backblast,
                    coq_user_id=spec.coq_user_id,
                    fng_count=spec.fng_count,
                )
            )
    return result


def pick_new_q(attendee_ids: list[str], old_q: str, removed_user: str) -> str | None:
    """Pure helper: next Q when ``removed_user`` leaves a beatdown."""
    remaining = [u for u in attendee_ids if u != removed_user]
    return remaining[0] if remaining else None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def connect(schema: str):
    import pymysql

    host = (
        os.environ.get("DATABASE_HOST")
        or os.environ.get("TARGET_HOST")
        or os.environ.get("host")
    )
    user = (
        os.environ.get("DATABASE_USER")
        or os.environ.get("TARGET_USER")
        or os.environ.get("user")
    )
    password = (
        os.environ.get("DATABASE_PASSWORD")
        or os.environ.get("TARGET_PASSWORD")
        or os.environ.get("password")
    )
    if not host or not user or password is None:
        raise SystemExit(
            "Set DATABASE_HOST/USER/PASSWORD in .env.deploy.test before running."
        )
    port = int(
        os.environ.get("DATABASE_PORT")
        or os.environ.get("TARGET_PORT")
        or os.environ.get("port")
        or "4000"
    )
    tls_raw = (
        os.environ.get("DATABASE_TLS_ENABLED")
        or os.environ.get("TARGET_TLS_ENABLED")
        or "true"
    )
    tls = tls_raw.lower() in ("1", "true", "yes")
    LOG.info("Connecting schema=%s host=%s port=%s tls=%s", schema, host, port, tls)
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=schema,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        ssl={"ssl": {}} if tls else None,
    )


def load_region_row(conn, registry_schema: str, regional_schema: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM `{registry_schema}`.`regions` WHERE schema_name=%s LIMIT 1",
            (regional_schema,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"No row in {registry_schema}.regions for schema_name={regional_schema!r}"
        )
    return row


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return cur.fetchone() is not None


# TiDB Cloud app users often lack REFERENCES; seeder DDL omits the FK.
_ACHIEVEMENTS_AWARDED_DDL_NO_FK = """
CREATE TABLE IF NOT EXISTS `{schema}`.`achievements_awarded` (
  `id` int NOT NULL AUTO_INCREMENT,
  `achievement_id` int NOT NULL,
  `pax_id` varchar(255) NOT NULL,
  `date_awarded` date NOT NULL,
  `created` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `achievement_id` (`achievement_id`),
  KEY `pax_id` (`pax_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _view_exists(cur, schema: str, view: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.views
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, view),
    )
    return cur.fetchone() is not None


def _is_permission_denied(exc: Exception) -> bool:
    args = getattr(exc, "args", ())
    if args and args[0] in (1044, 1045, 1142, 1227):
        return True
    return "command denied" in str(exc).lower()


def _try_ddl(cur, sql: str, what: str) -> bool:
    """Run DDL, tolerating the app DB user's missing privileges."""
    try:
        cur.execute(sql)
    except Exception as exc:
        if _is_permission_denied(exc):
            LOG.warning(
                "Cannot create %s as this DB user (%s). Run the migration with "
                "privileged credentials if it is actually missing.",
                what,
                exc,
            )
            return False
        raise
    LOG.info("Created %s", what)
    return True


def load_qsignups_aos(conn, qs_schema: str, team_id: str) -> list[tuple[str, str]]:
    """Curated AO list from QSignups: [(ao_display_name, ao_channel_id), ...].

    QSignups is the source of truth for what is actually an AO; PAXMiner's
    `aos` table tracks every channel it has seen (including paxminer_logs,
    social, etc.), which is too noisy to pick from.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ao_display_name, ao_channel_id
                FROM `{qs_schema}`.`qsignups_aos`
                WHERE team_id = %s AND ao_channel_id IS NOT NULL
                ORDER BY ao_display_name
                """,
                (team_id,),
            )
            rows = cur.fetchall() or []
    except Exception as exc:
        LOG.warning("Could not read %s.qsignups_aos: %s", qs_schema, exc)
        return []
    return [
        (str(r["ao_display_name"] or r["ao_channel_id"]), str(r["ao_channel_id"]))
        for r in rows
    ]


def select_ao_candidates(
    qs_aos: list[tuple[str, str]],
    db_aos: list[tuple[str, str]],
    slack_channels: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], str, list[tuple[str, str]]]:
    """Pick the AO list to offer: QSignups first, then PAXMiner aos, then channels.

    Returns (aos, source, skipped_not_in_slack).
    """
    live = {cid for _, cid in slack_channels}
    if qs_aos:
        picked = [(n, c) for n, c in qs_aos if c in live]
        missing = [(n, c) for n, c in qs_aos if c not in live]
        if picked:
            return picked, "qsignups", missing
    if db_aos:
        picked = [(n, c) for n, c in db_aos if c in live]
        if picked:
            return picked, "paxminer_aos", []
    return list(slack_channels), "slack_channels", []


def ensure_achievement_tables(cur, schema: str) -> None:
    from achievements.achievement_rules import (
        ACHIEVEMENTS_LIST_DDL,
        ACHIEVEMENTS_VIEW_DDL,
        ACHIEVEMENT_SEEDS,
    )

    if not _table_exists(cur, schema, "achievements_list"):
        if not _try_ddl(
            cur, ACHIEVEMENTS_LIST_DDL.format(schema=schema), f"{schema}.achievements_list"
        ):
            raise SystemExit(
                f"{schema}.achievements_list is missing and this DB user cannot "
                "create it. Run the PAXMiner migration first."
            )
    if not _table_exists(cur, schema, "achievements_awarded"):
        # Omit the FOREIGN KEY — app users typically lack REFERENCES.
        if not _try_ddl(
            cur,
            _ACHIEVEMENTS_AWARDED_DDL_NO_FK.format(schema=schema),
            f"{schema}.achievements_awarded",
        ):
            raise SystemExit(
                f"{schema}.achievements_awarded is missing and this DB user cannot "
                "create it. Run the PAXMiner migration first."
            )
    # Views usually already exist; creating them needs CREATE VIEW, which the
    # app user lacks, so only attempt when actually missing.
    for view, ddl in (
        ("achievements_view", ACHIEVEMENTS_VIEW_DDL),
        ("attendance_view", ATTENDANCE_VIEW_DDL),
    ):
        if not _view_exists(cur, schema, view):
            _try_ddl(cur, ddl.format(schema=schema), f"{schema}.{view}")
    for seed in ACHIEVEMENT_SEEDS:
        cur.execute(
            f"SELECT id FROM `{schema}`.`achievements_list` WHERE code=%s",
            (seed["code"],),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                f"""
                UPDATE `{schema}`.`achievements_list`
                SET name=%s, description=%s, verb=%s, metric=%s, activity=%s,
                    period=%s, threshold=%s
                WHERE code=%s
                """,
                (
                    seed["name"],
                    seed["description"],
                    seed["verb"],
                    seed["metric"],
                    seed["activity"],
                    seed["period"],
                    seed["threshold"],
                    seed["code"],
                ),
            )
        else:
            cur.execute(
                f"""
                INSERT INTO `{schema}`.`achievements_list`
                (name, description, verb, code, metric, activity, period, threshold)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    seed["name"],
                    seed["description"],
                    seed["verb"],
                    seed["code"],
                    seed["metric"],
                    seed["activity"],
                    seed["period"],
                    seed["threshold"],
                ),
            )


def load_achievement_catalog(cur, schema: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT id, name, description, verb, code, metric, activity, period, threshold
        FROM `{schema}`.`achievements_list`
        ORDER BY id
        """
    )
    return list(cur.fetchall() or [])


def upsert_user(
    cur,
    schema: str,
    user_id: str,
    name: str | None = None,
    *,
    allowed_ids: set[str] | None = None,
) -> None:
    """Insert user if missing. ``name=None`` preserves existing names on update."""
    if allowed_ids is not None and user_id not in allowed_ids:
        raise ValueError(f"Refusing off-roster user_id={user_id!r}")
    display = name if name is not None else user_id
    email = f"{user_id.lower()}@seed.example"
    set_name = 1 if name is not None else 0
    cur.execute(
        f"""
        INSERT INTO `{schema}`.`users`
        (user_id, user_name, real_name, phone, email, start_date, app, json)
        VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
        ON DUPLICATE KEY UPDATE
          user_name=IF(%s, VALUES(user_name), user_name),
          real_name=IF(%s, VALUES(real_name), real_name),
          app=0,
          email=IF(
            email IS NULL OR email='' OR LOWER(email) IN ('none', 'null'),
            VALUES(email),
            email
          )
        """,
        (
            user_id,
            display,
            display,
            "",
            email,
            (date.today() - timedelta(days=365)).isoformat(),
            "{}",
            set_name,
            set_name,
        ),
    )


def repair_user_names(cur, schema: str, profiles: list[dict[str, str]]) -> int:
    """UPDATE user_name/real_name from live Slack profiles for roster users."""
    updated = 0
    for p in profiles:
        uid = p["user_id"]
        display = p.get("display_name") or p.get("real_name") or uid
        real = p.get("real_name") or p.get("display_name") or uid
        cur.execute(
            f"""
            UPDATE `{schema}`.`users`
            SET user_name=%s, real_name=%s
            WHERE user_id=%s
            """,
            (display, real, uid),
        )
        updated += cur.rowcount
    return updated


def upsert_ao(cur, schema: str, ao_name: str, channel_id: str) -> None:
    cur.execute(
        f"""
        INSERT INTO `{schema}`.`aos` (channel_id, ao, channel_created, archived, backblast)
        VALUES (%s, %s, %s, 0, 1)
        ON DUPLICATE KEY UPDATE ao=VALUES(ao), archived=0, backblast=1
        """,
        (channel_id, ao_name, int(datetime.now().timestamp())),
    )


def load_calendar(cur, schema: str) -> list[CalendarEvent]:
    cur.execute(
        f"""
        SELECT b.ao_id, COALESCE(ao.ao, b.ao_id) AS ao_name, b.bd_date, b.q_user_id,
               b.coq_user_id, b.pax_count, b.backblast, b.json AS bd_json,
               a.user_id
        FROM `{schema}`.`beatdowns` b
        LEFT JOIN `{schema}`.`aos` ao ON ao.channel_id = b.ao_id
        LEFT JOIN `{schema}`.`bd_attendance` a
          ON a.ao_id = b.ao_id AND a.date = b.bd_date AND a.q_user_id = b.q_user_id
        ORDER BY b.bd_date DESC, b.ao_id
        """
    )
    rows = cur.fetchall() or []
    grouped: dict[tuple[str, date, str], CalendarEvent] = {}
    for r in rows:
        bd_date = r["bd_date"]
        if isinstance(bd_date, str):
            bd_date = date.fromisoformat(bd_date[:10])
        key = (r["ao_id"], bd_date, r["q_user_id"])
        is_seed = (
            SEED_SENTINEL in str(r.get("backblast") or "")
            or '"seed"' in str(r.get("bd_json") or "")
        )
        if key not in grouped:
            grouped[key] = CalendarEvent(
                ao_id=r["ao_id"],
                ao_name=str(r.get("ao_name") or r["ao_id"]),
                bd_date=bd_date,
                q_user_id=r["q_user_id"],
                coq_user_id=r.get("coq_user_id"),
                attendee_ids=[],
                pax_count=int(r.get("pax_count") or 0),
                is_seed=is_seed,
            )
        uid = r.get("user_id")
        if uid and uid not in grouped[key].attendee_ids:
            grouped[key].attendee_ids.append(str(uid))
    # Collapse to one event per (ao_id, date) — prefer seed row for overlays
    by_day: dict[tuple[str, date], CalendarEvent] = {}
    for ev in grouped.values():
        k = (ev.ao_id, ev.bd_date)
        prev = by_day.get(k)
        if prev is None or (ev.is_seed and not prev.is_seed):
            by_day[k] = ev
    return list(by_day.values())


def _is_seed_beatdown_clause(alias: str = "b") -> str:
    return (
        f"({alias}.backblast LIKE %s OR JSON_EXTRACT({alias}.json, '$.seed') = true "
        f"OR {alias}.json LIKE %s)"
    )


def clear_all_seed(cur, schema: str) -> dict[str, int]:
    """Remove all seed-tagged beatdowns, attendance, and legacy synthetic awards."""
    counts: dict[str, int] = {}
    seed_like = f"%{SEED_SENTINEL}%"
    seed_json_like = '%"seed": true%'
    synth_like = f"{LEGACY_SYNTHETIC_USER_PREFIX}%"

    cur.execute(
        f"""
        DELETE a FROM `{schema}`.`bd_attendance` a
        JOIN `{schema}`.`beatdowns` b
          ON a.ao_id = b.ao_id AND a.date = b.bd_date AND a.q_user_id = b.q_user_id
        WHERE {_is_seed_beatdown_clause("b")}
           OR a.json LIKE %s
           OR a.user_id LIKE %s
           OR a.user_id = %s
        """,
        (
            seed_like,
            seed_json_like,
            seed_json_like,
            synth_like,
            LEGACY_FILLER_Q_USER_ID,
        ),
    )
    counts["bd_attendance"] = cur.rowcount

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`beatdowns`
        WHERE backblast LIKE %s
           OR JSON_EXTRACT(json, '$.seed') = true
           OR json LIKE %s
           OR q_user_id LIKE %s
           OR q_user_id = %s
        """,
        (seed_like, seed_json_like, synth_like, LEGACY_FILLER_Q_USER_ID),
    )
    counts["beatdowns"] = cur.rowcount

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`achievements_awarded`
        WHERE pax_id LIKE %s OR pax_id = %s
        """,
        (synth_like, LEGACY_FILLER_Q_USER_ID),
    )
    counts["achievements_awarded"] = cur.rowcount

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`users`
        WHERE user_id LIKE %s OR user_id = %s
        """,
        (synth_like, LEGACY_FILLER_Q_USER_ID),
    )
    counts["users"] = cur.rowcount
    return counts


def purge_synthetic(
    cur, schema: str, slack_roster: set[str]
) -> dict[str, Any]:
    """Delete legacy USEEDPAX/USEEDFILLER rows and off-roster humans (app=0).

    FK-safe order: bd_attendance → beatdowns → achievements_awarded → users.
    Prints nothing; caller prints before/after receipt.
    """
    before = count_region_rows(cur, schema)
    synth_like = f"{LEGACY_SYNTHETIC_USER_PREFIX}%"
    filler = LEGACY_FILLER_Q_USER_ID
    deleted: dict[str, int] = {}

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`bd_attendance`
        WHERE user_id LIKE %s OR user_id = %s
           OR q_user_id LIKE %s OR q_user_id = %s
        """,
        (synth_like, filler, synth_like, filler),
    )
    deleted["bd_attendance_synthetic"] = cur.rowcount

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`beatdowns`
        WHERE q_user_id LIKE %s OR q_user_id = %s
           OR coq_user_id LIKE %s OR coq_user_id = %s
        """,
        (synth_like, filler, synth_like, filler),
    )
    deleted["beatdowns_synthetic"] = cur.rowcount

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`achievements_awarded`
        WHERE pax_id LIKE %s OR pax_id = %s
        """,
        (synth_like, filler),
    )
    deleted["achievements_awarded_synthetic"] = cur.rowcount

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`users`
        WHERE user_id LIKE %s OR user_id = %s
        """,
        (synth_like, filler),
    )
    deleted["users_synthetic"] = cur.rowcount

    cur.execute(
        f"""
        SELECT user_id, user_name FROM `{schema}`.`users`
        WHERE COALESCE(app, 0) = 0
        """
    )
    off_roster: list[dict[str, str]] = []
    for r in cur.fetchall() or []:
        uid = str(r["user_id"])
        if uid not in slack_roster:
            off_roster.append(
                {"user_id": uid, "user_name": str(r.get("user_name") or "")}
            )

    off_att = 0
    off_bd = 0
    off_aw = 0
    for row in off_roster:
        uid = row["user_id"]
        cur.execute(
            f"""
            DELETE FROM `{schema}`.`bd_attendance`
            WHERE user_id = %s OR q_user_id = %s
            """,
            (uid, uid),
        )
        off_att += cur.rowcount
        cur.execute(
            f"""
            DELETE FROM `{schema}`.`beatdowns`
            WHERE q_user_id = %s OR coq_user_id = %s
            """,
            (uid, uid),
        )
        off_bd += cur.rowcount
        cur.execute(
            f"DELETE FROM `{schema}`.`achievements_awarded` WHERE pax_id = %s",
            (uid,),
        )
        off_aw += cur.rowcount
        cur.execute(f"DELETE FROM `{schema}`.`users` WHERE user_id = %s", (uid,))
    deleted["bd_attendance_off_roster"] = off_att
    deleted["beatdowns_off_roster"] = off_bd
    deleted["achievements_awarded_off_roster"] = off_aw
    deleted["users_off_roster"] = len(off_roster)

    after = count_region_rows(cur, schema)
    return {
        "before": before,
        "after": after,
        "deleted": deleted,
        "off_roster": off_roster,
    }


def find_synthetic_and_off_roster(
    cur, schema: str, slack_roster: set[str]
) -> dict[str, list[str]]:
    """Report remaining legacy synthetic and off-roster human user_ids."""
    cur.execute(
        f"SELECT user_id, user_name, app FROM `{schema}`.`users`"
    )
    synthetic: list[str] = []
    off_roster: list[str] = []
    for r in cur.fetchall() or []:
        uid = str(r["user_id"])
        if uid.startswith(LEGACY_SYNTHETIC_USER_PREFIX) or uid == LEGACY_FILLER_Q_USER_ID:
            synthetic.append(uid)
        elif int(r.get("app") or 0) == 0 and uid not in slack_roster:
            off_roster.append(uid)
    return {"synthetic": synthetic, "off_roster": off_roster}


def clear_seed_for_user(cur, schema: str, user_id: str) -> dict[str, int]:
    """Safely remove seed rows for one user (reassign Q on shared beatdowns)."""
    counts: dict[str, int] = {}
    seed_like = f"%{SEED_SENTINEL}%"
    seed_json_like = '%"seed": true%'

    cur.execute(
        f"""
        SELECT b.ao_id, b.bd_date, b.q_user_id, b.pax_count
        FROM `{schema}`.`beatdowns` b
        WHERE {_is_seed_beatdown_clause("b")}
          AND (
            b.q_user_id = %s
            OR EXISTS (
              SELECT 1 FROM `{schema}`.`bd_attendance` a
              WHERE a.ao_id = b.ao_id AND a.date = b.bd_date
                AND a.q_user_id = b.q_user_id AND a.user_id = %s
            )
          )
        """,
        (seed_like, seed_json_like, user_id, user_id),
    )
    beatdowns = cur.fetchall() or []

    cur.execute(
        f"""
        DELETE FROM `{schema}`.`bd_attendance`
        WHERE user_id = %s
          AND (
            json LIKE %s
            OR EXISTS (
              SELECT 1 FROM `{schema}`.`beatdowns` b
              WHERE b.ao_id = `{schema}`.`bd_attendance`.ao_id
                AND b.bd_date = `{schema}`.`bd_attendance`.date
                AND b.q_user_id = `{schema}`.`bd_attendance`.q_user_id
                AND (b.backblast LIKE %s OR JSON_EXTRACT(b.json, '$.seed') = true
                     OR b.json LIKE %s)
            )
          )
        """,
        (user_id, seed_json_like, seed_like, seed_json_like),
    )
    counts["bd_attendance"] = cur.rowcount

    for row in beatdowns:
        ao_id = row["ao_id"]
        bd_date = row["bd_date"]
        old_q = row["q_user_id"]
        if isinstance(bd_date, str):
            bd_date = date.fromisoformat(bd_date[:10])

        cur.execute(
            f"""
            SELECT user_id FROM `{schema}`.`bd_attendance`
            WHERE ao_id = %s AND date = %s AND q_user_id = %s
            """,
            (ao_id, bd_date.isoformat(), old_q),
        )
        attendees = [r["user_id"] for r in (cur.fetchall() or [])]

        if old_q == user_id:
            new_q = pick_new_q(attendees, old_q, user_id)
            if new_q:
                cur.execute(
                    f"""
                    UPDATE `{schema}`.`beatdowns`
                    SET q_user_id = %s
                    WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
                    """,
                    (new_q, ao_id, bd_date.isoformat(), old_q),
                )
                cur.execute(
                    f"""
                    UPDATE `{schema}`.`bd_attendance`
                    SET q_user_id = %s
                    WHERE ao_id = %s AND date = %s AND q_user_id = %s
                    """,
                    (new_q, ao_id, bd_date.isoformat(), old_q),
                )
                counts["q_reassigns"] = counts.get("q_reassigns", 0) + 1
            else:
                cur.execute(
                    f"""
                    DELETE FROM `{schema}`.`beatdowns`
                    WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
                    """,
                    (ao_id, bd_date.isoformat(), old_q),
                )
                counts["beatdowns_deleted"] = counts.get("beatdowns_deleted", 0) + 1
                continue

        cur.execute(
            f"""
            SELECT COUNT(*) AS c FROM `{schema}`.`bd_attendance`
            WHERE ao_id = %s AND date = %s AND q_user_id = %s
            """,
            (ao_id, bd_date.isoformat(), old_q),
        )
        pax_count = int(cur.fetchone()["c"])
        if pax_count == 0:
            cur.execute(
                f"""
                DELETE FROM `{schema}`.`beatdowns`
                WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
                """,
                (ao_id, bd_date.isoformat(), old_q),
            )
            counts["beatdowns_deleted"] = counts.get("beatdowns_deleted", 0) + 1
        else:
            cur.execute(
                f"""
                UPDATE `{schema}`.`beatdowns`
                SET pax_count = %s
                WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
                """,
                (pax_count, ao_id, bd_date.isoformat(), old_q),
            )

    cur.execute(
        f"DELETE FROM `{schema}`.`achievements_awarded` WHERE pax_id = %s",
        (user_id,),
    )
    counts["achievements_awarded"] = cur.rowcount
    return counts


def write_beatdowns(
    cur,
    schema: str,
    beatdowns: list[BeatdownSpec],
    *,
    user_names: dict[str, str] | None = None,
    allowed_ids: set[str] | None = None,
) -> dict[str, int]:
    """Insert/update seed-tagged beatdowns and attendance.

    Refuses any user_id not in ``allowed_ids`` when that set is provided.
    """
    user_names = user_names or {}
    beatdowns_n = 0
    attendance_n = 0
    base_ts = int(datetime.now().timestamp() * 1000)
    seen_users: set[str] = set()

    for i, spec in enumerate(beatdowns):
        attendees = _ensure_q_in_attendees(spec.q_user_id, spec.attendee_ids)
        all_ids = {spec.q_user_id, *attendees}
        if spec.coq_user_id:
            all_ids.add(spec.coq_user_id)
        if allowed_ids is not None:
            bad = sorted(uid for uid in all_ids if uid not in allowed_ids)
            if bad:
                raise ValueError(f"Refusing off-roster user_id(s): {bad}")
        ts = str(base_ts + i)
        for uid in all_ids:
            if uid in seen_users:
                continue
            seen_users.add(uid)
            upsert_user(
                cur,
                schema,
                uid,
                user_names.get(uid),
                allowed_ids=allowed_ids,
            )

        cur.execute(
            f"""
            INSERT INTO `{schema}`.`beatdowns`
            (timestamp, ts_edited, ao_id, bd_date, q_user_id, coq_user_id,
             pax_count, backblast, fngs, fng_count, json)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              json=VALUES(json),
              pax_count=VALUES(pax_count),
              backblast=VALUES(backblast),
              q_user_id=VALUES(q_user_id),
              coq_user_id=VALUES(coq_user_id),
              fng_count=VALUES(fng_count)
            """,
            (
                ts,
                spec.ao_id,
                spec.bd_date.isoformat(),
                spec.q_user_id,
                spec.coq_user_id,
                len(attendees),
                spec.backblast,
                "",
                spec.fng_count,
                SEED_JSON,
            ),
        )
        beatdowns_n += 1
        for uid in attendees:
            cur.execute(
                f"""
                INSERT INTO `{schema}`.`bd_attendance`
                (timestamp, ts_edited, user_id, ao_id, date, q_user_id, json)
                VALUES (%s, NULL, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  timestamp=VALUES(timestamp),
                  q_user_id=VALUES(q_user_id),
                  json=VALUES(json)
                """,
                (
                    ts,
                    uid,
                    spec.ao_id,
                    spec.bd_date.isoformat(),
                    spec.q_user_id,
                    SEED_JSON,
                ),
            )
            attendance_n += 1
    return {"beatdowns": beatdowns_n, "bd_attendance": attendance_n}


def apply_overlay_writes(
    cur,
    schema: str,
    overlay: OverlayResult,
    *,
    user_names: dict[str, str] | None = None,
    allowed_ids: set[str] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {"beatdowns": 0, "bd_attendance": 0, "deletes": 0, "reassigns": 0}

    for ao_id, bd_date, old_q, new_q in overlay.q_reassigns:
        d = bd_date.isoformat() if isinstance(bd_date, date) else bd_date
        cur.execute(
            f"""
            UPDATE `{schema}`.`beatdowns`
            SET q_user_id = %s WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
            """,
            (new_q, ao_id, d, old_q),
        )
        cur.execute(
            f"""
            UPDATE `{schema}`.`bd_attendance`
            SET q_user_id = %s WHERE ao_id = %s AND date = %s AND q_user_id = %s
            """,
            (new_q, ao_id, d, old_q),
        )
        counts["reassigns"] += 1

    for user_id, ao_id, bd_date, q_user_id in overlay.attendance_deletes:
        d = bd_date.isoformat() if isinstance(bd_date, date) else bd_date
        cur.execute(
            f"""
            DELETE FROM `{schema}`.`bd_attendance`
            WHERE user_id = %s AND ao_id = %s AND date = %s AND q_user_id = %s
            """,
            (user_id, ao_id, d, q_user_id),
        )
        counts["deletes"] += cur.rowcount
        cur.execute(
            f"""
            SELECT COUNT(*) AS c FROM `{schema}`.`bd_attendance`
            WHERE ao_id = %s AND date = %s AND q_user_id = %s
            """,
            (ao_id, d, q_user_id),
        )
        pax_count = int(cur.fetchone()["c"])
        if pax_count == 0:
            cur.execute(
                f"""
                DELETE FROM `{schema}`.`beatdowns`
                WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
                """,
                (ao_id, d, q_user_id),
            )
        else:
            cur.execute(
                f"""
                UPDATE `{schema}`.`beatdowns`
                SET pax_count = %s
                WHERE ao_id = %s AND bd_date = %s AND q_user_id = %s
                """,
                (pax_count, ao_id, d, q_user_id),
            )

    inserted = write_beatdowns(
        cur,
        schema,
        overlay.beatdowns_to_upsert,
        user_names=user_names,
        allowed_ids=allowed_ids,
    )
    counts["beatdowns"] += inserted["beatdowns"]
    counts["bd_attendance"] += inserted["bd_attendance"]
    return counts


def write_seed_plan(
    cur,
    schema: str,
    plan: SeedPlan,
    *,
    target_user_id: str,
    target_name: str,
    aos: list[tuple[str, str]],
    pool: list[tuple[str, str]] | None = None,
) -> dict[str, int]:
    allowed = {target_user_id}
    names = {target_user_id: target_name}
    if pool:
        allowed.update(uid for _, uid in pool)
        names.update({uid: name for name, uid in pool})
    upsert_user(cur, schema, target_user_id, target_name, allowed_ids=allowed)
    for ao_name, ao_id in aos:
        upsert_ao(cur, schema, ao_name, ao_id)
    return write_beatdowns(
        cur, schema, plan.beatdowns, user_names=names, allowed_ids=allowed
    )


# ---------------------------------------------------------------------------
# Interactive UI
# ---------------------------------------------------------------------------


def _prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except EOFError:
        return ""


def _print_achievement_catalog(catalog: list[dict]) -> None:
    print("\nAchievement catalog:")
    for i, a in enumerate(catalog, 1):
        print(
            f"  {i:2d}. {a['name']}  "
            f"[{a.get('metric')}/{a.get('activity')}/{a.get('period')} "
            f">= {a.get('threshold')}]  ({a.get('code')})"
        )
        if a.get("description"):
            print(f"      {a['description']}")


def _print_ao_list(aos: list[tuple[str, str]]) -> None:
    print("\nAOs:")
    for i, (name, cid) in enumerate(aos, 1):
        print(f"  {i:2d}. {name} ({cid})")


def _ao_names(aos: Iterable[tuple[str, str]]) -> str:
    return ", ".join(name for name, _ in aos)


def _parse_index_list(raw: str, n: int) -> list[int] | None:
    """Parse a 1-based selection into 0-based indexes, or None if invalid.

    Accepts commas and/or spaces as separators and inclusive ranges:
    ``1,3,5``, ``1 3 5``, ``1-5``, ``1-3, 7`` all work.
    """
    raw = raw.strip()
    if not raw:
        return []
    out: list[int] = []
    for part in raw.replace(",", " ").split():
        if "-" in part[1:]:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                return None
            if lo < 1 or hi > n or lo > hi:
                return None
            out.extend(range(lo - 1, hi))
            continue
        try:
            idx = int(part)
        except ValueError:
            return None
        if idx < 1 or idx > n:
            return None
        out.append(idx - 1)
    return out


def pick_aos_simple(
    all_aos: list[tuple[str, str]],
    *,
    minimum: int,
) -> list[tuple[str, str]]:
    if len(all_aos) < minimum:
        raise SystemExit(
            f"Workspace has only {len(all_aos)} AO channel(s); need {minimum}."
        )
    preselect = all_aos[:minimum]
    _print_ao_list(all_aos)
    pre_idx = list(range(1, minimum + 1))
    pre_str = ",".join(str(i) for i in pre_idx)
    print(f"\nPre-selected AO(s): {pre_str} ({_ao_names(preselect)})")
    print(f"Minimum AOs required: {minimum} of {len(all_aos)}")
    print(
        "  Separate numbers with commas or spaces; ranges work too "
        f"(e.g. \"1,3,5\" or \"1 3 5\" or \"1-{minimum}\")."
    )
    while True:
        raw = _prompt(f"Enter to accept, or type AO numbers [min {minimum}]: ")
        if not raw:
            return list(preselect)
        idxs = _parse_index_list(raw, len(all_aos))
        if idxs is None:
            print(
                f"Invalid selection; use numbers 1-{len(all_aos)} separated by "
                'commas or spaces, or a range like "1-5".'
            )
            continue
        seen: set[int] = set()
        chosen: list[tuple[str, str]] = []
        for i in idxs:
            if i not in seen:
                seen.add(i)
                chosen.append(all_aos[i])
        if len(chosen) < minimum:
            print(f"Need at least {minimum} AO(s); got {len(chosen)}.")
            continue
        print(f"  Selected {len(chosen)} AO(s): {_ao_names(chosen)}")
        return chosen


def interactive_user_pass(
    users: list[tuple[str, str]],
    aos: list[tuple[str, str]],
    catalog: list[dict],
    thresholds: dict,
) -> list[dict]:
    """Walk every user; return list of action dicts."""
    actions: list[dict] = []
    print("\n" + "=" * 60)
    print(f"Interactive pass over {len(users)} user(s).")
    print("For each: [k]otter / [a]chievement / [c]lear seed data / [s]kip")
    print("=" * 60)

    for name, uid in users:
        print(f"\n--- {name} ({uid}) ---")
        while True:
            choice = _prompt("[k]otter / [a]chievement / [c]lear / [s]kip: ").lower()
            if choice in ("k", "a", "c", "s", "kotter", "achievement", "clear", "skip"):
                break
            print("Please enter k, a, c, or s.")

        if choice in ("s", "skip"):
            actions.append({"user_id": uid, "name": name, "action": "skip"})
            continue

        if choice in ("c", "clear"):
            actions.append({"user_id": uid, "name": name, "action": "clear"})
            continue

        if choice in ("k", "kotter"):
            while True:
                kind = _prompt("Kotter kind [mia / low-q / never-q]: ").lower()
                if kind in ("mia", "low-q", "never-q", "lowq", "neverq", "never"):
                    break
                print("Enter mia, low-q, or never-q.")
            if kind in ("lowq",):
                kind = "low-q"
            if kind in ("neverq", "never"):
                kind = "never-q"
            minimum = min_aos_required("kotter", kotter_kind=kind)
            chosen_aos = pick_aos_simple(aos, minimum=minimum)
            actions.append(
                {
                    "user_id": uid,
                    "name": name,
                    "action": "seed",
                    "goal_type": "kotter",
                    "kotter_kind": kind,
                    "aos": chosen_aos,
                }
            )
            continue

        # achievement
        _print_achievement_catalog(catalog)
        while True:
            raw = _prompt("Pick ONE achievement by number: ")
            try:
                idx = int(raw)
            except ValueError:
                print("Enter a number from the catalog.")
                continue
            if idx < 1 or idx > len(catalog):
                print(f"Enter 1..{len(catalog)}")
                continue
            achievement = catalog[idx - 1]
            break
        minimum = min_aos_required("achievement", achievement=achievement)
        chosen_aos = pick_aos_simple(aos, minimum=minimum)
        actions.append(
            {
                "user_id": uid,
                "name": name,
                "action": "seed",
                "goal_type": "achievement",
                "achievement": achievement,
                "aos": chosen_aos,
            }
        )

    return actions


def print_region_context(region: dict, regional_schema: str, registry_schema: str) -> None:
    print("\n" + "=" * 60)
    print("Region context (thresholds are used as-is; not modified)")
    print("=" * 60)
    print(f"  regional_schema:     {regional_schema}")
    print(f"  registry_schema:     {registry_schema}")
    print(f"  region:              {region.get('region')}")
    print(f"  send_achievements:   {region.get('send_achievements')}")
    print(f"  achievement_channel: {region.get('achievement_channel')}")
    print(f"  kotter_channel:      {region.get('kotter_channel')}")
    for key in (
        "NO_POST_THRESHOLD",
        "REMINDER_WEEKS",
        "HOME_AO_CAPTURE",
        "NO_Q_THRESHOLD_WEEKS",
        "NO_Q_THRESHOLD_POSTS",
    ):
        print(f"  {key}: {region.get(key)}")


def print_receipt(receipts: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("seed_test_region receipt")
    print("=" * 60)
    for r in receipts:
        print(f"\n  user: {r.get('name')} ({r.get('user_id')})")
        print(f"  action: {r.get('action')}")
        if r.get("goal_label"):
            print(f"  goal: {r['goal_label']}")
        if r.get("aos"):
            print(f"  aos: {r['aos']}")
        if r.get("inserted"):
            print(f"  inserted: {r['inserted']}")
        if r.get("cleared"):
            print(f"  cleared: {r['cleared']}")
        if r.get("expected_outcome"):
            print(f"  expected: {r['expected_outcome']}")
        if r.get("co_triggered"):
            print(f"  co-triggered: {r['co_triggered']}")
        for n in r.get("notes") or []:
            print(f"  note: {n}")
        if r.get("error"):
            print(f"  ERROR: {r['error']}")
    print("\nTo trigger awards/Kotter: Schedule → select item → Run Now, or wait for the daily tick.")
    print("Re-run achievements after clear_all_seed if real-user awards were wiped.")
    print("=" * 60)


def count_region_rows(cur, schema: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for table, sql in (
        ("users", f"SELECT COUNT(*) AS c FROM `{schema}`.`users`"),
        ("beatdowns", f"SELECT COUNT(*) AS c FROM `{schema}`.`beatdowns`"),
        ("bd_attendance", f"SELECT COUNT(*) AS c FROM `{schema}`.`bd_attendance`"),
        (
            "seed_beatdowns",
            f"""
            SELECT COUNT(*) AS c FROM `{schema}`.`beatdowns`
            WHERE backblast LIKE %s OR json LIKE %s
            """,
        ),
    ):
        if table == "seed_beatdowns":
            cur.execute(sql, (f"%{SEED_SENTINEL}%", '%"seed": true%'))
        else:
            cur.execute(sql)
        out[table] = int(cur.fetchone()["c"])
    return out


def verify_region(
    conn,
    schema: str,
    registry_schema: str,
    region: dict,
) -> list[dict]:
    """Run custom-report SQL + destination checks; print summary table."""
    from paxminer_db import read_sql_df
    from schedule_reports import _SOURCE_SQL
    from schedule_runner import resolve_destinations
    from scheduling import resolve_time_window

    rows: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.id, s.destination_type, s.enabled, d.code, d.name,
                   d.report_type, d.source, d.time_window_type, d.window_days,
                   d.window_start, d.window_end, d.metric, d.group_by, d.top_n
            FROM `{registry_schema}`.`region_schedules` s
            JOIN `{registry_schema}`.`region_report_definitions` d
              ON d.id = s.report_definition_id
            WHERE s.schema_name = %s
            ORDER BY d.code
            """,
            (schema,),
        )
        schedules = cur.fetchall() or []

    tz = region.get("timezone") or "America/Chicago"
    for sched in schedules:
        entry: dict[str, Any] = {
            "code": sched.get("code"),
            "report_type": sched.get("report_type"),
            "enabled": bool(sched.get("enabled")),
            "dest_type": sched.get("destination_type"),
        }
        try:
            dests = resolve_destinations(conn, sched)
            entry["destinations"] = len(dests)
        except Exception as exc:
            entry["destinations"] = f"err: {exc}"

        if sched.get("report_type") == "custom_report":
            source = (sched.get("source") or "bd_attendance").strip()
            sql = _SOURCE_SQL.get(source)
            if sql:
                try:
                    start, end = resolve_time_window(sched, timezone_name=tz)
                    df = read_sql_df(
                        conn, sql, params=(start.isoformat(), end.isoformat())
                    )
                    entry["rows"] = len(df)
                except Exception as exc:
                    entry["rows"] = f"err: {exc}"
        rows.append(entry)

    print("\n" + "=" * 60)
    print("verify_region (read-only)")
    print("=" * 60)
    hdr = f"{'code':<22} {'type':<16} {'en':<4} {'dest':<8} {'dest_type':<18} extra"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        extra = ""
        if "rows" in r:
            extra = f"rows={r['rows']}"
        print(
            f"{str(r.get('code','')):<22} "
            f"{str(r.get('report_type','')):<16} "
            f"{str(int(bool(r.get('enabled')))):<4} "
            f"{str(r.get('destinations','')):<8} "
            f"{str(r.get('dest_type','')):<18} "
            f"{extra}"
        )
    print("=" * 60)
    return rows


def _confirm_yes(yes_flag: bool, prompt: str = "Proceed? [y/N]: ") -> bool:
    if yes_flag:
        return True
    ans = _prompt(prompt).lower()
    return ans in ("y", "yes")


def write_baseline(
    cur,
    schema: str,
    plan: SeedPlan,
    pool: list[tuple[str, str]],
    aos: list[tuple[str, str]],
) -> dict[str, int]:
    allowed = {uid for _, uid in pool}
    names = {uid: name for name, uid in pool}
    for name, uid in pool:
        upsert_user(cur, schema, uid, name, allowed_ids=allowed)
    for ao_name, ao_id in aos:
        upsert_ao(cur, schema, ao_name, ao_id)
    return write_beatdowns(
        cur, schema, plan.beatdowns, user_names=names, allowed_ids=allowed
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default=None,
        help="Regional schema (default: PM_REGIONAL_SCHEMA or f3ttown_test)",
    )
    parser.add_argument(
        "--skip-env-load",
        action="store_true",
        help="Do not auto-load .env.deploy.test (env already set)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Walk each user and assign Kotter/achievement overlays",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Days of baseline history (default: 180)",
    )
    parser.add_argument(
        "--kotter",
        default="",
        help="Comma-separated Kotter kinds for first N real users: mia,lowq,noq",
    )
    parser.add_argument(
        "--purge-synthetic",
        action="store_true",
        help="Only purge legacy USEEDPAX/USEEDFILLER and off-roster humans; no seed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; do not write to the database",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run verify_region after seeding (or with --verify-only)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only run verify_region; no seed writes",
    )
    args = parser.parse_args(argv)

    load_test_env(skip=args.skip_env_load)
    regional_schema, registry_schema = resolve_schemas(args.schema)
    assert_test_only(regional_schema, registry_schema)

    conn = connect(regional_schema)
    try:
        region = load_region_row(conn, registry_schema, regional_schema)

        token = (os.environ.get("PM_SLACK_TOKEN") or "").strip()
        if not token:
            raise SystemExit("PM_SLACK_TOKEN is required in .env.deploy.test")
        expected_team = (os.environ.get("F3_REGION_SLACK_TEAM_ID") or "").strip()

        from slack_util import slack_client

        client = slack_client(token)
        assert_slack_team(client, expected_team)

        print("Fetching workspace users and channels…")
        profiles = fetch_workspace_profiles(client)
        users = [(p["display_name"], p["user_id"]) for p in profiles]
        channels = fetch_workspace_channels(client)
        slack_roster = {uid for _, uid in users}
        if not users:
            raise SystemExit("No human users returned from users_list")
        if not channels and not args.purge_synthetic and not args.verify_only:
            raise SystemExit("No channels returned from conversations_list")
        print(f"  users: {len(users)}  channels: {len(channels)}")

        if args.verify_only:
            verify_region(conn, regional_schema, registry_schema, region)
            with conn.cursor() as cur:
                health = find_synthetic_and_off_roster(
                    cur, regional_schema, slack_roster
                )
            print(
                f"\nRoster health: synthetic={len(health['synthetic'])} "
                f"off_roster={len(health['off_roster'])}"
            )
            if health["synthetic"]:
                print(f"  synthetic: {health['synthetic'][:20]}")
            if health["off_roster"]:
                print(f"  off_roster: {health['off_roster'][:20]}")
            return 0

        with conn.cursor() as cur:
            repaired = repair_user_names(cur, regional_schema, profiles)
            if repaired:
                LOG.info("Repaired user_name/real_name for %s roster user(s)", repaired)
        conn.commit()

        if args.purge_synthetic:
            if not _confirm_yes(
                args.yes, "Purge legacy synthetic + off-roster users? [y/N]: "
            ):
                print("Aborted.")
                return 1
            with conn.cursor() as cur:
                result = purge_synthetic(cur, regional_schema, slack_roster)
            conn.commit()
            print("\n" + "=" * 60)
            print("purge_synthetic receipt")
            print("=" * 60)
            print(f"  before: {result['before']}")
            print(f"  after:  {result['after']}")
            print(f"  deleted: {result['deleted']}")
            if result["off_roster"]:
                print(f"  off_roster removed: {len(result['off_roster'])}")
                for row in result["off_roster"][:20]:
                    print(f"    {row['user_id']} ({row['user_name']})")
            print("=" * 60)
            return 0

        with conn.cursor() as cur:
            ensure_achievement_tables(cur, regional_schema)
            catalog = load_achievement_catalog(cur, regional_schema)
        conn.commit()

        print_region_context(region, regional_schema, registry_schema)
        if args.interactive:
            _print_achievement_catalog(catalog)

        qs_schema = resolve_qsignups_schema()
        qs_aos = load_qsignups_aos(conn, qs_schema, expected_team)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT ao, channel_id FROM `{regional_schema}`.`aos` "
                f"WHERE COALESCE(archived,0)=0 ORDER BY ao"
            )
            db_aos = [(r["ao"], r["channel_id"]) for r in (cur.fetchall() or [])]

        aos, ao_source, ao_missing = select_ao_candidates(qs_aos, db_aos, channels)
        source_label = {
            "qsignups": f"{qs_schema}.qsignups_aos",
            "paxminer_aos": f"{regional_schema}.aos",
            "slack_channels": "all Slack channels (no AO list found)",
        }[ao_source]
        print(f"\nAO source: {source_label} — {len(aos)} AO(s)")
        if ao_missing:
            LOG.warning(
                "%s QSignups AO(s) skipped (channel not visible to the bot): %s",
                len(ao_missing),
                ", ".join(f"{n} ({c})" for n, c in ao_missing),
            )

        pool = build_pax_pool(users)
        allowed_ids = {uid for _, uid in pool}
        user_names = {uid: name for name, uid in pool}
        receipts: list[dict] = []

        with conn.cursor() as cur:
            before = count_region_rows(cur, regional_schema)
        print(f"\nCounts before: {before}")

        if not _confirm_yes(args.yes, "Write seed data to test region? [y/N]: "):
            print("Aborted.")
            return 1

        # Always purge legacy synthetics / off-roster before seeding.
        if not args.dry_run:
            with conn.cursor() as cur:
                purge_result = purge_synthetic(cur, regional_schema, slack_roster)
            receipts.append(
                {
                    "action": "purge_synthetic",
                    "cleared": purge_result["deleted"],
                    "notes": [
                        f"before={purge_result['before']}",
                        f"after={purge_result['after']}",
                        f"off_roster_removed={len(purge_result['off_roster'])}",
                    ],
                }
            )

        if args.interactive:
            actions = interactive_user_pass(users, aos, catalog, region)
            with conn.cursor() as cur:
                for act in actions:
                    uid = act["user_id"]
                    name = act["name"]
                    if act["action"] == "skip":
                        receipts.append({"user_id": uid, "name": name, "action": "skip"})
                        continue
                    if act["action"] == "clear":
                        if args.dry_run:
                            receipts.append(
                                {
                                    "user_id": uid,
                                    "name": name,
                                    "action": "clear",
                                    "notes": ["dry-run: not cleared"],
                                }
                            )
                            continue
                        cleared = clear_seed_for_user(cur, regional_schema, uid)
                        receipts.append(
                            {
                                "user_id": uid,
                                "name": name,
                                "action": "clear",
                                "cleared": cleared,
                            }
                        )
                        continue

                    calendar = load_calendar(cur, regional_schema)
                    try:
                        overlay = plan_realistic_overlay(
                            calendar=calendar,
                            goal_type=act["goal_type"],
                            target_user_id=uid,
                            aos=act["aos"],
                            pool=pool,
                            thresholds=region,
                            achievement=act.get("achievement"),
                            kotter_kind=act.get("kotter_kind"),
                            catalog=catalog,
                        )
                    except Exception as exc:
                        receipts.append(
                            {
                                "user_id": uid,
                                "name": name,
                                "action": "seed",
                                "error": str(exc),
                            }
                        )
                        continue

                    inserted = {"beatdowns": 0, "bd_attendance": 0}
                    if not args.dry_run:
                        clear_seed_for_user(cur, regional_schema, uid)
                        upsert_user(
                            cur, regional_schema, uid, name, allowed_ids=allowed_ids
                        )
                        for ao_name, ao_id in act["aos"]:
                            upsert_ao(cur, regional_schema, ao_name, ao_id)
                        inserted = apply_overlay_writes(
                            cur,
                            regional_schema,
                            overlay,
                            user_names=user_names,
                            allowed_ids=allowed_ids,
                        )
                    receipts.append(
                        {
                            "user_id": uid,
                            "name": name,
                            "action": "seed" + (" (dry-run)" if args.dry_run else ""),
                            "goal_label": overlay.goal_label,
                            "aos": [f"{n}:{c}" for n, c in act["aos"]],
                            "inserted": inserted,
                            "expected_outcome": overlay.expected_outcome,
                            "co_triggered": overlay.co_triggered,
                            "notes": overlay.notes,
                        }
                    )
        else:
            baseline_aos = aos[: min(len(aos), 8)]
            baseline_plan = build_baseline_plan(
                aos=baseline_aos, pool=pool, days=args.days
            )
            receipts.append(
                {
                    "action": "baseline",
                    "goal_label": baseline_plan.goal_label,
                    "expected_outcome": baseline_plan.expected_outcome,
                    "notes": baseline_plan.notes,
                    "beatdowns_planned": len(baseline_plan.beatdowns),
                }
            )

            kotter_kinds = [
                k.strip().lower().replace("lowq", "low-q").replace("noq", "never-q")
                for k in args.kotter.split(",")
                if k.strip()
            ]
            kotter_users = users[: len(kotter_kinds)] if kotter_kinds else []

            almost_achievement = next(
                (a for a in catalog if a.get("code") == "el_quatro"),
                catalog[0] if catalog else None,
            )

            with conn.cursor() as cur:
                if not args.dry_run:
                    cleared = clear_all_seed(cur, regional_schema)
                    receipts.append({"action": "clear_all", "cleared": cleared})
                    inserted = write_baseline(
                        cur, regional_schema, baseline_plan, pool, baseline_aos
                    )
                    receipts.append({"action": "baseline_write", "inserted": inserted})

                    calendar = load_calendar(cur, regional_schema)
                    for (name, uid), kind in zip(kotter_users, kotter_kinds):
                        overlay = plan_realistic_overlay(
                            calendar=calendar,
                            goal_type="kotter",
                            target_user_id=uid,
                            aos=baseline_aos[:1],
                            pool=pool,
                            thresholds=region,
                            kotter_kind=kind,
                        )
                        stats = apply_overlay_writes(
                            cur,
                            regional_schema,
                            overlay,
                            user_names=user_names,
                            allowed_ids=allowed_ids,
                        )
                        calendar = load_calendar(cur, regional_schema)
                        receipts.append(
                            {
                                "user_id": uid,
                                "name": name,
                                "action": f"kotter:{kind}",
                                "inserted": stats,
                                "expected_outcome": overlay.expected_outcome,
                                "notes": overlay.notes,
                            }
                        )

                    for name, uid in users[3:6]:
                        if not almost_achievement:
                            break
                        ach = dict(almost_achievement)
                        threshold = int(ach.get("threshold") or 1)
                        ach["threshold"] = max(1, threshold - 1)
                        overlay = plan_realistic_overlay(
                            calendar=load_calendar(cur, regional_schema),
                            goal_type="achievement",
                            target_user_id=uid,
                            aos=baseline_aos[:1],
                            pool=pool,
                            thresholds=region,
                            achievement=ach,
                            catalog=catalog,
                        )
                        stats = apply_overlay_writes(
                            cur,
                            regional_schema,
                            overlay,
                            user_names=user_names,
                            allowed_ids=allowed_ids,
                        )
                        receipts.append(
                            {
                                "user_id": uid,
                                "name": name,
                                "action": "almost-there",
                                "goal_label": overlay.goal_label,
                                "inserted": stats,
                                "expected_outcome": overlay.expected_outcome,
                                "notes": overlay.notes,
                            }
                        )

            if args.dry_run:
                receipts.append(
                    {
                        "action": "dry-run",
                        "notes": [
                            f"Would clear seed and write {len(baseline_plan.beatdowns)} baseline beatdowns",
                            f"Kotter overlays: {len(kotter_users)}",
                        ],
                    }
                )

        if not args.dry_run:
            conn.commit()
        else:
            conn.rollback()

        if args.verify:
            verify_region(conn, regional_schema, registry_schema, region)
            with conn.cursor() as cur:
                health = find_synthetic_and_off_roster(
                    cur, regional_schema, slack_roster
                )
            print(
                f"\nRoster health: synthetic={len(health['synthetic'])} "
                f"off_roster={len(health['off_roster'])}"
            )
            if health["synthetic"]:
                print(f"  synthetic: {health['synthetic'][:20]}")
            if health["off_roster"]:
                print(f"  off_roster: {health['off_roster'][:20]}")

        print_receipt(receipts)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
