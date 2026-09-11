# Troubleshooting

## Slack / Bolt

- **`expired_trigger_id` / cold start:** First interaction after idle may fail; user retries usually succeed. Mitigations: **Lambda Function URLs** (less hop latency than API Gateway), EventBridge keep-warm, module-init DB/Fernet/Lambda-client warmup, lazy-listener self-invoke (zip apps), or provisioned concurrency if still tight on Slack’s 3s window.
- **Duplicate backblast / preblast in a channel:** Slack retried a `view_submission` after a slow ack (>3s). Slackblast claims `view.id` in `interaction_claims` before file downloads and `chat_postMessage`, so retries no-op. If duplicates reappear after deploy, confirm the table exists (below) and check CloudWatch for `Skipping duplicate`.
- **INFO missing in CloudWatch (slackblast):** Bolt’s `clear_all_log_handlers()` strips Lambda’s root handler. The app must re-attach a `StreamHandler` or INFO lines never appear.
- **OAuth tables missing:** Run the one-shot **`CREATE_OAUTH_TABLES`** flag for that app/stack after first deploy (see [DEPLOY.md](DEPLOY.md)).

## Lambda

- **`AccessDeniedException` on self-invoke:** Ensure the function’s IAM role allows **`lambda:InvokeFunction`** on its own ARN (slackblast / qsignups lazy listeners).
- **Slow first Slack ack after keep-warm:** Keep-warm attaches a boto3 Lambda client onto Bolt’s `LambdaLazyListenerRunner` (not a throwaway client). Module-init warmup does the same so the first Slack ack does not construct a new client.

## Database

- **Encryption key mismatch:** `DB_ENCRYPTION_KEY` used at **migration** time must match **deploy** for that stage; otherwise reads fail or data looks corrupt.
- **Regional schema not linked (QSignups):** If **`PM_REGIONAL_SCHEMA`** is unset, Site Q / past-Q detection is skipped; only Slack admins get calendar management.
- **`interaction_claims` missing:** Slackblast claim-before-post needs `slackblast_<stage>.interaction_claims`. On existing environments run `python migration/migrate_data.py --env <stage> --bootstrap-only` (idempotent DDL) before relying on the new code; missing table fails closed (raises — does not post).

## PAXminer container upgrade

- Zip-to-container or image updates can fail if ECR permissions, image tags, or stack parameters drift. Check CloudFormation events and [DEPLOY.md](DEPLOY.md) PAXminer notes.

## Assets

- **Missing Strava images in S3:** Confirm **`IMAGE_S3_BUCKET`** and deploy-time upload; re-run image sync if documented for your stack.

## Still stuck?

Open an issue with **app name**, **environment** (test/prod), and **recent CloudWatch / SAM logs** (redact tokens).
