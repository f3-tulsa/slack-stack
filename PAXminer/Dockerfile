FROM public.ecr.aws/lambda/python:3.14

WORKDIR ${LAMBDA_TASK_ROOT}

COPY PAXminer/requirements-lambda.txt requirements-lambda.txt
RUN pip install --no-cache-dir -r requirements-lambda.txt

COPY PAXminer/handlers.py ./
COPY PAXminer/paxminer_db.py ./
COPY PAXminer/slack_util.py ./
COPY PAXminer/slack_http.py ./
COPY PAXminer/config_paxminer.py ./
COPY PAXminer/common/ ./common/
COPY PAXminer/achievements/ ./achievements/
COPY PAXminer/kotter/ ./kotter/
COPY PAXminer/database_management/F3SlackChannelLister.py ./database_management/
COPY PAXminer/database_management/F3SlackUserLister.py ./database_management/
COPY PAXminer/database_management/PAXminer_SlackUserUpdate.py ./database_management/
COPY PAXminer/monthly_charts/ ./monthly_charts/

CMD ["handlers.sync_handler"]
