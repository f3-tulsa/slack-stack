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

## Slack interactivity

Use Slack Bolt + Block Kit for slash commands, modals, and buttons. Ack within
3 seconds; async-invoke heavy work (charts, Kotter, etc.). Do not hand-roll
Slack HMAC verification for interactive traffic.

Detailed Cursor rule (patterns, ack rules, exceptions):
[`.cursor/rules/slack-bolt.mdc`](.cursor/rules/slack-bolt.mdc).
