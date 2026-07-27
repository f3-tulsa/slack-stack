# PAXminer

Part of the **[slack-stack](../README.md)** monorepo. Deploy with SAM (`PAXminer/template.yaml`), GitHub Actions, or `./deploy.sh`. Database credentials, **`DB_ENCRYPTION_KEY`**, CI: **[docs/DEPLOY.md](../docs/DEPLOY.md)**; architecture: **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)**.

## What it does

PAXminer pulls workout (“backblast”) data from regional Slack workspaces, normalizes it, and stores it in a shared MySQL/TiDB database. It also generates charts and stats, runs **data-driven achievements** (grant, revoke, leaderboard, almost-there), and sends **Kotter** reports.

Each region has its own **schema** in the same database; registry rows in `paxminer.regions` point Lambdas at the right schema, timezone, achievement toggles, and encrypted Slack token. Deploy passes **`PM_SLACK_TOKEN`**, **`PM_SLACK_SIGNING_SECRET`**, **`PM_ACHIEVEMENTS_WEBHOOK_SECRET`**, **`F3_REGION_NAME`**, and **`STAGE`** via SAM; the Lambda **encrypts** the bot token with **`DB_ENCRYPTION_KEY`** and **upserts** it into `paxminer.regions` on cold start.

**Achievements, Kotter, and the achievement leaderboard are single-region:** they only read attendance from the region's own schema (e.g. `f3ttown_test` / `f3ttown`). Cross-region / “down range” attendance requires the F3 Nation API and is out of scope for now.

## Scheduling (unified)

Posting cadence and destinations come from PAXMiner-owned schedule tables (always-on; no feature flag):

| Table | Role |
|-------|------|
| `paxminer.region_report_definitions` | What a report is (builtin code-rendered producers + custom builder reports). Builtins are editable/deletable; `is_builtin` is provenance, `is_customized` marks admin edits. |
| `paxminer.region_schedules` | When/where (destination, frequency, `time_of_day`, enabled) |
| `paxminer.regions.timezone` | Region TZ (default `America/Chicago`) for due-now evaluation |

`ScheduleFunction` ticks every **15 minutes**, evaluates region-local now, and runs due items (idempotent via `last_run_on` / `last_run_status`). Configure via `/config-paxminer` → **Schedule** / **PAX Reports**.

**Builtin reports** (from `report_defaults.json`) render from dedicated Python (charts, Kotter, achievement leaderboard). You can **rename**, set a **time window** (honored by the SQL), **duplicate**, **delete**, and **schedule** them. Kotter has no time window — it uses Kotter threshold config. **Custom reports** use the full builder (source, fields, metric, chart vs table).

**Defaults load on demand only** — migration creates tables/columns but does **not** seed `report_defaults.json`. Use **Load defaults** on an empty Reports/Schedule list, or **Restore Defaults** later. Restore merges missing builtins/schedules; rows with `is_customized=1` keep their edits. **Delete** removes the definition and any schedules that reference it (FK stays `RESTRICT` with app-level cascade). **Duplicate** copies a definition with a uniquified `*_copy` code and no schedules.

Builtin defaults seed **`specific_channels`** destinations with an **empty** channel list — those items stay **skipped** until an admin picks a channel under Schedule. `dm_all_pax` and `all_ao_channels` destinations fan out immediately once due.

Migration: `python migration/paxminer_migrate.py --env test|prod --all` (phases: weaselbot → scheduler DDL → drop-legacy-columns). Deploy updated Slackblast + PAXMiner code **before** `--all`. Legacy scripts `migrate_weaselbot_to_paxminer.py` and `add_report_scheduler.py` are deprecated wrappers.

**Production cutover order:** (1) deploy updated Slackblast + PAXMiner code, (2) run `paxminer_migrate.py --all`, (3) **Load defaults** (or Restore Defaults) in Slack if the region has no reports yet, (4) set Schedule channels and disable any unwanted fan-out.

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
| Schedule / Reports modals | hub buttons | editable builtins + custom builder; Duplicate; Load/Restore defaults; Delete All; Run Now (DMs result) |
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

Best-effort lines in the region's `#paxminer_logs` channel (same channel used by beatdown/user sync). Labels use **`schema_name`** (e.g. `f3ttown_test`), not the display region name:

| Event | Example line |
|-------|----------------|
| Achievement granted / revoked | `- Achievement (f3ttown_test): granted 'Ironman' to <@U…>` |
| Achievement region failure | `- Achievement (f3ttown_test): FAILED - …` |
| Automatic schedule run | `- Schedule (f3ttown_test) #3 (kotter): success - posted to 1 channel(s) \| posted: kotter (C…)` |

Schedule Run Now DMs and automatic log lines list **posted** and **failed** destinations (AO name + channel/user ID + reason), capped for Slack length. Chart producers report real upload successes — a resolved AO count is no longer treated as “posted.”

Empty attendance for Achievements/Kotter returns a clear skip/error (and Achievements will **not** mass-revoke awards when attendance data is missing).

### Seed test-region data (dev only)

**Test-only** seeder. Always loads [`.env.deploy.test`](../.env.deploy.example) (no `--env` switch) and hard-fails unless the regional/registry schemas end in `_test` and Slack `auth.test` matches `F3_REGION_SLACK_TEAM_ID`.

**Default (one-shot):** clears prior `[SEED]` / `json.seed` rows, then rebuilds a realistic ~180-day calendar — weekly multi-PAX beatdowns at every QSignups AO, Q from a pool of real humans plus synthetic `[SEED] PAX nn` users (`--synthetic-pax`, default 12). Synthetic PAX make leaderboards meaningful but are not in Slack, so Kotter mentions them literally and `dm_all_pax` may log DM failures for them — use `dm_specific_pax` (yourself) when testing PAX charts, or `--synthetic-pax 0`.

```bash
# From repo root, with .env.deploy.test filled in
python PAXminer/scripts/seed_test_region.py --yes --verify
python PAXminer/scripts/seed_test_region.py --yes --days 180 --synthetic-pax 12 --kotter mia,lowq,noq
# Interactive overlays on top of whatever is already in the DB
python PAXminer/scripts/seed_test_region.py --interactive
# Destination / row-count preflight only (no writes)
python PAXminer/scripts/seed_test_region.py --verify-only
```

AO list comes from QSignups (`qsignups_test.qsignups_aos`), falling back to regional `aos` then all Slack channels. Interactive mode walks each user (Kotter / one Achievement / clear / skip); overlays **join** existing beatdowns when possible (bump `pax_count`) instead of inventing one-man events, and spread multi-AO goals across distinct dates. Seeded rows are tagged `[SEED]` in backblast and `{"seed": true}` in `json` so clear only removes synthetic data. After seeding, run the achievements job (or Schedule → Run Now) to grant awards from attendance — the seeder shapes data rather than inserting fake awards. Not wired into CI or deploy.

To wipe prod-derived attendance first (same test-only guards, typed confirmation):

```bash
python PAXminer/scripts/reset_test_region.py --dry-run   # report only
python PAXminer/scripts/reset_test_region.py
```

Reset clears **all** `bd_attendance` / `beatdowns` / `achievements_awarded`, and prunes `users` / `aos` rows whose Slack IDs are not in the test workspace (`--keep-roster` skips the prune). `achievements_list` rules and views are preserved. Migrated prod attendance is not useful in test because the Slack user IDs differ — reset, then seed.

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
