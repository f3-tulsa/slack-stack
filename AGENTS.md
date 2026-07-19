# Contributor / AI agent guide

Shared, tool-agnostic rules for everyone working in this repo (humans and AI
agents). Cursor also loads these alongside `.cursor/rules/`.

## Commits

- Commit in **small, logical chunks** at meaningful breakpoints — one concern
  per commit.
- **Do not push** unless the user explicitly asks.
- Every AI-assisted commit must include a `Co-authored-by` trailer for the tool
  that assisted. Use a HEREDOC so the trailer lands in the commit body.

  Cursor (this repo’s usual tool):

  ```
  Co-authored-by: Cursor <cursoragent@cursor.com>
  ```

  If a different AI tool authored the work, use **that** tool’s trailer instead
  (for example `Co-authored-by: Claude <noreply@anthropic.com>`), not the Cursor
  line.
- Do **not** change local git `user.name` / `user.email`. Keep the human author
  identity from the existing git config.

## Testing

Review and add/update tests as part of the **same** change — not a follow-up.

- **Dev / unit tests** — run in CI (`.github/workflows/ci.yml`), e.g.
  `PAXminer/tests/` via `pytest`. Add or adjust coverage for new behavior and
  regressions.
- **Deploy smoke tests** — kept-warm Lambda invokes in `run_smoke_test_lambdas`
  / `invoke_one` in `deploy.sh`. Update these when adding functions or changing
  deploy-time behavior.
- Bug fixes get a **regression test** that fails before the fix.
- If a change genuinely needs no test update, say so in the summary and why.

## Deploys

Deploys are **selective** — only the apps and functions that actually changed
should build and ship. Preserve this; do not regress it.

- **CI picks apps by path.** `.github/workflows/deploy.yml` has a
  `detect-changes` job using `dorny/paths-filter`; each app's deploy job is
  gated on it (`PAXminer/**`+`common/**` → paxminer, `slackblast/**`,
  `qsignups/**`, and `infra/**` → all). When you add code, move a shared module,
  or add a new app, **update these filters** so the right stack (and only it)
  deploys.
- **CloudFormation picks functions.** Every `sam deploy` uses
  `--no-fail-on-empty-changeset`, so CFN updates only the functions whose image
  digest or config changed. Keep that flag; never force-replace unchanged
  functions.
- **Shared image = shared blast radius.** All heavy PAXMiner functions (`sync`,
  `achievements`, `schedule`) build from the same
  `PAXminer/Dockerfile` (differing only by `ImageConfig.Command`), so any change
  under `PAXminer/**` rebuilds/reships all of them together. That's expected —
  don't "fix" it by splitting images without a reason. Keep Dockerfiles layered
  deps-first (copy `requirements*.txt` and `pip install` **before** copying app
  code) so Docker's layer cache is reused on code-only changes.
- **Local `deploy.sh` builds all stacks by default.** It has no path detection —
  scope it with `--stack paxminer|slackblast|qsignups` to build/deploy just the
  app you touched. `--no-cached` (SAM's artifact cache, distinct from Docker's
  layer cache) is intentional for CI parity/reproducibility; leave it as-is.
- Keep `deploy.sh` and `deploy.yml` in sync (bucket resolution, parameter
  overrides, smoke-tested functions). When you add a function, add it to both
  the smoke tests (`run_smoke_test_lambdas` locally, `post-deploy` in CI) and,
  if it belongs to a new app, the path filters.

## Slack interactivity

Use Slack Bolt + Block Kit for slash commands, modals, and buttons. Ack within
3 seconds; async-invoke heavy work (charts, Kotter, etc.). Do not hand-roll
Slack HMAC verification for interactive traffic.

Detailed Cursor rule (patterns, ack rules, exceptions):
[`.cursor/rules/slack-bolt.mdc`](.cursor/rules/slack-bolt.mdc).
