#!/usr/bin/env python3
"""
Migrate MySQL schemas from source RDS to TiDB with schema renaming, resilience, and view recreation.

Also bootstraps paxminer/slackblast/weaselbot admin schemas on the target, recreates qsignups vw_* views,
and widens encrypted token columns before optional Fernet encryption.

Read-only on source. Uses exponential backoff on connection errors.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv

# Env file is loaded in main() from .env.migration.<env> (see --env).

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("migrate")

# MySQL / MariaDB: do not retry these — wrong grants won't heal with backoff
ACCESS_DENIED_MYSQL_CODES = frozenset({1044, 1045, 1142, 1227})

LIST_TABLES_SQL_TEMPLATE = (
    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME"
)


def _mysql_error_code(exc: BaseException) -> int | None:
    if isinstance(exc, pymysql.err.OperationalError) and exc.args:
        try:
            return int(exc.args[0])
        except (TypeError, ValueError):
            return None
    return None


def is_access_denied_error(exc: BaseException) -> bool:
    code = _mysql_error_code(exc)
    return code in ACCESS_DENIED_MYSQL_CODES if code is not None else False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_migration_report(
    source_host: str,
    source_port: int,
    target_host: str,
    target_port: int,
) -> dict[str, Any]:
    return {
        "started_at": _iso_now(),
        "finished_at": None,
        "source": {"host": source_host, "port": source_port},
        "target": {"host": target_host, "port": target_port},
        "schemas": {},
        "fixups_errors": [],
        "column_widens": [],
        "summary": {
            "schemas_completed": 0,
            "schemas_failed": 0,
            "schemas_skipped": 0,
            "total_tables": 0,
            "total_rows": 0,
            "total_errors": 0,
        },
    }


def empty_schema_report_entry(target_schema: str) -> dict[str, Any]:
    return {
        "target_schema": target_schema,
        "status": "pending",
        "tables": [],
        "views": [],
        "errors": [],
    }


def report_append_error(
    entry: dict[str, Any],
    operation: str,
    sql: str,
    exc: BaseException | None = None,
    message: str | None = None,
) -> None:
    err_msg = message if message is not None else (str(exc) if exc is not None else "")
    entry["errors"].append(
        {
            "operation": operation,
            "sql": sql,
            "error_code": _mysql_error_code(exc) if exc is not None else None,
            "error_message": err_msg,
        }
    )


def write_migration_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2))


def format_migration_receipt(
    report: dict[str, Any],
    env_suffix: str,
    report_path: Path | None = None,
) -> str:
    """Human-readable summary matching console-oriented deploy receipts."""
    lines: list[str] = []
    lines.append("=== Migration receipt ===")
    lines.append(f"Environment suffix: {env_suffix}")
    src = report.get("source") or {}
    tgt = report.get("target") or {}
    lines.append(f"Source: {src.get('host')}:{src.get('port')}")
    lines.append(f"Target: {tgt.get('host')}:{tgt.get('port')}")
    lines.append(f"Started (UTC): {report.get('started_at')}")
    lines.append(f"Finished (UTC): {report.get('finished_at')}")
    try:
        start = report.get("started_at")
        end = report.get("finished_at")
        if start and end:
            t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
            delta = t1 - t0
            lines.append(f"Duration: {delta}")
    except (TypeError, ValueError):
        pass
    lines.append("")
    lines.append(f"{'Schema (source)':<28} {'Target':<28} {'Status':<18} {'Tables ok':<10} {'Rows':<12}")
    lines.append("-" * 96)
    schemas = report.get("schemas") or {}
    for source_name in sorted(schemas.keys()):
        se = schemas[source_name]
        target = se.get("target_schema", "")
        status = se.get("status", "")
        ok_tables = sum(1 for t in se.get("tables", []) if t.get("status") == "ok")
        rows = sum(int(t.get("rows_migrated") or 0) for t in se.get("tables", []) if t.get("status") == "ok")
        lines.append(f"{source_name:<28} {target:<28} {status:<18} {ok_tables:<10} {rows:<12}")
        for err in se.get("errors", [])[:5]:
            msg = err.get("error_message") or err.get("operation") or ""
            lines.append(f"  ERROR: {msg[:200]}")
        if len(se.get("errors", [])) > 5:
            lines.append(f"  ... and {len(se['errors']) - 5} more errors in this schema")
    lines.append("")
    fix_errs = report.get("fixups_errors") or []
    if fix_errs:
        lines.append("--- Fixup / post-step errors ---")
        for fe in fix_errs[:20]:
            lines.append(f"  {fe.get('operation', fe)}: {fe.get('error', fe)}")
        if len(fix_errs) > 20:
            lines.append(f"  ... and {len(fix_errs) - 20} more")
        lines.append("")
    summ = report.get("summary") or {}
    lines.append("=== Summary ===")
    lines.append(f"  Schemas completed: {summ.get('schemas_completed')}")
    lines.append(f"  Schemas failed:     {summ.get('schemas_failed')}")
    lines.append(f"  Schemas skipped:    {summ.get('schemas_skipped')}")
    lines.append(f"  Tables (reported):  {summ.get('total_tables')}")
    lines.append(f"  Rows migrated (ok): {summ.get('total_rows')}")
    lines.append(f"  Total errors:       {summ.get('total_errors')}")
    enc = report.get("encryption_fixups")
    if enc:
        lines.append(f"  Encryption fixups:  {enc}")
    s3m = report.get("s3_image_migration")
    if s3m:
        lines.append(f"  S3 image migration: {s3m}")
    lines.append("")
    lines.append(f"JSON report: {report_path if report_path is not None else Path(__file__).resolve().parent / 'migration_report.json'}")
    return "\n".join(lines) + "\n"


def finalize_migration_summary(report: dict[str, Any]) -> None:
    schemas = report["schemas"]
    completed = sum(1 for v in schemas.values() if v.get("status") == "completed")
    failed = sum(
        1
        for v in schemas.values()
        if v.get("status") in ("permission_denied", "error", "empty_warning")
    )
    skipped = sum(1 for v in schemas.values() if v.get("status") == "skipped_by_flag")
    total_rows = 0
    total_tables = 0
    for v in schemas.values():
        for t in v.get("tables", []):
            total_tables += 1
            if t.get("status") == "ok":
                total_rows += int(t.get("rows_migrated") or 0)
    # One row per failure in schema errors + fixups (avoid double-counting table/view rows)
    total_errors = sum(len(v.get("errors", [])) for v in schemas.values()) + len(
        report.get("fixups_errors", [])
    )
    report["summary"] = {
        "schemas_completed": completed,
        "schemas_failed": failed,
        "schemas_skipped": skipped,
        "total_tables": total_tables,
        "total_rows": total_rows,
        "total_errors": total_errors,
    }


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _connect_kwargs(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str | None,
    tls_enabled: bool,
) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "connect_timeout": 30,
        "read_timeout": 300,
        "write_timeout": 300,
    }
    if database:
        kw["database"] = database
    if tls_enabled:
        kw["ssl"] = ssl.create_default_context()
    return kw


def with_retry(fn, operation_name: str):
    max_retries = int(os.environ.get("MAX_RETRIES", "10"))
    base = float(os.environ.get("BACKOFF_BASE_SECONDS", "2"))
    cap = float(os.environ.get("BACKOFF_MAX_SECONDS", "60"))
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except pymysql.err.OperationalError as e:
            if is_access_denied_error(e):
                raise
            last_exc = e
            wait = min(cap, base * (2**attempt)) + random.uniform(0, 0.5)
            LOG.warning("%s failed (attempt %s/%s): %s; sleeping %.1fs", operation_name, attempt + 1, max_retries, e, wait)
            time.sleep(wait)
        except pymysql.err.InterfaceError as e:
            last_exc = e
            wait = min(cap, base * (2**attempt)) + random.uniform(0, 0.5)
            LOG.warning("%s interface error (attempt %s/%s): %s; sleeping %.1fs", operation_name, attempt + 1, max_retries, e, wait)
            time.sleep(wait)
    raise last_exc


# Source schemas the migration user can read (national RDS). paxminer/slackblast/weaselbot
# are bootstrapped on the target at the start of this script (no source read access).
F3STCHARLES_SOURCE = "f3stcharles"

TOKEN_VARCHAR_LEN = 512


def parse_qsignups_team_ids() -> list[str] | None:
    """Comma-separated Slack team IDs for filtering f3stcharles qsignups_* copy. None = no filter."""
    raw = (os.environ.get("QSIGNUPS_TEAM_IDS") or "").strip()
    if not raw:
        return None
    out = [p.strip() for p in raw.split(",") if p.strip()]
    return out or None


def build_qsignups_copy_filter(
    source_schema: str,
    table: str,
    team_ids: list[str],
) -> tuple[str, tuple[Any, ...]]:
    """Return (WHERE clause with leading WHERE, bind tuple) for qsignups table copy."""
    placeholders = ", ".join(["%s"] * len(team_ids))
    params: tuple[Any, ...] = tuple(team_ids)
    if table == "qsignups_features":
        clause = (
            f"WHERE region_id IN (SELECT id FROM `{source_schema}`.`qsignups_regions` "
            f"WHERE team_id IN ({placeholders}))"
        )
        return clause, params
    clause = f"WHERE team_id IN ({placeholders})"
    return clause, params


def default_schema_map(env_suffix: str) -> dict[str, str]:
    qs = os.environ.get("QSIGNUPS_SCHEMA", "qsignups")
    return {
        "f3ttown": f"f3ttown_{env_suffix}",
        "f3scissortail": f"f3scissortail_{env_suffix}",
        F3STCHARLES_SOURCE: f"{qs}_{env_suffix}",
    }


def target_admin_schema_names(stage: str) -> tuple[str, str, str, str]:
    """Target DB names: paxminer, slackblast, weaselbot, qsignups (with stage suffix)."""
    pm = os.environ.get("PAXMINER_SCHEMA", "paxminer").strip()
    sb = os.environ.get("SLACKBLAST_SCHEMA", "slackblast").strip()
    wb = os.environ.get("WEASELBOT_SCHEMA", "weaselbot").strip()
    qs = os.environ.get("QSIGNUPS_SCHEMA", "qsignups").strip()
    return f"{pm}_{stage}", f"{sb}_{stage}", f"{wb}_{stage}", f"{qs}_{stage}"


def _row_get(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row:
            return row[k]
    lower = {str(a).lower(): a for a in row}
    for k in keys:
        lk = k.lower()
        if lk in lower:
            return row[lower[lk]]
    raise KeyError(keys)


def extract_view_body(create_sql: str) -> str:
    """Return SELECT ... part after AS."""
    m = re.search(r"\bAS\b\s+(.*)\s*$", create_sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return create_sql.strip().rstrip(";")
    return m.group(1).strip().rstrip(";")


def build_target_view_ddl(
    create_sql: str,
    source_schema: str,
    target_schema: str,
    view: str,
    source_to_target: dict[str, str],
) -> str:
    s = clean_view_ddl(create_sql, source_to_target)
    body = extract_view_body(s)
    return f"CREATE OR REPLACE VIEW `{target_schema}`.`{view}` AS {body}"


def clean_view_ddl(create_sql: str, source_to_target: dict[str, str]) -> str:
    """Strip DEFINER/ALGORITHM/SQL SECURITY; rewrite schema names."""
    s = create_sql
    s = re.sub(r"DEFINER\s*=\s*`[^`]+`@`[^`]+`\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"ALGORITHM\s*=\s*UNDEFINED\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"ALGORITHM\s*=\s*MERGE\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"ALGORITHM\s*=\s*TEMPTABLE\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"SQL\s+SECURITY\s+DEFINER\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"SQL\s+SECURITY\s+INVOKER\s*", "", s, flags=re.IGNORECASE)
    # Replace backtick-wrapped schema names (longest keys first)
    for src, tgt in sorted(source_to_target.items(), key=lambda x: -len(x[0])):
        s = re.sub(rf"`{re.escape(src)}`", f"`{tgt}`", s)
    return s


def load_checkpoint(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("done", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_checkpoint(path: Path, done: set[str]) -> None:
    path.write_text(json.dumps({"done": sorted(done)}, indent=2))


def migrate_table(
    source_conn,
    target_conn,
    source_schema: str,
    target_schema: str,
    table: str,
    batch_size: int,
    read_delay: float,
    where_clause: str | None = None,
    where_params: tuple[Any, ...] | None = None,
) -> int:
    wp = where_params or ()

    def count_rows():
        with source_conn.cursor() as cur:
            sql = f"SELECT COUNT(*) AS c FROM `{source_schema}`.`{table}`"
            if where_clause:
                sql += f" {where_clause}"
            cur.execute(sql, wp)
            row = cur.fetchone()
            return _row_get(row, "c", "C")

    total = with_retry(count_rows, f"COUNT {source_schema}.{table}")
    LOG.info(
        "Table %s.%s: %s rows to copy%s",
        source_schema,
        table,
        total,
        f" (filtered)" if where_clause else "",
    )
    if total == 0:
        return 0

    offset = 0
    inserted = 0
    while offset < total:
        time.sleep(read_delay)

        def fetch_batch():
            with source_conn.cursor() as cur:
                sql = f"SELECT * FROM `{source_schema}`.`{table}`"
                if where_clause:
                    sql += f" {where_clause}"
                sql += " LIMIT %s OFFSET %s"
                cur.execute(sql, wp + (batch_size, offset))
                return cur.fetchall()

        rows = with_retry(lambda: fetch_batch(), f"SELECT {source_schema}.{table}")
        if not rows:
            break
        cols = list(rows[0].keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(f"`{c}`" for c in cols)
        insert_sql = f"INSERT INTO `{target_schema}`.`{table}` ({col_list}) VALUES ({placeholders})"
        values = [[row[c] for c in cols] for row in rows]

        def do_insert():
            with target_conn.cursor() as cur:
                cur.executemany(insert_sql, values)
            target_conn.commit()

        with_retry(do_insert, f"INSERT {target_schema}.{table}")
        inserted += len(rows)
        offset += len(rows)
        LOG.info("  ... inserted %s / %s", inserted, total)
    return inserted


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s LIMIT 1",
        (schema, table),
    )
    return cur.fetchone() is not None


def _ddl_paxminer_regions(schema: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS `{schema}`.`regions` (
  `region` varchar(45) NOT NULL,
  `slack_token` varchar(512) NOT NULL,
  `schema_name` varchar(45) DEFAULT NULL,
  `active` tinyint DEFAULT 1,
  `firstf_channel` varchar(45) DEFAULT NULL,
  `contact` varchar(45) DEFAULT NULL,
  `send_pax_charts` tinyint DEFAULT 0,
  `send_ao_leaderboard` tinyint DEFAULT 0,
  `send_q_charts` tinyint DEFAULT 0,
  `send_region_leaderboard` tinyint DEFAULT 0,
  `scrape_backblasts` tinyint DEFAULT 0,
  `send_achievements` tinyint DEFAULT 1,
  `send_aoq_reports` tinyint DEFAULT 1,
  `send_achievement_leaderboard` tinyint DEFAULT 1,
  `achievement_channel` varchar(100) DEFAULT NULL,
  `kotter_channel` varchar(100) DEFAULT NULL,
  `NO_POST_THRESHOLD` int DEFAULT 2,
  `REMINDER_WEEKS` int DEFAULT 2,
  `HOME_AO_CAPTURE` int DEFAULT 8,
  `NO_Q_THRESHOLD_WEEKS` int DEFAULT 4,
  `NO_Q_THRESHOLD_POSTS` int DEFAULT 4,
  `comments` text,
  PRIMARY KEY (`region`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _ddl_slackblast_regions(schema: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS `{schema}`.`regions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `team_id` varchar(100) NOT NULL,
  `workspace_name` varchar(100) DEFAULT NULL,
  `bot_token` varchar(512) DEFAULT NULL,
  `paxminer_schema` varchar(100) DEFAULT NULL,
  `email_enabled` tinyint(1) DEFAULT 0,
  `email_server` varchar(100) DEFAULT NULL,
  `email_server_port` int DEFAULT NULL,
  `email_user` varchar(100) DEFAULT NULL,
  `email_password` longtext,
  `email_to` varchar(100) DEFAULT NULL,
  `email_option_show` tinyint(1) DEFAULT 0,
  `postie_format` tinyint(1) DEFAULT 1,
  `editing_locked` tinyint(1) DEFAULT 0,
  `default_destination` varchar(30) DEFAULT 'ao_channel',
  `backblast_moleskin_template` json DEFAULT NULL,
  `preblast_moleskin_template` json DEFAULT NULL,
  `strava_enabled` tinyint(1) DEFAULT 1,
  `custom_fields` json DEFAULT NULL,
  `welcome_dm_enable` tinyint DEFAULT NULL,
  `welcome_dm_template` json DEFAULT NULL,
  `welcome_channel_enable` tinyint DEFAULT NULL,
  `welcome_channel` varchar(100) DEFAULT NULL,
  `post_achievements_to_ao` tinyint(1) DEFAULT 0,
  `created` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _ddl_weaselbot_regions(schema: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS `{schema}`.`regions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `team_id` varchar(100) NOT NULL,
  `workspace_name` varchar(100) DEFAULT NULL,
  `slack_token` varchar(512) DEFAULT NULL,
  `paxminer_schema` varchar(100) DEFAULT NULL,
  `email_enabled` tinyint(1) DEFAULT 0,
  `email_server` varchar(100) DEFAULT NULL,
  `email_server_port` int DEFAULT NULL,
  `email_user` varchar(100) DEFAULT NULL,
  `email_password` longtext,
  `email_to` varchar(100) DEFAULT NULL,
  `email_option_show` tinyint(1) DEFAULT 0,
  `postie_format` tinyint(1) DEFAULT 1,
  `editing_locked` tinyint(1) DEFAULT 0,
  `default_destination` varchar(30) DEFAULT 'ao_channel',
  `backblast_moleskin_template` json DEFAULT NULL,
  `preblast_moleskin_template` json DEFAULT NULL,
  `strava_enabled` tinyint(1) DEFAULT 1,
  `custom_fields` json DEFAULT NULL,
  `welcome_dm_enable` tinyint DEFAULT NULL,
  `welcome_dm_template` json DEFAULT NULL,
  `welcome_channel_enable` tinyint DEFAULT NULL,
  `welcome_channel` varchar(100) DEFAULT NULL,
  `send_achievements` tinyint(1) DEFAULT 1,
  `send_aoq_reports` tinyint(1) DEFAULT 1,
  `achievement_channel` varchar(100) DEFAULT NULL,
  `default_siteq` varchar(45) DEFAULT NULL,
  `NO_POST_THRESHOLD` int DEFAULT 2,
  `REMINDER_WEEKS` int DEFAULT 2,
  `HOME_AO_CAPTURE` int DEFAULT 8,
  `NO_Q_THRESHOLD_WEEKS` int DEFAULT 4,
  `NO_Q_THRESHOLD_POSTS` int DEFAULT 4,
  `created` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _ddl_slackblast_users(schema: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS `{schema}`.`slackblast_users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `team_id` varchar(100) DEFAULT NULL,
  `user_id` varchar(100) DEFAULT NULL,
  `strava_access_token` varchar(512) DEFAULT NULL,
  `strava_refresh_token` varchar(512) DEFAULT NULL,
  `strava_expires_at` datetime DEFAULT NULL,
  `strava_athlete_id` int DEFAULT NULL,
  `created` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _seed_slackblast_regions(cur, conn: Any, sb_s: str, stage: str) -> None:
    """Optional rows in slackblast regions (team_id + paxminer_schema)."""
    tt_tid = (os.environ.get("MIGRATION_SEED_TEAM_F3TTOWN") or "").strip()
    sci_tid = (os.environ.get("MIGRATION_SEED_TEAM_F3SCISSORTAIL") or "").strip()
    if not tt_tid and not sci_tid:
        LOG.info(
            "Skipping slackblast regions seed (set MIGRATION_SEED_TEAM_F3TTOWN and/or MIGRATION_SEED_TEAM_F3SCISSORTAIL)"
        )
        return
    f3ttown = f"f3ttown_{stage}"
    f3sci = f"f3scissortail_{stage}"
    pairs: list[tuple[str, str]] = []
    if tt_tid:
        pairs.append((tt_tid, f3ttown))
    if sci_tid:
        pairs.append((sci_tid, f3sci))
    for team_id, pax_schema in pairs:
        cur.execute(
            f"SELECT COUNT(*) AS c FROM `{sb_s}`.`regions` "
            "WHERE `team_id`=%s AND `paxminer_schema`=%s",
            (team_id, pax_schema),
        )
        row = cur.fetchone()
        n = int(_row_get(row, "c", "C") or 0)
        if n > 0:
            LOG.info(
                "Skip seed %s.regions (team_id=%s paxminer_schema=%s already present)",
                sb_s,
                team_id,
                pax_schema,
            )
            continue
        cur.execute(
            f"INSERT INTO `{sb_s}`.`regions` (`team_id`, `paxminer_schema`) VALUES (%s, %s)",
            (team_id, pax_schema),
        )
        LOG.info(
            "Seeded %s.regions team_id=%s paxminer_schema=%s",
            sb_s,
            team_id,
            pax_schema,
        )
    conn.commit()


def pre_migration_bootstrap_schemas(conn: Any, stage: str) -> None:
    pm_s, sb_s, _, _ = target_admin_schema_names(stage)
    f3ttown = f"f3ttown_{stage}"
    f3sci = f"f3scissortail_{stage}"
    with conn.cursor() as cur:
        for db in (pm_s, sb_s):
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}`")
        conn.commit()
        cur.execute(_ddl_paxminer_regions(pm_s))
        cur.execute(_ddl_slackblast_regions(sb_s))
        cur.execute(_ddl_slackblast_users(sb_s))
        conn.commit()
        cur.execute(
            f"SELECT COUNT(*) AS seed_cnt FROM `{pm_s}`.`regions` WHERE `schema_name` IN (%s, %s)",
            (f3ttown, f3sci),
        )
        row = cur.fetchone()
        n = int(_row_get(row, "seed_cnt", "SEED_CNT") or 0)
        if n == 0:
            cur.execute(
                f"""
                INSERT INTO `{pm_s}`.`regions`
                (`region`, `slack_token`, `schema_name`, `active`)
                VALUES
                (%s, '', %s, 1),
                (%s, '', %s, 1)
                """,
                ("f3ttown", f3ttown, "f3scissortail", f3sci),
            )
            LOG.info("Inserted paxminer.regions rows (empty slack_token; deploy/Lambda upserts encrypted token)")
        else:
            LOG.info("Skipping seed: paxminer.regions already has rows for target schemas")
        conn.commit()
        _seed_slackblast_regions(cur, conn, sb_s, stage)
    LOG.info("Bootstrap complete: %s, %s", pm_s, sb_s)


def _q_signups(schema: str, name: str) -> str:
    return f"`{schema}`.`{name}`"


def _ddl_vw_weekly_events(schema: str) -> str:
    w, a = _q_signups(schema, "qsignups_weekly"), _q_signups(schema, "qsignups_aos")
    return f"""
CREATE OR REPLACE VIEW {_q_signups(schema, "vw_weekly_events")} AS
SELECT w.*, a.ao_display_name
FROM {w} w
INNER JOIN {a} a
ON w.ao_channel_id = a.ao_channel_id AND w.team_id = a.team_id
ORDER BY REPLACE(ao_display_name, 'The ', ''),
  FIELD(event_day_of_week, 'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'),
  event_time
""".strip()


def _ddl_vw_aos_sort(schema: str) -> str:
    t = _q_signups(schema, "qsignups_aos")
    return f"""
CREATE OR REPLACE VIEW {_q_signups(schema, "vw_aos_sort")} AS
SELECT *
FROM {t}
ORDER BY REPLACE(ao_display_name, 'The ', '')
""".strip()


def _ddl_vw_master_events(schema: str) -> str:
    m, a = _q_signups(schema, "qsignups_master"), _q_signups(schema, "qsignups_aos")
    return f"""
CREATE OR REPLACE VIEW {_q_signups(schema, "vw_master_events")} AS
SELECT m.*, a.ao_display_name, a.ao_location_subtitle
FROM {m} m
LEFT JOIN {a} a
ON m.team_id = a.team_id
  AND m.ao_channel_id = a.ao_channel_id
""".strip()


def post_migration_create_qsignups_views(conn: Any, qsignups_schema: str, report: dict[str, Any]) -> None:
    required = ("qsignups_weekly", "qsignups_aos", "qsignups_master")
    with conn.cursor(DictCursor) as cur:
        if not all(_table_exists(cur, qsignups_schema, t) for t in required):
            LOG.info(
                "Skipping qsignups views: missing base tables on %s (need %s)",
                qsignups_schema,
                ", ".join(required),
            )
            return
    try:
        for view_name, ddl in (
            ("vw_weekly_events", _ddl_vw_weekly_events(qsignups_schema)),
            ("vw_aos_sort", _ddl_vw_aos_sort(qsignups_schema)),
            ("vw_master_events", _ddl_vw_master_events(qsignups_schema)),
        ):
            LOG.info("Creating view %s.%s", qsignups_schema, view_name)
            with conn.cursor() as c:
                c.execute(ddl)
            conn.commit()
    except Exception as e:
        report["fixups_errors"].append({"operation": "qsignups views", "error": str(e)})
        LOG.exception("Failed to create qsignups views on %s", qsignups_schema)


def _column_meta(cur, schema: str, table: str, column: str) -> tuple[str | None, int | None]:
    cur.execute(
        """
        SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """,
        (schema, table, column),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    dt = _row_get(row, "DATA_TYPE", "data_type")
    maxlen = row.get("CHARACTER_MAXIMUM_LENGTH") or row.get("character_maximum_length")
    return (str(dt).lower() if dt else None), (int(maxlen) if maxlen is not None else None)


def _widen_varchar_column(cur, conn, schema: str, table: str, column: str) -> str:
    """Widen VARCHAR for encrypted token storage. Returns status string for audit."""
    if not _table_exists(cur, schema, table):
        LOG.info("Skip widen %s.%s.%s (no table)", schema, table, column)
        return "skipped_no_table"
    dt, maxlen = _column_meta(cur, schema, table, column)
    if dt is None:
        LOG.info("Skip widen %s.%s.%s (no column)", schema, table, column)
        return "skipped_no_column"
    if dt in ("text", "mediumtext", "longtext", "blob", "mediumblob", "longblob", "json"):
        LOG.info("Skip widen %s.%s.%s (already %s)", schema, table, column, dt)
        return f"skipped_unbounded_{dt}"
    if dt == "varchar" and maxlen is not None and maxlen >= TOKEN_VARCHAR_LEN:
        LOG.info("Skip widen %s.%s.%s (already varchar(%s))", schema, table, column, maxlen)
        return "already_wide"
    sql = f"ALTER TABLE `{schema}`.`{table}` MODIFY COLUMN `{column}` VARCHAR({TOKEN_VARCHAR_LEN})"
    LOG.info("Running: %s", sql)
    cur.execute(sql)
    conn.commit()
    dt_after, maxlen_after = _column_meta(cur, schema, table, column)
    if maxlen_after is not None and maxlen_after < TOKEN_VARCHAR_LEN:
        raise RuntimeError(
            f"ALTER TABLE did not take effect: {schema}.{table}.{column} "
            f"still {dt_after}({maxlen_after}) after ALTER to VARCHAR({TOKEN_VARCHAR_LEN})"
        )
    LOG.info("Widened %s.%s.%s to VARCHAR(%s)", schema, table, column, TOKEN_VARCHAR_LEN)
    return "widened"


def _widen_google_auth_column(cur, conn, schema: str, table: str, column: str) -> str:
    if not _table_exists(cur, schema, table):
        return "skipped_no_table"
    dt, _ = _column_meta(cur, schema, table, column)
    if dt is None:
        return "skipped_no_column"
    if dt in ("longtext", "mediumtext", "text"):
        LOG.info("Skip widen %s.%s.%s (already %s)", schema, table, column, dt)
        return "already_longtext"
    sql = f"ALTER TABLE `{schema}`.`{table}` MODIFY COLUMN `{column}` LONGTEXT"
    LOG.info("Running: %s", sql)
    cur.execute(sql)
    conn.commit()
    return "widened_to_longtext"


def pre_encryption_widen_columns(conn: Any, stage: str, report: dict[str, Any]) -> None:
    report.setdefault("column_widens", [])
    pm, sb, wb, qs = target_admin_schema_names(stage)
    targets: list[tuple[str, str, str]] = [
        (pm, "regions", "slack_token"),
        (sb, "regions", "bot_token"),
        (sb, "slackblast_users", "strava_access_token"),
        (sb, "slackblast_users", "strava_refresh_token"),
        (wb, "regions", "slack_token"),
        (qs, "qsignups_regions", "bot_token"),
    ]
    cur = conn.cursor(DictCursor)
    for schema, table, col in targets:
        entry: dict[str, Any] = {"schema": schema, "table": table, "column": col}
        try:
            entry["result"] = _widen_varchar_column(cur, conn, schema, table, col)
        except Exception as e:
            entry["result"] = "error"
            entry["error"] = str(e)
            report["fixups_errors"].append(
                {"operation": f"widen {schema}.{table}.{col}", "error": str(e)}
            )
            LOG.warning("%s.%s.%s: %s", schema, table, col, e)
        report["column_widens"].append(entry)
    gentry: dict[str, Any] = {
        "schema": qs,
        "table": "qsignups_regions",
        "column": "google_auth_data",
    }
    try:
        gentry["result"] = _widen_google_auth_column(cur, conn, qs, "qsignups_regions", "google_auth_data")
    except Exception as e:
        gentry["result"] = "error"
        gentry["error"] = str(e)
        report["fixups_errors"].append({"operation": "widen google_auth_data", "error": str(e)})
        LOG.warning("google_auth_data: %s", e)
    report["column_widens"].append(gentry)
    LOG.info("Column width fixes for encryption done.")


def post_migration_encrypt_secrets(conn, schema_map: dict[str, str], report: dict, *, stage: str) -> None:
    # Unlike deployed Lambdas (which require DB_ENCRYPTION_KEY at startup), this step stays optional:
    # initial loads may run before a key exists. If you set a key, use the same min length as production (16+).
    key = (os.environ.get("DB_ENCRYPTION_KEY") or "").strip()
    if not key or key == "123" or len(key) < 16:
        LOG.info(
            "Post-migration encryption skipped (DB_ENCRYPTION_KEY unset, placeholder, or shorter than 16 chars)"
        )
        return
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from common.encryption import encrypt_field

    os.environ["DB_ENCRYPTION_KEY"] = key
    cur = conn.cursor(DictCursor)
    enc_stats: dict[str, int] = {}

    def run_enc(schema: str, table: str, col: str, pk_col: str) -> None:
        if not schema or not _table_exists(cur, schema, table):
            return
        try:
            cur.execute(f"SELECT `{pk_col}`, `{col}` FROM `{schema}`.`{table}` WHERE `{col}` IS NOT NULL AND `{col}` != ''")
        except Exception as e:
            report["fixups_errors"].append({"operation": f"encrypt scan {schema}.{table}.{col}", "error": str(e)})
            LOG.warning("Encrypt scan failed %s.%s: %s", schema, table, e)
            return
        n = 0
        for row in cur.fetchall():
            rid = row[pk_col]
            val = row[col]
            if val is None:
                continue
            s = val if isinstance(val, str) else str(val)
            if not s.strip() or s.startswith("gAAAAA"):
                continue
            try:
                enc = encrypt_field(s)
                cur.execute(
                    f"UPDATE `{schema}`.`{table}` SET `{col}`=%s WHERE `{pk_col}`=%s",
                    (enc, rid),
                )
                n += 1
            except Exception as e:
                report["fixups_errors"].append(
                    {"operation": f"encrypt {schema}.{table}.{col}", "pk": str(rid), "error": str(e)}
                )
        if n:
            enc_stats[f"{schema}.{table}.{col}"] = n
            LOG.info("Encrypted %s rows: %s.%s.%s", n, schema, table, col)

    pm, sb, wb, qs_from_env = target_admin_schema_names(stage)
    qs = schema_map.get(F3STCHARLES_SOURCE) or qs_from_env

    if pm:
        run_enc(pm, "regions", "slack_token", "region")
    if wb:
        run_enc(wb, "regions", "slack_token", "team_id")
    if sb:
        run_enc(sb, "regions", "bot_token", "id")
        run_enc(sb, "slackblast_users", "strava_access_token", "id")
        run_enc(sb, "slackblast_users", "strava_refresh_token", "id")
    if qs:
        run_enc(qs, "qsignups_regions", "bot_token", "team_id")

    # qsignups google_auth_data: JSON or str — encrypt JSON string form
    if qs and _table_exists(cur, qs, "qsignups_regions"):
        try:
            cur.execute(f"SELECT team_id, google_auth_data FROM `{qs}`.`qsignups_regions` WHERE google_auth_data IS NOT NULL")
        except Exception as e:
            report["fixups_errors"].append({"operation": "encrypt scan google_auth_data", "error": str(e)})
        else:
            n = 0
            for row in cur.fetchall():
                tid = row["team_id"]
                blob = row["google_auth_data"]
                if blob is None:
                    continue
                if isinstance(blob, (dict, list)):
                    raw = json.dumps(blob)
                else:
                    raw = str(blob)
                if not raw.strip() or raw.startswith("gAAAAA"):
                    continue
                try:
                    enc = encrypt_field(raw)
                    cur.execute(
                        f"UPDATE `{qs}`.`qsignups_regions` SET google_auth_data=%s WHERE team_id=%s",
                        (enc, tid),
                    )
                    n += 1
                except Exception as e:
                    report["fixups_errors"].append(
                        {"operation": "encrypt google_auth_data", "team_id": str(tid), "error": str(e)}
                    )
            if n:
                enc_stats[f"{qs}.qsignups_regions.google_auth_data"] = n
                LOG.info("Encrypted %s google_auth_data rows", n)

    conn.commit()
    report["encryption_fixups"] = enc_stats


def _s3_url_key_and_skip(url: str, new_bucket: str) -> tuple[str | None, bool]:
    """Parse an HTTP(S) URL; return (object_key, skip_if_already_on_new_bucket)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return None, False
    netloc = (parsed.netloc or "").lower()
    nb = new_bucket.lower()
    if netloc == f"{nb}.s3.amazonaws.com" or (
        netloc.startswith(f"{nb}.s3.") and netloc.endswith(".amazonaws.com")
    ):
        return None, True
    path = (parsed.path or "").lstrip("/")
    if not path:
        return None, False
    # Virtual-hosted style only: https://bucket.s3.../key — path is the object key
    return path, False


def _s3_object_key_from_bucket_url(url: str, bucket: str) -> str | None:
    """If url is an object in bucket (virtual-hosted style), return the object key; else None."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    netloc = (parsed.netloc or "").lower()
    nb = bucket.lower()
    if netloc == f"{nb}.s3.amazonaws.com" or (
        netloc.startswith(f"{nb}.s3.") and netloc.endswith(".amazonaws.com")
    ):
        path = (parsed.path or "").lstrip("/")
        return path if path else None
    return None


def post_migration_s3_images(conn, schema_map: dict[str, str], report: dict) -> None:
    new_bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not new_bucket:
        LOG.info("S3 image migration skipped (set IMAGE_S3_BUCKET)")
        return
    new_base = f"https://{new_bucket}.s3.amazonaws.com"

    try:
        import boto3
        import requests
    except ImportError:
        LOG.warning("boto3/requests not installed; skipping S3 image migration")
        return

    s3 = boto3.client("s3")
    cur = conn.cursor(DictCursor)
    stats: dict[str, Any] = {"downloaded": 0, "updated_rows": 0, "errors": [], "url_mappings": []}

    uploaded_keys: set[str] = set()
    targets = list(schema_map.values())
    current_schema: str = ""
    current_row: dict[str, Any] = {}
    LOG.info(
        "S3 image migration starting: bucket=%s, schemas=%s (%d)",
        new_bucket,
        targets,
        len(targets),
    )

    def process_url(url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            return url
        key, already_new = _s3_url_key_and_skip(url, new_bucket)
        if already_new:
            return url
        if key is None:
            return url
        new_url = f"{new_base}/{key}"
        try:
            if key not in uploaded_keys:
                LOG.debug("GET %s -> S3 key %s", url, key)
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "application/octet-stream")
                s3.put_object(Bucket=new_bucket, Key=key, Body=r.content, ContentType=ct)
                uploaded_keys.add(key)
                stats["downloaded"] += 1
                if stats["downloaded"] % 25 == 0:
                    LOG.info(
                        "Progress: %s new objects uploaded to s3://%s (updated_rows=%s)",
                        stats["downloaded"],
                        new_bucket,
                        stats["updated_rows"],
                    )
            stats["url_mappings"].append(
                {
                    "old_url": url,
                    "new_url": new_url,
                    "s3_key": key,
                    "schema": current_schema,
                    "ao_id": str(current_row.get("ao_id", "")),
                    "bd_date": str(current_row.get("bd_date", "")),
                }
            )
            return new_url
        except Exception as e:
            stats["errors"].append({"url": url, "error": str(e)})
            LOG.warning("S3 migrate fetch failed %s: %s", url, e)
            return url

    for _src, tgt in schema_map.items():
        if not _table_exists(cur, tgt, "beatdowns"):
            LOG.info("Schema %s: no beatdowns table, skipping", tgt)
            continue
        LOG.info("Schema %s: querying beatdowns with non-null json", tgt)
        try:
            cur.execute(
                f"SELECT `ao_id`, `bd_date`, `q_user_id`, `json` FROM `{tgt}`.`beatdowns` WHERE `json` IS NOT NULL"
            )
        except Exception as e:
            stats["errors"].append({"schema": tgt, "error": str(e)})
            LOG.error("Schema %s: SELECT failed: %s", tgt, e)
            continue
        rows = cur.fetchall()
        nrows = len(rows)
        LOG.info("Schema %s: fetched %d rows with json", tgt, nrows)
        for row_idx, row in enumerate(rows, start=1):
            current_schema = tgt
            current_row = row
            if row_idx % 100 == 0 or row_idx == nrows:
                LOG.info(
                    "Schema %s: scanning row %d/%d (downloaded=%s updated=%s errors=%s)",
                    tgt,
                    row_idx,
                    nrows,
                    stats["downloaded"],
                    stats["updated_rows"],
                    len(stats["errors"]),
                )
            j = row["json"]
            if not j:
                continue
            if isinstance(j, str):
                try:
                    j = json.loads(j)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(j, dict):
                continue
            url_count = 0
            for _k in ("files", "low_res_files"):
                if _k in j and isinstance(j[_k], list):
                    url_count += sum(1 for x in j[_k] if isinstance(x, str) and x.strip())
            if url_count:
                LOG.debug(
                    "Schema %s row ao_id=%s bd_date=%s q_user_id=%s: %d URL(s) in files/low_res_files",
                    tgt,
                    row.get("ao_id"),
                    row.get("bd_date"),
                    row.get("q_user_id"),
                    url_count,
                )
            changed = False
            for k in ("files", "low_res_files"):
                if k not in j or not isinstance(j[k], list):
                    continue
                new_list = []
                for u in j[k]:
                    if isinstance(u, str):
                        nu = process_url(u)
                        if nu != u:
                            changed = True
                        new_list.append(nu)
                    else:
                        new_list.append(u)
                j[k] = new_list
            if changed:
                try:
                    LOG.info(
                        "Schema %s: UPDATE beatdowns ao_id=%s bd_date=%s q_user_id=%s",
                        tgt,
                        row["ao_id"],
                        row["bd_date"],
                        row["q_user_id"],
                    )
                    cur.execute(
                        f"UPDATE `{tgt}`.`beatdowns` SET `json`=%s "
                        f"WHERE `ao_id`=%s AND `bd_date`=%s AND `q_user_id`=%s",
                        (json.dumps(j), row["ao_id"], row["bd_date"], row["q_user_id"]),
                    )
                    stats["updated_rows"] += 1
                except Exception as e:
                    stats["errors"].append(
                        {
                            "schema": tgt,
                            "ao_id": str(row.get("ao_id")),
                            "bd_date": str(row.get("bd_date")),
                            "q_user_id": str(row.get("q_user_id")),
                            "error": str(e),
                        }
                    )
                    LOG.error(
                        "Schema %s: UPDATE failed ao_id=%s bd_date=%s: %s",
                        tgt,
                        row.get("ao_id"),
                        row.get("bd_date"),
                        e,
                    )
        LOG.info(
            "Schema %s done: downloaded=%s updated_rows=%s error_count=%s",
            tgt,
            stats["downloaded"],
            stats["updated_rows"],
            len(stats["errors"]),
        )

    conn.commit()
    report["s3_image_migration"] = stats
    LOG.info("S3 image migration: %s objects copied, %s rows updated", stats["downloaded"], stats["updated_rows"])


def source_fallback_reupload(
    source_conn: Any,
    target_conn: Any,
    schema_map: dict[str, str],
    report: dict[str, Any],
) -> None:
    """
    Re-upload images when target beatdowns.json already points at IMAGE_S3_BUCKET but objects were deleted.
    For each target URL on the bucket, looks up the same beatdowns row on source RDS and uses the
    original URL at the same index in files/low_res_files to re-download and PUT to the existing key.
    Does not modify the target database.
    """
    new_bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not new_bucket:
        LOG.info("S3 source-fallback skipped (set IMAGE_S3_BUCKET)")
        return

    try:
        import boto3
        import requests
    except ImportError:
        LOG.warning("boto3/requests not installed; skipping S3 source-fallback")
        return

    s3 = boto3.client("s3")
    target_cur = target_conn.cursor(DictCursor)
    source_cur = source_conn.cursor(DictCursor)
    target_to_source = {tgt: src for src, tgt in schema_map.items()}
    stats: dict[str, Any] = {
        "downloaded": 0,
        "skipped_rows": 0,
        "errors": [],
        "url_mappings": [],
    }
    uploaded_keys: set[str] = set()

    LOG.info(
        "S3 source-fallback: bucket=%s, target schemas=%s",
        new_bucket,
        list(target_to_source.keys()),
    )

    for tgt, src in target_to_source.items():
        if not _table_exists(target_cur, tgt, "beatdowns"):
            LOG.info("Source-fallback: schema %s has no beatdowns, skipping", tgt)
            continue
        if not _table_exists(source_cur, src, "beatdowns"):
            LOG.warning("Source-fallback: source schema %s has no beatdowns, skipping target %s", src, tgt)
            continue
        try:
            target_cur.execute(
                f"SELECT `ao_id`, `bd_date`, `q_user_id`, `json` FROM `{tgt}`.`beatdowns` WHERE `json` IS NOT NULL"
            )
        except Exception as e:
            stats["errors"].append({"schema": tgt, "error": str(e)})
            LOG.error("Source-fallback: SELECT target %s failed: %s", tgt, e)
            continue
        rows = target_cur.fetchall()
        LOG.info("Source-fallback: schema %s, %d rows with json", tgt, len(rows))
        for row in rows:
            j_t = row["json"]
            if not j_t:
                continue
            if isinstance(j_t, str):
                try:
                    j_t = json.loads(j_t)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(j_t, dict):
                continue

            needs_fallback = False
            for list_key in ("files", "low_res_files"):
                lst = j_t.get(list_key)
                if not isinstance(lst, list):
                    continue
                for u in lst:
                    if isinstance(u, str) and _s3_object_key_from_bucket_url(u, new_bucket):
                        needs_fallback = True
                        break
                if needs_fallback:
                    break
            if not needs_fallback:
                stats["skipped_rows"] += 1
                continue

            try:
                source_cur.execute(
                    f"SELECT `json` FROM `{src}`.`beatdowns` "
                    f"WHERE `ao_id`=%s AND `bd_date`=%s AND `q_user_id`=%s",
                    (row["ao_id"], row["bd_date"], row["q_user_id"]),
                )
            except Exception as e:
                stats["errors"].append(
                    {
                        "schema": tgt,
                        "ao_id": str(row.get("ao_id")),
                        "error": f"source SELECT: {e}",
                    }
                )
                continue
            srow = source_cur.fetchone()
            if not srow or not srow.get("json"):
                stats["errors"].append(
                    {
                        "schema": tgt,
                        "ao_id": str(row.get("ao_id")),
                        "bd_date": str(row.get("bd_date")),
                        "error": "no matching source beatdowns row",
                    }
                )
                LOG.warning(
                    "Source-fallback: no source row %s.%s ao_id=%s bd_date=%s",
                    src,
                    tgt,
                    row.get("ao_id"),
                    row.get("bd_date"),
                )
                continue
            j_s = srow["json"]
            if isinstance(j_s, str):
                try:
                    j_s = json.loads(j_s)
                except (json.JSONDecodeError, TypeError):
                    stats["errors"].append(
                        {
                            "schema": tgt,
                            "ao_id": str(row.get("ao_id")),
                            "error": "source json parse failed",
                        }
                    )
                    continue
            if not isinstance(j_s, dict):
                continue

            for list_key in ("files", "low_res_files"):
                t_list = j_t.get(list_key)
                if not isinstance(t_list, list):
                    continue
                s_list = j_s.get(list_key)
                if not isinstance(s_list, list):
                    s_list = []
                for i, target_url in enumerate(t_list):
                    if not isinstance(target_url, str):
                        continue
                    s3_key = _s3_object_key_from_bucket_url(target_url, new_bucket)
                    if not s3_key:
                        continue
                    old_url = s_list[i] if i < len(s_list) else None
                    if not isinstance(old_url, str) or not old_url.strip():
                        stats["errors"].append(
                            {
                                "schema": tgt,
                                "ao_id": str(row.get("ao_id")),
                                "bd_date": str(row.get("bd_date")),
                                "list_key": list_key,
                                "index": i,
                                "error": "missing source URL at index",
                            }
                        )
                        continue
                    new_url = f"https://{new_bucket}.s3.amazonaws.com/{s3_key}"
                    try:
                        if s3_key not in uploaded_keys:
                            LOG.debug("Source-fallback GET %s -> key %s", old_url, s3_key)
                            r = requests.get(old_url, timeout=120)
                            r.raise_for_status()
                            ct = r.headers.get("Content-Type", "application/octet-stream")
                            s3.put_object(Bucket=new_bucket, Key=s3_key, Body=r.content, ContentType=ct)
                            uploaded_keys.add(s3_key)
                            stats["downloaded"] += 1
                            if stats["downloaded"] % 25 == 0:
                                LOG.info(
                                    "Source-fallback progress: uploaded=%s to s3://%s",
                                    stats["downloaded"],
                                    new_bucket,
                                )
                        stats["url_mappings"].append(
                            {
                                "old_url": old_url,
                                "new_url": new_url,
                                "s3_key": s3_key,
                                "schema": tgt,
                                "ao_id": str(row.get("ao_id", "")),
                                "bd_date": str(row.get("bd_date", "")),
                            }
                        )
                    except Exception as e:
                        stats["errors"].append({"old_url": old_url, "s3_key": s3_key, "error": str(e)})
                        LOG.warning("Source-fallback upload failed %s: %s", old_url, e)

    report["s3_image_migration"] = stats
    LOG.info(
        "S3 source-fallback done: uploaded=%s skipped_rows=%s errors=%s",
        stats["downloaded"],
        stats["skipped_rows"],
        len(stats["errors"]),
    )


def receipt_fallback_reupload(receipt_path: Path, report: dict[str, Any]) -> None:
    """Re-upload using old_url -> s3_key from a prior migrate_images receipt JSON."""
    new_bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not new_bucket:
        LOG.error("Set IMAGE_S3_BUCKET for receipt-fallback")
        return

    try:
        import boto3
        import requests
    except ImportError:
        LOG.warning("boto3/requests not installed; skipping receipt-fallback")
        return

    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    mig = data.get("s3_image_migration") or {}
    mappings = mig.get("url_mappings") or []
    if not mappings:
        LOG.error(
            "No url_mappings in %s — need a receipt from a run after url mapping was added, or use --source-fallback",
            receipt_path,
        )
        report["s3_image_migration"] = {
            "downloaded": 0,
            "errors": [{"error": "empty url_mappings"}],
            "url_mappings": [],
        }
        return

    s3 = boto3.client("s3")
    stats: dict[str, Any] = {"downloaded": 0, "errors": [], "url_mappings": []}
    uploaded_keys: set[str] = set()

    for m in mappings:
        old_url = m.get("old_url")
        s3_key = m.get("s3_key")
        if not old_url or not s3_key:
            stats["errors"].append({"mapping": m, "error": "missing old_url or s3_key"})
            continue
        new_url = f"https://{new_bucket}.s3.amazonaws.com/{s3_key}"
        try:
            if s3_key not in uploaded_keys:
                r = requests.get(old_url, timeout=120)
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "application/octet-stream")
                s3.put_object(Bucket=new_bucket, Key=s3_key, Body=r.content, ContentType=ct)
                uploaded_keys.add(s3_key)
                stats["downloaded"] += 1
                if stats["downloaded"] % 25 == 0:
                    LOG.info("Receipt-fallback: uploaded %s objects", stats["downloaded"])
            stats["url_mappings"].append(
                {
                    "old_url": old_url,
                    "new_url": new_url,
                    "s3_key": s3_key,
                    "schema": m.get("schema", ""),
                    "ao_id": m.get("ao_id", ""),
                    "bd_date": m.get("bd_date", ""),
                }
            )
        except Exception as e:
            stats["errors"].append({"old_url": old_url, "s3_key": s3_key, "error": str(e)})
            LOG.warning("Receipt-fallback failed %s: %s", old_url, e)

    report["s3_image_migration"] = stats
    LOG.info(
        "Receipt-fallback done: uploaded=%s errors=%s",
        stats["downloaded"],
        len(stats["errors"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate F3 schemas from RDS to TiDB")
    parser.add_argument(
        "--env",
        required=True,
        choices=["test", "prod"],
        help="Environment: loads migration/.env.migration.<env> (same idea as deploy.sh --env)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only log planned steps, no writes to target")
    parser.add_argument("--skip-schema", action="append", default=[], help="Skip a source schema name (repeatable)")
    parser.add_argument("--skip-table", action="append", default=[], help="Skip table as schema.table (repeatable)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Ignore existing checkpoint file")
    args = parser.parse_args()

    mig_dir = Path(__file__).resolve().parent
    env_file = mig_dir / f".env.migration.{args.env}"
    if not env_file.is_file():
        LOG.error(
            "Missing %s — copy from .env.migration.example to .env.migration.test or .env.migration.prod and fill in values.",
            env_file,
        )
        return 1
    load_dotenv(env_file)
    env_suffix = args.env
    os.environ["STAGE"] = env_suffix
    schema_map = default_schema_map(env_suffix)
    qsignups_team_ids = parse_qsignups_team_ids()

    read_delay = float(os.environ.get("READ_DELAY_SECONDS", "1"))
    schema_delay = float(os.environ.get("SCHEMA_DELAY_SECONDS", "5"))
    batch_size = int(os.environ.get("BATCH_SIZE", "500"))
    checkpoint_path = Path(os.environ.get("CHECKPOINT_FILE", Path(__file__).parent / "migration_checkpoint.json"))

    source_host = os.environ["SOURCE_HOST"]
    source_port = int(os.environ.get("SOURCE_PORT", "3306"))
    source_user = os.environ["SOURCE_USER"]
    source_password = os.environ["SOURCE_PASSWORD"]
    source_tls = _env_bool("SOURCE_TLS_ENABLED", False)

    target_host = os.environ["TARGET_HOST"]
    target_port = int(os.environ.get("TARGET_PORT", "4000"))
    target_user = os.environ["TARGET_USER"]
    target_password = os.environ["TARGET_PASSWORD"]
    target_tls = _env_bool("TARGET_TLS_ENABLED", True)

    done = set() if args.reset_checkpoint else load_checkpoint(checkpoint_path)
    skip_tables = {tuple(s.split(".", 1)) for s in args.skip_table if "." in s}

    if args.dry_run:
        LOG.info("DRY RUN: would migrate %s", schema_map)
        return 0

    report_path = Path(
        os.environ.get("MIGRATION_REPORT_FILE", Path(__file__).parent / "migration_report.json")
    )
    report = new_migration_report(source_host, source_port, target_host, target_port)
    write_migration_report(report_path, report)

    def source_connect(db: str | None = None):
        return pymysql.connect(**_connect_kwargs(source_host, source_port, source_user, source_password, db, source_tls))

    def target_connect(db: str | None = None):
        return pymysql.connect(**_connect_kwargs(target_host, target_port, target_user, target_password, db, target_tls))

    tgt_boot = with_retry(lambda: target_connect(), "target connect for bootstrap")
    try:
        pre_migration_bootstrap_schemas(tgt_boot, env_suffix)
    finally:
        tgt_boot.close()

    source_to_target = {k: v for k, v in schema_map.items()}

    for source_schema, target_schema in schema_map.items():
        se = empty_schema_report_entry(target_schema)
        report["schemas"][source_schema] = se

        if source_schema in args.skip_schema:
            LOG.info("Skipping schema %s", source_schema)
            se["status"] = "skipped_by_flag"
            finalize_migration_summary(report)
            write_migration_report(report_path, report)
            continue

        time.sleep(schema_delay)
        LOG.info("=== Schema %s -> %s ===", source_schema, target_schema)

        src: Any = None
        tgt: Any = None
        try:
            src = with_retry(lambda: source_connect(), "source connect")
            tgt = with_retry(lambda: target_connect(), "target connect")

            create_db_sql = f"CREATE DATABASE IF NOT EXISTS `{target_schema}`"
            try:
                with tgt.cursor() as c:
                    c.execute(create_db_sql)
                tgt.commit()
                tgt.select_db(target_schema)
            except Exception as e:
                report_append_error(se, "create target database", create_db_sql, e)
                se["status"] = "error"
                LOG.exception("Target CREATE DATABASE failed for %s", target_schema)
                finalize_migration_summary(report)
                write_migration_report(report_path, report)
                continue

            list_sql_display = (
                "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = '{source_schema}' ORDER BY TABLE_NAME"
            )
            try:
                with src.cursor() as cur:
                    cur.execute(LIST_TABLES_SQL_TEMPLATE, (source_schema,))
                    objects = cur.fetchall()
            except Exception as e:
                report_append_error(se, "list tables", list_sql_display, e)
                if is_access_denied_error(e):
                    se["status"] = "permission_denied"
                    LOG.warning("Access denied listing tables for schema %s — skipping", source_schema)
                else:
                    se["status"] = "error"
                    LOG.exception("Failed to list tables for %s", source_schema)
                finalize_migration_summary(report)
                write_migration_report(report_path, report)
                continue

            tables: list[str] = []
            views: list[str] = []
            for r in objects:
                tname = _row_get(r, "TABLE_NAME", "table_name")
                ttype = _row_get(r, "TABLE_TYPE", "table_type")
                if source_schema == F3STCHARLES_SOURCE and not tname.startswith("qsignups_"):
                    continue
                if ttype == "BASE TABLE":
                    tables.append(tname)
                elif ttype == "VIEW":
                    views.append(tname)

            if source_schema == F3STCHARLES_SOURCE:
                skipped_non_qs = sum(
                    1
                    for r in objects
                    if not str(_row_get(r, "TABLE_NAME", "table_name") or "").startswith("qsignups_")
                )
                report["qsignups_filter"] = {
                    "enabled": bool(qsignups_team_ids),
                    "team_ids": list(qsignups_team_ids) if qsignups_team_ids else [],
                    "warning": None
                    if qsignups_team_ids
                    else "QSIGNUPS_TEAM_IDS unset — all qsignups rows from source are copied (security risk)",
                    "f3stcharles_objects_skipped_non_qsignups": skipped_non_qs,
                    "qsignups_base_tables_to_migrate": len(tables),
                }
                write_migration_report(report_path, report)
                LOG.info(
                    "f3stcharles: migrating %s qsignups_* base tables; %s source objects skipped "
                    "(non-qsignups_*); qsignups vw_* views recreated on target after copy",
                    len(tables),
                    skipped_non_qs,
                )

            if not tables and not views:
                report_append_error(
                    se,
                    "list tables",
                    list_sql_display,
                    None,
                    "No tables or views found -- possible permission issue (schema may be hidden from this user)",
                )
                se["status"] = "empty_warning"
                LOG.warning("No objects in source schema %s (hidden or empty)", source_schema)
                finalize_migration_summary(report)
                write_migration_report(report_path, report)
                continue

            with tgt.cursor() as c:
                c.execute("SET FOREIGN_KEY_CHECKS=0")
            tgt.commit()

            schema_aborted_access = False
            for table in tables:
                key = f"{source_schema}.{table}"
                if key in done:
                    LOG.info("Skip (checkpoint): %s", key)
                    se["tables"].append({"name": table, "rows_migrated": 0, "status": "skipped"})
                    finalize_migration_summary(report)
                    write_migration_report(report_path, report)
                    continue
                if (source_schema, table) in skip_tables:
                    LOG.info("Skip: %s", key)
                    se["tables"].append({"name": table, "rows_migrated": 0, "status": "skipped"})
                    finalize_migration_summary(report)
                    write_migration_report(report_path, report)
                    continue

                show_create_sql = f"SHOW CREATE TABLE `{source_schema}`.`{table}`"
                try:
                    with src.cursor() as cur:
                        cur.execute(show_create_sql)
                        row = cur.fetchone()
                        create_sql = _row_get(row, "Create Table", "Create table")
                    create_sql = clean_view_ddl(create_sql, source_to_target)
                    create_sql = create_sql.replace(f"`{source_schema}`", f"`{target_schema}`")

                    def create_t():
                        with tgt.cursor() as c:
                            c.execute(f"DROP TABLE IF EXISTS `{target_schema}`.`{table}`")
                            c.execute(create_sql)
                        tgt.commit()

                    with_retry(create_t, f"CREATE TABLE {target_schema}.{table}")

                    where_clause: str | None = None
                    where_params: tuple[Any, ...] | None = None
                    source_unfiltered_count: int | None = None
                    if source_schema == F3STCHARLES_SOURCE:
                        if qsignups_team_ids:
                            where_clause, where_params = build_qsignups_copy_filter(
                                source_schema, table, qsignups_team_ids
                            )

                            def count_unfiltered():
                                with src.cursor() as cur2:
                                    cur2.execute(
                                        f"SELECT COUNT(*) AS c FROM `{source_schema}`.`{table}`"
                                    )
                                    r2 = cur2.fetchone()
                                    return int(_row_get(r2, "c", "C") or 0)

                            source_unfiltered_count = with_retry(
                                count_unfiltered,
                                f"COUNT unfiltered {source_schema}.{table}",
                            )
                        else:
                            LOG.warning(
                                "QSIGNUPS_TEAM_IDS unset: copying all rows from %s.%s — "
                                "other regions' secrets may be copied",
                                source_schema,
                                table,
                            )

                    rows_migrated = migrate_table(
                        src,
                        tgt,
                        source_schema,
                        target_schema,
                        table,
                        batch_size,
                        read_delay,
                        where_clause=where_clause,
                        where_params=where_params,
                    )

                    done.add(key)
                    save_checkpoint(checkpoint_path, done)
                    tbl_entry: dict[str, Any] = {
                        "name": table,
                        "rows_migrated": rows_migrated,
                        "status": "ok",
                    }
                    if source_schema == F3STCHARLES_SOURCE:
                        tbl_entry["qsignups_team_filter_applied"] = bool(qsignups_team_ids)
                        if source_unfiltered_count is not None:
                            tbl_entry["source_row_count_unfiltered"] = source_unfiltered_count
                    se["tables"].append(tbl_entry)
                except Exception as e:
                    report_append_error(
                        se,
                        f"migrate table {source_schema}.{table}",
                        show_create_sql,
                        e,
                    )
                    se["tables"].append({"name": table, "rows_migrated": 0, "status": "error"})
                    if is_access_denied_error(e):
                        se["status"] = "permission_denied"
                        schema_aborted_access = True
                        LOG.warning(
                            "Access denied on %s.%s — skipping rest of schema %s",
                            source_schema,
                            table,
                            source_schema,
                        )
                        break
                    LOG.exception("Table %s.%s failed", source_schema, table)

                finalize_migration_summary(report)
                write_migration_report(report_path, report)

            with tgt.cursor() as c:
                c.execute("SET FOREIGN_KEY_CHECKS=1")
            tgt.commit()

            if not schema_aborted_access:
                for view in views:
                    vkey = f"{source_schema}.{view}.view"
                    if vkey in done:
                        se["views"].append({"name": view, "status": "skipped"})
                        continue
                    show_view_sql = f"SHOW CREATE VIEW `{source_schema}`.`{view}`"
                    try:
                        with src.cursor() as cur:
                            cur.execute(show_view_sql)
                            row = cur.fetchone()
                            raw_view = _row_get(row, "Create View", "create view")
                        create_sql = build_target_view_ddl(
                            raw_view, source_schema, target_schema, view, source_to_target
                        )

                        def create_v():
                            with tgt.cursor() as c:
                                c.execute(f"DROP VIEW IF EXISTS `{target_schema}`.`{view}`")
                                c.execute(create_sql)
                            tgt.commit()

                        with_retry(create_v, f"CREATE VIEW {target_schema}.{view}")
                        probe_sql = f"SELECT 1 FROM `{target_schema}`.`{view}` LIMIT 1"
                        with tgt.cursor() as c:
                            c.execute(probe_sql)
                        done.add(vkey)
                        save_checkpoint(checkpoint_path, done)
                        se["views"].append({"name": view, "status": "ok"})
                    except Exception as e:
                        report_append_error(se, f"create view {source_schema}.{view}", show_view_sql, e)
                        se["views"].append({"name": view, "status": "error"})
                        LOG.error(
                            "View %s.%s failed: %s — you may need to fix DDL manually",
                            target_schema,
                            view,
                            e,
                        )

                if se["status"] == "pending":
                    if any(t.get("status") == "error" for t in se["tables"]) or any(
                        v.get("status") == "error" for v in se["views"]
                    ):
                        se["status"] = "error"
                    else:
                        se["status"] = "completed"

            elif se["status"] == "permission_denied":
                pass
            elif se["status"] == "pending":
                se["status"] = "error"

        except pymysql.err.OperationalError as e:
            if is_access_denied_error(e):
                report_append_error(se, f"schema {source_schema}", "", e)
                se["status"] = "permission_denied"
                LOG.warning("Access denied for schema %s: %s", source_schema, e)
            else:
                report_append_error(se, f"schema {source_schema}", "", e)
                se["status"] = "error"
                LOG.exception("OperationalError for schema %s", source_schema)
                raise
        except Exception as e:
            report_append_error(se, f"schema {source_schema}", "", e)
            se["status"] = "error"
            LOG.exception("Unexpected error for schema %s", source_schema)
        finally:
            if src is not None:
                try:
                    src.close()
                except Exception:
                    pass
            if tgt is not None:
                try:
                    tgt.close()
                except Exception:
                    pass

        finalize_migration_summary(report)
        write_migration_report(report_path, report)

    tgt_post = with_retry(lambda: target_connect(), "target connect for post-migration fixups")
    try:
        qsignups_tgt = schema_map.get(F3STCHARLES_SOURCE)
        if qsignups_tgt:
            post_migration_create_qsignups_views(tgt_post, qsignups_tgt, report)
        pre_encryption_widen_columns(tgt_post, env_suffix, report)
        post_migration_encrypt_secrets(tgt_post, schema_map, report, stage=env_suffix)
        post_migration_s3_images(tgt_post, schema_map, report)
    finally:
        tgt_post.close()
    report["finished_at"] = _iso_now()
    finalize_migration_summary(report)
    write_migration_report(report_path, report)

    receipt_dir = Path(__file__).resolve().parent / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_name = f"migration-{env_suffix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.txt"
    receipt_path = receipt_dir / receipt_name
    receipt_body = format_migration_receipt(report, env_suffix, report_path)
    receipt_path.write_text(receipt_body, encoding="utf-8")
    print(receipt_body, end="", flush=True)
    LOG.info("Migration finished. Report written to %s", report_path)
    LOG.info("Receipt written to %s", receipt_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
