# Troubleshooting

## Slack / Bolt

- **`expired_trigger_id` / cold start:** First interaction after idle may fail; user retries usually succeed. Mitigations: provisioned concurrency, warmer pings, or lazy-listener self-invoke (already used in zip apps).
- **OAuth tables missing:** Run the one-shot **`CREATE_OAUTH_TABLES`** flag for that app/stack after first deploy (see [DEPLOY.md](DEPLOY.md)).

## Lambda

- **`AccessDeniedException` on self-invoke:** Ensure the function’s IAM role allows **`lambda:InvokeFunction`** on its own ARN (slackblast / qsignups lazy listeners).

## Database

- **Encryption key mismatch:** `DB_ENCRYPTION_KEY` used at **migration** time must match **deploy** for that stage; otherwise reads fail or data looks corrupt.
- **Regional schema not linked (QSignups):** If **`PAXMINER_REGIONAL_SCHEMA`** is unset, Site Q / past-Q detection is skipped; only Slack admins get calendar management.

## PAXminer container upgrade

- Zip-to-container or image updates can fail if ECR permissions, image tags, or stack parameters drift. Check CloudFormation events and [DEPLOY.md](DEPLOY.md) PAXminer notes.

## Assets

- **Missing Strava images in S3:** Confirm **`IMAGE_S3_BUCKET`** and deploy-time upload; re-run image sync if documented for your stack.

## Still stuck?

Open an issue with **app name**, **environment** (test/prod), and **recent CloudWatch / SAM logs** (redact tokens).
