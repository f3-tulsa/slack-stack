#!/usr/bin/env python3
"""Dev-only seeder: inject synthetic attendance into a regional schema.

Usage (from repo root, with migration or deploy env loaded):

  python PAXminer/scripts/seed_test_region.py --schema f3ttown_test \\
    --ao "The Fort:C0123456789" --ao "Brickyard:C0987654321" \\
    --user "Beaker:U0123456789" --user "Hot Wheels:U0987654321" \\
    --weeks 8

Env (either set works):
  DATABASE_HOST / DATABASE_USER / DATABASE_PASSWORD / DATABASE_PORT / DATABASE_TLS_ENABLED
  or TARGET_HOST / TARGET_USER / TARGET_PASSWORD / TARGET_PORT / TARGET_TLS_ENABLED

Not wired into CI or deploy. Real Slack channel/user IDs are optional; without them
Slack posts and @mentions will not resolve in the test workspace.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_PAX_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PAX_ROOT.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
if str(_REPO_ROOT / "migration") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "migration"))

LOG = logging.getLogger("seed_test_region")


def _parse_named_id(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise argparse.ArgumentTypeError(f"expected Name:ID, got {raw!r}")
    name, cid = raw.split(":", 1)
    name, cid = name.strip(), cid.strip()
    if not name or not cid:
        raise argparse.ArgumentTypeError(f"expected Name:ID, got {raw!r}")
    return name, cid


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for stage in ("test", "prod"):
        for path in (
            _REPO_ROOT / "migration" / f".env.migration.{stage}",
            _REPO_ROOT / f".env.deploy.{stage}",
        ):
            if path.exists():
                load_dotenv(path)
                LOG.info("Loaded env from %s", path)
                return


def _connect(schema: str):
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
            "Set DATABASE_HOST/USER/PASSWORD (or TARGET_*) before running the seeder."
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


def _default_aos(n: int = 3) -> list[tuple[str, str]]:
    return [(f"AO Seed {i + 1}", f"CSEED{i + 1:04d}XXX") for i in range(n)]


def _default_users(n: int = 8) -> list[tuple[str, str]]:
    names = [
        "Beaker",
        "Hot Wheels",
        "Cotton",
        "Gadget",
        "Ryno",
        "Sprocket",
        "Torch",
        "Vapor",
        "Zigzag",
        "Anchor",
    ]
    return [(names[i % len(names)] + (f" {i}" if i >= len(names) else ""), f"USEED{i + 1:04d}XXX") for i in range(n)]


def _clear_seed_rows(cur, aos: list[tuple[str, str]], users: list[tuple[str, str]]) -> dict[str, int]:
    ao_ids = [c for _, c in aos]
    user_ids = [u for _, u in users]
    counts: dict[str, int] = {}
    if ao_ids:
        placeholders = ",".join(["%s"] * len(ao_ids))
        cur.execute(
            f"DELETE FROM bd_attendance WHERE ao_id IN ({placeholders})",
            ao_ids,
        )
        counts["bd_attendance"] = cur.rowcount
        cur.execute(
            f"DELETE FROM beatdowns WHERE ao_id IN ({placeholders})",
            ao_ids,
        )
        counts["beatdowns"] = cur.rowcount
        cur.execute(
            f"DELETE FROM aos WHERE channel_id IN ({placeholders})",
            ao_ids,
        )
        counts["aos"] = cur.rowcount
    if user_ids:
        placeholders = ",".join(["%s"] * len(user_ids))
        cur.execute(
            f"DELETE FROM users WHERE user_id IN ({placeholders})",
            user_ids,
        )
        counts["users"] = cur.rowcount
    return counts


def seed(
    schema: str,
    aos: list[tuple[str, str]],
    users: list[tuple[str, str]],
    *,
    weeks: int = 8,
    clear: bool = False,
) -> dict:
    today = date.today()
    receipt: dict = {
        "schema": schema,
        "aos": len(aos),
        "users": len(users),
        "weeks": weeks,
        "inserted": {},
        "cleared": {},
        "placeholder_ids": False,
    }
    if any(cid.startswith("CSEED") for _, cid in aos) or any(
        uid.startswith("USEED") for _, uid in users
    ):
        receipt["placeholder_ids"] = True
        LOG.warning(
            "Using placeholder Slack IDs — posts/@mentions will not resolve in Slack. "
            "Pass --ao Name:Cxxxx and --user Name:Uxxxx with real test-workspace IDs."
        )

    conn = _connect(schema)
    try:
        with conn.cursor() as cur:
            if clear:
                receipt["cleared"] = _clear_seed_rows(cur, aos, users)
                LOG.info("Cleared prior seed rows: %s", receipt["cleared"])

            users_n = 0
            for i, (name, uid) in enumerate(users):
                email = f"{name.lower().replace(' ', '.')}@seed.example"
                cur.execute(
                    """
                    INSERT INTO users
                    (user_id, user_name, real_name, phone, email, start_date, app, json)
                    VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
                    ON DUPLICATE KEY UPDATE
                      user_name=VALUES(user_name),
                      real_name=VALUES(real_name),
                      email=VALUES(email),
                      app=0
                    """,
                    (
                        uid,
                        name,
                        name,
                        "",
                        email,
                        (today - timedelta(days=365)).isoformat(),
                        "{}",
                    ),
                )
                users_n += 1
            receipt["inserted"]["users"] = users_n

            aos_n = 0
            for ao_name, channel_id in aos:
                cur.execute(
                    """
                    INSERT INTO aos (channel_id, ao, channel_created, archived, backblast)
                    VALUES (%s, %s, %s, 0, 1)
                    ON DUPLICATE KEY UPDATE ao=VALUES(ao), archived=0, backblast=1
                    """,
                    (channel_id, ao_name, int(datetime.now().timestamp())),
                )
                aos_n += 1
            receipt["inserted"]["aos"] = aos_n

            beatdowns_n = 0
            attendance_n = 0
            # Design: most PAX post regularly; last 2 users are MIA / never-Q for Kotter.
            regular = users[:-2] if len(users) >= 4 else users
            mia_users = users[-2:] if len(users) >= 4 else []

            for week in range(weeks):
                bd_date = today - timedelta(days=7 * week + 1)  # roughly weekly, recent
                for ao_idx, (ao_name, channel_id) in enumerate(aos):
                    q_user = regular[week % len(regular)][1] if regular else users[0][1]
                    coq_user = (
                        regular[(week + 1) % len(regular)][1] if len(regular) > 1 else None
                    )
                    attendees = list(regular)
                    # Older weeks: include MIA users so they have a last-post in the Kotter window.
                    if week >= weeks - 3 and mia_users and week % 2 == 0:
                        attendees = list(regular) + mia_users[:1]
                    # Newest weeks: MIA stay out (haven't posted recently).
                    if week < 2:
                        attendees = list(regular)

                    ts = str(datetime.combine(bd_date, datetime.min.time()).timestamp())
                    cur.execute(
                        """
                        INSERT INTO beatdowns
                        (timestamp, ts_edited, ao_id, bd_date, q_user_id, coq_user_id,
                         pax_count, backblast, fngs, fng_count)
                        VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          pax_count=VALUES(pax_count),
                          backblast=VALUES(backblast),
                          coq_user_id=VALUES(coq_user_id)
                        """,
                        (
                            ts,
                            channel_id,
                            bd_date.isoformat(),
                            q_user,
                            coq_user,
                            len(attendees),
                            f"Backblast — {ao_name} on {bd_date.isoformat()}",
                            "",
                            0,
                        ),
                    )
                    beatdowns_n += 1

                    for uname, uid in attendees:
                        cur.execute(
                            """
                            INSERT INTO bd_attendance
                            (timestamp, ts_edited, user_id, ao_id, date, q_user_id)
                            VALUES (%s, NULL, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE timestamp=VALUES(timestamp)
                            """,
                            (
                                ts,
                                uid,
                                channel_id,
                                bd_date.isoformat(),  # string form matching beatdowns.bd_date join
                                q_user,
                            ),
                        )
                        attendance_n += 1

            receipt["inserted"]["beatdowns"] = beatdowns_n
            receipt["inserted"]["bd_attendance"] = attendance_n
            conn.commit()
    finally:
        conn.close()

    receipt["kotter_hint"] = (
        f"MIA/never-Q candidates: {[n for n, _ in mia_users]}" if mia_users else "n/a"
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="f3ttown_test", help="Regional schema name")
    parser.add_argument(
        "--ao",
        action="append",
        default=[],
        type=_parse_named_id,
        help='AO as "Name:Cxxxx" (repeatable)',
    )
    parser.add_argument(
        "--user",
        action="append",
        default=[],
        type=_parse_named_id,
        help='User as "Name:Uxxxx" (repeatable)',
    )
    parser.add_argument("--weeks", type=int, default=8, help="Weeks of beatdown history")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete prior rows for the listed AO/user IDs before insert",
    )
    parser.add_argument("--skip-env-load", action="store_true", help="Do not auto-load .env files")
    args = parser.parse_args(argv)

    if not args.skip_env_load:
        _load_env()

    aos = args.ao or _default_aos()
    users = args.user or _default_users()
    if args.weeks < 2:
        raise SystemExit("--weeks must be >= 2")

    receipt = seed(args.schema, aos, users, weeks=args.weeks, clear=args.clear)
    print("=" * 60)
    print("seed_test_region receipt")
    print("=" * 60)
    for k, v in receipt.items():
        print(f"  {k}: {v}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
