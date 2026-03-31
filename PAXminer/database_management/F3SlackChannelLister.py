#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script queries Slack for Channels and inserts channel IDs/names into the AWS database for recordkeeping.
The Channels data table is used by PAXminer to query only AO channels for backblasts. Uses parameterized inputs for
multiple region updates.

Usage: F3SlackChannelLister.py [db_name] [slack_token]
'''

import pandas as pd
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
import logging


def database_slack_channel_update(region_db, key, mydb):
    logging.info("Database_slack_user_update")

    slack = WebClient(token=key)
    slack.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))

    cursor_slack = ""
    all_channels = []
    while True:
        kwargs = {"limit": 999, "types": "public_channel,private_channel"}
        if cursor_slack:
            kwargs["cursor"] = cursor_slack
        channels_response = slack.conversations_list(**kwargs)
        batch = channels_response.data.get("channels") or []
        all_channels.extend(batch)
        cursor_slack = (channels_response.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor_slack:
            break

    channels_df = pd.json_normalize(all_channels)
    channels_df = channels_df[["id", "name", "created", "is_archived"]]
    channels_df = channels_df.rename(
        columns={
            "id": "channel_id",
            "name": "ao",
            "created": "channel_created",
            "is_archived": "archived",
        }
    )

    logging.info("Updating Slack channel list / AOs for region..." + region_db)
    inserted = 0
    updated = 0
    try:
        with mydb.cursor() as cursor:
            for _index, row in channels_df.iterrows():
                sql = (
                    "INSERT INTO aos (ao, channel_id, channel_created, archived) VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE ao=%s, archived=%s"
                )
                channel_name_tmp = row["ao"]
                channel_id_tmp = row["channel_id"]
                channel_created_tmp = row["channel_created"]
                archived_tmp = row["archived"]
                val = (
                    channel_name_tmp,
                    channel_id_tmp,
                    channel_created_tmp,
                    archived_tmp,
                    channel_name_tmp,
                    archived_tmp,
                )

                cursor.execute(sql, val)
                rc = cursor.rowcount
                if rc == 1:
                    logging.info(channel_name_tmp + " record inserted.")
                    inserted += 1
                elif rc == 2:
                    logging.info(channel_name_tmp + " record updated.")
                    updated += 1
        mydb.commit()
        with mydb.cursor() as cursor3:
            sql3 = "UPDATE aos SET backblast = 0 where backblast IS NULL"
            cursor3.execute(sql3)
        mydb.commit()

    finally:
        mydb.close()

    if inserted or updated:
        try:
            slack.chat_postMessage(
                channel="paxminer_logs",
                text=f" - Channel sync ({region_db}): {inserted} created, {updated} updated",
            )
        except Exception as log_exc:
            logging.debug("paxminer_logs channel summary failed: %s", log_exc, exc_info=True)
