# Troubleshooting

## Slack / Bolt

- **`expired_trigger_id` / cold start:** First interaction after idle may fail; user retries usually succeed. Mitigations: **Lambda Function URLs** (less hop latency than API Gateway), EventBridge keep-warm, module-init DB/Fernet warmup, lazy-listener self-invoke (zip apps), or provisioned concurrency if still tight on Slack’s 3s window.
- **OAuth tables missing:** Run the one-shot **`CREATE_OAUTH_TABLES`** flag for that app/stack after first deploy (see [DEPLOY.md](DEPLOY.md)).

## Lambda

- **`AccessDeniedException` on self-invoke:** Ensure the function’s IAM role allows **`lambda:InvokeFunction`** on its own ARN (slackblast / qsignups lazy listeners).

## Database

- **Encryption key mismatch:** `DB_ENCRYPTION_KEY` used at **migration** time must match **deploy** for that stage; otherwise reads fail or data looks corrupt.
- **Regional schema not linked (QSignups):** If **`PM_REGIONAL_SCHEMA`** is unset, Site Q / past-Q detection is skipped; only Slack admins get calendar management.

## PAXminer container upgrade

- Zip-to-container or image updates can fail if ECR permissions, image tags, or stack parameters drift. Check CloudFormation events and [DEPLOY.md](DEPLOY.md) PAXminer notes.

## Assets

- **Missing Strava images in S3:** Confirm **`IMAGE_S3_BUCKET`** and deploy-time upload; re-run image sync if documented for your stack.

## Still stuck?

Open an issue with **app name**, **environment** (test/prod), and **recent CloudWatch / SAM logs** (redact tokens).
