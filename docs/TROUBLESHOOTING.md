# Troubleshooting

## Slack / Bolt

- **`expired_trigger_id` / cold start:** First interaction after idle may fail; user retries usually succeed. Mitigations: **Lambda Function URLs** (less hop latency than API Gateway), EventBridge keep-warm, module-init DB/Fernet/Lambda-client warmup, lazy-listener self-invoke (zip apps), or provisioned concurrency if still tight on Slack’s 3s window.
- **Duplicate backblast / preblast in a channel:** Slack retried one `view_submission` after a slow ack (>3s). Each retry is a new Lambda; `beatdowns` uniqueness cannot help because it needs Slack’s `ts` from `chat_postMessage`. Slackblast INSERTs `view.id` into `interaction_claims` *before* posting; a 1062 skip is logged as `Skipping duplicate`. If extras reappear, confirm the table exists (below). Design: [ARCHITECTURE.md](ARCHITECTURE.md) (slackblast retry receipts).
- **INFO missing in CloudWatch (slackblast):** Bolt’s `clear_all_log_handlers()` strips Lambda’s root handler. The app must re-attach a `StreamHandler` or INFO lines never appear.
- **OAuth tables missing:** Run the one-shot **`CREATE_OAUTH_TABLES`** flag for that app/stack after first deploy (see [DEPLOY.md](DEPLOY.md)).

## Lambda

- **`AccessDeniedException` on self-invoke:** Ensure the function’s IAM role allows **`lambda:InvokeFunction`** on its own ARN (slackblast / qsignups lazy listeners).
- **Slow first Slack ack after keep-warm:** Keep-warm attaches a boto3 Lambda client onto Bolt’s `LambdaLazyListenerRunner` (not a throwaway client). Module-init warmup does the same so the first Slack ack does not construct a new client.

## Database

- **Encryption key mismatch:** `DB_ENCRYPTION_KEY` used at **migration** time must match **deploy** for that stage; otherwise reads fail or data looks corrupt.
- **Regional schema not linked (QSignups):** If **`PM_REGIONAL_SCHEMA`** is unset, Site Q / past-Q detection is skipped; only Slack admins get calendar management.
- **`interaction_claims` missing:** Slackblast claim-before-post needs `slackblast_<stage>.interaction_claims` (PK `claim_key` + `kind`). On existing environments run `python migration/migrate_data.py --env <stage> --bootstrap-only` (idempotent DDL) **before** deploying the Lambda that writes it; missing table fails closed (raises — does not post). Use venv `.venv-migration`. Rows older than 7 days are pruned on each claim; do not truncate the table by hand while Slack may still retry a submit.

## PAXminer container upgrade

- Zip-to-container or image updates can fail if ECR permissions, image tags, or stack parameters drift. Check CloudFormation events and [DEPLOY.md](DEPLOY.md) PAXminer notes.

## Assets

- **Missing Strava images in S3:** Confirm **`IMAGE_S3_BUCKET`** and deploy-time upload; re-run image sync if documented for your stack.

## Still stuck?

Open an issue with **app name**, **environment** (test/prod), and **recent CloudWatch / SAM logs** (redact tokens).
