import os
import ssl
import sys
from pathlib import Path

import pandas as pd

_pm_root = Path(__file__).resolve().parent.parent
if str(_pm_root) not in sys.path:
    sys.path.insert(0, str(_pm_root))

from common.encryption import decrypt_field
import pymysql.cursors
from F3SlackUserLister import database_slack_user_update, init_db
from F3SlackChannelLister import database_slack_channel_update
import logging

def database_management_update():
    logging.basicConfig(format=f'%(asctime)s %(levelname)-8s %(message)s',
                            datefmt = '%Y-%m-%d %H:%M:%S',
                            level = logging.INFO)

    host = os.environ.get("DATABASE_HOST") or os.environ["host"]
    port = int(os.environ.get("DATABASE_PORT", os.environ.get("port", "3306")))
    user = os.environ.get("DATABASE_USER") or os.environ["user"]
    password = os.environ.get("DATABASE_PASSWORD") or os.environ["password"]
    full_run = os.environ.get("full_run") or os.environ.get("DATABASE_FULL_RUN")
    db = os.environ.get("PAXMINER_SCHEMA", "paxminer")
    # Default TLS on when unset (matches paxminer_db.connect_from_env and SAM template)
    _tls = os.environ.get("DATABASE_TLS_ENABLED", "").strip().lower()
    if _tls in ("0", "false", "no", "off"):
        tls = False
    elif _tls in ("1", "true", "yes", "on"):
        tls = True
    else:
        tls = True

    conn_kw = dict(
        host=host,
        port=port,
        user=user,
        password=password,
        db=db,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    if tls:
        conn_kw["ssl"] = ssl.create_default_context()
    mydb1 = pymysql.connect(**conn_kw)

    # Get list of regions and Slack tokens for PAXminer execution
    try:
        with mydb1.cursor() as cursor:
            sql = f"SELECT * FROM `{db}`.`regions` where active = 1"
            cursor.execute(sql)
            regions = cursor.fetchall()
            regions_df = pd.DataFrame(regions, columns=['region', 'slack_token', 'schema_name'])
    finally:
        mydb1.close()
        logging.info('Getting list of regions that use PAXminer...')

    for index, row in regions_df.iterrows():
        region = row['region']
        _tok = row["slack_token"]
        key = decrypt_field(_tok) if _tok else None
        region_db = row['schema_name']
        
        logging.info('Executing user updates for region ' + region)
        try :
            database_slack_user_update(region_db, key, full_run, init_db(host, port, user, password, region_db))
        except Exception as e:
            logging.error("An error occured updating the users for region " + region_db)
            logging.error(e)

        try :
            database_slack_channel_update(region_db, key, init_db(host, port, user, password, region_db))
        except Exception as e:
            logging.error("An error occured updating the channels for region " + region_db)
            logging.error(e)
        
        logging.info("----------------- End of Region Update -----------------")