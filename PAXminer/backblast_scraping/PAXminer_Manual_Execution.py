#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script executes the daily PAXminer backblast queries and data updates for all F3 regions using PAXminer.
'''

import configparser
import os
import sys
import warnings
from pathlib import Path

import pandas as pd
import pymysql.cursors

warnings.simplefilter(action='ignore', category=FutureWarning)

_PAX_ROOT = Path(__file__).resolve().parent.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
from paxminer_db import connect_from_credentials_ini, paxminer_schema_from_ini

_ini = _PAX_ROOT / "config" / "credentials.ini"
_cfg = configparser.ConfigParser()
_cfg.read(_ini)
registry_db = _cfg["aws"]["db"]
pm_schema = paxminer_schema_from_ini(_ini)

mydb1 = connect_from_credentials_ini(registry_db, _ini)

# Get list of regions and Slack tokens for PAXminer execution
try:
    with mydb1.cursor() as cursor:
        sql = (
            "SELECT * FROM `"
            + pm_schema
            + "`.`regions` where region = 'Mobile'"
        )  # <-- Update region filter for whatever region is being tested
        cursor.execute(sql)
        regions = cursor.fetchall()
        regions_df = pd.DataFrame(regions, columns=['region', 'slack_token', 'schema_name'])
finally:
    print('Getting list of regions that use PAXminer...')

for index, row in regions_df.iterrows():
    region = row['region']
    key = row['slack_token']
    db = row['schema_name']
    print('Executing user updates for region ' + region)
    os.system("./F3SlackUserLister.py " + db + " " + key)
    os.system("./F3SlackChannelLister.py " + db + " " + key)
    #os.system("./PAX_BD_Miner.py " + db + " " + key)
    print('----------------- End of Region Update -----------------\n')
print('\nPAXminer execution complete.')