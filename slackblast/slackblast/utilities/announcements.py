# A quick script to make announcements (changelogs, etc) to Slack
import time
from logging import Logger
from typing import List

from slack_sdk import WebClient

from utilities.database import DbManager, paxminer_schema_name
from utilities.field_encryption import decrypt_field
from utilities.database.orm import PaxminerRegion, Region

msg = "Hello, {region}! This is an automated notice from the Slackblast operators. A couple of known Slack client issues may affect your Slackblast usage:\n\n"
msg += ":warning: *Tagging* - Particularly on Android phones, you've probably noticed that you can only tag other PAX with their full name, not their display / F3 name\n\n"
msg += ":warning: *Errors while using emojis* - Particularly on iOS devices, editing moleskines that have emojis will result in an error when you try to submit\n\n"
msg += "\nBoth of these are known issues that I've bubbled up to Slack support. Unfortunately, there's nothing we can do but wait at this point. To avoid the second issue, I would avoid the use of emojis for now.\n"
msg += "\n~ :moneybag: :baseball:"


def send(client: WebClient, body: dict, logger: Logger, context: dict, region_record: Region):
    if body.get("text") == "confirm":
        region_records: List[Region] = DbManager.find_records(Region, filters=[True])
        paxminer_regions = DbManager.find_records(PaxminerRegion, filters=[True], schema=paxminer_schema_name())
        paxminer_dict = {region.schema_name: region.firstf_channel for region in paxminer_regions}

        for region in region_records:
            if region.paxminer_schema:
                send_channel = paxminer_dict.get(region.paxminer_schema)
                if send_channel:
                    logger.info("Announcement: sending message to %s", region.workspace_name)
                    client = WebClient(token=decrypt_field(region.bot_token))
                    try:
                        client.chat_postMessage(channel=send_channel, text=msg.format(region=region.workspace_name))
                        logger.info("Announcement: message sent to %s", region.workspace_name)
                    except Exception as e:
                        resp = getattr(e, "response", None)
                        slack_err = resp.get("error") if isinstance(resp, dict) else None
                        if slack_err == "ratelimited":
                            logger.info(
                                "Announcement: rate limited, waiting 10 seconds (region=%s)", region.workspace_name
                            )
                            time.sleep(10)
                            try:
                                client.chat_postMessage(
                                    channel=send_channel, text=msg.format(region=region.workspace_name)
                                )
                                logger.info("Announcement: message sent to %s after retry", region.workspace_name)
                            except Exception as retry_exc:
                                logger.error(
                                    "Announcement: error sending to %s after retry: %s",
                                    region.workspace_name,
                                    retry_exc,
                                    exc_info=True,
                                )
                        else:
                            logger.error(
                                "Announcement: error sending message to %s: %s",
                                region.workspace_name,
                                e,
                                exc_info=True,
                            )
