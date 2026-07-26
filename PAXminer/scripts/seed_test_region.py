#!/usr/bin/env python3
"""Interactive, test-only seeder for PAXMiner.

Pulls real users and AO channels from the **test** Slack workspace, walks each
user, and lets you assign one goal (Kotter case or one achievement) so the next
PAXMiner run awards it. Always loads ``.env.deploy.test`` — there is no ``--env``
switch, and the script hard-fails if the target does not look like test.

Usage (from repo root):

  python PAXminer/scripts/seed_test_region.py
  python PAXminer/scripts/seed_test_region.py --schema f3ttown_test

Not wired into CI or deploy.
"""

from __future__ import annotations

import argparse
import logging
import os
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
FILLER_Q_USER_ID = "USEEDFILLER0XX"
FILLER_Q_NAME = f"{SEED_SENTINEL} Q"
DEPLOY_ENV_FILE = _REPO_ROOT / ".env.deploy.test"

ATTENDANCE_VIEW_DDL = """
CREATE OR REPLACE VIEW `{schema}`.`attendance_view` AS
SELECT
  a.date AS Date,
  ao.ao AS AO,
  u.user_name AS PAX
FROM `{schema}`.`bd_attendance` a
JOIN `{schema}`.`users` u ON u.user_id = a.user_id
JOIN `{schema}`.`aos` ao ON ao.channel_id = a.ao_id
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


def fetch_workspace_users(client: Any) -> list[tuple[str, str]]:
    """Return [(display_name, user_id), ...] excluding bots/deleted/Slackbot."""
    users: list[tuple[str, str]] = []
    cursor = None
    while True:
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
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or u.get("real_name")
                or u.get("name")
                or u.get("id")
            )
            users.append((str(name), str(u["id"])))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    users.sort(key=lambda x: x[0].lower())
    return users


def fetch_workspace_channels(client: Any) -> list[tuple[str, str]]:
    """Return [(channel_name, channel_id), ...] for public + member private."""
    channels: list[tuple[str, str]] = []
    cursor = None
    while True:
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
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
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


@dataclass
class SeedPlan:
    beatdowns: list[BeatdownSpec] = field(default_factory=list)
    expected_outcome: str = ""
    co_triggered: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    goal_label: str = ""


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
    filler_q_id: str = FILLER_Q_USER_ID,
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
    filler_q_id: str,
    catalog: list[dict],
) -> SeedPlan:
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

    if metric == "distinct_aos":
        dates = _dates_in_period(today, period, 1)
        bd_date = dates[0]
        for ao_name, ao_id in aos[:threshold]:
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=bd_date,
                    q_user_id=target_user_id,
                    attendee_ids=[target_user_id],
                    backblast=_bb(activity, ao_name, bd_date),
                )
            )
        plan.notes.append(f"Q'd at {threshold} distinct AOs in period={period}")
        return plan

    dates = _dates_in_period(today, period, threshold)
    ao_name, ao_id = aos[0]

    if metric in ("posts", "posts_at_single_ao"):
        for bd_date in dates:
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=bd_date,
                    q_user_id=filler_q_id,
                    attendee_ids=[target_user_id],
                    backblast=_bb(activity, ao_name, bd_date),
                )
            )
        plan.notes.append(
            f"{threshold} posts as attendee (filler Q={filler_q_id}) at {ao_name}"
        )
        return plan

    if metric == "qs":
        for bd_date in dates:
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=bd_date,
                    q_user_id=target_user_id,
                    attendee_ids=[target_user_id],
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
    filler_q_id: str,
) -> SeedPlan:
    no_post = int(thresholds.get("NO_POST_THRESHOLD") or 2)
    reminder = int(thresholds.get("REMINDER_WEEKS") or 2)
    home_ao_capture = int(thresholds.get("HOME_AO_CAPTURE") or 8)
    no_q_weeks = int(thresholds.get("NO_Q_THRESHOLD_WEEKS") or 4)
    no_q_posts = int(thresholds.get("NO_Q_THRESHOLD_POSTS") or 4)

    ao_name, ao_id = aos[0]
    plan = SeedPlan(goal_label=f"kotter:{kind}")

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
        plan.beatdowns.append(
            BeatdownSpec(
                ao_id=ao_id,
                ao_name=ao_name,
                bd_date=bd_date,
                q_user_id=filler_q_id,
                attendee_ids=[target_user_id],
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
                attendee_ids=[target_user_id],
                backblast=_bb("beatdown", ao_name, q_date, "low-q-as-q"),
            )
        )
        if recent != q_date:
            plan.beatdowns.append(
                BeatdownSpec(
                    ao_id=ao_id,
                    ao_name=ao_name,
                    bd_date=recent,
                    q_user_id=filler_q_id,
                    attendee_ids=[target_user_id],
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
        plan.beatdowns.append(
            BeatdownSpec(
                ao_id=ao_id,
                ao_name=ao_name,
                bd_date=bd_date,
                q_user_id=filler_q_id,
                attendee_ids=[target_user_id],
                backblast=_bb("beatdown", ao_name, bd_date, tag),
            )
        )
    plan.expected_outcome = (
        f"Appear on Kotter never-Q list (posts {old_post.isoformat()} / "
        f"{recent.isoformat()}, never Q)"
    )
    plan.notes.append(f"never-q post window {lo} .. {hi}")
    return plan


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


def ensure_achievement_tables(cur, schema: str) -> None:
    from achievements.achievement_rules import (
        ACHIEVEMENTS_LIST_DDL,
        ACHIEVEMENTS_VIEW_DDL,
        ACHIEVEMENT_SEEDS,
    )

    if not _table_exists(cur, schema, "achievements_list"):
        cur.execute(ACHIEVEMENTS_LIST_DDL.format(schema=schema))
        LOG.info("Created %s.achievements_list", schema)
    if not _table_exists(cur, schema, "achievements_awarded"):
        # Avoid FOREIGN KEY — slack_test user gets 1142 REFERENCES denied.
        cur.execute(_ACHIEVEMENTS_AWARDED_DDL_NO_FK.format(schema=schema))
        LOG.info("Created %s.achievements_awarded (no FK)", schema)
    cur.execute(ACHIEVEMENTS_VIEW_DDL.format(schema=schema))
    cur.execute(ATTENDANCE_VIEW_DDL.format(schema=schema))
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


def upsert_user(cur, schema: str, user_id: str, name: str) -> None:
    email = f"{user_id.lower()}@seed.example"
    cur.execute(
        f"""
        INSERT INTO `{schema}`.`users`
        (user_id, user_name, real_name, phone, email, start_date, app, json)
        VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
        ON DUPLICATE KEY UPDATE
          user_name=VALUES(user_name),
          real_name=VALUES(real_name),
          email=VALUES(email),
          app=0
        """,
        (
            user_id,
            name,
            name,
            "",
            email,
            (date.today() - timedelta(days=365)).isoformat(),
            "{}",
        ),
    )


def upsert_ao(cur, schema: str, ao_name: str, channel_id: str) -> None:
    cur.execute(
        f"""
        INSERT INTO `{schema}`.`aos` (channel_id, ao, channel_created, archived, backblast)
        VALUES (%s, %s, %s, 0, 1)
        ON DUPLICATE KEY UPDATE ao=VALUES(ao), archived=0, backblast=1
        """,
        (channel_id, ao_name, int(datetime.now().timestamp())),
    )


def clear_seed_for_user(cur, schema: str, user_id: str) -> dict[str, int]:
    """Delete only [SEED]-tagged rows for this user (+ their awards)."""
    counts: dict[str, int] = {}
    # Attendance on seed beatdowns
    cur.execute(
        f"""
        DELETE a FROM `{schema}`.`bd_attendance` a
        JOIN `{schema}`.`beatdowns` b
          ON a.ao_id = b.ao_id AND a.date = b.bd_date
        WHERE a.user_id = %s AND b.backblast LIKE %s
        """,
        (user_id, f"%{SEED_SENTINEL}%"),
    )
    counts["bd_attendance"] = cur.rowcount

    # Seed beatdowns this user Q'd
    cur.execute(
        f"""
        DELETE FROM `{schema}`.`beatdowns`
        WHERE q_user_id = %s AND backblast LIKE %s
        """,
        (user_id, f"%{SEED_SENTINEL}%"),
    )
    counts["beatdowns_as_q"] = cur.rowcount

    # Orphan seed beatdowns (filler Q) with no remaining attendance
    cur.execute(
        f"""
        DELETE b FROM `{schema}`.`beatdowns` b
        WHERE b.backblast LIKE %s
          AND b.q_user_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM `{schema}`.`bd_attendance` a
            WHERE a.ao_id = b.ao_id AND a.date = b.bd_date
          )
        """,
        (f"%{SEED_SENTINEL}%", FILLER_Q_USER_ID),
    )
    counts["orphan_filler_beatdowns"] = cur.rowcount

    cur.execute(
        f"DELETE FROM `{schema}`.`achievements_awarded` WHERE pax_id = %s",
        (user_id,),
    )
    counts["achievements_awarded"] = cur.rowcount
    return counts


def write_seed_plan(
    cur,
    schema: str,
    plan: SeedPlan,
    *,
    target_user_id: str,
    target_name: str,
    aos: list[tuple[str, str]],
) -> dict[str, int]:
    upsert_user(cur, schema, target_user_id, target_name)
    upsert_user(cur, schema, FILLER_Q_USER_ID, FILLER_Q_NAME)
    for ao_name, ao_id in aos:
        upsert_ao(cur, schema, ao_name, ao_id)

    beatdowns_n = 0
    attendance_n = 0
    # Unique timestamps: base + index
    base_ts = int(datetime.now().timestamp() * 1000)
    for i, spec in enumerate(plan.beatdowns):
        ts = str(base_ts + i)
        cur.execute(
            f"""
            INSERT INTO `{schema}`.`beatdowns`
            (timestamp, ts_edited, ao_id, bd_date, q_user_id, coq_user_id,
             pax_count, backblast, fngs, fng_count)
            VALUES (%s, NULL, %s, %s, %s, NULL, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              pax_count=VALUES(pax_count),
              backblast=VALUES(backblast),
              q_user_id=VALUES(q_user_id)
            """,
            (
                ts,
                spec.ao_id,
                spec.bd_date.isoformat(),
                spec.q_user_id,
                len(spec.attendee_ids),
                spec.backblast,
                "",
                0,
            ),
        )
        beatdowns_n += 1
        for uid in spec.attendee_ids:
            cur.execute(
                f"""
                INSERT INTO `{schema}`.`bd_attendance`
                (timestamp, ts_edited, user_id, ao_id, date, q_user_id)
                VALUES (%s, NULL, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE timestamp=VALUES(timestamp), q_user_id=VALUES(q_user_id)
                """,
                (
                    ts,
                    uid,
                    spec.ao_id,
                    spec.bd_date.isoformat(),
                    spec.q_user_id,
                ),
            )
            attendance_n += 1
    return {"beatdowns": beatdowns_n, "bd_attendance": attendance_n}


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
    print("\nAO channels:")
    for i, (name, cid) in enumerate(aos, 1):
        print(f"  {i:2d}. {name} ({cid})")


def _parse_index_list(raw: str, n: int) -> list[int] | None:
    raw = raw.strip()
    if not raw:
        return []
    out: list[int] = []
    for part in raw.replace(",", " ").split():
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
    print(f"\nPre-selected AO index(es): {pre_str}")
    print(f"Minimum AOs required: {minimum}")
    while True:
        raw = _prompt(
            "Accept pre-selection with Enter, or type AO numbers "
            f"(e.g. {pre_str}) [min {minimum}]: "
        )
        if not raw:
            return list(preselect)
        idxs = _parse_index_list(raw, len(all_aos))
        if idxs is None:
            print("Invalid selection; enter numbers from the list.")
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
    print("\nTo trigger: Schedule → select item → Run Now, or wait for the daily tick.")
    print("=" * 60)


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
        "--dry-run",
        action="store_true",
        help="Plan only; do not write to the database",
    )
    args = parser.parse_args(argv)

    load_test_env(skip=args.skip_env_load)
    regional_schema, registry_schema = resolve_schemas(args.schema)
    assert_test_only(regional_schema, registry_schema)

    token = (os.environ.get("PM_SLACK_TOKEN") or "").strip()
    if not token:
        raise SystemExit("PM_SLACK_TOKEN is required in .env.deploy.test")
    expected_team = (os.environ.get("F3_REGION_SLACK_TEAM_ID") or "").strip()

    from slack_util import slack_client

    client = slack_client(token)
    assert_slack_team(client, expected_team)

    print("Fetching workspace users and channels…")
    users = fetch_workspace_users(client)
    channels = fetch_workspace_channels(client)
    if not users:
        raise SystemExit("No human users returned from users_list")
    if not channels:
        raise SystemExit("No channels returned from conversations_list")
    print(f"  users: {len(users)}  channels: {len(channels)}")

    # Prefer channels that look like AOs: already in DB, else all channels.
    conn = connect(regional_schema)
    try:
        # Reconnect-style: also need registry on same host — use database switch.
        with conn.cursor() as cur:
            ensure_achievement_tables(cur, regional_schema)
            catalog = load_achievement_catalog(cur, regional_schema)
        conn.commit()

        # Load region from registry (same server, different schema)
        region = load_region_row(conn, registry_schema, regional_schema)
        print_region_context(region, regional_schema, registry_schema)
        _print_achievement_catalog(catalog)

        # Use AO rows already in DB when present; else all Slack channels.
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT ao, channel_id FROM `{regional_schema}`.`aos` "
                f"WHERE COALESCE(archived,0)=0 ORDER BY ao"
            )
            db_aos = [(r["ao"], r["channel_id"]) for r in (cur.fetchall() or [])]
        # Intersect with live Slack channels when possible so IDs are real.
        channel_by_id = {cid: name for name, cid in channels}
        if db_aos:
            aos = [
                (channel_by_id.get(cid, name), cid)
                for name, cid in db_aos
                if cid in channel_by_id
            ]
            if not aos:
                LOG.warning(
                    "DB aos not found in Slack channel list; using all Slack channels"
                )
                aos = channels
            else:
                print(f"\nUsing {len(aos)} AO(s) from DB that exist in Slack.")
        else:
            aos = channels
            print("\nNo aos rows yet; using all Slack channels as AO candidates.")

        actions = interactive_user_pass(users, aos, catalog, region)
        receipts: list[dict] = []

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

                try:
                    plan = build_seed_plan(
                        goal_type=act["goal_type"],
                        target_user_id=uid,
                        aos=act["aos"],
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
                    # Clear prior seed for this user so re-runs are clean
                    clear_seed_for_user(cur, regional_schema, uid)
                    inserted = write_seed_plan(
                        cur,
                        regional_schema,
                        plan,
                        target_user_id=uid,
                        target_name=name,
                        aos=act["aos"],
                    )
                receipts.append(
                    {
                        "user_id": uid,
                        "name": name,
                        "action": "seed" + (" (dry-run)" if args.dry_run else ""),
                        "goal_label": plan.goal_label,
                        "aos": [f"{n}:{c}" for n, c in act["aos"]],
                        "inserted": inserted,
                        "expected_outcome": plan.expected_outcome,
                        "co_triggered": plan.co_triggered,
                        "notes": plan.notes,
                    }
                )
            if not args.dry_run:
                conn.commit()
            else:
                conn.rollback()
    finally:
        conn.close()

    print_receipt(receipts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
