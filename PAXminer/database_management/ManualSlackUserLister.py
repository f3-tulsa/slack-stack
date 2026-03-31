from F3SlackUserLister import database_slack_user_update, init_db

import configparser
import sys
from slack_sdk import WebClient


# Configure AWS credentials
config = configparser.ConfigParser()
config.read('../config/credentials.ini')
host = config['aws']['host']
port = int(config['aws']['port'])
user = config['aws']['user']
password = config['aws']['password']
tls = config['aws'].get('tls', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
#db = config['aws']['db']
db = sys.argv[1]

# Set Slack token
key = sys.argv[2]
slack = WebClient(token=key)

database_slack_user_update(db, key, False, init_db(host, port, user, password, db, tls_enabled=tls))