#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script ensures all AO and FirstF channels are joined by PAXminer and it also ensures that the required log and plot directories exist.
'''

import logging
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import os
import sys

db = sys.argv[1]
region = sys.argv[3]

_PAX_ROOT = Path(__file__).resolve().parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
from paxminer_db import connect_from_credentials_ini

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Set Slack token
key = sys.argv[2]
slack = WebClient(token=key)
slack.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
firstf = sys.argv[4] #designated 1st-f channel for the region

mydb = connect_from_credentials_ini(db)
logging.info("Setting up PAXminer environment for region %s", region)
logging.info("Joining FirstF channel")
try:
    slack.conversations_join(channel=firstf)
    logging.info("Joined FirstF channel %s", firstf)
except Exception:
    logging.exception("Could not join firstf channel %s", firstf)

try:
    with mydb.cursor() as cursor:
        sql = "SELECT channel_id, ao FROM aos WHERE backblast = 1 and archived = 0"
        cursor.execute(sql)
        aos = cursor.fetchall()
        aos_df = pd.DataFrame(aos, columns=["channel_id", "ao"])
finally:
    logging.info("Pulling all AO channels... Stand by...")

# Join each AO channel
logging.info("Ensuring PAXminer is a member of all AO channels...")
for index, row in aos_df.iterrows():
    ao = row['ao']
    channel_id = row['channel_id']
    logging.info("Joining AO %s", ao)
    try:
        slack.conversations_join(channel=channel_id)
    except Exception:
        logging.exception("Error joining AO %s channel_id=%s", ao, channel_id)

#Make sure log and plot directories are created
plotdir ='plots/' + db
logdir = 'logs/' + db
parent_dir = "./"

# Plot Path
plotpath = os.path.join(parent_dir, plotdir)

# Log Path
logpath = os.path.join(parent_dir, logdir)

# Create the directories
logging.info("Creating required log and plot directories for region %s", region)
try:
    os.mkdir(plotpath)
    logging.info("Directory '%s' created", plotpath)
except OSError as ploterror:
    logging.warning("%s", ploterror)
try:
    os.mkdir(logpath)
    logging.info("Directory '%s' created", logpath)
except OSError as logerror:
    logging.warning("%s", logerror)

logging.info("End of preparations")