"""Encrypt Slack tokens and upsert into PAXminer registry tables (Lambda cold start)."""

from __future__ import annotations

import logging
import os
import ssl
from typing import Any

import pymysql

from common.encryption import encrypt_field

LOG = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _connect_mysql(*, database: str) -> Any:
    host = os.environ.get("DATABASE_HOST") or os.environ.get("host")
    user = os.environ.get("DATABASE_USER") or os.environ.get("user")
    password = os.environ.get("DATABASE_PASSWORD") or os.environ.get("password")
    if not host or not user or password is None:
        raise OSError(
            "DATABASE_HOST, DATABASE_USER, and DATABASE_PASSWORD (or legacy host, user, password) must be set"
        )
    port = int(os.environ.get("DATABASE_PORT", os.environ.get("port", "3306")))
    tls = _env_bool("DATABASE_TLS_ENABLED", True)
    kw: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "db": database,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if tls:
        kw["ssl"] = ssl.create_default_context()
    return pymysql.connect(**kw)


def upsert_paxminer_slack_token(
    *,
    registry_schema: str,
    region_key: str,
    regional_schema_name: str,
    plaintext_token: str,
) -> None:
    """Insert or update ``paxminer.<registry>.regions`` with encrypted ``slack_token``."""
    enc = encrypt_field(plaintext_token.strip())
    if not enc:
        return
    conn = _connect_mysql(database=registry_schema)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO `{registry_schema}`.`regions`
                    (`region`, `slack_token`, `schema_name`, `active`)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE
                    `slack_token` = VALUES(`slack_token`),
                    `schema_name` = VALUES(`schema_name`),
                    `active` = VALUES(`active`)
                """,
                (region_key, enc, regional_schema_name),
            )
        conn.commit()
        LOG.info("PAXminer regions: upserted encrypted slack_token for region=%s schema=%s", region_key, regional_schema_name)
    finally:
        conn.close()
