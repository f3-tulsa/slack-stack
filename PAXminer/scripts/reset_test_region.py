#!/usr/bin/env python3
"""Dev-only reset for a PAXMiner **test** regional schema.

Clears prod-derived attendance so the region only contains data written by
``seed_test_region.py``. Reuses that script's test-only guards: always loads
``.env.deploy.test``, requires ``_test`` schemas, and requires Slack
``auth.test`` to match ``F3_REGION_SLACK_TEAM_ID``.

What it deletes:
  * all ``bd_attendance`` rows
  * all ``beatdowns`` rows
  * all ``achievements_awarded`` rows
  * ``users`` / ``aos`` rows whose Slack IDs are not in the test workspace
    (skip with ``--keep-roster``)

What it keeps: ``achievements_list`` rules and all views.

Usage (from repo root):

  python PAXminer/scripts/reset_test_region.py --dry-run
  python PAXminer/scripts/reset_test_region.py

Not wired into CI or deploy.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_test_region as seeder  # noqa: E402

LOG = logging.getLogger("reset_test_region")

# Wiped unconditionally: every row is attendance-derived.
WIPE_TABLES = ("bd_attendance", "beatdowns", "achievements_awarded")
# Never touched: rule config and views.
KEEP_TABLES = ("achievements_list",)


def table_counts(cur, schema: str, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tables:
        cur.execute(f"SELECT COUNT(*) AS n FROM `{schema}`.`{t}`")
        row = cur.fetchone() or {}
        counts[t] = int(row.get("n") or 0)
    return counts


def wipe_attendance(cur, schema: str) -> dict[str, int]:
    """Delete every attendance-derived row. Order avoids FK complaints."""
    deleted: dict[str, int] = {}
    for t in WIPE_TABLES:
        cur.execute(f"DELETE FROM `{schema}`.`{t}`")
        deleted[t] = cur.rowcount
        LOG.info("Deleted %s rows from %s.%s", cur.rowcount, schema, t)
    return deleted


def prune_roster(
    cur,
    schema: str,
    *,
    live_user_ids: set[str],
    live_channel_ids: set[str],
) -> dict[str, int]:
    """Drop users/aos that do not exist in the test Slack workspace."""
    pruned: dict[str, int] = {}

    cur.execute(f"SELECT user_id FROM `{schema}`.`users`")
    stale_users = [
        r["user_id"] for r in (cur.fetchall() or []) if r["user_id"] not in live_user_ids
    ]
    pruned["users"] = _delete_in_batches(
        cur, f"DELETE FROM `{schema}`.`users` WHERE user_id IN", stale_users
    )

    cur.execute(f"SELECT channel_id FROM `{schema}`.`aos`")
    stale_aos = [
        r["channel_id"]
        for r in (cur.fetchall() or [])
        if r["channel_id"] not in live_channel_ids
    ]
    pruned["aos"] = _delete_in_batches(
        cur, f"DELETE FROM `{schema}`.`aos` WHERE channel_id IN", stale_aos
    )
    return pruned


def _delete_in_batches(cur, prefix: str, ids: list[str], batch: int = 200) -> int:
    total = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        if not chunk:
            continue
        placeholders = ",".join(["%s"] * len(chunk))
        cur.execute(f"{prefix} ({placeholders})", chunk)
        total += cur.rowcount
    return total


def confirm(schema: str) -> bool:
    expected = f"RESET {schema}"
    print(f"\nThis permanently deletes ALL attendance data in {schema}.")
    try:
        typed = input(f'Type "{expected}" to continue: ').strip()
    except EOFError:
        return False
    return typed == expected


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default=None,
        help="Regional schema (default: PM_REGIONAL_SCHEMA or f3ttown_test)",
    )
    parser.add_argument(
        "--keep-roster",
        action="store_true",
        help="Do not prune users/aos that are missing from the test workspace",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; no deletes")
    parser.add_argument("--yes", action="store_true", help="Skip the typed confirmation")
    parser.add_argument(
        "--skip-env-load",
        action="store_true",
        help="Do not auto-load .env.deploy.test (env already set)",
    )
    args = parser.parse_args(argv)

    seeder.load_test_env(skip=args.skip_env_load)
    regional_schema, registry_schema = seeder.resolve_schemas(args.schema)
    seeder.assert_test_only(regional_schema, registry_schema)

    import os

    token = (os.environ.get("PM_SLACK_TOKEN") or "").strip()
    if not token:
        raise SystemExit("PM_SLACK_TOKEN is required in .env.deploy.test")

    from slack_util import slack_client

    client = slack_client(token)
    seeder.assert_slack_team(client, os.environ.get("F3_REGION_SLACK_TEAM_ID", ""))

    live_user_ids: set[str] = set()
    live_channel_ids: set[str] = set()
    if not args.keep_roster:
        print("Fetching test-workspace users and channels…")
        live_user_ids = {uid for _, uid in seeder.fetch_workspace_users(client)}
        live_channel_ids = {cid for _, cid in seeder.fetch_workspace_channels(client)}
        print(f"  live users: {len(live_user_ids)}  live channels: {len(live_channel_ids)}")

    conn = seeder.connect(regional_schema)
    receipt: dict = {"schema": regional_schema, "dry_run": args.dry_run}
    try:
        with conn.cursor() as cur:
            before = table_counts(
                cur, regional_schema, WIPE_TABLES + KEEP_TABLES + ("users", "aos")
            )
            receipt["before"] = before
            print("\nCurrent row counts:")
            for t, n in before.items():
                print(f"  {t:<24} {n}")

            if args.dry_run:
                receipt["would_delete"] = {t: before[t] for t in WIPE_TABLES}
                if not args.keep_roster:
                    cur.execute(f"SELECT user_id FROM `{regional_schema}`.`users`")
                    stale_u = sum(
                        1
                        for r in (cur.fetchall() or [])
                        if r["user_id"] not in live_user_ids
                    )
                    cur.execute(f"SELECT channel_id FROM `{regional_schema}`.`aos`")
                    stale_a = sum(
                        1
                        for r in (cur.fetchall() or [])
                        if r["channel_id"] not in live_channel_ids
                    )
                    receipt["would_prune"] = {"users": stale_u, "aos": stale_a}
                conn.rollback()
            else:
                if not args.yes and not confirm(regional_schema):
                    print("Aborted; nothing deleted.")
                    return 1
                receipt["deleted"] = wipe_attendance(cur, regional_schema)
                if not args.keep_roster:
                    receipt["pruned"] = prune_roster(
                        cur,
                        regional_schema,
                        live_user_ids=live_user_ids,
                        live_channel_ids=live_channel_ids,
                    )
                conn.commit()
                receipt["after"] = table_counts(
                    cur, regional_schema, WIPE_TABLES + KEEP_TABLES + ("users", "aos")
                )
    finally:
        conn.close()

    print("\n" + "=" * 60)
    print("reset_test_region receipt")
    print("=" * 60)
    for k, v in receipt.items():
        print(f"  {k}: {v}")
    print("=" * 60)
    if not args.dry_run:
        print("\nNext: python PAXminer/scripts/seed_test_region.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
