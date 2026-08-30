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
    if not field.get("enabled"):
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


def canonicalize_exclude_filter(labels: list[str] | None) -> list[str]:
    """Trim, dedupe, drop unlabeled. No beatdown-sentinel collapse (unlike include)."""
    items = unique_activity_labels(
        [str(x) for x in (labels or []) if str(x).strip()]
    )
    return unique_activity_labels(
        [a for a in items if a.lower() != UNLABELED_POST_TYPE]
    )


def empty_activity_filter() -> dict[str, list[str]]:
    return {"include": [], "exclude": []}


def normalize_activity_filter(spec: dict | None) -> dict[str, list[str]]:
    spec = spec or {}
    return {
        "include": canonicalize_activity_filter(spec.get("include") or []),
        "exclude": canonicalize_exclude_filter(spec.get("exclude") or []),
    }


VERSION_POINTER_RE = re.compile(r"^v\d+$", re.I)


def activity_filter_from_rule(rule: dict | None) -> dict[str, list[str]]:
    """Spec from version JSON, a bare include array, a dict, or a legacy enum string."""
    raw = (rule or {}).get("activity")
    if raw is None or raw == "" or raw == []:
        return empty_activity_filter()
    if isinstance(raw, dict):
        return normalize_activity_filter(raw)
    if isinstance(raw, list):
        return {
            "include": canonicalize_activity_filter(
                [str(x) for x in raw if str(x).strip()]
            ),
            "exclude": [],
        }
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                return activity_filter_from_rule({"activity": parsed})
            except json.JSONDecodeError:
                pass
        return {
            "include": canonicalize_activity_filter(legacy_activity_to_list(text)),
            "exclude": [],
        }
    return empty_activity_filter()


def coerce_activity_filter(value: Any) -> dict[str, list[str]]:
    if isinstance(value, dict) and (
        "include" in value or "exclude" in value or not value
    ):
        return normalize_activity_filter(value)
    return activity_filter_from_rule({"activity": value})


def resolve_activity_filter_for_save(values: dict, existing: dict | None) -> dict[str, list[str]]:
    prior = activity_filter_from_rule(existing) if existing else empty_activity_filter()
    filt = values.get("activity_filter")
    if isinstance(filt, dict) and ("include" in filt or "exclude" in filt):
        include = filt.get("include")
        exclude = filt.get("exclude")
        if include is None:
            include = prior["include"]
        if exclude is None:
            exclude = prior["exclude"]
        return normalize_activity_filter({"include": include or [], "exclude": exclude or []})
    if values.get("activity_list") is not None:
        return {
            "include": canonicalize_activity_filter(values.get("activity_list") or []),
            "exclude": prior["exclude"],
        }
    return prior


def activity_filter_conflicts(spec: dict) -> list[str]:
    include = spec.get("include") or []
    exclude_lower = {a.lower() for a in (spec.get("exclude") or [])}
    seen: set[str] = set()
    out: list[str] = []
    for label in include:
        key = str(label).lower()
        if key in exclude_lower and key not in seen:
            seen.add(key)
            out.append(str(label))
    return out


def activity_json_for_version(activity_filter: Any = None) -> str | None:
    """JSON column payload. Empty filter is SQL NULL, never [] or the beatdown sentinel.

    Bare include array when exclude is empty so a pre-5e reader still parses the
    rule. The {include, exclude} dict is only written when there is an exclusion.
    """
    spec = coerce_activity_filter(activity_filter)
    if not spec["include"] and not spec["exclude"]:
        return None
    if not spec["exclude"]:
        return json.dumps(spec["include"])
    return json.dumps({"include": spec["include"], "exclude": spec["exclude"]})


def legacy_activity_to_list(activity: str | None) -> list[str]:
    """Map old enum (beatdown|qsource|any) to a versioned activity list. Empty = all types."""
    raw = (activity or "").strip().lower()
    if raw in ("", "any", "*", "all", UNLABELED_POST_TYPE) or VERSION_POINTER_RE.fullmatch(raw):
        return []
    if raw in QSOURCE_LABELS:
        return unique_activity_labels(["qsource", "QSource", "Q-Source"])
    if raw in RUCK_LABELS:
        return unique_activity_labels(["rucking", "ruck", "Rucking"])
    text = (activity or "").strip()
    return [text] if text else []


def activity_list_from_rule(rule: dict) -> list[str]:
    """Include list only. Empty include is any-event on that side (excludes may still apply)."""
    return activity_filter_from_rule(rule)["include"]


def _mirror_pointer(version: int) -> str:
    return f"v{int(version)}"[:32]


def activity_legacy_mirror(activity_filter: Any = None, *, version: int) -> str:
    """Best-effort string for achievements_list.activity (varchar NOT NULL). Empty = any."""
    spec = coerce_activity_filter(activity_filter)
    include = spec["include"]
    exclude = spec["exclude"]
    if exclude:
        return _mirror_pointer(version)
    if not include:
        return "any"
    lowered = {a.lower() for a in include}
    if (lowered <= QSOURCE_LABELS or "qsource" in lowered) and not (
        lowered & {"bootcamp", "rucking", "ruck"}
    ):
        return "qsource"
    if lowered <= RUCK_LABELS:
        return "rucking"
    if len(include) > 1:
        return _mirror_pointer(version)
    return include[0][:32]


def filter_by_activity_types(df, activity_filter):
    if df is None or getattr(df, "empty", True):
        return df
    spec = coerce_activity_filter(activity_filter)
    include = spec["include"]
    exclude = spec["exclude"]
    if not include and not exclude:
        return df
    if "activity_type" not in df.columns:
        return df
    col = df["activity_type"].fillna(UNLABELED_POST_TYPE).astype(str).str.strip()
    col = col.mask(col.eq(""), UNLABELED_POST_TYPE).str.lower()
    out = df
    if include:
        out = out[col.isin({a.lower() for a in include})]
        col = out["activity_type"].fillna(UNLABELED_POST_TYPE).astype(str).str.strip()
        col = col.mask(col.eq(""), UNLABELED_POST_TYPE).str.lower()
    if exclude:
        out = out[~col.isin({a.lower() for a in exclude})]
    return out


def classify_null_activity_types(cur, schema: str) -> None:
    """Set-based catch-up; no-op when the column is missing or the UPDATE fails."""
    try:
        cur.execute(CLASSIFY_NULL_SQL.format(schema=schema))
    except Exception:
        LOG.debug("activity_type catch-up skipped schema=%s", schema, exc_info=True)
