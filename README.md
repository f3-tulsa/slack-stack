# F3 Slack Stack

Monorepo for four Slack applications used by F3 Regions, intended to run on AWS and share a MySQL/TiDB database:

| App | Role |
|-----|------|
| **PAXminer** | Attendance and Monthly Charts |
| **Weaselbot** | Achievements and Kotter Reports |
| **slackblast** | Preblasts and Backblasts |
| **qsignups** | Region Schedule |

Each app has its own SAM template under its directory; deploy order is flexible (no hard dependency between stacks at deploy time).

## Prerequisites

- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python **3.12** (matches Lambda runtimes) and **Python 3** on your PATH (used by `deploy.sh` for manifest substitution)
- **Docker** (required to build **PAXminer** and **Weaselbot** container images when deploying `paxminer`, `weaselbot`, or `all`)
- AWS account with permissions for Lambda, API Gateway, CloudFormation, IAM, S3, ECR, EventBridge
- [GitHub CLI `gh`](https://cli.github.com/) (optional; required only for `./deploy.sh --setup-github`)

## Environment variables

All deploy configuration is driven by environment variables. Copy [`.env.deploy.example`](.env.deploy.example) to **`.env.deploy.test`** or **`.env.deploy.prod`** and fill in the values. `deploy.sh` sources **`.env.deploy.<env>`** when you pass `--env test` or `--env prod`.

### Required (all stacks)

| Variable | Description |
|----------|-------------|
| `AWS_REGION` | AWS region, e.g. `us-east-2` |
| *(stage)* | **Not in this file.** `./deploy.sh --env test` or `--env prod` sets `STAGE` for SAM, stack names, and manifests (must match `StagesMap` in slackblast/qsignups). |
| `DATABASE_HOST` | MySQL / TiDB hostname |
| `DATABASE_PORT` | Database port (default `4000` for TiDB, `3306` for MySQL) |
| `DATABASE_USER` | Database user |
| `DATABASE_PASSWORD` | Database password |
| `DATABASE_TLS_ENABLED` | `true` for TiDB Cloud and other TLS-required hosts; `false` for local MySQL or RDS without TLS |
| `DB_ENCRYPTION_KEY` | **Required.** Any random string (min **16** characters) used as a passphrase for DB field encryption (see **Database encryption**). Must be the **same value** in `.env.deploy.*` and `migration/.env.migration.*` for a given stage. |
| `PAXMINER_SCHEMA` | **Bare** base name for PAXminer (e.g. `paxminer`). Deploy appends `_${STAGE}` → `paxminer_test` / `paxminer_prod` |
| `WEASELBOT_SCHEMA` | **Bare** base name for Weaselbot (e.g. `weaselbot`). Same auto-suffix |
| `SLACKBLAST_SCHEMA` | **Bare** base name for slackblast (e.g. `slackblast`). Same auto-suffix |
| `QSIGNUPS_SCHEMA` | **Bare** base name for qsignups (e.g. `qsignups`). Same auto-suffix |
| `IMAGE_S3_BUCKET` | Globally unique S3 bucket name for slackblast backblast images and **qsignups weinke** calendar PNGs under `weinkes/` (the slackblast stack creates this bucket) |
| `F3_REGION_NAME` | F3 region key stored in `paxminer_<stage>.regions.region` (e.g. `f3ttown`); regional DB schema is `{F3_REGION_NAME}_{STAGE}` |
| `PM_SLACK_TOKEN` | PAXminer Slack **bot** token for that region; SAM passes to Lambdas, which **encrypt** and **upsert** into `paxminer_<stage>.regions.slack_token` on cold start |
| `WB_SLACK_TOKEN` | Weaselbot Slack **bot** token (separate app); encrypted and upserted into `weaselbot_<stage>.regions` on cold start |
| `F3_REGION_SLACK_TEAM_ID` | Slack workspace Team ID (e.g. `T01234567`); passed to Lambdas (e.g. `weaselbot.regions.team_id` when inserting a new row) |

### Bootstrap (optional; for `--bootstrap`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOTSTRAP_STACK_NAME` | `slack-stack-bootstrap` | CloudFormation stack name for OIDC + SAM artifact bucket |
| `CREATE_OIDC_PROVIDER` | `true` | Set `false` if `token.actions.githubusercontent.com` OIDC already exists in the account |
| `DEPLOY_BUCKET_PREFIX` | `slack-stack-deploy` | Prefix for the SAM artifact bucket: `{prefix}-{account}-{region}` |

### Optional

| Variable | Description |
|----------|-------------|
| `AWS_ROLE_ARN` | OIDC deploy role ARN for `--setup-github` if not reading from the bootstrap stack outputs |
| `DEPLOYMENT_S3_BUCKET` | SAM deploy artifact bucket override (bootstrap output `DeploymentBucketName`). If unset, `deploy.sh` reads `DeploymentBucketName` from the bootstrap stack (same as GitHub Actions). Falls back to `--resolve-s3` only as a last resort — the OIDC deploy role may lack `s3:PutObject` on SAM’s default bucket. |

### PAXminer / Weaselbot and Slack

**PAXminer** and **Weaselbot** use **`PM_SLACK_TOKEN`** and **`WB_SLACK_TOKEN`** in `.env.deploy.*` (separate Slack apps). Deploy passes them to SAM; on **Lambda cold start** each stack encrypts with **`DB_ENCRYPTION_KEY`** and **upserts** into **`paxminer_<stage>.regions`** and **`weaselbot_<stage>.regions`**. At runtime they still **read** tokens from the DB (decrypted). **`F3_REGION_NAME`** and **`F3_REGION_SLACK_TEAM_ID`** identify the region row and workspace.

**slackblast** (`SB_*`) and **qsignups** (`QS_*`) use their own Slack (and Google) env vars as below.

### Required (slackblast)

| Variable | Description |
|----------|-------------|
| `SB_SLACK_TOKEN` | Slack Bot token |
| `SB_SLACK_SIGNING_SECRET` | Slack signing secret |
| `SB_SLACK_CLIENT_ID` | Slack OAuth **Client ID** (App credentials on [api.slack.com](https://api.slack.com/apps); must match the app that owns `SB_SLACK_TOKEN`) |
| `SB_SLACK_CLIENT_SECRET` | Slack OAuth client secret |

### Optional (slackblast)

| Variable | Description |
|----------|-------------|
| `SB_STRAVA_CLIENT_ID` | Strava API client ID (omit to disable Strava in slackblast; SAM uses template defaults) |
| `SB_STRAVA_CLIENT_SECRET` | Strava API client secret |
| `SB_CREATE_OAUTH_TABLES` | Set to `true` for one deploy to run OAuth `create_tables()` DDL on cold start; omit or `false` normally (see **Slack OAuth (database)**) |

### Required (qsignups)

| Variable | Description |
|----------|-------------|
| `QS_SLACK_TOKEN` | Slack Bot token |
| `QS_SLACK_SIGNING_SECRET` | Slack signing secret |
| `QS_SLACK_CLIENT_ID` | Slack OAuth **Client ID** for the qsignups Slack app (must match `QS_SLACK_TOKEN`) |
| `QS_SLACK_CLIENT_SECRET` | Slack OAuth client secret |

### Optional (qsignups)

| Variable | Description |
|----------|-------------|
| `QS_GOOGLE_CLIENT_ID` | Google Calendar API client ID (optional; Calendar integration is **not** active in the deployed QSignups app until code and `requirements.txt` Google deps are restored — see [qsignups/README.md](qsignups/README.md)) |
| `QS_GOOGLE_CLIENT_SECRET` | Google Calendar API client secret (same as above) |
| `QS_CREATE_OAUTH_TABLES` | Set to `true` for one deploy to run OAuth `create_tables()` DDL on cold start; omit or `false` normally (see **Slack OAuth (database)**) |

## Slack OAuth (database)

**slackblast** and **qsignups** store Slack app install data in the **same MySQL/TiDB schema** as the rest of each app (the suffixed schema: e.g. `slackblast_test` / `qsignups_prod`), not in S3. On Lambda cold start, Bolt’s `create_tables()` runs **only** when the Lambda env var **`CREATE_OAUTH_TABLES`** is `"true"` (SAM parameter `CreateOauthTables`, default `"false"`). That creates (if missing) three tables: `slack_bots`, `slack_installations`, `slack_oauth_states`. For **first deploy** or after a migration, set **`SB_CREATE_OAUTH_TABLES=true`** and/or **`QS_CREATE_OAUTH_TABLES=true`** in `.env.deploy.*` (or GitHub environment variables of the same names), deploy once, then set back to `false` or remove. Normal deploys skip DDL to reduce cold-start latency. Ensure the DB user has `CREATE TABLE` on that schema when you opt in (typical for app-owned schemas).

**`ENV_SLACK_CLIENT_ID` in Lambda** comes from `SB_SLACK_CLIENT_ID` / `QS_SLACK_CLIENT_ID` in your deploy env (SAM parameter `SlackClientId`). It must match the Slack app whose install row you store; otherwise Bolt cannot resolve the bot token from `slack_installations`.

### Populating `slack_installations` after deploy

Bolt resolves the workspace bot token from the **`slack_installations`** table (keyed by `client_id` + `team_id`), not from the placeholder `SLACK_BOT_TOKEN` parameter alone.

1. Deploy the stack and note the API base URL from CloudFormation (same base as **`SlackblastApi`** / **`QSignupsApi`**, e.g. `https://xxxx.execute-api.us-east-1.amazonaws.com/Prod`).
2. In each Slack app’s settings, add an **OAuth Redirect URL**: `{API_BASE}/slack/oauth_redirect` (Bolt’s OAuth callback path; must match API Gateway).
3. Open **`{API_BASE}/slack/install`** in a browser while signed into Slack and complete the install for your workspace. That writes **`slack_installations`** (and related rows) in `slackblast_<stage>` / `qsignups_<stage>`.

If you skip this, slash commands and modals can fail (e.g. missing auth, `expired_trigger_id` on cold starts, or `lambda:InvokeFunction` errors until the lazy listener can run with a valid client).

### Weaselbot `regions` row

Scheduled Weaselbot Lambdas read **`slack_token`** from **`weaselbot_<stage>.regions`** for each PAXminer regional schema (see `weaselbot/weaselbot/pax_achievements.py`). For your workspace to receive achievement/Kotter Slack messages:

- Ensure **`WB_SLACK_TOKEN`**, **`F3_REGION_NAME`**, **`STAGE`**, **`WEASELBOT_SCHEMA`**, and **`F3_REGION_SLACK_TEAM_ID`** are set on the Weaselbot Lambda (via deploy). On cold start, [`weaselbot/handlers.py`](weaselbot/handlers.py) can bootstrap an encrypted token into **`weaselbot_<stage>.regions`** when all of those are present.
- The row’s **`paxminer_schema`** must match the regional schema name (e.g. `f3ttown_test` for `F3_REGION_NAME=f3ttown` and `STAGE=test`).
- Set **`achievement_channel`** (and other Weaselbot fields) via slackblast’s Weaselbot config UI or direct SQL if achievements should post to a channel.

### Manual Lambda invocation (PAXminer / Weaselbot)

**PAXminer** and **Weaselbot** upsert encrypted bot tokens into the DB at **cold start** (see `common/token_bootstrap.py`). EventBridge schedules may run only daily, so after deploy the first cold start might not happen for hours.

The **GitHub Actions** deploy workflow runs a **smoke-test** step that synchronously invokes:

- `paxminer-<stage>-paxminer-sync`
- `weaselbot-<stage>-weaselbot-achievements`

so token bootstrap runs immediately after each deploy.

To **manually trigger** the same Lambdas (replace `test` with your stage: `test` or `prod`; set `AWS_REGION` as needed):

```bash
export AWS_REGION=us-east-1   # or your stack region

aws lambda invoke \
  --function-name paxminer-test-paxminer-sync \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' \
  --log-type Tail \
  /tmp/pm-sync.json && cat /tmp/pm-sync.json

aws lambda invoke \
  --function-name weaselbot-test-weaselbot-achievements \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' \
  --log-type Tail \
  /tmp/wb-ach.json && cat /tmp/wb-ach.json
```

Optional: run the other scheduled functions (charts / Kotter):

```bash
aws lambda invoke --function-name paxminer-test-paxminer-charts \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/pm-charts.json && cat /tmp/pm-charts.json

aws lambda invoke --function-name weaselbot-test-weaselbot-kotter \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/wb-kotter.json && cat /tmp/wb-kotter.json
```

With `--log-type Tail`, the CLI prints metadata to **stdout** (including base64 `LogResult`); decode with `jq -r '.LogResult' | base64 -d` if you need the tail of CloudWatch logs inline.

### Manual Lambda invocation (qsignups)

The **qsignups** stack runs `extend_all_schedules` after each deploy (CloudFormation custom resource) and on a **7-day** EventBridge schedule. To trigger a full calendar reconciliation manually (replace `test` with your stage):

```bash
export AWS_REGION=us-east-1   # or your stack region

aws lambda invoke \
  --function-name qsignups-test-QSignupsFunction-XXXXX \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source": "qsignups.extend-schedule"}' \
  --log-type Tail \
  /tmp/qs-extend.json && cat /tmp/qs-extend.json
```

Use the physical function name from CloudFormation (e.g. **Stack resources** → `QSignupsFunction`) or:

```bash
aws cloudformation describe-stack-resources \
  --stack-name "qsignups-${STAGE}" \
  --query "StackResources[?LogicalResourceId=='QSignupsFunction'].PhysicalResourceId" \
  --output text
```

### Lambda lazy listeners and `lambda:InvokeFunction`

**slackblast** and **qsignups** use Bolt’s **`process_before_response`** pattern on Lambda: the function must be allowed to **invoke itself** so the “lazy” handler runs after Slack gets an immediate `ack`. The SAM templates grant **`lambda:InvokeFunction`** on functions in the same stack. If this permission were missing, you would see **`AccessDeniedException`** on self-invoke and Slack would report that the app did not respond.

## Local development

**Packaging on AWS:** **PAXminer** and **Weaselbot** deploy as **container images** (Docker builds pushed to ECR). **slackblast** and **qsignups** use SAM **zip** packaging (CI uses `--use-container` on `sam build` for Linux-compatible native wheels).

Use a **virtual environment per app** (dependencies differ):

```bash
cd PAXminer && python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-lambda.txt
# repeat for weaselbot (requirements-lambda.txt), slackblast (slackblast/slackblast/requirements.txt), qsignups (qsignups/qsignups/requirements.txt) as needed
```

**slackblast** runtime deps are maintained with **Poetry** under [`slackblast/slackblast/`](slackblast/slackblast/) (`pyproject.toml`, `poetry.lock`). The committed [`requirements.txt`](slackblast/slackblast/requirements.txt) is generated for SAM (`poetry export`). Use **Python 3.12** with Poetry locally (`poetry env use python3.12`) so builds match Lambda. After editing `pyproject.toml`, run `poetry update` then `poetry export -f requirements.txt --without-hashes -o requirements.txt` (CI also auto-syncs via `requirements-sync` if the files drift).

**Tests (mirrors `ci.yml`):** use a separate venv per app to avoid conflicting pins (e.g. `pymysql`). From the repo root, with `DB_ENCRYPTION_KEY` set to any string ≥16 chars for the qsignups handler import test:

```bash
python3.12 -m venv .venv-wb && . .venv-wb/bin/activate && pip install pytest -r weaselbot/requirements-lambda.txt
(cd weaselbot && pytest -q tests/)

python3.12 -m venv .venv-sb && . .venv-sb/bin/activate && pip install pytest -r slackblast/slackblast/requirements.txt
pytest -q slackblast/test/

python3.12 -m venv .venv-qs && . .venv-qs/bin/activate && pip install pytest boto3 -r qsignups/qsignups/requirements.txt
DB_ENCRYPTION_KEY='your-test-key-at-least-16' pytest -q qsignups/testing/
```

`PAXminer/tests/test_BD_Comparer.py` requires `config/credentials_test.ini` and a live database; it is **skipped** when `CI=true` (e.g. in GitHub Actions). Run it only locally with a configured test DB.

## Database migration (existing host → new host)

Use this when migrating data from an existing MySQL/RDS instance to a new TiDB or MySQL host.

### What `migrate_data.py` does

- **Target bootstrap (no source read):** creates `paxminer_{STAGE}`, `slackblast_{STAGE}`, `weaselbot_{STAGE}` with core admin tables and seeds `paxminer.regions` rows for `f3ttown_{STAGE}` / `f3scissortail_{STAGE}` with **empty** `slack_token` (first deploy/Lambda cold start fills encrypted tokens from **`PM_SLACK_TOKEN`** / **`WB_SLACK_TOKEN`**). Optionally set **`MIGRATION_SEED_TEAM_F3TTOWN`** / **`MIGRATION_SEED_TEAM_F3SCISSORTAIL`** to seed matching rows in `slackblast` / `weaselbot` `regions` (`team_id` + `paxminer_schema`).
- **Source copy:** `f3ttown`, `f3scissortail`, and `f3stcharles` → regional schemas on the target; **`f3stcharles`** only copies base tables named `qsignups_*` (regional PAXminer objects in that schema are skipped). Set **`QSIGNUPS_TEAM_IDS`** in `migration/.env.migration.<env>` to comma-separated Slack **source** team IDs so only those rows are copied from the shared national `f3stcharles` qsignups tables (avoids importing other regions’ tokens). Use the team ID as it appears in the source DB (often prod), not the test workspace ID.
- **Qsignups views:** after copy, recreates **`vw_weekly_events`**, **`vw_aos_sort`**, and **`vw_master_events`** on `{QSIGNUPS_SCHEMA}_{STAGE}` (same definitions as `qsignups/db/views/*.sql`).
- **Encryption prep:** widens token columns to `VARCHAR(512)` and `qsignups_regions.google_auth_data` to `LONGTEXT` where needed, verifies each widen, and logs results under **`column_widens`** in the JSON report. If **`DB_ENCRYPTION_KEY`** is set (min 16 characters, not a placeholder), the script encrypts secrets in place; otherwise this step is skipped (Lambdas still require the key at runtime after deploy).
- **Images (optional):** if **`IMAGE_S3_BUCKET`** is set, copies backblast images and rewrites URLs at the end of the same run; otherwise run **`migrate_images.py --env test|prod`** after deploy creates the bucket.

### Setup

1. Copy `migration/.env.migration.example` to `migration/.env.migration.test` and/or `migration/.env.migration.prod` and fill in values. Pass **`--env test`** or **`--env prod`** when running the scripts (this selects the file and the stage suffix for schemas). Schema base names should match your deploy `.env.deploy.*` file. If you still have a legacy `migration/.env.migration`, rename it to `.env.migration.test` or `.env.migration.prod` to match its stage.
2. Install deps: `pip install -r migration/requirements.txt` (use a venv).

### Recommended order

1. **`python migration/migrate_data.py --env test`** (or **`--env prod`**) — bootstrap admin schemas, copy data, create qsignups views, widen columns, optional in-run field encryption (if `DB_ENCRYPTION_KEY` is set and valid length), optional in-run S3 image migration.
2. **(Test only, optional)** If your test Slack workspace uses different channel / team IDs than the source data, run **`python migration/remap_qsignups.py --env test --csv path/to/mapping.csv`** after step 1. The CSV maps prod `ao_channel_id` / `team_id` to test values; **`--env prod`** exits without changes.
3. **Deploy** (`./deploy.sh --env test|prod`) if you have not already — creates the image S3 bucket (slackblast stack). Use `.env.deploy.test` / `.env.deploy.prod` (see **Deploy (local)** below).
4. **`python migration/migrate_images.py --env test`** (or **`--env prod`**) — if the bucket did not exist during step 1, run this after deploy with **`IMAGE_S3_BUCKET`** set in the matching `.env.migration.<env>` to copy images and rewrite `beatdowns.json` URLs.

### Artifacts

Reports/checkpoints are written under `migration/` (gitignored). After `migrate_data.py`, a **human-readable receipt** is saved under `migration/receipts/` (same content is printed to the console).

## Deploy (local)

1. Copy `.env.deploy.example` to `.env.deploy.test` (or `.env.deploy.prod`) and fill in all values.
2. **First-time AWS / GitHub Actions (optional):** from the repo root, with `origin` pointing at your GitHub repo:
   - `./deploy.sh --env test --bootstrap` — creates/updates [`infra/template.bootstrap.yaml`](infra/template.bootstrap.yaml): GitHub OIDC provider (if needed), SAM artifact bucket, and an IAM role trusted for `repo:<owner>/<repo>:*`. When combined with a full deploy in the same command, SAM uses the bootstrap bucket for packaged artifacts.
   - After a successful deploy: `./deploy.sh --env test --setup-github` — requires `gh auth login`; creates the GitHub **environment** named after **`--env`** (`test` or `prod`) and sets the same variables/secrets documented under **GitHub Environments** below.
   - You can combine flags, e.g. `./deploy.sh --env test --bootstrap --setup-github`.
3. Deploy:

```bash
./deploy.sh --env test                    # all four stacks
./deploy.sh --env test --stack paxminer   # single stack
./deploy.sh --env test --build-only       # build only (no deploy)
./deploy.sh --env prod --confirm          # prompt for SAM changeset confirmation
./deploy.sh --env test --bootstrap        # bootstrap stack, then deploy all stacks
./deploy.sh --env test --setup-github     # after deploy: push env to GitHub (needs gh)
```

### PAXminer: zip to container image (one-time migration)

If an older **PAXminer** stack was deployed as **zip**-packaged Lambdas and you upgrade to **Docker** images, CloudFormation may be unable to replace functions that use a fixed `FunctionName`. Delete the stack once, then redeploy:

```bash
aws cloudformation delete-stack --stack-name paxminer-<stage>
aws cloudformation wait stack-delete-complete --stack-name paxminer-<stage>
```

Replace `<stage>` with `test` or `prod`. Then run `./deploy.sh` or push to CI. EventBridge schedules and log groups are recreated by the template.

4. **Post-deploy (first time only):** upload Strava assets to the image bucket:

```bash
aws s3 cp slackblast/assets/ s3://YOUR_IMAGE_BUCKET/ --recursive
```

5. After deploy, the script prints CloudFormation outputs, a **per-stack summary** (success / failure), and writes a **receipt** to `receipts/deploy-{STAGE}-{timestamp}.txt` (same output as the console; gitignored). It also generates **stage-specific Slack manifests** `manifest-{STAGE}.json` under each app directory for the stacks that deployed successfully — **slackblast** and **qsignups** manifests include the deployed API Gateway base URL (replace `__HOSTNAME__`). Use those JSON files when creating or updating Slack apps at [api.slack.com](https://api.slack.com/apps). Committed templates are the base `manifest.json` files in each app folder.

## Deploy (GitHub Actions)

### Workflows

| Workflow | When it runs |
|----------|----------------|
| **[`.github/workflows/ci.yml`](.github/workflows/ci.yml)** | Pull requests and pushes to **`main`**, **`test`**, and **`prod`**: **`requirements-sync`** (regenerates `slackblast/slackblast/requirements.txt` from `poetry.lock` if drifted; auto-commit on same-repo branches), SAM lint, Python tests, and **`pip-audit`** on every app `requirements*.txt`. No AWS credentials. |
| **[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)** | Pushes to **`test`** and **`prod`** only, plus manual *Run workflow*. **`main`** stays PR-only for merges. |

Manual deploy: *Actions → Deploy Slack Stack → Run workflow*. Choose **environment** (`test` / `prod`) and optional **stack** (`all` or a single app). On **push**, path-based detection is used (the stack input is ignored).

### Selective deploys (push only)

On **push** to `test` or `prod`, only apps whose paths changed are built and deployed (jobs run **in parallel**):

| Deploy job | Paths that trigger it (or always when “infra” changed) |
|------------|--------------------------------------------------------|
| PAXminer | `PAXminer/**` |
| Weaselbot | `weaselbot/**`, `common/**` |
| slackblast | `slackblast/**` |
| qsignups | `qsignups/**` |
| **All four** | `.github/workflows/**`, `infra/**` |

Bootstrap stack deployment is **not** automated in Actions; run `./deploy.sh --env <env> --bootstrap` locally when needed. CI still **lints** `infra/template.bootstrap.yaml` in the deploy **setup** job and in **`ci.yml`**.

### Concurrency

Overlapping pushes to the same branch (`test` or `prod`) **cancel** the older deploy run so two full deploys do not race the same stacks.

### Optional GitHub Environment variables

Same names as in `.env.deploy.*` for `deploy.sh`:

- **`SAM_NO_CACHE=true`** — full uncached `sam build` for each app job that runs.
- **`SKIP_SMOKE_TEST=true`** — skip post-deploy Lambda invokes. When a stack was **not** deployed in that run, its smoke invoke is skipped automatically.
- **`RUN_EXTEND_SCHEDULE=true`** — QSignups extend-schedule nonce (only affects **qsignups** when that job runs).
- **`ENABLE_XRAY=true`** — X-Ray on slackblast and qsignups (when those jobs run).

### After deploy

When the workflow finishes without deploy failures, the **post-deploy** job appends a **summary** to the job log and `$GITHUB_STEP_SUMMARY`, writes a receipt under `receipts/`, generates **stage-specific `manifest-{STAGE}.json`** files (from current CloudFormation outputs), and uploads receipts + manifests as a workflow **artifact**.

### Dependabot

[`.github/dependabot.yml`](.github/dependabot.yml) opens weekly PRs to update **GitHub Actions**, **Docker** base images (`PAXminer/`, `weaselbot/`), and **pip** requirements under each app directory (`PAXminer/`, `weaselbot/`, `slackblast/slackblast/`, `qsignups/`, `migration/`, etc.).

### GitHub Environments

Create environments **`test`** and **`prod`** in your repo settings (or run `./deploy.sh --env test --setup-github` / `--env prod --setup-github` after `gh auth login` to create them and set values from your `.env.deploy.*` file). Each needs:

**Secrets:**

| Secret | Used by |
|--------|---------|
| `AWS_ROLE_ARN` | All stacks (OIDC role ARN) |
| `DATABASE_HOST` | All |
| `DATABASE_PORT` | All |
| `DATABASE_USER` | All |
| `DATABASE_PASSWORD` | All |
| `DB_ENCRYPTION_KEY` | All (min 16 characters) |
| `PM_SLACK_TOKEN` | PAXminer |
| `WB_SLACK_TOKEN` | Weaselbot |
| `SB_SLACK_TOKEN` | slackblast |
| `SB_SLACK_SIGNING_SECRET` | slackblast |
| `SB_SLACK_CLIENT_SECRET` | slackblast |
| `SB_STRAVA_CLIENT_ID` | slackblast (optional — Strava) |
| `SB_STRAVA_CLIENT_SECRET` | slackblast (optional — Strava) |
| `QS_SLACK_TOKEN` | qsignups |
| `QS_SLACK_SIGNING_SECRET` | qsignups |
| `QS_SLACK_CLIENT_SECRET` | qsignups |
| `QS_GOOGLE_CLIENT_ID` | qsignups (optional — Google Calendar) |
| `QS_GOOGLE_CLIENT_SECRET` | qsignups (optional — Google Calendar) |

**Variables:**

| Variable | Example | Notes |
|----------|---------|-------|
| `STAGE` | `test` or `prod` | Must match SAM `StagesMap` keys |
| `AWS_REGION` | `us-east-2` | |
| `DATABASE_TLS_ENABLED` | `true` | Passed to all four stacks as `DatabaseTlsEnabled`; use `false` if your DB has no TLS |
| `PAXMINER_SCHEMA` | `paxminer` | Bare name; workflow appends `_${STAGE}` to match migrated DB |
| `WEASELBOT_SCHEMA` | `weaselbot` | Same |
| `SLACKBLAST_SCHEMA` | `slackblast` | Same |
| `QSIGNUPS_SCHEMA` | `qsignups` | Same |
| `IMAGE_S3_BUCKET` | `slack-stack-images-prod` | Globally unique |
| `F3_REGION_NAME` | `f3ttown` | Region key in `paxminer.regions`; regional schema `{F3_REGION_NAME}_${STAGE}` |
| `F3_REGION_SLACK_TEAM_ID` | `T01234567` | Slack workspace Team ID (available to any app that needs it; Weaselbot uses it for `weaselbot.regions`) |
| `SB_SLACK_CLIENT_ID` | `10773766677089.xxx` | Slack OAuth Client ID for slackblast (public app id; use a variable, not a secret) |
| `QS_SLACK_CLIENT_ID` | `10773766677089.xxx` | Slack OAuth Client ID for qsignups (public app id; use a variable, not a secret) |
| `SB_CREATE_OAUTH_TABLES` | *(omit)* | Optional; set to `true` for one deploy to create OAuth tables (see **Slack OAuth (database)**) |
| `QS_CREATE_OAUTH_TABLES` | *(omit)* | Optional; set to `true` for one deploy to create OAuth tables (see **Slack OAuth (database)**) |
| `SAM_NO_CACHE` | *(omit)* | Optional; `true` = `sam build --no-cached` (slower, clean rebuild) |
| `SKIP_SMOKE_TEST` | *(omit)* | Optional; `true` = skip post-deploy Lambda smoke test (PAXminer sync + Weaselbot achievements) |
| `RUN_EXTEND_SCHEDULE` | *(omit)* | Optional; `true` = run QSignups extend-schedule on deploy via new `ExtendScheduleDeployNonce` |
| `ENABLE_XRAY` | *(omit)* | Optional; `true` = enable AWS X-Ray on slackblast + qsignups |

### AWS OIDC (one-time setup)

**Automated:** Run `./deploy.sh --env test --bootstrap` (and/or `--env prod` with the matching `.env.deploy.prod`). That deploys the bootstrap stack in [`infra/template.bootstrap.yaml`](infra/template.bootstrap.yaml), which can create the GitHub OIDC identity provider, a SAM artifact bucket, and IAM role `slack-stack-github-deploy-<region>` trusted for `repo:<owner>/<repo>:*` (parsed from `git remote get-url origin`). Set **`CREATE_OIDC_PROVIDER=false`** in your env file if the OIDC provider already exists.

**Manual (fallback):**

1. In IAM, add an **OIDC identity provider** for `https://token.actions.githubusercontent.com` (audience `sts.amazonaws.com`).
2. Create a role trusted for `sts:AssumeRoleWithWebIdentity` with a condition limiting `sub` to this repository (e.g. `repo:OWNER/REPO:*` or stricter branch claims).
3. Attach policies sufficient for SAM: Lambda, API Gateway, CloudFormation, IAM (for generated roles), S3, ECR, EventBridge.
4. Set the role ARN as secret **`AWS_ROLE_ARN`** in both GitHub environments.

## Database encryption

Sensitive columns in the shared DB are encrypted using **`DB_ENCRYPTION_KEY`** as a **passphrase** (see `common/encryption.py`). The code stretches that string with PBKDF2 and then uses Fernet for the actual ciphertext — you do **not** paste a raw Fernet key; any cryptographically random string of **at least 16 characters** is valid. Avoid placeholders like `123`.

All four apps share one key per environment. The key is **required** for every deploy and at Lambda cold start.

**Migration and deploy must match:** use the **exact same** `DB_ENCRYPTION_KEY` in `migration/.env.migration.test` (or `.prod`) and in `.env.deploy.test` / `.env.deploy.prod` for that stage. If migration encrypts data with one passphrase and deploy uses another, decrypts will fail.

Ways to generate a strong random passphrase (pick one):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```bash
openssl rand -base64 32
```

## S3 buckets

| Bucket | Created by | Purpose |
|--------|-----------|---------|
| `IMAGE_S3_BUCKET` | slackblast SAM stack | Public-read bucket for backblast and Strava images |

After the first deploy of slackblast, upload the Strava assets:

```bash
aws s3 cp slackblast/assets/ s3://$IMAGE_S3_BUCKET/ --recursive
```

## App-specific docs

- [PAXminer/README.md](PAXminer/README.md)
- [weaselbot/README.md](weaselbot/README.md)
- [slackblast/README.md](slackblast/README.md)
- [qsignups/README.md](qsignups/README.md)

## License

**AGPL-3.0** — see [LICENSE](LICENSE).