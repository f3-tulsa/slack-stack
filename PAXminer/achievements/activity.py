"""Resolve beatdown activity_type once; never regex at query time."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

LOG = logging.getLogger(__name__)

QSOURCE_BB_RE = re.compile(r"q.{0,1}source|q{0,1}[1-9]\.[0-9]\s", re.I)
QSOURCE_AO_RE = re.compile(r"q.{0,1}source", re.I)
RUCK_AO_RE = re.compile(r"ruck", re.I)

BUILTIN_ACTIVITY_TYPES = ("beatdown", "qsource", "rucking", "bootcamp", "2nd F")

QSOURCE_LABELS = frozenset({"qsource", "q-source", "q source", "qsource lesson"})
RUCK_LABELS = frozenset({"rucking", "ruck", "rucksack"})

# Set-based catch-up for rows Slackblast wrote since the last run (column NULL).
CLASSIFY_NULL_SQL = """
UPDATE `{schema}`.`beatdowns` b
JOIN `{schema}`.`aos` ao ON b.ao_id = ao.channel_id
SET b.activity_type = CASE
  WHEN JSON_UNQUOTE(JSON_EXTRACT(b.json, '$."Event Type"')) IS NOT NULL
       AND JSON_UNQUOTE(JSON_EXTRACT(b.json, '$."Event Type"')) NOT IN ('', 'null')
    THEN JSON_UNQUOTE(JSON_EXTRACT(b.json, '$."Event Type"'))
  WHEN LOWER(IFNULL(ao.ao, '')) REGEXP 'q.?source' THEN 'qsource'
  WHEN LOWER(IFNULL(ao.ao, '')) REGEXP 'ruck' THEN 'rucking'
  WHEN LOWER(LEFT(IFNULL(b.backblast, ''), 100)) REGEXP 'q.?source|q?[1-9]\\\\.[0-9][[:space:]]'
    THEN 'qsource'
  ELSE 'beatdown'
END
WHERE b.activity_type IS NULL
"""


def event_type_from_json(raw: Any) -> str | None:
    """Slackblast stores custom fields on beatdowns.json; Event Type is the configured name."""
    data = raw
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if str(key).strip().lower() != "event type" or value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            value = value.get("value") or value.get("name")
        text = str(value).strip()
        return text or None
    return None


def classify_activity_type(
    *,
    json_blob: Any = None,
    ao_name: str | None = None,
    backblast: str | None = None,
    existing: str | None = None,
) -> str:
    """Resolution: existing → Event Type custom field → AO name → backblast text → beatdown."""
    if existing:
        return str(existing).strip()
    custom = event_type_from_json(json_blob)
    if custom:
        return custom
    ao = ao_name or ""
    if QSOURCE_AO_RE.search(ao):
        return "qsource"
    if RUCK_AO_RE.search(ao):
        return "rucking"
    bb = (backblast or "")[:100]
    if QSOURCE_BB_RE.search(bb):
        return "qsource"
    return "beatdown"


def legacy_activity_to_list(activity: str | None) -> list[str]:
    """Map old enum (beatdown|qsource|any) to a versioned activity list. Empty = all types."""
    raw = (activity or "beatdown").strip().lower()
    if raw in ("any", "*", "all"):
        return []
    if raw in QSOURCE_LABELS:
        return ["qsource", "QSource", "Q-Source"]
    if raw in RUCK_LABELS:
        return ["rucking", "ruck", "Rucking"]
    if raw == "beatdown":
        return ["beatdown", "Bootcamp", "bootcamp"]
    return [activity] if activity else []


def activity_list_from_rule(rule: dict) -> list[str]:
    raw = rule.get("activity")
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if str(x).strip()]
            except json.JSONDecodeError:
                pass
        return legacy_activity_to_list(text)
    return []


def activity_legacy_mirror(activity_list: list[str]) -> str:
    """Best-effort string for achievements_list.activity (Slackblast ORM)."""
    if not activity_list:
        return "any"
    lowered = {a.lower() for a in activity_list}
    if lowered <= QSOURCE_LABELS or "qsource" in lowered:
        if not (lowered & {"beatdown", "bootcamp", "rucking", "ruck"}):
            return "qsource"
    if lowered <= {"beatdown", "bootcamp"}:
        return "beatdown"
    if lowered <= RUCK_LABELS:
        return "rucking"
    if len(activity_list) > 1:
        return "any"
    return activity_list[0][:32]


def filter_by_activity_types(df, activity_list: list[str]):
    if df is None or getattr(df, "empty", True):
        return df
    if not activity_list:
        return df
    if "activity_type" not in df.columns:
        return df
    lowered = {a.lower() for a in activity_list}
    col = df["activity_type"].fillna("beatdown").astype(str).str.lower()
    return df[col.isin(lowered)]


def classify_null_activity_types(cur, schema: str) -> None:
    """Set-based catch-up; no-op when the column is missing or the UPDATE fails."""
    try:
        cur.execute(CLASSIFY_NULL_SQL.format(schema=schema))
    except Exception:
        LOG.debug("activity_type catch-up skipped schema=%s", schema, exc_info=True)
