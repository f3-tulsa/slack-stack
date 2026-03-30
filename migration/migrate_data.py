#!/usr/bin/env python3
"""
Migrate MySQL schemas from source RDS to TiDB with schema renaming, resilience, and view recreation.

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

# Load .env.migration from this directory
_ENV_FILE = Path(__file__).resolve().parent / ".env.migration"
load_dotenv(_ENV_FILE)

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


def default_schema_map(env_suffix: str) -> dict[str, str]:
    return {
        "paxminer": f"paxminer_{env_suffix}",
        "f3ttown": f"f3ttown_{env_suffix}",
        "f3scissortail": f"f3scissortail_{env_suffix}",
        "f3stcharles": f"qsignups_{env_suffix}",
        "slackblast": f"slackblast_{env_suffix}",
        "weaselbot": f"weaselbot_{env_suffix}",
    }


def region_name_rewrites(env_suffix: str) -> dict[str, str]:
    return {
        "f3ttown": f"f3ttown_{env_suffix}",
        "f3scissortail": f"f3scissortail_{env_suffix}",
    }


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
) -> int:
    def count_rows():
        with source_conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM `{source_schema}`.`{table}`")
            row = cur.fetchone()
            return _row_get(row, "c", "C")

    total = with_retry(count_rows, f"COUNT {source_schema}.{table}")
    LOG.info("Table %s.%s: %s rows", source_schema, table, total)
    if total == 0:
        return 0

    offset = 0
    inserted = 0
    while offset < total:
        time.sleep(read_delay)

        def fetch_batch():
            with source_conn.cursor() as cur:
                cur.execute(f"SELECT * FROM `{source_schema}`.`{table}` LIMIT %s OFFSET %s", (batch_size, offset))
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


def post_migration_encrypt_secrets(conn, schema_map: dict[str, str], report: dict) -> None:
    key = (os.environ.get("DB_ENCRYPTION_KEY") or "").strip()
    if not key or key == "123":
        LOG.info("Post-migration encryption skipped (DB_ENCRYPTION_KEY unset or placeholder)")
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

    pm = schema_map.get("paxminer")
    wb = schema_map.get("weaselbot")
    sb = schema_map.get("slackblast")
    qs = schema_map.get("f3stcharles")

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
    stats = {"downloaded": 0, "updated_rows": 0, "errors": []}

    uploaded_keys: set[str] = set()

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
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "application/octet-stream")
                s3.put_object(Bucket=new_bucket, Key=key, Body=r.content, ContentType=ct)
                uploaded_keys.add(key)
                stats["downloaded"] += 1
            return new_url
        except Exception as e:
            stats["errors"].append({"url": url, "error": str(e)})
            LOG.warning("S3 migrate fetch failed %s: %s", url, e)
            return url

    for _src, tgt in schema_map.items():
        if not _table_exists(cur, tgt, "beatdowns"):
            continue
        try:
            cur.execute(
                f"SELECT `ao_id`, `bd_date`, `q_user_id`, `json` FROM `{tgt}`.`beatdowns` WHERE `json` IS NOT NULL"
            )
        except Exception as e:
            stats["errors"].append({"schema": tgt, "error": str(e)})
            continue
        for row in cur.fetchall():
            j = row["json"]
            if not j or not isinstance(j, dict):
                continue
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

    conn.commit()
    report["s3_image_migration"] = stats
    LOG.info("S3 image migration: %s objects copied, %s rows updated", stats["downloaded"], stats["updated_rows"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate F3 schemas from RDS to TiDB")
    parser.add_argument("--env", choices=("test", "prod"), required=True, help="Suffix for target schema names")
    parser.add_argument("--dry-run", action="store_true", help="Only log planned steps, no writes to target")
    parser.add_argument("--skip-schema", action="append", default=[], help="Skip a source schema name (repeatable)")
    parser.add_argument("--skip-table", action="append", default=[], help="Skip table as schema.table (repeatable)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Ignore existing checkpoint file")
    args = parser.parse_args()

    env_suffix = args.env
    schema_map = default_schema_map(env_suffix)
    rewrites = region_name_rewrites(env_suffix)

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
                if ttype == "BASE TABLE":
                    tables.append(tname)
                elif ttype == "VIEW":
                    views.append(tname)

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

                    with tgt.cursor() as c:
                        c.execute("SET FOREIGN_KEY_CHECKS=0")
                    tgt.commit()

                    rows_migrated = migrate_table(src, tgt, source_schema, target_schema, table, batch_size, read_delay)

                    with tgt.cursor() as c:
                        c.execute("SET FOREIGN_KEY_CHECKS=1")
                    tgt.commit()

                    done.add(key)
                    save_checkpoint(checkpoint_path, done)
                    se["tables"].append({"name": table, "rows_migrated": rows_migrated, "status": "ok"})
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

    # Cross-reference fixups in target
    pm = schema_map["paxminer"]
    sb = schema_map["slackblast"]
    wb = schema_map["weaselbot"]

    tgt = with_retry(lambda: target_connect(), "target connect for fixups")
    try:
        with tgt.cursor() as cur:
            for old, new in rewrites.items():
                sql_pm = f"UPDATE `{pm}`.`regions` SET `schema_name` = %s WHERE `schema_name` = %s"
                try:
                    cur.execute(sql_pm, (new, old))
                    LOG.info("Updated paxminer.regions schema_name %s -> %s (%s rows)", old, new, cur.rowcount)
                except Exception as e:
                    report["fixups_errors"].append(
                        {"operation": "fixup paxminer.regions", "sql": sql_pm, "params": [new, old], "error": str(e)}
                    )
                    LOG.error("Fixup paxminer.regions failed: %s", e)
                sql_sb = f"UPDATE `{sb}`.`regions` SET `paxminer_schema` = %s WHERE `paxminer_schema` = %s"
                try:
                    cur.execute(sql_sb, (new, old))
                    LOG.info(
                        "Updated slackblast.regions paxminer_schema %s -> %s (%s rows)",
                        old,
                        new,
                        cur.rowcount,
                    )
                except Exception as e:
                    report["fixups_errors"].append(
                        {"operation": "fixup slackblast.regions", "sql": sql_sb, "params": [new, old], "error": str(e)}
                    )
                    LOG.error("Fixup slackblast.regions failed: %s", e)
                sql_wb = f"UPDATE `{wb}`.`regions` SET `paxminer_schema` = %s WHERE `paxminer_schema` = %s"
                try:
                    cur.execute(sql_wb, (new, old))
                    LOG.info(
                        "Updated weaselbot.regions paxminer_schema %s -> %s (%s rows)",
                        old,
                        new,
                        cur.rowcount,
                    )
                except Exception as e:
                    report["fixups_errors"].append(
                        {"operation": "fixup weaselbot.regions", "sql": sql_wb, "params": [new, old], "error": str(e)}
                    )
                    LOG.error("Fixup weaselbot.regions failed: %s", e)
        tgt.commit()
    finally:
        tgt.close()

    tgt_post = with_retry(lambda: target_connect(), "target connect for encryption/S3 fixups")
    try:
        post_migration_encrypt_secrets(tgt_post, schema_map, report)
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
