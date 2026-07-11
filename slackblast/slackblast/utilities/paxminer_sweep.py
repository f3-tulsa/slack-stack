"""Fire-and-forget PAXMiner achievement sweep after backblast DB write."""

from __future__ import annotations

import json
import logging
import os
from logging import Logger

import requests

LOG = logging.getLogger(__name__)


def achievements_coupling_configured(region_record) -> bool:
    if not getattr(region_record, "paxminer_schema", None):
        return False
    url = (os.environ.get("PAXMINER_ACHIEVEMENTS_URL") or "").strip()
    secret = (os.environ.get("PAXMINER_SWEEP_SECRET") or "").strip()
    return bool(url and secret)


def trigger_achievement_sweep(
    *,
    region_record,
    pax_user_ids: set[str],
    bd_date: str,
    ao_channel_id: str | None,
    post_to_ao: bool,
    logger: Logger | None = None,
) -> None:
    log = logger or LOG
    if not achievements_coupling_configured(region_record):
        log.debug("Achievement sweep skipped: coupling guard not satisfied")
        return
    url = os.environ["PAXMINER_ACHIEVEMENTS_URL"].strip()
    secret = os.environ["PAXMINER_SWEEP_SECRET"].strip()
    payload = {
        "schema": region_record.paxminer_schema,
        "pax_user_ids": sorted(pax_user_ids),
        "bd_date": bd_date,
        "ao_channel_id": ao_channel_id,
        "post_to_ao": post_to_ao,
    }
    try:
        requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-Paxminer-Sweep-Secret": secret,
            },
            data=json.dumps(payload),
            timeout=3,
        )
        log.info("Achievement sweep invoked schema=%s pax_count=%s", region_record.paxminer_schema, len(pax_user_ids))
    except Exception as e:
        log.warning("Achievement sweep failed (non-fatal): %s", e)
