# QSignups

Slack app for **Q signups**, schedule management, and **Google Calendar** integration.

Part of **[slack-stack](../README.md)**. Deploy with `qsignups/template.yaml`, `./deploy.sh`, or GitHub Actions — see the root README for `DB_ENCRYPTION_KEY`, Google OAuth secrets, and database schema variables.

## Install (production)

Use the OAuth install URL from your deployed API (CloudFormation output **`QSignupsApi`**) or from the Slack app’s **Install to Workspace** flow. If Slack shows a transient error, use “Try again” as usual.

## Features

- Home tab / slash commands for schedule and signups.
- Google Calendar sync (OAuth tokens for `google_auth_data` are encrypted at rest; `DB_ENCRYPTION_KEY` is required at deploy/runtime — see root README).

## Schedule reconciliation

- **Rolling calendar:** Adding a recurring AO schedule creates `qsignups_master` rows from the chosen start date through **today + `SCHEDULE_CREATE_LENGTH_DAYS`** (SAM parameter `ScheduleCreateLengthDays`, default **365**). The same horizon is used when extending and when editing a recurring series.
- **`extend_all_schedules`:** Fills gaps in the master calendar and removes **orphan** future recurring rows (series that no longer exist in `qsignups_weekly`, e.g. after a bad cleanup). It runs on a **7-day** EventBridge schedule (`qsignups.extend-schedule` payload) and **after each stack deploy** via a CloudFormation custom resource (pass a new `ExtendScheduleDeployNonce` each deploy — `deploy.sh` and CI set this automatically).
- **Editing a recurring schedule:** The Slack edit flow **deletes** future recurring `qsignups_master` rows for the **old** series (after today) and **recreates** rows through the rolling horizon with the new day/time/AO. **Today’s** row is left in place. Q signups on deleted future dates are cleared (those beats are no longer valid).
- **Deleting a recurring schedule:** Removes all future master rows for that series and the `qsignups_weekly` row (existing behavior).

Local tests (run from `qsignups/qsignups` with `PYTHONPATH=.`):

- Extend/edit schedule: [`testing/test_extend_schedule_local.py`](testing/test_extend_schedule_local.py)
- Weinke rendering / S3 / grid logic: [`testing/test_weinke_local.py`](testing/test_weinke_local.py)

## CI/CD

Workflow: [.github/workflows/deploy.yml](../.github/workflows/deploy.yml) (branches `test` / `prod`, AWS OIDC).

## Local development

1. Clone this monorepo and install [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
2. Create a Slack app from **[manifest.json](manifest.json)**. For local dev, replace `__HOSTNAME__` with your ngrok base URL, or use **`python generate.py --hostname https://...`** to emit `generate/manifest.json`. After **`./deploy.sh`** from the repo root, use **`qsignups/manifest-{test|prod}.json`** (gitignored) for the deployed API URL.

3. From the **`qsignups/`** directory, create `env.json` for **`sam local start-api --env-vars`** (keys are **Lambda environment variable names**, logical resource id **`QSignupsFunction`**):

```json
{
  "QSignupsFunction": {
    "SLACK_BOT_TOKEN": "xoxb-...",
    "SLACK_SIGNING_SECRET": "...",
    "ENV_SLACK_CLIENT_SECRET": "...",
    "ENV_SLACK_SCOPES": "app_mentions:read,channels:history,...",
    "ENV_SLACK_CLIENT_ID": "your-slack-app-client-id",
    "DATABASE_HOST": "host.docker.internal",
    "DATABASE_PORT": "3306",
    "DATABASE_TLS_ENABLED": "false",
    "ADMIN_DATABASE_USER": "local_user",
    "ADMIN_DATABASE_PASSWORD": "local_password",
    "ADMIN_DATABASE_SCHEMA": "your_schema",
    "DB_ENCRYPTION_KEY": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "TIMEZONE": "US/Central",
    "IMAGE_S3_BUCKET": "your-public-image-bucket"
  }
}
```

4. Run ngrok on port **3000**, update the Slack app URLs, then:

```bash
sam build -t template.yaml
sam local start-api --env-vars env.json --warm-containers EAGER
```

Use your machine’s LAN IP for `DATABASE_HOST` if `host.docker.internal` does not work from the Lambda container.

## Weekly weinke images (calendar grids)

Weinke PNGs power the **Home tab** `image` blocks (`current_week_weinke` / `next_week_weinke` on `qsignups_regions`). Objects are stored under **`s3://{IMAGE_S3_BUCKET}/weinkes/`** (same public bucket as slackblast).

### On Refresh (Lambda)

Each time a user clicks **Refresh Screen**, the QSignups Lambda (after Slack’s immediate `ack`) regenerates both week images with **Pillow**, uploads them to S3, and updates the DB URLs. Requires:

- **`IMAGE_S3_BUCKET`** on the function (SAM parameter `ImageBucketName`, set from `IMAGE_S3_BUCKET` in `.env.deploy.*` via `deploy.sh` / GitHub Actions `vars.IMAGE_S3_BUCKET`).
- IAM **`s3:PutObject`** on `arn:aws:s3:::bucket/weinkes/*` (included in `qsignups/template.yaml`).

### Standalone job (optional)

[`weinkes/create_weinkes.py`](weinkes/create_weinkes.py) is an alternate generator using pandas + **dataframe-image** (Chrome). Use on a schedule from a machine or runner if you want periodic refreshes without user clicks. Needs AWS credentials (`s3:PutObject`), DB env vars (`DATABASE_HOST`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_SCHEMA`, `DATABASE_PORT`, `DATABASE_TLS_ENABLED`, `IMAGE_S3_BUCKET`), and `pip install -r weinkes/requirements.txt`.

## Contributing

Open issues and PRs in your team’s tracker for this repo.
