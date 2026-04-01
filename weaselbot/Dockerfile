FROM public.ecr.aws/lambda/python:3.13

WORKDIR ${LAMBDA_TASK_ROOT}

COPY weaselbot/requirements-lambda.txt requirements-lambda.txt
RUN pip install --no-cache-dir -r requirements-lambda.txt

COPY common/ ./common/
COPY weaselbot/weaselbot/ ./weaselbot/
COPY weaselbot/handlers.py ./

# SAM overrides per-function via ImageConfig.Command
CMD ["handlers.achievements_handler"]
