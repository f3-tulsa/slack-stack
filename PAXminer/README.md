# PAXminer

Part of the **[slack-stack](../README.md)** monorepo. Deploy with SAM (`PAXminer/template.yaml`), GitHub Actions, or `./deploy.sh`. Database credentials, **`DB_ENCRYPTION_KEY`**, CI: **[docs/DEPLOY.md](../docs/DEPLOY.md)**; architecture: **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)**.

## What it does

PAXminer pulls workout (“backblast”) data from regional Slack workspaces, normalizes it, and stores it in a shared MySQL/TiDB database. It also generates charts and stats, runs **data-driven achievements** (grant, revoke, leaderboard, almost-there), and sends **Kotter** reports.

Each region has its own **schema** in the same database; registry rows in `paxminer.regions` point Lambdas at the right schema, channels, toggles, and encrypted Slack token. Deploy passes **`PM_SLACK_TOKEN`**, **`PM_SLACK_SIGNING_SECRET`**, **`PM_ACHIEVEMENTS_WEBHOOK_SECRET`**, **`F3_REGION_NAME`**, and **`STAGE`** via SAM; the Lambda **encrypts** the bot token with **`DB_ENCRYPTION_KEY`** and **upserts** it into `paxminer.regions` on cold start.

## What PAXMiner posts

Everything PAXMiner can send, with the `paxminer.regions` flag that enables it and the actual destination.

### Text messages (channels + DMs)

| Message | When | Enabled by (flag) | Destination(s) | Config channel |
|---------|------|-------------------|----------------|----------------|
| Achievement **granted** (+ emoji reaction) | Daily achievements run | `send_achievements` | Achievement channel **and** a DM to the PAX (+ the AO channel if `post_to_ao`) | `achievement_channel` |
| Achievement **revoked** | Daily achievements run | `send_achievements` | Achievement channel (+ AO channel if `post_to_ao`) | `achievement_channel` |
| **Achievement leaderboard (YTD)** | Monthly | `send_achievement_leaderboard` | Achievement channel | `achievement_channel` (shared) |
| **"Almost there"** progress list | Monthly (with leaderboard) | `send_achievement_leaderboard` | Achievement channel | `achievement_channel` (shared) |
| **Kotter / AOQ report** | Monthly + manual button | `send_aoq_reports` | Kotter channel | `kotter_channel` |

Source: `achievements/runner.py`, `achievements/leaderboard.py`, `kotter/kotter_report.py`.

### Chart images (`files_upload_v2`)

| Chart | When | Enabled by (flag) | Destination(s) | Config channel |
|-------|------|-------------------|----------------|----------------|
| **PAX attendance** charts | Monthly | `send_pax_charts` (gated on `firstf_channel` set) | **DM to each PAX** | none (gated by 1stF only) |
| **Q charts per AO** | Monthly | `send_q_charts` (gated on `firstf_channel`) | **Each AO's own channel** | none (per-AO `channel_id`) |
| **Q "stepping up" region chart** | Monthly | `send_q_charts` | 1stF channel | `firstf_channel` |
| **Region leaderboard** (monthly + YTD) | Monthly | `send_region_leaderboard` | 1stF channel | `firstf_channel` |
| **AO leaderboard** (monthly + YTD) per AO | Monthly | `send_ao_leaderboard` | **Each AO's own channel** | none (per-AO `channel_id`) |

Source: `monthly_charts/PAXcharter.py`, `Qcharter.py`, `Leaderboard_Charter.py`, `LeaderboardByAO_Charter.py`; gating in `handlers.py`.

Note: `firstf_channel` is the real destination only for the region leaderboard and the region-wide Q summary chart. For the other charts it acts only as an on/off gate — PAX charts go to DMs, and per-AO Q and AO-leaderboard charts post to each AO's own channel.

### Interactive / ephemeral (Slack front door, only the invoking admin sees these)

| Surface | Trigger | Notes |
|---------|---------|-------|
| `/config-paxminer` settings modal | slash command | admin-only; field errors are ephemeral |
| `/kotter-report` controls (button) | slash command | admin-only ephemeral message |
| Manual Kotter send confirmations | button click | admin-only ephemeral |

Source: `slack_app.py`.

## Lambdas (five functions)

| Function | Trigger | Role |
|----------|---------|------|
| **slack** | Function URL + keep-warm every 5 min | Lightweight Slack Bolt front door (`/config-paxminer`, `/kotter-report`); async-invokes Kotter for Send Now |
| **sync** | Daily schedule | User/channel sync |
| **charts** | Monthly schedule | PAX/Q charts and region/AO leaderboards |
| **achievements** | Daily schedule + Function URL webhook from Slackblast | Grant/revoke awards; leaderboard smoke path |
| **kotter** | Monthly schedule + async invoke from SlackFunction | Kotter report generation and posting |

Function URL outputs: **`SlackFunctionUrl`**, **`AchievementsFunctionUrl`** (passed to slackblast as `PM_ACHIEVEMENTS_URL`).

## Slack app manifest

Use **[manifest.json](manifest.json)** when creating the PAXMiner Slack app. After deploy, **`manifest-{test|prod}.json`** substitutes **`SlackFunctionUrl`** for `__HOSTNAME__` (slash commands and interactivity). Requires **`reactions:write`** for `:fire:` on achievement posts.

Admin commands: **`/config-paxminer`**, **`/kotter-report`**.

## Layout (high level)

| Area | Role |
|------|------|
| `slack_app.py` | Bolt app + Lambda handler for Slack interactivity |
| `handlers.py` | Lambda entrypoints (sync, charts, achievements, kotter) |
| `config_paxminer.py` | `/config-paxminer` modal builders + achievements_list helpers |
| `slack_blocks.py` | Shared Block Kit builders for outbound messages |
| `achievements/` | Rules engine, runner, leaderboard |
| `kotter/` | Kotter report generation |
| `backblast_scraping/` | Backblast mining from Slack history |
| `monthly_charts/` | Chart generation and posting |
| `database_management/` | User/channel sync helpers |
| `common/encryption.py` | Same Fernet-style helpers as repo root `common/encryption.py` (copied into the Docker image) |
| `Dockerfile` | Heavy image (pandas/matplotlib) for sync/charts/achievements/kotter |
| `Dockerfile.slack` | Light image (Bolt only) for SlackFunction |

## Tests

```bash
cd PAXminer && python -m pytest tests/ -q
```
