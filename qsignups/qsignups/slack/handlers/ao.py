import logging
import os
import re
from typing import Optional

from sqlalchemy import text

from database import DbManager, get_engine
from database.orm import Weekly, Master, AO
from utilities import safe_get
from . import UpdateResponse

_LOG = logging.getLogger(__name__)

# Regional PAXminer schema names: letters, digits, underscore only (defense in depth for SQL identifiers).
_SCHEMA_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")


def _resolved_paxminer_regional_schema() -> Optional[str]:
    raw = (os.environ.get("PAXMINER_REGIONAL_SCHEMA") or "").strip()
    if not raw:
        return None
    first = raw.split(",")[0].strip()
    if not first or not _SCHEMA_TOKEN.match(first):
        _LOG.warning("PAXMINER_REGIONAL_SCHEMA invalid or empty after parse; got %r", raw)
        return None
    return first


def get_site_q(ao_channel_id: Optional[str]) -> Optional[str]:
    """Read site_q_user_id from regional PAXminer `aos` table (Slack channel_id)."""
    schema = _resolved_paxminer_regional_schema()
    if not schema or not ao_channel_id:
        return None
    sql = text(
        f"SELECT site_q_user_id FROM `{schema}`.aos WHERE channel_id = :ch LIMIT 1"
    )
    try:
        with get_engine().connect() as conn:
            row = conn.execute(sql, {"ch": ao_channel_id}).first()
        if row is None:
            return None
        val = row[0]
        return val if val else None
    except Exception:
        _LOG.exception(
            "get_site_q failed schema=%s channel_id=%s", schema, ao_channel_id
        )
        return None


def set_site_q(ao_channel_id: Optional[str], site_q_user_id: Optional[str]) -> None:
    """Write site_q_user_id on regional PAXminer `aos`; no-op if schema unset or channel missing."""
    schema = _resolved_paxminer_regional_schema()
    if not schema or not ao_channel_id:
        return
    sql = text(
        f"UPDATE `{schema}`.aos SET site_q_user_id = :uid WHERE channel_id = :ch"
    )
    try:
        with get_engine().begin() as conn:
            conn.execute(sql, {"uid": site_q_user_id, "ch": ao_channel_id})
    except Exception:
        _LOG.exception(
            "set_site_q failed schema=%s channel_id=%s", schema, ao_channel_id
        )
        raise


def edit(client, user_id, team_id, logger, ao_channel_id, input_data) -> UpdateResponse:

    # Parse inputs
    ao_display_name = input_data["ao_display_name"]["ao_display_name"]["value"]
    ao_location_subtitle = input_data["ao_location_subtitle"]["ao_location_subtitle"][
        "value"
    ]
    site_q_user_id = safe_get(
        input_data, "site_q_user_id", "site_q_user_id", "selected_user"
    )

    # Attempt updates
    try:
        DbManager.update_records(
            cls=AO,
            filters=[AO.ao_channel_id == ao_channel_id],
            fields={
                AO.ao_display_name: ao_display_name,
                AO.ao_location_subtitle: ao_location_subtitle,
            },
        )
        set_site_q(ao_channel_id, site_q_user_id)
        return UpdateResponse(success=True, message=":white_check_mark: Locked in—the Weinke has been updated!")
    except Exception as e:
        logger.error(f"Error updating: {e}")
        return UpdateResponse(
            success=False,
            message=f"Uh-oh, something broke out in the Gloom! Please try again or contact your Weasel Shaker. Errors:\n{e}",
        )


def delete(client, user_id, team_id, logger, ao_channel_id) -> UpdateResponse:

    # Attempt deletion
    try:
        DbManager.delete_records(
            cls=AO, filters=[AO.ao_channel_id == ao_channel_id]
        )
        DbManager.delete_records(
            cls=Weekly, filters=[Weekly.ao_channel_id == ao_channel_id]
        )
        DbManager.delete_records(
            cls=Master, filters=[Master.ao_channel_id == ao_channel_id]
        )
        return UpdateResponse(success=True, message=":white_check_mark: Locked in—the Weinke has been updated!")
    except Exception as e:
        logger.error(f"Error deleting AO: {e}")
        return UpdateResponse(
            success=False,
            message=f"Uh-oh, something broke out in the Gloom! Please try again or contact your Weasel Shaker. Errors:\n{e}",
        )


def insert(client, user_id, team_id, logger, input_data) -> UpdateResponse:

    # Parse inputs
    ao_channel_id = safe_get(
        input_data, "add_ao_channel_select", "add_ao_channel_select", "selected_channel"
    )
    ao_display_name = safe_get(input_data, "ao_display_name", "ao_display_name", "value")
    ao_location_subtitle = safe_get(
        input_data, "ao_location_subtitle", "ao_location_subtitle", "value"
    )
    site_q_user_id = safe_get(
        input_data, "site_q_user_id", "site_q_user_id", "selected_user"
    )

    # replace double quotes with single quotes
    ao_display_name = ao_display_name.replace('"', "'")
    if ao_location_subtitle:
        ao_location_subtitle = ao_location_subtitle.replace('"', "'")
    else:
        ao_location_subtitle = ""  # TODO: I don't like this, but this field is currently non-nullable

    # Attempt insert
    try:
        DbManager.create_record(
            AO(
                ao_channel_id=ao_channel_id,
                ao_display_name=ao_display_name,
                ao_location_subtitle=ao_location_subtitle,
                team_id=team_id,
            )
        )
        set_site_q(ao_channel_id, site_q_user_id)
        return UpdateResponse(success=True, message=":white_check_mark: Locked in—the Weinke has been updated!")
    except Exception as e:
        logger.error(f"Error inserting: {e}")
        return UpdateResponse(
            success=False,
            message=f"Uh-oh, something broke out in the Gloom! Please try again or contact your Weasel Shaker. Errors:\n{e}",
        )
