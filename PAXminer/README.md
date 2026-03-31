# PAXminer

Part of the **[slack-stack](../README.md)** monorepo. Deploy with SAM (`PAXminer/template.yaml`), GitHub Actions, or `./deploy.sh` — see the root README for database credentials, `DB_ENCRYPTION_KEY`, and CI.

## What it does

PAXminer pulls workout (“backblast”) data from regional Slack workspaces, normalizes it, and stores it in a shared MySQL/TiDB database. It also generates charts and stats (per user, AO, and region) and can post them to Slack on a schedule.

Typical data captured per beatdown:

- AO, date, Q / Co-Q, attendance, FNGs, and related metadata

Each region usually has its own **schema** in the same database; registry rows in `paxminer.regions` point Lambdas at the right schema and Slack token. Deploy passes **`PM_SLACK_TOKEN`**, **`F3_REGION_NAME`**, and **`STAGE`** via SAM; the Lambda **encrypts** the token with **`DB_ENCRYPTION_KEY`** and **upserts** it into `paxminer.regions` on cold start.

## Slack app manifest

Use **[manifest.json](manifest.json)** (JSON) when creating the Slack app. PAXminer Lambdas are schedule-driven only (no HTTP API); there are no request URLs in the manifest. After `./deploy.sh`, a copy is written as **`manifest-{test|prod}.json`** for consistency (gitignored).

## Layout (high level)

| Area | Role |
|------|------|
| `handlers.py` | Lambda entrypoints (daily sync, monthly charts) |
| `backblast_scraping/` | Backblast mining from Slack history |
| `monthly_charts/` | Chart generation and posting |
| `database_management/` | User/channel sync helpers |
| `common/encryption.py` | Same Fernet-style helpers as repo root `common/encryption.py` (copied into the Docker image) |

Legacy **manual / cron** scripts under this tree may still read `slack_token` from the DB without decrypting; the **deployed Lambda path** uses `decrypt_field` and requires `DB_ENCRYPTION_KEY` (min 16 characters). Prefer running through SAM for production.

## License

See [LICENSE](LICENSE) in this directory (GNU GPL v3 where applicable).
