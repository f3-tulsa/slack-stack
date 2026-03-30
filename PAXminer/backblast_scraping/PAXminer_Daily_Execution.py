#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script executes the daily PAXminer backblast queries and data updates for all F3 regions using PAXminer.
'''

import configparser
import os
import sys
from pathlib import Path

import pandas as pd
import pymysql.cursors

# Set the working directory to the directory of the script
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

_PAX_ROOT = Path(__file__).resolve().parent.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
from common.encryption import decrypt_field
from paxminer_db import connect_from_credentials_ini, paxminer_schema_from_ini

# Set RegEx range for which regions will be queried. Command line input parameter 1 should be a regex range (e.g. A-M) which will search for all regions starting with A through M.
region_regex = sys.argv[1]

_ini = _PAX_ROOT / "config" / "credentials.ini"
_cfg = configparser.ConfigParser()
_cfg.read(_ini)
registry_db = _cfg["aws"]["db"]
pm_schema = paxminer_schema_from_ini(_ini)

# Define AWS Database connection criteria
mydb1 = connect_from_credentials_ini(registry_db, _ini)

# Get list of regions and Slack tokens for PAXminer execution
try:
    with mydb1.cursor() as cursor:
        sql = (
            "SELECT * from `"
            + pm_schema
            + "`.`regions` WHERE active = 1 AND region REGEXP '^["
            + region_regex
            + "]' and scrape_backblasts = 1"
        )
        cursor.execute(sql)
        regions = cursor.fetchall()
        regions_df = pd.DataFrame(regions, columns=['region', 'slack_token', 'schema_name'])
finally:
    print('Getting list of regions that use PAXminer...')

for index, row in regions_df.iterrows():
    region = row['region']
    key = decrypt_field(row['slack_token'])
    db = row['schema_name']
    print('Executing user updates for region ' + region)
    #os.system("./F3SlackUserLister.py " + db + " " + key)
    #os.system("./F3SlackChannelLister.py " + db + " " + key)
    #os.system("./BDminer.py " + db + " " + key)
    #os.system("./PAXminer.py " + db + " " + key)
    print('----------------- End of Region Update -----------------\n')