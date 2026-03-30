#!/usr/bin/env python3
"""
Copy backblast images from stored public URLs into IMAGE_S3_BUCKET and rewrite URLs in beatdowns.json.

Uses migration/.env.migration.<env> (see --env): TARGET_*, IMAGE_S3_BUCKET, and schema_map.

Modes:
  (default)      Target DB only — migrates URLs not already on the bucket; receipt includes url_mappings.
  --receipt-fallback PATH  Re-download old_url from a prior receipt and PUT to s3_key (no DB).
  --source-fallback        Read original URLs from source RDS for rows already pointing at the bucket.

Run after deploy creates the bucket. Default mode is idempotent (skips URLs already on the new bucket).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
import ssl
import sys
from pathlib import Path
from typing import Any

import pymysql
from dotenv import load_dotenv

# Ensure sibling migrate_data is importable when run as python migration/migrate_images.py
_MIG_DIR = Path(__file__).resolve().parent
if str(_MIG_DIR) not in sys.path:
    sys.path.insert(0, str(_MIG_DIR))

from migrate_data import (  # noqa: E402
    default_schema_map,
    post_migration_s3_images,
    receipt_fallback_reupload,
    source_fallback_reupload,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("migrate_images")


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
        "connect_timeout": 30,
        "read_timeout": 300,
        "write_timeout": 300,
    }
    if database:
        kw["database"] = database
    if tls_enabled:
        kw["ssl"] = ssl.create_default_context()
    return kw


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate backblast images to S3")
    parser.add_argument(
        "--env",
        required=True,
        choices=["test", "prod"],
        help="Environment: loads migration/.env.migration.<env> (same idea as deploy.sh --env)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--receipt-fallback",
        type=Path,
        metavar="PATH",
        default=None,
        help="Re-upload using url_mappings from a prior migrate_images receipt JSON (no DB).",
    )
    mode.add_argument(
        "--source-fallback",
        action="store_true",
        help="Re-upload by looking up original URLs on source RDS (requires SOURCE_* in env).",
    )
    args = parser.parse_args()

    env_file = _MIG_DIR / f".env.migration.{args.env}"
    if not env_file.is_file():
        LOG.error(
            "Missing %s — copy from .env.migration.example to .env.migration.test or .env.migration.prod.",
            env_file,
        )
        return 1
    load_dotenv(env_file)
    stage = args.env
    os.environ["STAGE"] = stage

    bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not bucket:
        LOG.error("Set IMAGE_S3_BUCKET in .env.migration.%s", args.env)
        return 1

    schema_map = default_schema_map(stage)
    report: dict[str, Any] = {"fixups_errors": []}

    if args.receipt_fallback is not None:
        receipt_in = args.receipt_fallback
        if not receipt_in.is_file():
            LOG.error("Receipt file not found: %s", receipt_in)
            return 1
        receipt_fallback_reupload(receipt_in, report)
        err = (report.get("s3_image_migration") or {}).get("errors") or []
        if any(e.get("error") == "empty url_mappings" for e in err if isinstance(e, dict)):
            return 1
    elif args.source_fallback:
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

        source_conn = pymysql.connect(
            **_connect_kwargs(
                source_host, source_port, source_user, source_password, None, source_tls
            )
        )
        target_conn = pymysql.connect(
            **_connect_kwargs(
                target_host, target_port, target_user, target_password, None, target_tls
            )
        )
        try:
            source_fallback_reupload(source_conn, target_conn, schema_map, report)
        finally:
            source_conn.close()
            target_conn.close()
    else:
        host = os.environ["TARGET_HOST"]
        port = int(os.environ.get("TARGET_PORT", "4000"))
        user = os.environ["TARGET_USER"]
        password = os.environ["TARGET_PASSWORD"]
        tls = _env_bool("TARGET_TLS_ENABLED", True)

        conn = pymysql.connect(**_connect_kwargs(host, port, user, password, None, tls))
        try:
            post_migration_s3_images(conn, schema_map, report)
        finally:
            conn.close()

    receipt_dir = _MIG_DIR / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_name = (
        f"migrate-images-{stage}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
    )
    receipt_path = receipt_dir / receipt_name
    receipt_path.write_text(json.dumps(report, indent=2))
    LOG.info("Receipt written to %s", receipt_path)

    stats = report.get("s3_image_migration") or {}
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
