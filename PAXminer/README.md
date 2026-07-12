# PAXminer

Part of the **[slack-stack](../README.md)** monorepo. Deploy with SAM (`PAXminer/template.yaml`), GitHub Actions, or `./deploy.sh`. Database credentials, **`DB_ENCRYPTION_KEY`**, CI: **[docs/DEPLOY.md](../docs/DEPLOY.md)**; architecture: **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)**.

## What it does

PAXminer pulls workout (“backblast”) data from regional Slack workspaces, normalizes it, and stores it in a shared MySQL/TiDB database. It also generates charts and stats, runs **data-driven achievements** (grant, revoke, leaderboard, almost-there), and sends **Kotter** reports.

Each region has its own **schema** in the same database; registry rows in `paxminer.regions` point Lambdas at the right schema, channels, toggles, and encrypted Slack token. Deploy passes **`PM_SLACK_TOKEN`**, **`PM_SLACK_SIGNING_SECRET`**, **`PAXMINER_ACHIEVEMENTS_WEBHOOK_SECRET`**, **`F3_REGION_NAME`**, and **`STAGE`** via SAM; the Lambda **encrypts** the bot token with **`DB_ENCRYPTION_KEY`** and **upserts** it into `paxminer.regions` on cold start.

## Lambdas (four functions)

| Function | Trigger | Role |
|----------|---------|------|
| **sync** | Daily schedule | User/channel sync |
| **charts** | Monthly schedule | PAX/Q charts and region/AO leaderboards |
| **achievements** | Daily schedule + Function URL webhook from Slackblast | Grant/revoke awards; leaderboard smoke path |
| **kotter** | Monthly schedule + Function URL | Kotter reports; `/config-paxminer` and `/kotter-report` Slack interactivity |

Function URL outputs: **`KotterFunctionUrl`**, **`AchievementsFunctionUrl`** (passed to slackblast as `PAXMINER_ACHIEVEMENTS_URL`).

## Slack app manifest

Use **[manifest.json](manifest.json)** when creating the PAXMiner Slack app. After deploy, **`manifest-{test|prod}.json`** substitutes **`KotterFunctionUrl`** for `__HOSTNAME__` (slash commands and interactivity). Requires **`reactions:write`** for `:fire:` on achievement posts.

Admin commands: **`/config-paxminer`**, **`/kotter-report`**.

## Layout (high level)

| Area | Role |
|------|------|
| `handlers.py` | Lambda entrypoints (sync, charts, achievements, kotter) |
| `config_paxminer.py` | `/config-paxminer` modals — settings + achievements_list CRUD |
| `achievements/` | Rules engine, runner, leaderboard |
| `kotter/` | Kotter report generation |
| `backblast_scraping/` | Backblast mining from Slack history |
| `monthly_charts/` | Chart generation and posting |
| `database_management/` | User/channel sync helpers |
| `common/encryption.py` | Same Fernet-style helpers as repo root `common/encryption.py` (copied into the Docker image) |

## Tests

From `PAXminer/` with deps installed:

```bash
pytest -q tests/
```

## License

See [LICENSE](LICENSE) in this directory (GNU GPL v3 where applicable).
