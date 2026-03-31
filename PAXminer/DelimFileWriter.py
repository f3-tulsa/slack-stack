#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script queries the AWS F3(region) database for all beatdown records. It then deposits the beatdown info and
posting history to tab delimited files on a google sheet.
'''

import matplotlib
matplotlib.use('Agg')
import sys
import os
from pathlib import Path

import pandas as pd

# Set the working directory to the directory of the script
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

_PAX_ROOT = Path(__file__).resolve().parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))
from paxminer_db import connect_from_credentials_ini

db = sys.argv[1]

# argv[2] retained for backward-compatible CLI invocation (token unused for DB export)
_key = sys.argv[2] if len(sys.argv) > 2 else None

mydb = connect_from_credentials_ini(db)

try:
    with mydb.cursor() as cursor:
        sql = "SELECT * FROM beatdown_info"
        cursor.execute(sql)
        bds = cursor.fetchall()
        bds_df = pd.DataFrame(bds)
finally:
    print('Now pulling all beatdown records... Stand by...')

try:
    with mydb.cursor() as cursor:
        sql2 = "SELECT * FROM attendance_view"
        cursor.execute(sql2)
        posts = cursor.fetchall()
        posts_df = pd.DataFrame(posts, columns=["Date", "AO", "PAX"])
finally:
    print('Now pulling all post records... Stand by...')


# saving beatdowns as a CSV file
bds_df.to_csv('/import/f3/' + db + '_Beatdowns.csv', sep =',', index=False)
posts_df.to_csv('/import/f3/' + db + '_Posts.csv', sep =',', index=False)
