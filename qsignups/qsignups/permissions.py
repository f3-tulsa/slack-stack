"""QSignups tiered permissions: Slack admin/owner, AOQ (site Q), past Q, or user."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from sqlalchemy import text

from database import get_engine

_LOG = logging.getLogger(__name__)

_SCHEMA_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")


def resolved_paxminer_regional_schema() -> Optional[str]:
    """First valid schema from PAXMINER_REGIONAL_SCHEMA (comma-separated allowed)."""
    raw = (os.environ.get("PAXMINER_REGIONAL_SCHEMA") or "").strip()
    if not raw:
        return None
    first = raw.split(",")[0].strip()
    if not first or not _SCHEMA_TOKEN.match(first):
        _LOG.warning("PAXMINER_REGIONAL_SCHEMA invalid or empty after parse; got %r", raw)
        return None
    return first


class PermissionLevel(Enum):
    ADMIN = "admin"
    AOQ = "aoq"
    Q = "q"
    USER = "user"


@dataclass
class UserPermission:
    level: PermissionLevel
    aoq_channel_ids: List[str] = field(default_factory=list)


def slack_is_admin_or_owner(user_info_dict: Any) -> bool:
    """True if Slack workspace admin or owner (including primary owner)."""
    u = (user_info_dict or {}).get("user") or {}
    return bool(
        u.get("is_admin") or u.get("is_owner") or u.get("is_primary_owner")
    )


def get_aoq_channels(user_id: str, schema: str) -> List[str]:
    """Slack channel_ids where this user is site_q_user_id in regional PAXminer aos."""
    if not schema or not _SCHEMA_TOKEN.match(schema):
        return []
    sql = text(f"SELECT channel_id FROM `{schema}`.aos WHERE site_q_user_id = :uid")
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(sql, {"uid": user_id}).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        _LOG.exception("get_aoq_channels failed schema=%s user_id=%s", schema, user_id)
        return []


def has_q_history(user_id: str, schema: str) -> bool:
    """True if user appears as primary Q on any beatdown in regional schema."""
    if not schema or not _SCHEMA_TOKEN.match(schema):
        return False
    sql = text(
        f"SELECT 1 FROM `{schema}`.beatdowns WHERE q_user_id = :uid LIMIT 1"
    )
    try:
        with get_engine().connect() as conn:
            row = conn.execute(sql, {"uid": user_id}).first()
        return row is not None
    except Exception:
        _LOG.exception("has_q_history failed schema=%s user_id=%s", schema, user_id)
        return False


def resolve_user_permission(
    user_info_dict: Any,
    user_id: str,
    paxminer_schema: Optional[str],
) -> UserPermission:
    """
    Waterfall: Slack admin/owner -> AOQ -> past Q -> USER.
    Without paxminer_schema, AOQ/Q cannot be detected -> USER (unless admin).
    """
    if slack_is_admin_or_owner(user_info_dict):
        return UserPermission(PermissionLevel.ADMIN, [])

    if not paxminer_schema:
        return UserPermission(PermissionLevel.USER, [])

    aoq = get_aoq_channels(user_id, paxminer_schema)
    if aoq:
        return UserPermission(PermissionLevel.AOQ, aoq)

    if has_q_history(user_id, paxminer_schema):
        return UserPermission(PermissionLevel.Q, [])

    return UserPermission(PermissionLevel.USER, [])


def can_manage_ao(permission: UserPermission, ao_channel_id: Optional[str]) -> bool:
    if permission.level == PermissionLevel.ADMIN:
        return True
    if permission.level == PermissionLevel.AOQ and ao_channel_id:
        return ao_channel_id in permission.aoq_channel_ids
    return False


def can_manage_events_for_ao(permission: UserPermission, ao_channel_id: Optional[str]) -> bool:
    return can_manage_ao(permission, ao_channel_id)


def can_edit_any_q_slot(permission: UserPermission) -> bool:
    return permission.level in (
        PermissionLevel.ADMIN,
        PermissionLevel.AOQ,
        PermissionLevel.Q,
    )


def can_open_manage_region_calendar(permission: UserPermission) -> bool:
    return permission.level in (PermissionLevel.ADMIN, PermissionLevel.AOQ)
