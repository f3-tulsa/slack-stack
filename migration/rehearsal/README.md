# Rehearsal and cutover operators

Scripts to copy prod into a rehearsal replica, migrate Weaselbot into PAXMiner, audit achievement rules, and re-evaluate awards. Sitting notes, audit snapshots, and receipts stay **out of git**.

## What is tracked vs local

| In git | Local only (do not commit) |
| --- | --- |
| These scripts and this README | `migration/.env.migration.<stage>` |
| | `migration/rehearsal/sittings/` — notes, audit JSON, copies of receipts |
| | `migration/receipts/` — migrate already writes here |

Create the sittings folder if it does not exist:

```bash
mkdir -p migration/rehearsal/sittings
```

The detailed cutover runbook (schema names, abort numbers, timings) is a local plan, not a file in this repo.

## Env

Copy `migration/.env.migration.example` to `migration/.env.migration.rehearsal` (and `.env.migration.prod` for cutover). Fill `TARGET_*` for that stage. Rehearsal must use a **throwaway** `DB_ENCRYPTION_KEY`, not prod’s.

`--env rehearsal` loads `.env.migration.rehearsal` with `override=True`. The file is required; a missing file is a hard error.

## Replica copy (rehearsal only)

`prepare_rehearsal.py` copies prod schemas into `*_rehearsal` and rewrites registry pointers. It refuses any target other than `rehearsal`. Schema bases to copy are listed in `COPY_BASES` in that script.

```bash
python migration/prepare_rehearsal.py --from prod --to rehearsal --preflight
python migration/prepare_rehearsal.py --from prod --to rehearsal --dry-run
python migration/prepare_rehearsal.py --from prod --to rehearsal --drop-existing
```

Preflight should show SELECT/SHOW VIEW on prod and ALL on rehearsal for the scoped user. Do not set `TARGET_PAXMINER_SCHEMA` or `SOURCE_HOST` on a rehearsal run.

## Audit

Never prints token values. Writes snapshots wherever you point `--snapshot` — use the sittings folder.

```bash
python migration/audit_achievements.py --env rehearsal \
  --snapshot migration/rehearsal/sittings/audit-before.json
python migration/audit_achievements.py --env rehearsal \
  --snapshot migration/rehearsal/sittings/audit-after.json \
  --compare migration/rehearsal/sittings/audit-before.json
```

Non-zero exit means failing findings (unknown codes, orphaned awards, plaintext tokens, unexpected list/version edits). `catalog_alignment` on first migrate of a legacy schema is informational.

## Migrate

Use the same CLI as test/prod. Receipts go to `migration/receipts/` (gitignored).

```bash
python migration/paxminer_migrate.py --env rehearsal --all
```

Do not pass `--drop-weaselbot-schema` until awards have been live on the new path. After the weaselbot phase, regions that had no Weaselbot config row inherit `send_achievements=1` from the ALTER default — set those to `0` before any scheduled tick if they should not announce.

## Re-eval

Use the PAXMiner venv (pandas, slack_sdk). `--schemas` is required. Pass only the regional schema that should be scored.

Dry-run first. Cutover write pass:

```bash
PAXminer/.venv/bin/python migration/rehearsal_reconcile.py \
  --env <stage> --schemas <regional_schema> \
  --all-attendance --allow-revoke --dry-run

PAXminer/.venv/bin/python migration/rehearsal_reconcile.py \
  --env <stage> --schemas <regional_schema> \
  --all-attendance --allow-revoke
```

That calls existing `reconcile_rule_awards` per enabled rule (no T-Claps; one summary log per rule). `--allow-revoke` without `--all-attendance` is refused. Abort unless a second dry-run with the same flags reports `grants: 0` / `revokes: 0`. Keep the Award Achievements schedule disabled until then.

A YTD run (omit `--all-attendance` and `--allow-revoke`) is not the cutover contract.
