#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script executes the monthly PAXcharter backblast queries and data updates for all F3 regions using PAXminer.
'''

import configparser
import sys
from pathlib import Path

import pandas as pd

_PAX_ROOT = Path(__file__).resolve().parent.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
from common.encryption import decrypt_field
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
        # sql = "SELECT * FROM `"+pm_schema+"`.`regions` where send_region_stats = 1" # <-- Update filter as needed
        sql = "SELECT * FROM `" + pm_schema + "`.`regions` where region = 'Geneva'"  # <-- Update region for testing
        cursor.execute(sql)
        regions = cursor.fetchall()
        regions_df = pd.DataFrame(regions)
finally:
    print('Getting list of regions that use PAXminer...')

for index, row in regions_df.iterrows():
    region = row['region']
    key = decrypt_field(row['slack_token'])
    db = row['schema_name']
    firstf = row['firstf_channel']
    #firstf = 'U0187M4NWG4' # <--- Use this if sending a test msg to a specific user
    print('Processing statistics for region ' + region)
    #os.system("./PAXcharter.py " + db + " " + key)
    #os.system("./UniquePAXCharter.py " + db + " " + key + " " + region + " " + firstf)
    #os.system("./QCharter.py " + db + " " + key + " " + region + " " + firstf)
    #os.system("./Leaderboard_Charter.py " + db + " " + key + " " + region + " " + firstf)
    #os.system("./LeaderboardByAO_Charter.py " + db + " " + key + " " + region + " " + firstf)
    #os.system("./Join_Channels_and_Create_Directories.py " + db + " " + key + " " + region + " " + firstf)
    # AOCharter.py is not shipped in this repo; use LeaderboardByAO_Charter.py or another charter above.
    print('----------------- End of Region Update -----------------\n')
print('\nPAXcharter execution complete.')