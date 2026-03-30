"""Encrypt Slack tokens and upsert into PAXminer / Weaselbot registry tables (Lambda cold start)."""

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


def upsert_weaselbot_slack_token(
    *,
    weaselbot_schema: str,
    team_id: str,
    paxminer_regional_schema: str,
    plaintext_token: str,
) -> None:
    """Insert or update ``weaselbot.<schema>.regions`` row for ``paxminer_schema`` with encrypted ``slack_token``."""
    enc = encrypt_field(plaintext_token.strip())
    if not enc:
        return
    conn = _connect_mysql(database=weaselbot_schema)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `id` FROM `{weaselbot_schema}`.`regions` WHERE `paxminer_schema` = %s LIMIT 1",
                (paxminer_regional_schema,),
            )
            row = cur.fetchone()
            if row:
                rid = row.get("id") or row.get("ID")
                cur.execute(
                    f"UPDATE `{weaselbot_schema}`.`regions` SET `slack_token` = %s WHERE `id` = %s",
                    (enc, rid),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO `{weaselbot_schema}`.`regions`
                        (`team_id`, `slack_token`, `paxminer_schema`, `send_achievements`, `send_aoq_reports`)
                    VALUES (%s, %s, %s, 1, 1)
                    """,
                    (team_id.strip(), enc, paxminer_regional_schema),
                )
        conn.commit()
        LOG.info(
            "Weaselbot regions: upserted encrypted slack_token for paxminer_schema=%s",
            paxminer_regional_schema,
        )
    finally:
        conn.close()
