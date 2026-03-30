#!/usr/bin/env python3
"""
Copy backblast images from stored public URLs into IMAGE_S3_BUCKET and rewrite URLs in beatdowns.json.

Target DB only (no source RDS). Uses migration/.env.migration: TARGET_*, STAGE, schema names, IMAGE_S3_BUCKET.

Run after deploy creates the bucket. Idempotent (skips URLs already on the new bucket).
"""
from __future__ import annotations

import json
import logging
import os
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
)

_ENV_FILE = _MIG_DIR / ".env.migration"
load_dotenv(_ENV_FILE)

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
    stage = os.environ.get("STAGE", "").strip()
    if stage not in ("test", "prod"):
        LOG.error("STAGE must be 'test' or 'prod' in .env.migration (got %r)", stage)
        return 1

    bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not bucket:
        LOG.error("Set IMAGE_S3_BUCKET in .env.migration")
        return 1

    host = os.environ["TARGET_HOST"]
    port = int(os.environ.get("TARGET_PORT", "4000"))
    user = os.environ["TARGET_USER"]
    password = os.environ["TARGET_PASSWORD"]
    tls = _env_bool("TARGET_TLS_ENABLED", True)

    schema_map = default_schema_map(stage)
    conn = pymysql.connect(**_connect_kwargs(host, port, user, password, None, tls))
    report: dict[str, Any] = {"fixups_errors": []}
    try:
        post_migration_s3_images(conn, schema_map, report)
    finally:
        conn.close()

    stats = report.get("s3_image_migration") or {}
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
