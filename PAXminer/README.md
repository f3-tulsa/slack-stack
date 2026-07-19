# PAXminer

Part of the **[slack-stack](../README.md)** monorepo. Deploy with SAM (`PAXminer/template.yaml`), GitHub Actions, or `./deploy.sh`. Database credentials, **`DB_ENCRYPTION_KEY`**, CI: **[docs/DEPLOY.md](../docs/DEPLOY.md)**; architecture: **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)**.

## What it does

PAXminer pulls workout (“backblast”) data from regional Slack workspaces, normalizes it, and stores it in a shared MySQL/TiDB database. It also generates charts and stats, runs **data-driven achievements** (grant, revoke, leaderboard, almost-there), and sends **Kotter** reports.

Each region has its own **schema** in the same database; registry rows in `paxminer.regions` point Lambdas at the right schema, channels, toggles, timezone, and encrypted Slack token. Deploy passes **`PM_SLACK_TOKEN`**, **`PM_SLACK_SIGNING_SECRET`**, **`PM_ACHIEVEMENTS_WEBHOOK_SECRET`**, **`F3_REGION_NAME`**, and **`STAGE`** via SAM; the Lambda **encrypts** the bot token with **`DB_ENCRYPTION_KEY`** and **upserts** it into `paxminer.regions` on cold start.

## Scheduling (unified)

When **`PM_USE_SCHEDULE_DISPATCHER=true`**, posting cadence and destinations come from PAXMiner-owned tables (not the legacy `send_*` flags):

| Table | Role |
|-------|------|
| `paxminer.region_report_definitions` | What a report is (builtin native producers + custom builder reports) |
| `paxminer.region_schedules` | When/where (destination, frequency, `time_of_day`, enabled) |
| `paxminer.regions.timezone` | Region TZ (default `America/Chicago`) for due-now evaluation |

`ScheduleFunction` ticks every **15 minutes**, evaluates region-local now, and runs due items (idempotent via `last_run_on` / `last_run_status`). Configure via `/config-paxminer` → **Schedule**. **Restore Defaults** merges builtin schedule rows; **Delete All** clears schedules first if you want a clean reseed.

Legacy columns `firstf_channel`, `achievement_channel`, `kotter_channel` and `send_*` flags remain for seeding / cutover fallback and are **deprecated** as the primary config surface.

Migration: `migration/add_report_scheduler.py --env test|prod`.

## What PAXMiner posts

### Text messages (channels + DMs)

| Message | When (legacy / schedule) | Enabled by (legacy flag) | Destination(s) | Config channel (legacy) |
|---------|--------------------------|--------------------------|----------------|-------------------------|
| Achievement **granted** (+ emoji reaction) | Daily achievements run | `send_achievements` | Achievement channel **and** a DM to the PAX (+ the AO channel if `post_to_ao`) | `achievement_channel` |
| Achievement **revoked** | Daily achievements run | `send_achievements` | Achievement channel (+ AO channel if `post_to_ao`) | `achievement_channel` |
| **Achievement leaderboard (YTD)** | Monthly / schedule | `send_achievement_leaderboard` | Schedule destinations (default: achievement channel) | `achievement_channel` |
| **"Almost there"** progress list | With leaderboard | `send_achievement_leaderboard` | Same as leaderboard | `achievement_channel` |
| **Kotter / AOQ report** | Monthly / schedule + manual | `send_aoq_reports` | Schedule destinations (default: kotter channel) | `kotter_channel` |

Achievement **grant/revoke** evaluation stays on the daily Achievements Lambda + webhook (not the unified schedule).

### Chart images (`files_upload_v2`)

| Chart | When (legacy / schedule) | Enabled by (legacy flag) | Destination(s) | Config channel (legacy) |
|-------|--------------------------|--------------------------|----------------|-------------------------|
| **PAX attendance** charts | Monthly / schedule | `send_pax_charts` | **DM to each PAX** (or specific PAX) | gated by `firstf_channel` historically |
| **Q charts per AO** | Monthly / schedule | `send_q_charts` | **Each AO channel** or specific channels | per-AO `channel_id` |
| **Q region summary** | Monthly / schedule | `send_q_charts` | Region / specific channels | `firstf_channel` |
| **Region leaderboard** | Monthly / schedule | `send_region_leaderboard` | Specific / AO channels | `firstf_channel` |
| **AO leaderboard** | Monthly / schedule | `send_ao_leaderboard` | **Each AO channel** or specific | per-AO `channel_id` |
| **Custom reports** | Schedule only | (definition) | Chart PNG or Block Kit table | schedule destinations |

### Interactive / ephemeral

| Surface | Trigger | Notes |
|---------|---------|-------|
| `/config-paxminer` hub | slash | admin-only; timezone + Achievements / Reports / Kotter / Schedule |
| Schedule / Reports modals | hub buttons | line-item schedule, report builder, Delete All, Restore Defaults, Run Now (DMs result) |
| App Home | `app_home_opened` | minimal stub; full dashboard later |

## Lambdas (six functions)

| Function | Trigger | Role |
|----------|---------|------|
| **slack** | Function URL + keep-warm every 5 min | Bolt front door; async-invokes ScheduleFunction for Run Now |
| **sync** | Daily | User/channel sync |
| **charts** | Monthly (skipped when dispatcher on) | Legacy chart fan-out |
| **achievements** | Daily + webhook | Grant/revoke |
| **kotter** | Monthly EventBridge (skipped when dispatcher on; smoke still runs) | Kotter generation |
| **schedule** | `rate(15 minutes)` + async fan-out / Run Now | Unified dispatcher (manual Kotter via Run Now) |

Function URL outputs: **`SlackFunctionUrl`**, **`AchievementsFunctionUrl`**.

Cutover: migrate DB → deploy with `PM_USE_SCHEDULE_DISPATCHER=false` → seed/UI → set `true` → legacy monthly Chart/Kotter EventBridge becomes no-op.

**Run Now:** Schedule list → select item → **Run Now** async-invokes ScheduleFunction immediately (`force=True`), even when `PM_USE_SCHEDULE_DISPATCHER` is off (the tick stays gated). The worker DMs the requesting admin with success / skipped / error, and the list shows `last_run_status` / `last_run_on`.

## Slack app manifest

Use **[manifest.json](manifest.json)**. After deploy, **`manifest-{test|prod}.json`** substitutes **`SlackFunctionUrl`**. Includes **App Home** + `app_home_opened`. Do **not** add `incoming-webhook`.

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
```
