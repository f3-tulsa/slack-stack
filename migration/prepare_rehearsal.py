#!/usr/bin/env python3
"""Copy prod schemas into the rehearsal replica and make it self-contained.

Refuses any target that is not ``rehearsal``. Copies BASE TABLEs first
(``CREATE TABLE ... LIKE`` fails on views), then recreates views from
``SHOW CREATE VIEW``. Rewrites registry pointers from ``_prod`` to
``_rehearsal`` and replaces non-empty Slack tokens with a dummy encrypted
under the rehearsal throwaway key.

Usage:
  python migration/prepare_rehearsal.py --from prod --to rehearsal --dry-run
  python migration/prepare_rehearsal.py --from prod --to rehearsal --drop-existing
  python migration/prepare_rehearsal.py --preflight
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

_MIGRATION_DIR = Path(__file__).resolve().parent
_REPO = _MIGRATION_DIR.parent
sys.path.insert(0, str(_MIGRATION_DIR))
sys.path.insert(0, str(_REPO / "PAXminer"))

from paxminer_phases.db import (  # noqa: E402
    _column_exists,
    _connect,
    _load_env,
    _table_exists,
)

LOG = logging.getLogger(__name__)

REHEARSAL_STAGE = "rehearsal"
SOURCE_STAGE_ALLOWED = "prod"
COPY_BASES = ("paxminer", "weaselbot", "slackblast", "f3ttown", "f3scissortail")
DUMMY_TOKEN = "xoxb-rehearsal-not-a-real-token"

# Long enough for server-side INSERT ... SELECT of large regional tables.
COPY_TIMEOUT_S = 7200


def schema_pairs(source_stage: str, target_stage: str) -> list[tuple[str, str]]:
    return [(f"{base}_{source_stage}", f"{base}_{target_stage}") for base in COPY_BASES]


def schema_mapping(source_stage: str, target_stage: str) -> dict[str, str]:
    return dict(schema_pairs(source_stage, target_stage))


def validate_copy_args(source_stage: str, target_stage: str) -> None:
    if target_stage != REHEARSAL_STAGE:
        raise SystemExit(
            f"Refusing target stage {target_stage!r}: prepare_rehearsal only writes "
            f"to {REHEARSAL_STAGE!r}"
        )
    if source_stage != SOURCE_STAGE_ALLOWED:
        raise SystemExit(
            f"Refusing source stage {source_stage!r}: prepare_rehearsal only copies "
            f"from {SOURCE_STAGE_ALLOWED!r}"
        )


def rewrite_schema_names(sql: str, mapping: dict[str, str]) -> str:
    out = sql
    # Longest names first so a shorter prefix cannot eat a longer identifier.
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        out = out.replace(f"`{old}`", f"`{new}`")
        out = out.replace(f"{old}.", f"{new}.")
    return out


def rewrite_view_ddl(create_view: str, mapping: dict[str, str], *, dest_schema: str, view_name: str) -> str:
    sql = create_view
    sql = re.sub(r"DEFINER=`[^`]+`@`[^`]+`\s*", "", sql)
    sql = re.sub(r"SQL SECURITY DEFINER", "SQL SECURITY INVOKER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"^CREATE\s+", "CREATE OR REPLACE ", sql, count=1, flags=re.IGNORECASE)
    sql = rewrite_schema_names(sql, mapping)
    # SHOW CREATE VIEW emits an unqualified view name; qualify it or TiDB errors 1046.
    sql = re.sub(
        rf"(VIEW\s+)`{re.escape(view_name)}`",
        rf"\1`{dest_schema}`.`{view_name}`",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    return sql


def list_base_tables(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT TABLE_NAME FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'
        ORDER BY TABLE_NAME
        """,
        (schema,),
    )
    return [row["TABLE_NAME"] for row in cur.fetchall()]


def list_views(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT TABLE_NAME FROM information_schema.VIEWS
        WHERE TABLE_SCHEMA=%s
        ORDER BY TABLE_NAME
        """,
        (schema,),
    )
    return [row["TABLE_NAME"] for row in cur.fetchall()]


def schema_size_bytes(cur, schema: str) -> dict:
    cur.execute(
        """
        SELECT
          COALESCE(SUM(DATA_LENGTH), 0) AS data_length,
          COALESCE(SUM(INDEX_LENGTH), 0) AS index_length,
          COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'
        """,
        (schema,),
    )
    row = cur.fetchone() or {}
    data = int(row.get("data_length") or 0)
    index = int(row.get("index_length") or 0)
    return {
        "schema": schema,
        "tables": int(row.get("table_count") or 0),
        "data_bytes": data,
        "index_bytes": index,
        "total_bytes": data + index,
    }


def wipe_schema(cur, schema: str) -> None:
    views = list_views(cur, schema)
    tables = list_base_tables(cur, schema)
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for name in views:
        cur.execute(f"DROP VIEW IF EXISTS `{schema}`.`{name}`")
        LOG.info("Dropped view %s.%s", schema, name)
    for name in tables:
        cur.execute(f"DROP TABLE IF EXISTS `{schema}`.`{name}`")
        LOG.info("Dropped table %s.%s", schema, name)
    cur.execute("SET FOREIGN_KEY_CHECKS=1")


def _create_table_like(cur, src: str, dst: str, table: str) -> None:
    try:
        cur.execute(f"CREATE TABLE `{dst}`.`{table}` LIKE `{src}`.`{table}`")
        return
    except Exception as exc:
        LOG.warning("CREATE TABLE LIKE failed for %s.%s (%s); falling back to SHOW CREATE TABLE", src, table, exc)
    cur.execute(f"SHOW CREATE TABLE `{src}`.`{table}`")
    row = cur.fetchone()
    create = row.get("Create Table") or row.get("CREATE TABLE")
    if not create:
        raise RuntimeError(f"SHOW CREATE TABLE returned no DDL for {src}.{table}")
    create = re.sub(
        rf"^CREATE TABLE `{re.escape(table)}`",
        f"CREATE TABLE `{dst}`.`{table}`",
        create,
        count=1,
        flags=re.IGNORECASE,
    )
    cur.execute(create)


def copy_schema(cur, src: str, dst: str, mapping: dict[str, str]) -> dict:
    src_tables = list_base_tables(cur, src)
    src_views = list_views(cur, src)
    LOG.info("Copying %s → %s (%s tables, %s views)", src, dst, len(src_tables), len(src_views))
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for table in src_tables:
        _create_table_like(cur, src, dst, table)
        cur.execute(f"INSERT INTO `{dst}`.`{table}` SELECT * FROM `{src}`.`{table}`")
        LOG.info("  copied table %s (%s row(s))", table, cur.rowcount)
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    for view in src_views:
        cur.execute(f"SHOW CREATE VIEW `{src}`.`{view}`")
        row = cur.fetchone()
        create = row.get("Create View") or row.get("CREATE VIEW")
        if not create:
            raise RuntimeError(f"SHOW CREATE VIEW returned no DDL for {src}.{view}")
        ddl = rewrite_view_ddl(create, mapping, dest_schema=dst, view_name=view)
        cur.execute(ddl)
        LOG.info("  recreated view %s", view)
    dest_views = set(list_views(cur, dst))
    missing = [v for v in src_views if v not in dest_views]
    if missing:
        raise RuntimeError(f"View(s) missing after copy {src} → {dst}: {missing}")
    dest_tables = set(list_base_tables(cur, dst))
    missing_tables = [t for t in src_tables if t not in dest_tables]
    if missing_tables:
        raise RuntimeError(f"Table(s) missing after copy {src} → {dst}: {missing_tables}")
    return {"tables": len(src_tables), "views": len(src_views)}


def _schema_exists(cur, schema: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS c FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
        (schema,),
    )
    return int(cur.fetchone()["c"]) > 0


def rewrite_registry_pointers(cur, mapping: dict[str, str], target_stage: str) -> dict:
    """Rewrite copied registry pointers so the replica points at itself."""
    pm = f"paxminer_{target_stage}"
    wb = f"weaselbot_{target_stage}"
    sb = f"slackblast_{target_stage}"
    updates = {"paxminer.schema_name": 0, "weaselbot.paxminer_schema": 0, "slackblast.paxminer_schema": 0}
    deactivated = 0

    def _rewrite(schema: str, table: str, column: str, key: str) -> None:
        if not _table_exists(cur, schema, table) or not _column_exists(cur, schema, table, column):
            LOG.info("Skip pointer rewrite %s.%s.%s (absent)", schema, table, column)
            return
        for old, new in mapping.items():
            cur.execute(
                f"UPDATE `{schema}`.`{table}` SET `{column}`=%s WHERE `{column}`=%s",
                (new, old),
            )
            if cur.rowcount:
                LOG.info("Rewrote %s.%s.%s %s → %s (%s row(s))", schema, table, column, old, new, cur.rowcount)
            updates[key] += int(cur.rowcount or 0)

    _rewrite(pm, "regions", "schema_name", "paxminer.schema_name")
    _rewrite(wb, "regions", "paxminer_schema", "weaselbot.paxminer_schema")
    _rewrite(sb, "regions", "paxminer_schema", "slackblast.paxminer_schema")

    if _table_exists(cur, pm, "regions") and _column_exists(cur, pm, "regions", "schema_name"):
        cur.execute(f"SELECT region, schema_name, active FROM `{pm}`.`regions`")
        for row in cur.fetchall():
            name = row.get("schema_name")
            if not name:
                continue
            if not _schema_exists(cur, name):
                cur.execute(
                    f"UPDATE `{pm}`.`regions` SET active=0 WHERE region=%s",
                    (row["region"],),
                )
                deactivated += int(cur.rowcount or 0)
                LOG.warning(
                    "Set %s.regions.active=0 for region=%s (schema_name=%s has no copy)",
                    pm,
                    row.get("region"),
                    name,
                )
    return {**updates, "deactivated_missing_copy": deactivated}


def neutralize_tokens(cur, target_stage: str) -> dict:
    """Overwrite non-empty tokens with a dummy encrypted under the rehearsal key."""
    from common.encryption import encrypt_field

    dummy = encrypt_field(DUMMY_TOKEN)
    if not dummy or not dummy.startswith("gAAAAA"):
        raise RuntimeError("encrypt_field did not return Fernet ciphertext for the dummy token")
    counts = {}
    targets = [
        (f"paxminer_{target_stage}", "regions", "slack_token"),
        (f"weaselbot_{target_stage}", "regions", "slack_token"),
        (f"slackblast_{target_stage}", "regions", "bot_token"),
    ]
    for schema, table, column in targets:
        key = f"{schema}.{table}.{column}"
        if not _table_exists(cur, schema, table) or not _column_exists(cur, schema, table, column):
            counts[key] = 0
            LOG.info("Skip token neutralize %s (absent)", key)
            continue
        cur.execute(
            f"""
            UPDATE `{schema}`.`{table}`
            SET `{column}`=%s
            WHERE `{column}` IS NOT NULL AND `{column}` <> ''
            """,
            (dummy,),
        )
        counts[key] = int(cur.rowcount or 0)
        LOG.info("Neutralized %s non-empty token(s) on %s", counts[key], key)
    return counts


def report_preflight(cur, source_stage: str, target_stage: str) -> int:
    user = os.environ.get("TARGET_USER", "")
    key = os.environ.get("DB_ENCRYPTION_KEY", "")
    print(f"TARGET_USER={user}", flush=True)
    print(f"TARGET_HOST={os.environ.get('TARGET_HOST', '')}", flush=True)
    print(f"TARGET_PORT={os.environ.get('TARGET_PORT', '')}", flush=True)
    print(f"TARGET_PAXMINER_SCHEMA={os.environ.get('TARGET_PAXMINER_SCHEMA', '')!r}", flush=True)
    print(f"SOURCE_HOST set={bool(os.environ.get('SOURCE_HOST'))}", flush=True)
    print(f"DB_ENCRYPTION_KEY length={len(key)} (value not printed)", flush=True)
    problems: list[str] = []
    if not user.endswith(".rehearsal"):
        problems.append(f"TARGET_USER {user!r} does not end with .rehearsal")
    if os.environ.get("TARGET_PAXMINER_SCHEMA"):
        problems.append("TARGET_PAXMINER_SCHEMA is set; leave it unset so the stage derives paxminer_rehearsal")
    if os.environ.get("SOURCE_HOST"):
        problems.append("SOURCE_HOST is set; keep the SOURCE_* block commented")
    if len(key) < 16:
        problems.append("DB_ENCRYPTION_KEY is shorter than 16 characters")
    try:
        cur.execute("SHOW GRANTS")
        grants = [list(row.values())[0] for row in cur.fetchall()]
    except Exception as exc:
        grants = [f"(SHOW GRANTS failed: {exc})"]
        problems.append("SHOW GRANTS failed")
    print("SHOW GRANTS:", flush=True)
    for g in grants:
        print(f"  {g}", flush=True)
        g_upper = str(g).upper()
        if "_PROD" in g_upper and "ALL PRIVILEGES" in g_upper:
            problems.append(f"prod grant looks writable: {g}")
    mapping = schema_mapping(source_stage, target_stage)
    print("Schema sizes:", flush=True)
    for src, dst in mapping.items():
        src_sz = schema_size_bytes(cur, src) if _schema_exists(cur, src) else None
        dst_sz = schema_size_bytes(cur, dst) if _schema_exists(cur, dst) else None
        print(f"  {src}: {src_sz}", flush=True)
        print(f"  {dst}: {dst_sz}", flush=True)
        if src_sz is None:
            problems.append(f"source schema {src} does not exist")
        if dst_sz is None:
            problems.append(f"target schema {dst} does not exist (pre-create empty databases as admin)")
    if problems:
        print("PREFLIGHT FAILED:", flush=True)
        for p in problems:
            print(f"  {p}", flush=True)
        return 1
    print("PREFLIGHT OK", flush=True)
    return 0


def run_copy(
    cur,
    conn,
    source_stage: str,
    target_stage: str,
    *,
    drop_existing: bool,
    dry_run: bool,
) -> dict:
    mapping = schema_mapping(source_stage, target_stage)
    sizes_before = {}
    for src, dst in mapping.items():
        sizes_before[src] = schema_size_bytes(cur, src) if _schema_exists(cur, src) else None
        sizes_before[dst] = schema_size_bytes(cur, dst) if _schema_exists(cur, dst) else None
        LOG.info("size %s = %s", src, sizes_before[src])
        LOG.info("size %s = %s", dst, sizes_before[dst])
        if sizes_before[src] is None:
            raise RuntimeError(f"source schema {src} does not exist")
        if not _schema_exists(cur, dst):
            raise RuntimeError(f"target schema {dst} does not exist; pre-create it as admin")
    if dry_run:
        for src, dst in mapping.items():
            tables = list_base_tables(cur, src)
            views = list_views(cur, src)
            LOG.info("dry-run would copy %s → %s tables=%s views=%s", src, dst, tables, views)
        LOG.info("dry-run would rewrite registry pointers and neutralize non-empty tokens")
        return {"dry_run": True, "sizes_before": sizes_before}

    copied = {}
    for src, dst in mapping.items():
        if drop_existing:
            wipe_schema(cur, dst)
            conn.commit()
        copied[dst] = copy_schema(cur, src, dst, mapping)
        conn.commit()
        LOG.info("Committed copy %s → %s", src, dst)
    pointers = rewrite_registry_pointers(cur, mapping, target_stage)
    tokens = neutralize_tokens(cur, target_stage)
    conn.commit()
    sizes_after = {dst: schema_size_bytes(cur, dst) for _, dst in mapping.items()}
    return {
        "dry_run": False,
        "copied": copied,
        "pointers": pointers,
        "tokens": tokens,
        "sizes_before": sizes_before,
        "sizes_after": sizes_after,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Copy prod into the rehearsal replica")
    parser.add_argument("--from", dest="source_stage", default="prod")
    parser.add_argument("--to", dest="target_stage", default="rehearsal")
    parser.add_argument("--drop-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Confirm scoped user, grants, and schema sizes, then exit",
    )
    args = parser.parse_args(argv)
    validate_copy_args(args.source_stage, args.target_stage)
    _load_env(REHEARSAL_STAGE)
    conn = _connect(read_timeout=COPY_TIMEOUT_S, write_timeout=COPY_TIMEOUT_S)
    try:
        with conn.cursor() as cur:
            if args.preflight:
                return report_preflight(cur, args.source_stage, args.target_stage)
            result = run_copy(
                cur,
                conn,
                args.source_stage,
                args.target_stage,
                drop_existing=args.drop_existing,
                dry_run=args.dry_run,
            )
        print(result, flush=True)
        return 0
    except Exception:
        conn.rollback()
        LOG.exception("prepare_rehearsal failed")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
