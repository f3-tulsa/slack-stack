# slack-stack

Monorepo for four AWS Lambda / SAM applications that share a MySQL/TiDB database:

| App | Role |
|-----|------|
| **PAXminer** | Backblast scraping, attendance, monthly charts |
| **Weaselbot** | Achievements and Kotter reports |
| **slackblast** | Backblasts, preblasts, Strava, email |
| **qsignups** | Q signups, Google Calendar |

Each app has its own SAM template under its directory; deploy order is flexible (no hard dependency between stacks at deploy time).

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python **3.12** (matches Lambda runtimes)
- **Docker** (required to build **Weaselbot** container images)
- AWS account with permissions for Lambda, API Gateway, CloudFormation, IAM, S3, ECR, EventBridge

## Environment variables

All configuration is driven by environment variables. Copy `.env.example` to `.env.test` (or `.env.prod`) and fill in the values. The file is sourced by `deploy.sh`.

### Required (all stacks)

| Variable | Description |
|----------|-------------|
| `AWS_REGION` | AWS region, e.g. `us-east-2` |
| `STAGE` | `test` or `prod` — maps to the SAM `StagesMap` in slackblast/qsignups |
| `DATABASE_HOST` | MySQL / TiDB hostname |
| `DATABASE_PORT` | Database port (default `4000` for TiDB, `3306` for MySQL) |
| `DATABASE_USER` | Database user |
| `DATABASE_PASSWORD` | Database password |
| `DB_ENCRYPTION_KEY` | Fernet key for encrypting sensitive DB columns (see **Database encryption** below) |
| `PAXMINER_SCHEMA` | **Bare** base name for PAXminer (e.g. `paxminer`). Deploy appends `_${STAGE}` → `paxminer_test` / `paxminer_prod` |
| `WEASELBOT_SCHEMA` | **Bare** base name for Weaselbot (e.g. `weaselbot`). Same auto-suffix |
| `SLACKBLAST_SCHEMA` | **Bare** base name for slackblast (e.g. `slackblast`). Same auto-suffix |
| `QSIGNUPS_SCHEMA` | **Bare** base name for qsignups (e.g. `qsignups`). Same auto-suffix |
| `IMAGE_S3_BUCKET` | Globally unique S3 bucket name for backblast images (the slackblast stack creates this bucket) |

### Required (slackblast)

| Variable | Description |
|----------|-------------|
| `SB_SLACK_TOKEN` | Slack Bot token |
| `SB_SLACK_SIGNING_SECRET` | Slack signing secret |
| `SB_SLACK_CLIENT_SECRET` | Slack OAuth client secret |
| `SB_STRAVA_CLIENT_ID` | Strava API client ID |
| `SB_STRAVA_CLIENT_SECRET` | Strava API client secret |

### Required (qsignups)

| Variable | Description |
|----------|-------------|
| `QS_SLACK_TOKEN` | Slack Bot token |
| `QS_SLACK_SIGNING_SECRET` | Slack signing secret |
| `QS_SLACK_CLIENT_SECRET` | Slack OAuth client secret |
| `QS_GOOGLE_CLIENT_ID` | Google Calendar API client ID |
| `QS_GOOGLE_CLIENT_SECRET` | Google Calendar API client secret |

## Slack OAuth (database)

**slackblast** and **qsignups** store Slack app install data in the **same MySQL/TiDB schema** as the rest of each app (the suffixed schema: e.g. `slackblast_test` / `qsignups_prod`), not in S3. On first Lambda cold start after deploy, the code creates (if missing) three tables: `slack_bots`, `slack_installations`, `slack_oauth_states`. Ensure the DB user has `CREATE TABLE` on that schema (typical for app-owned schemas).

## Local development

Use a **virtual environment per app** (dependencies differ):

```bash
cd PAXminer && python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-lambda.txt
# repeat for weaselbot, slackblast, qsignups as needed
```

## Database migration (existing host → new host)

Use this when migrating data from an existing MySQL/RDS instance to a new TiDB or MySQL host.

1. Copy `migration/.env.migration.example` to `migration/.env.migration` and fill in values. Set `STAGE` to `test` or `prod`. Schema base names (`PAXMINER_SCHEMA`, etc.) should match your deploy `.env`.
2. Install deps: `pip install -r migration/requirements.txt` (use a venv).
3. Run: `python migration/migrate_data.py`.
4. Optional post-steps (set in `.env.migration`):
   - **`DB_ENCRYPTION_KEY`** — encrypt tokens after copy.
   - **`IMAGE_S3_BUCKET`** — copy backblast images from their stored public URLs into your S3 bucket and rewrite the URLs in `beatdowns.json`.
5. Reports/checkpoints are written under `migration/` (gitignored). After a successful run, a **human-readable receipt** is also saved under `migration/receipts/` (same content is printed to the console).

## Deploy (local)

1. Copy `.env.example` to `.env.test` (or `.env.prod`) and fill in all values.
2. Deploy:

```bash
./deploy.sh --env test                    # all four stacks
./deploy.sh --env test --stack paxminer   # single stack
./deploy.sh --env test --build-only       # build only (no deploy)
./deploy.sh --env prod --confirm          # prompt for SAM changeset confirmation
```

3. **Post-deploy (first time only):** upload Strava assets to the image bucket:

```bash
aws s3 cp slackblast/assets/ s3://YOUR_IMAGE_BUCKET/ --recursive
```

4. After deploy, the script prints CloudFormation outputs, a **per-stack summary** (success / failure), and writes a **receipt** to `receipts/deploy-{STAGE}-{timestamp}.txt` (same output as the console; gitignored). It also generates **stage-specific Slack manifests** `manifest-{STAGE}.json` under each app directory for the stacks that deployed successfully — **slackblast** and **qsignups** manifests include the deployed API Gateway base URL (replace `__HOSTNAME__`). Use those JSON files when creating or updating Slack apps at [api.slack.com](https://api.slack.com/apps). Committed templates are the base `manifest.json` files in each app folder.

## Deploy (GitHub Actions)

Pushes to branches **`test`** and **`prod`** run `.github/workflows/deploy.yml`. **`main`** is for PRs only. Manual runs are also supported via *Actions → Deploy Slack Stack → Run workflow*.

After all stacks deploy successfully, the workflow appends a **summary** to the job log and `$GITHUB_STEP_SUMMARY`, writes a receipt under `receipts/`, generates the same **stage-specific `manifest-{STAGE}.json`** files, and uploads receipts + manifests as a workflow **artifact**.

### GitHub Environments

Create environments **`test`** and **`prod`** in your repo settings. Each needs:

**Secrets:**

| Secret | Used by |
|--------|---------|
| `AWS_ROLE_ARN` | All stacks (OIDC role ARN) |
| `DATABASE_HOST` | All |
| `DATABASE_PORT` | All |
| `DATABASE_USER` | All |
| `DATABASE_PASSWORD` | All |
| `DB_ENCRYPTION_KEY` | All |
| `SB_SLACK_TOKEN` | slackblast |
| `SB_SLACK_SIGNING_SECRET` | slackblast |
| `SB_SLACK_CLIENT_SECRET` | slackblast |
| `SB_STRAVA_CLIENT_ID` | slackblast |
| `SB_STRAVA_CLIENT_SECRET` | slackblast |
| `QS_SLACK_TOKEN` | qsignups |
| `QS_SLACK_SIGNING_SECRET` | qsignups |
| `QS_SLACK_CLIENT_SECRET` | qsignups |
| `QS_GOOGLE_CLIENT_ID` | qsignups |
| `QS_GOOGLE_CLIENT_SECRET` | qsignups |

**Variables:**

| Variable | Example | Notes |
|----------|---------|-------|
| `STAGE` | `test` or `prod` | Must match SAM `StagesMap` keys |
| `AWS_REGION` | `us-east-2` | |
| `PAXMINER_SCHEMA` | `paxminer` | Bare name; workflow appends `_${STAGE}` to match migrated DB |
| `WEASELBOT_SCHEMA` | `weaselbot` | Same |
| `SLACKBLAST_SCHEMA` | `slackblast` | Same |
| `QSIGNUPS_SCHEMA` | `qsignups` | Same |
| `IMAGE_S3_BUCKET` | `slack-stack-images-prod` | Globally unique |

### AWS OIDC (one-time setup)

1. In IAM, add an **OIDC identity provider** for `https://token.actions.githubusercontent.com` (audience `sts.amazonaws.com`).
2. Create a role (e.g. `slack-stack-deploy`) trusted for `sts:AssumeRoleWithWebIdentity` with a condition limiting `sub` to this repo and branches `ref:refs/heads/test` and `ref:refs/heads/prod`.
3. Attach policies sufficient for SAM: Lambda, API Gateway, CloudFormation, IAM (for generated roles), S3, ECR, EventBridge.
4. Set the role ARN as secret **`AWS_ROLE_ARN`** in both GitHub environments.

## Database encryption

Sensitive columns in the shared DB use **Fernet** symmetric encryption derived from **`DB_ENCRYPTION_KEY`** (see `common/encryption.py`). All four apps use the same key. Generate a key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
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