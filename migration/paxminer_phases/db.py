"""Shared DB helpers for PAXMiner migration phases."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
_RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "receipts"


class _ListHandler(logging.Handler):
    """Capture formatted log lines for the receipt file."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


def _load_env(stage: str) -> None:
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parent.parent / f".env.migration.{stage}"
    if env_file.exists():
        load_dotenv(env_file)


def _connect():
    import pymysql

    return pymysql.connect(
        host=os.environ["TARGET_HOST"],
        port=int(os.environ.get("TARGET_PORT", "4000")),
        user=os.environ["TARGET_USER"],
        password=os.environ["TARGET_PASSWORD"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        ssl={"ssl": {}} if os.environ.get("TARGET_TLS_ENABLED", "true").lower() in ("1", "true", "yes") else None,
    )


def _column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """,
        (schema, table, column),
    )
    return int(cur.fetchone()["c"]) > 0


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        """,
        (schema, table),
    )
    return int(cur.fetchone()["c"]) > 0


def _pm_schema(stage: str) -> str:
    return os.environ.get("TARGET_PAXMINER_SCHEMA") or f"paxminer_{stage}"


def _sb_schema(stage: str) -> str:
    return os.environ.get("TARGET_SLACKBLAST_SCHEMA") or f"slackblast_{stage}"


def _wb_schema(stage: str) -> str:
    return os.environ.get("TARGET_WEASELBOT_SCHEMA") or f"weaselbot_{stage}"


def _write_receipt(
    filename_prefix: str,
    stage: str,
    header: list[str],
    log_lines: list[str],
    *,
    summary: list[str] | None = None,
) -> Path:
    """Write a migration receipt under migration/receipts/."""
    _RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = _RECEIPTS_DIR / f"{filename_prefix}-{stage}-{stamp}.txt"
    body_lines = [*header]
    if summary:
        body_lines.extend(["", "=== Summary ===", *summary])
    body_lines.extend(["", "=== Console log ===", *log_lines, ""])
    path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    return path
