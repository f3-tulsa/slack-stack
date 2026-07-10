FROM public.ecr.aws/lambda/python:3.14

WORKDIR ${LAMBDA_TASK_ROOT}

COPY PAXminer/requirements-lambda.txt requirements-lambda.txt
RUN pip install --no-cache-dir -r requirements-lambda.txt

COPY PAXminer/handlers.py ./
COPY PAXminer/paxminer_db.py ./
COPY PAXminer/common/ ./common/
COPY PAXminer/database_management/F3SlackChannelLister.py ./database_management/
COPY PAXminer/database_management/F3SlackUserLister.py ./database_management/
COPY PAXminer/database_management/PAXminer_SlackUserUpdate.py ./database_management/
COPY PAXminer/monthly_charts/ ./monthly_charts/

# SAM overrides per-function via ImageConfig.Command
CMD ["handlers.sync_handler"]
