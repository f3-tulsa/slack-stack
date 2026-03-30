"""Shared MySQL connection helpers for PAXminer scripts (credentials.ini + optional env overrides)."""
from __future__ import annotations

import configparser
import logging
import os
import ssl
from pathlib import Path

import pymysql

_ROOT = Path(__file__).resolve().parent


def credentials_path(relative: str = "config/credentials.ini") -> Path:
    return _ROOT / relative


def load_aws_section(ini_path: Path | None = None) -> dict:
    path = ini_path or credentials_path()
    cfg = configparser.ConfigParser()
    cfg.read(path)
    sec = cfg["aws"]
    return {
        "host": sec["host"],
        "port": int(sec.get("port", "3306")),
        "user": sec["user"],
        "password": sec["password"],
        "paxminer_schema": sec.get("paxminer_schema", "paxminer").strip() or "paxminer",
        "tls": sec.get("tls", "false").strip().lower() in ("1", "true", "yes", "on"),
    }


def connect_mysql(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    tls: bool = False,
):
    kw: dict = {
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


def connect_from_credentials_ini(database: str, ini_path: Path | None = None):
    """Connect using [aws] from credentials.ini; database is the schema name to USE."""
    aws = load_aws_section(ini_path)
    return connect_mysql(
        host=aws["host"],
        port=aws["port"],
        user=aws["user"],
        password=aws["password"],
        database=database,
        tls=aws["tls"],
    )


def paxminer_schema_from_ini(ini_path: Path | None = None) -> str:
    return load_aws_section(ini_path)["paxminer_schema"]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def connect_from_env(database: str):
    """Connect using Lambda / TiDB-style env vars (same names as slackblast/weaselbot)."""
    host = os.environ.get("DATABASE_HOST") or os.environ.get("host")
    user = os.environ.get("DATABASE_USER") or os.environ.get("user")
    password = os.environ.get("DATABASE_PASSWORD") or os.environ.get("password")
    if not host or not user or password is None:
        raise OSError(
            "DATABASE_HOST, DATABASE_USER, and DATABASE_PASSWORD (or legacy host, user, password) must be set"
        )
    port = int(os.environ.get("DATABASE_PORT", os.environ.get("port", "3306")))
    tls = _env_bool("DATABASE_TLS_ENABLED", True)
    logging.getLogger(__name__).info(
        "connect_from_env: database=%s host=%s port=%s tls=%s",
        database,
        host,
        port,
        tls,
    )
    return connect_mysql(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        tls=tls,
    )


def paxminer_schema_from_env() -> str:
    return os.environ.get("PAXMINER_SCHEMA", "paxminer").strip() or "paxminer"
