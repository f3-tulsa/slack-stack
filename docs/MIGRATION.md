# Database migration

Use this when copying data from an existing MySQL/RDS (or similar) host to a new TiDB or MySQL host for the same logical environments (`test` / `prod`).

## Overview

The main script is **`migration/migrate_data.py`**. It can:

- **Bootstrap** target admin schemas `paxminer_{STAGE}`, `slackblast_{STAGE}`, `weaselbot_{STAGE}` and seed registry rows (tokens filled later by Lambdas).
- **Copy** regional schemas (e.g. `f3ttown`, `f3scissortail`) and selective **qsignups** data from a shared source schema (`f3stcharles` / `qsignups_*` tables) when **`QSIGNUPS_TEAM_IDS`** is set.
- **Recreate** QSignups views (`vw_weekly_events`, `vw_aos_sort`, `vw_master_events`).
- **Widen** encrypted token columns and optionally **encrypt** fields in place when **`DB_ENCRYPTION_KEY`** is set (must match deploy).
- Optionally copy **S3 images** when **`IMAGE_S3_BUCKET`** is set, or run **`migration/migrate_images.py`** after the bucket exists.

## Setup

1. Copy **`migration/.env.migration.example`** to **`migration/.env.migration.test`** and/or **`migration/.env.migration.prod`**.
2. Fill **source** and **target** connection settings, schema base names (`PAXMINER_SCHEMA`, etc.), throttling, and optional `QSIGNUPS_TEAM_IDS`, `DB_ENCRYPTION_KEY`, `IMAGE_S3_BUCKET`.
3. Install deps: `pip install -r migration/requirements.txt` (use a venv).

## Recommended order

1. `python migration/migrate_data.py --env test` (or `--env prod`).
2. **(Test only, optional)** `python migration/remap_qsignups.py --env test --csv path/to/mapping.csv` if channel/team IDs differ from source.
3. **Deploy** — `./deploy.sh --env test|prod` so the image bucket exists (slackblast stack).
4. **`python migration/migrate_images.py --env test|prod`** if the bucket was not available during step 1.

## Artifacts

Reports and checkpoints are written under `migration/` (often gitignored). Human-readable receipts also go under **`migration/receipts/`**.

## After migration

Continue with **[DEPLOY.md](DEPLOY.md)** for Slack OAuth install URLs, `CREATE_OAUTH_TABLES` one-shot flags, and smoke tests.

## Environment reference

See inline comments in [`migration/.env.migration.example`](../migration/.env.migration.example) for all variables (`SOURCE_*`, `TARGET_*`, `BATCH_SIZE`, `QSIGNUPS_TEAM_IDS`, `MIGRATION_SEED_*`, etc.).
