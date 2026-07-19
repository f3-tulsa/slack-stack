# PAXminer

Part of the **[slack-stack](../README.md)** monorepo. Deploy with SAM (`PAXminer/template.yaml`), GitHub Actions, or `./deploy.sh`. Database credentials, **`DB_ENCRYPTION_KEY`**, CI: **[docs/DEPLOY.md](../docs/DEPLOY.md)**; architecture: **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)**.

## What it does

PAXminer pulls workout (“backblast”) data from regional Slack workspaces, normalizes it, and stores it in a shared MySQL/TiDB database. It also generates charts and stats, runs **data-driven achievements** (grant, revoke, leaderboard, almost-there), and sends **Kotter** reports.

Each region has its own **schema** in the same database; registry rows in `paxminer.regions` point Lambdas at the right schema, timezone, achievement toggles, and encrypted Slack token. Deploy passes **`PM_SLACK_TOKEN`**, **`PM_SLACK_SIGNING_SECRET`**, **`PM_ACHIEVEMENTS_WEBHOOK_SECRET`**, **`F3_REGION_NAME`**, and **`STAGE`** via SAM; the Lambda **encrypts** the bot token with **`DB_ENCRYPTION_KEY`** and **upserts** it into `paxminer.regions` on cold start.

## Scheduling (unified)

Posting cadence and destinations come from PAXMiner-owned schedule tables (always-on; no feature flag):

| Table | Role |
|-------|------|
| `paxminer.region_report_definitions` | What a report is (builtin native producers + custom builder reports) |
| `paxminer.region_schedules` | When/where (destination, frequency, `time_of_day`, enabled) |
| `paxminer.regions.timezone` | Region TZ (default `America/Chicago`) for due-now evaluation |

`ScheduleFunction` ticks every **15 minutes**, evaluates region-local now, and runs due items (idempotent via `last_run_on` / `last_run_status`). Configure via `/config-paxminer` → **Schedule**. **Restore Defaults** merges builtin schedule rows from **`report_defaults.json`** (all six builtins enabled); **Delete All** clears schedules first if you want a clean reseed.

Builtin defaults seed **`specific_channels`** destinations with an **empty** channel list — those items stay **skipped** until an admin picks a channel under Schedule. `dm_all_pax` and `all_ao_channels` destinations fan out immediately once due.

Migration: `python migration/paxminer_migrate.py --env test|prod --all` (phases: weaselbot → scheduler → drop-legacy-columns). Deploy updated Slackblast + PAXMiner code **before** `--all`. Legacy scripts `migrate_weaselbot_to_paxminer.py` and `add_report_scheduler.py` are deprecated wrappers.

**Production cutover order:** (1) deploy updated Slackblast + PAXMiner code, (2) run `paxminer_migrate.py --all`, (3) set Schedule channels and disable any unwanted fan-out.

## What PAXMiner posts

### Text messages (channels + DMs)

| Message | When | Enabled by | Destination(s) |
|---------|------|------------|----------------|
| Achievement **granted** (+ emoji reaction) | Daily achievements run | `send_achievements` | Achievement channel **and** a DM to the PAX (+ the AO channel if `post_to_ao`) |
| Achievement **revoked** | Daily achievements run | `send_achievements` | Achievement channel (+ AO channel if `post_to_ao`) |
| **Achievement leaderboard (YTD)** | Schedule (default: monthly) | Schedule row `enabled` | Schedule destinations |
| **"Almost there"** progress list | With leaderboard | Schedule row `enabled` | Same as leaderboard |
| **Kotter / AOQ report** | Schedule + **Run Now** | Schedule row `enabled` | Schedule destinations |

Daily achievement **grant/revoke** uses `achievement_channel` from `/config-paxminer` (not the schedule). Leaderboards, Kotter, and charts are schedule-driven.

### Chart images (`files_upload_v2`)

| Chart | When | Destination(s) |
|-------|------|----------------|
| **PAX attendance** charts | Schedule (default: monthly) | **DM to each PAX** (or specific PAX) |
| **Q charts per AO** | Schedule | **Each AO channel** or specific channels |
| **Q region summary** | Schedule | Region / specific channels |
| **Region leaderboard** | Schedule | Specific / AO channels |
| **AO leaderboard** | Schedule | **Each AO channel** or specific |
| **Custom reports** | Schedule | Chart PNG or Block Kit table |

### Interactive / ephemeral

| Surface | Trigger | Notes |
|---------|---------|-------|
| `/config-paxminer` hub | slash | admin-only; timezone + Achievements on Save; hub buttons for Reports / Kotter thresholds / Schedule |
| Schedule / Reports modals | hub buttons | line-item schedule, report builder, Delete All, Restore Defaults, Run Now (DMs result) |
| App Home | `app_home_opened` | minimal stub; full dashboard later |

## Lambdas (four functions)

| Function | Trigger | Role |
|----------|---------|------|
| **slack** | Function URL + keep-warm every 5 min | Bolt front door; async-invokes ScheduleFunction for Run Now |
| **sync** | Daily | User/channel sync |
| **achievements** | Daily + webhook | Grant/revoke |
| **schedule** | `rate(15 minutes)` + async fan-out / Run Now | Unified dispatcher for charts, leaderboards, Kotter, and custom reports |

Function URL outputs: **`SlackFunctionUrl`**, **`AchievementsFunctionUrl`**.

**Run Now:** Schedule list → select item → **Run Now** async-invokes ScheduleFunction immediately (`force=True`). The worker DMs the requesting admin with success / skipped / error (no `paxminer_logs` post for manual runs), and the list shows `last_run_status` / `last_run_on`. The Slack app **Messages** tab must stay enabled (`messages_tab_enabled: true`) so those DMs are visible.

### Operational log (`paxminer_logs`)

Best-effort lines in the region's `#paxminer_logs` channel (same channel used by beatdown/user sync):

| Event | Example line |
|-------|----------------|
| Achievement granted / revoked | `- Achievement (Tulsa): granted 'Ironman' to <@U…>` |
| Achievement region failure | `- Achievement (Tulsa): FAILED - …` |
| Automatic schedule run | `- Schedule (Tulsa) #3 (kotter): success - posted to 1 channel(s)` |

Manual **Run Now** does **not** post here; the admin gets a DM instead.

## Slack app manifest

Use **[manifest.json](manifest.json)**. After deploy, **`manifest-{test|prod}.json`** substitutes **`SlackFunctionUrl`**. Includes **App Home** (Home + Messages tabs) + `app_home_opened`. Do **not** add `incoming-webhook`.

## Layout (high level)

| Area | Role |
|------|------|
| `slack_app.py` / `slack_schedule.py` | Bolt listeners |
| `config_paxminer.py` / `config_schedule.py` | Modal builders |
| `scheduling.py` | Pure due-now / time-window helpers |
| `schedule_schema.py` | DDL + seed / Restore Defaults |
| `schedule_runner.py` / `schedule_reports.py` | Dispatcher + custom report runner |
| `handlers.py` | Lambda entrypoints (incl. `schedule_handler`) |
| `Dockerfile` / `Dockerfile.slack` | Heavy vs light images |

## Tests

```bash
cd PAXminer && python -m pytest tests/ -q
pytest -q migration/tests   # from repo root (orchestrator unit tests)
```
