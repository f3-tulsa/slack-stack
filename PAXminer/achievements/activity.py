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


def unique_activity_labels(items: list[str] | tuple[str, ...]) -> list[str]:
    """Keep first spelling of each activity; later case variants are dropped."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        label = str(raw).strip() if raw is not None else ""
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def map_activities_to_options(selected: list[str], options: list[str]) -> list[str]:
    """Case-fold duplicates and use the option's spelling when it matches."""
    by_lower = {o.lower(): o for o in options}
    out: list[str] = []
    seen: set[str] = set()
    for raw in selected:
        label = str(raw).strip() if raw is not None else ""
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(by_lower.get(key, label))
    return out


UNLABELED_POST_TYPE = "beatdown"


def event_type_options_from_custom_fields(raw: Any) -> list[str]:
    """Slackblast Event Type dropdown options, catalog order, no unlabeled sentinel."""
    data = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
        data = raw
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    field = None
    for key, value in data.items():
        if str(key).strip().lower() == "event type":
            field = value
            break
    if not isinstance(field, dict):
        return []
    options = field.get("options") or []
    if not isinstance(options, list):
        return []
    return unique_activity_labels(
        [
            str(item).strip()
            for item in options
            if str(item).strip() and str(item).strip().lower() != UNLABELED_POST_TYPE
        ]
    )


def canonicalize_activity_filter(labels: list[str] | None) -> list[str]:
    """Empty / the old beatdown sentinel means any event. Drop unlabeled from mixed lists."""
    items = unique_activity_labels(
        [str(x) for x in (labels or []) if str(x).strip()]
    )
    lowered = {a.lower() for a in items}
    if not lowered or lowered <= {UNLABELED_POST_TYPE}:
        return []
    if UNLABELED_POST_TYPE in lowered and lowered <= {UNLABELED_POST_TYPE, "bootcamp"}:
        return []
    return unique_activity_labels(
        [a for a in items if a.lower() != UNLABELED_POST_TYPE]
    )


def activity_json_for_version(activity_list: list[str] | None) -> str | None:
    """JSON column payload. Empty filter is SQL NULL, never [] or the beatdown sentinel."""
    cleaned = canonicalize_activity_filter(activity_list)
    if not cleaned:
        return None
    return json.dumps(cleaned)


def legacy_activity_to_list(activity: str | None) -> list[str]:
    """Map old enum (beatdown|qsource|any) to a versioned activity list. Empty = all types."""
    raw = (activity or "").strip().lower()
    if raw in ("", "any", "*", "all", UNLABELED_POST_TYPE):
        return []
    if raw in QSOURCE_LABELS:
        return unique_activity_labels(["qsource", "QSource", "Q-Source"])
    if raw in RUCK_LABELS:
        return unique_activity_labels(["rucking", "ruck", "Rucking"])
    text = (activity or "").strip()
    return [text] if text else []


def activity_list_from_rule(rule: dict) -> list[str]:
    raw = rule.get("activity")
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, list):
        return canonicalize_activity_filter([str(x) for x in raw if str(x).strip()])
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return canonicalize_activity_filter(
                        [str(x) for x in parsed if str(x).strip()]
                    )
            except json.JSONDecodeError:
                pass
        return canonicalize_activity_filter(legacy_activity_to_list(text))
    return []


def activity_legacy_mirror(activity_list: list[str]) -> str:
    """Best-effort string for achievements_list.activity (varchar NOT NULL). Empty = any."""
    cleaned = canonicalize_activity_filter(activity_list)
    if not cleaned:
        return "any"
    lowered = {a.lower() for a in cleaned}
    if lowered <= QSOURCE_LABELS or "qsource" in lowered:
        if not (lowered & {"bootcamp", "rucking", "ruck"}):
            return "qsource"
    if lowered <= RUCK_LABELS:
        return "rucking"
    if len(cleaned) > 1:
        return "any"
    return cleaned[0][:32]


def filter_by_activity_types(df, activity_list: list[str]):
    if df is None or getattr(df, "empty", True):
        return df
    activity_list = canonicalize_activity_filter(activity_list)
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
