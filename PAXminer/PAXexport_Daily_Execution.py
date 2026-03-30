#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script executes the daily PAXminer export to tab delimited files for all F3 regions using PAXminer.
'''

import os
import sys
from pathlib import Path

import pandas as pd
import pymysql.cursors

# Set the working directory to the directory of the script
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

_PAX_ROOT = Path(__file__).resolve().parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
from common.encryption import decrypt_field
from paxminer_db import connect_from_credentials_ini, paxminer_schema_from_ini

_ini = _PAX_ROOT / "config" / "credentials.ini"
pm_schema = paxminer_schema_from_ini(_ini)
mydb1 = connect_from_credentials_ini(pm_schema, _ini)

# Get list of regions and Slack tokens for PAXminer execution
try:
    with mydb1.cursor() as cursor:
        sql = "SELECT * FROM `" + pm_schema + "`.`regions` WHERE active = 1"
        cursor.execute(sql)
        regions = cursor.fetchall()
        regions_df = pd.DataFrame(regions, columns=["region", "slack_token", "schema_name"])
finally:
    print('Getting list of regions for export...')

for index, row in regions_df.iterrows():
    region = row['region']
    key = decrypt_field(row['slack_token'])
    db = row['schema_name']
    print('Exporting data for region ' + region)
    os.system("./DelimFileWriter.py " + db + " " + key)
    print('----------------- End of Region Export -----------------\n')