# F3 Slack Stack

Monorepo for four Slack applications used by F3 regions. They run on **AWS** (Lambda + SAM) and share a **MySQL/TiDB** database.

| App | Role |
|-----|------|
| **[PAXminer](PAXminer/README.md)** | Backblast mining, attendance, monthly charts |
| **[Weaselbot](weaselbot/README.md)** | Achievements and Kotter reports |
| **[slackblast](slackblast/README.md)** | Preblasts, backblasts, Strava, email, welcome flows |
| **[qsignups](qsignups/README.md)** | Region Q schedule and signups |

Each app has its own `template.yaml`; deploy order between stacks is flexible.

## Quick start

1. **Prerequisites:** [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), **Python 3.12**, **Docker** (for PAXminer & Weaselbot images), AWS account access for Lambda, API Gateway, CloudFormation, IAM, S3, ECR, EventBridge.
2. **Configure deploy:** Copy [`.env.deploy.example`](.env.deploy.example) to `.env.deploy.test` or `.env.deploy.prod` and fill in values.
3. **Deploy:** See **[docs/DEPLOY.md](docs/DEPLOY.md)** for bootstrap, `./deploy.sh`, GitHub Actions, OAuth, and secrets.
4. **Migrate existing data:** **[docs/MIGRATION.md](docs/MIGRATION.md)** when moving from another MySQL host.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Env vars, OAuth tables, local & CI deploy, Lambda smoke tests, encryption, S3 |
| [docs/MIGRATION.md](docs/MIGRATION.md) | `migrate_data.py`, remap, images, artifacts |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Repo layout, schemas, DB map, QSignups permissions |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | End-user help for each app |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Venvs, tests, Poetry / slackblast |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common failures and fixes |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branches, PRs, style |

## App READMEs

Per-app details: [PAXminer/README.md](PAXminer/README.md), [weaselbot/README.md](weaselbot/README.md), [slackblast/README.md](slackblast/README.md), [qsignups/README.md](qsignups/README.md).

## License

**AGPL-3.0** — see [LICENSE](LICENSE).
