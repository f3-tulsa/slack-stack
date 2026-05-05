# QSignups

Slack app for **Q signups** and schedule management. **Google Calendar** integration exists in the `google/` package but is **not currently wired** in the deployed app (handlers and slash commands are commented out in `app.py`).

Part of **[slack-stack](../README.md)**. Deploy with `qsignups/template.yaml`, `./deploy.sh`, or GitHub Actions. Shared deploy, env, and OAuth details: **[docs/DEPLOY.md](../docs/DEPLOY.md)**. **Permission tiers** (Admin, AOQ, Q, User) and schema notes: **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)**.

## Install (production)

Use the OAuth install URL from your deployed stack (CloudFormation output **`QSignupsApi`** = Lambda **Function URL**; append **`/slack/install`**) or from the Slack app’s **Install to Workspace** flow. If Slack shows a transient error, use “Try again” as usual.

## Features

- Home tab / slash commands for schedule and signups.
- **Weekly reminders:** Region admins can enable **Q reminders** (DMs to upcoming Qs) and **AO reminders** (AO-channel posts announcing each upcoming workout's Q/leader, with open slots called out) in **General Settings**. Admins also get a **Send Reminders Now** Home tab button as a manual fallback.
- **Manage Region Calendar:** Cancel / back on Add, Edit, and Delete flows for AOs and events returns to the **Manage Region Calendar** menu (not the main Home screen). The “Return to the Home Page” control on that menu still goes to Home.
- Google Calendar sync is **disabled** in production builds (no Google client packages in Lambda `requirements.txt`). Schema fields such as `google_auth_data` remain for a future re-enable; when restoring, add the Google deps back and uncomment the handlers in `app.py`.

## Schedule reconciliation

- **Rolling calendar:** Adding a recurring AO schedule creates `qsignups_master` rows from the chosen start date through **today + `SCHEDULE_CREATE_LENGTH_DAYS`** (SAM parameter `ScheduleCreateLengthDays`, default **365**). The same horizon is used when extending and when editing a recurring series.
- **`extend_all_schedules`:** Fills gaps in the master calendar and removes **orphan** future recurring rows (series that no longer exist in `qsignups_weekly`, e.g. after a bad cleanup). The weekly EventBridge automation now uses the **`qsignups.weekly-automation`** payload to run schedule extension and reminder delivery together. A deploy-time custom resource can still invoke **schedule extension only** with **`qsignups.extend-schedule`** when **`RUN_EXTEND_SCHEDULE=true`** (GitHub Environment variable or `.env.deploy.*`): then CI passes a new `ExtendScheduleDeployNonce` (e.g. git SHA). For fast routine deploys, leave it unset/false and `ExtendScheduleDeployNonce` stays **`no-extend`** so the custom resource does not re-run.
- **Editing a recurring schedule:** The Slack edit flow **deletes** future recurring `qsignups_master` rows for the **old** series (after today) and **recreates** rows through the rolling horizon with the new day/time/AO. **Today’s** row is left in place. Q signups on deleted future dates are cleared (those beats are no longer valid).
- **Deleting a recurring schedule:** Removes all future master rows for that series and the `qsignups_weekly` row (existing behavior).

## Edit and delete confirmations

- **Edit AO / single event / recurring event:** Submitting an edit form opens a **confirmation modal** listing changed fields (old → new). The database update runs only after **Confirm**. Dismiss the modal or use **Cancel** to abort.
- **Edit recurring event:** The modal includes a warning that **existing Q signups for this series under the current schedule will be removed** when the series definition changes (Slack then regenerates future master rows for the new day/time/AO).
- **Delete recurring event / Delete AO / delete single event:** Each delete action opens a **confirmation modal** with *You are about to delete:*, a short summary of the item, and a warning. The delete runs only after **Delete** on the modal; **Cancel** dismisses without changes.

Local tests (run from `qsignups/qsignups` with `PYTHONPATH=.`):

- Extend/edit schedule: [`testing/test_extend_schedule_local.py`](testing/test_extend_schedule_local.py)
- Weinke rendering / S3 / grid logic: [`testing/test_weinke_local.py`](testing/test_weinke_local.py)
- AO insert/edit (including PAXminer regional `aos.site_q_user_id`): [`testing/test_ao_handler_local.py`](testing/test_ao_handler_local.py)
- Confirmation modal helpers (edit + delete): [`testing/test_confirm_modals_local.py`](testing/test_confirm_modals_local.py)

## CI/CD

Workflow: [.github/workflows/deploy.yml](../.github/workflows/deploy.yml) (branches `test` / `prod`, AWS OIDC).

## Local development

1. Clone this monorepo and install [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
2. Create a Slack app from **[manifest.json](manifest.json)**. For local dev, replace `__HOSTNAME__` with your ngrok base URL, or use **`python generate.py --hostname https://...`** to emit `generate/manifest.json`. After **`./deploy.sh`** from the repo root, use **`qsignups/manifest-{test|prod}.json`** (gitignored) for the deployed **Function URL** base.

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
