# Running slack-stack for free on AWS

**Constraint: this stack must cost $0/month on AWS.** Treat that as a design
requirement, not an aspiration. Anything that would push a service past its free
allowance needs a deliberate decision, not a shrug.

Everything below is us-east-1. Free-tier allowances are **per region and do not
pool across regions**, so running a second region doubles nothing — it just
starts a second meter.

> Accounts created after mid-2025 get AWS's newer credit-based free plan instead
> of the classic always-free allowances. This repo's account predates that, so
> the always-free numbers below apply. Re-check if the stack ever moves accounts.

## Does CloudWatch cost? Effectively no, here

**5 GB per month is always free**, and that single bucket covers all three of:

- log **ingestion**
- log **archive storage**
- data **scanned by Logs Insights queries**

Past that: $0.50/GB ingested (Standard class), $0.03/GB-month stored, $0.005/GB
scanned by Insights.

This stack stays far under 5 GB because every Lambda log group sets
**`RetentionInDays: 30`** in its template — [PAXminer](../PAXminer/template.yaml)
(four functions), [slackblast](../slackblast/template.yaml), and
[qsignups](../qsignups/template.yaml). Without that, SAM's default is *never
expire* and archive storage grows forever until it eats the 5 GB.

Two things to keep true:

- **Never remove the retention setting**, and set it on any new function.
- **Logs Insights queries bill against the same 5 GB.** Routine debugging with
  `aws logs tail` uses `FilterLogEvents`, which counts against the 1,000,000
  free API requests instead — cheap. Habitual large Insights scans are what
  would blow the budget, not the logging itself.

Log *volume* is not the risk here. The Slack front door writes a handful of
lines per interaction.

## What actually costs money: ECR

**This is the real exposure, and it is currently not free.**

Measured 2026-08-21: **87 GB across 10 repositories, 317 images.**

- paxminer test — 225 images, ~57 GB
- weaselbot test + prod — 78 images, ~27 GB (dead code, see cutover)
- paxminer prod — 14 images, ~4 GB

Private ECR gives **500 MB-month free, and only for the first 12 months** — it
is not an always-free allowance. Past that it is **$0.10/GB-month**, so 87 GB is
roughly **$8.70/month**, growing with every deploy.

**Why it grows.** `deploy.sh` and CI both pass `--resolve-image-repos`, so SAM
creates and owns the ECR repositories. Every deploy pushes a new image and
**nothing ever deletes the old one**. The heavy PAXMiner image is ~250-450 MB, so
a day of iteration adds several GB.

**The fix** is a lifecycle policy per repository. Apply with:

```bash
./scripts/prune-ecr.sh --preview   # show what would be expired
./scripts/prune-ecr.sh             # apply the policy
```

It keeps the most recent images per repo and expires the rest. Keeping several
matters: **Lambda needs the image its function version references to still exist
in ECR**, so never prune down to fewer than the deployed image plus headroom for
rollback.

**Also delete the four `weaselbot*` repositories at cutover.** That is ~27 GB of
images for a retired app. It is already on the teardown list in the program plan
alongside dropping the CloudFormation stacks.

## Everything else

| Service | Free allowance | How this stack uses it |
| --- | --- | --- |
| Lambda | 1M requests + 400,000 GB-seconds per month, always free | Four PAXMiner functions plus slackblast and qsignups. The keep-warm ping is every 5 minutes (~8,600/month) and the schedule tick every 15 minutes (~2,900/month). Nowhere near the cap. |
| Lambda Function URLs | Free | Used instead of API Gateway specifically to avoid per-request charges. Do not introduce API Gateway. |
| EventBridge | Scheduled rules are free | Keep-warm and the schedule tick. |
| CloudWatch Logs | 5 GB/month combined | See above. 30-day retention set. |
| X-Ray | 100,000 traces recorded/month | Off by default (`ENABLE_XRAY=false`). Leave it off unless debugging. |
| S3 | 5 GB, **12 months only** | Deploy artifact buckets and the backblast image bucket. Worth watching; not currently a problem. |
| Data transfer out | 100 GB/month free | Slack API calls and image serving. Not close. |
| Secrets Manager | **Not free** ($0.40/secret/month) | **Not used.** Secrets are CloudFormation parameters and Lambda env vars. Do not introduce it. |

The database is **TiDB Cloud Serverless**, not AWS, and has its own free tier.
It is outside this budget but inside the same constraint.

## Before adding anything

Ask which meter it starts. The pattern that has kept this free so far:

- Function URLs, not API Gateway.
- Env vars and CFN parameters, not Secrets Manager.
- Explicit log retention on every log group.
- Container images on Lambda, which is free at this volume — but the **images
  themselves accumulate in ECR**, which is the one place this stack has actually
  leaked money.

## Periodic check

```bash
# ECR storage by repository
for r in $(aws ecr describe-repositories --region us-east-1 \
    --query 'repositories[].repositoryName' --output text); do
  printf '%-60s %s MB\n' "$r" \
    "$(( $(aws ecr describe-images --region us-east-1 --repository-name "$r" \
        --query 'sum(imageDetails[].imageSizeInBytes)' --output text | cut -d. -f1) / 1048576 ))"
done

# Confirm retention is still set on every log group
aws logs describe-log-groups --region us-east-1 \
  --query 'logGroups[].{name:logGroupName,retention:retentionInDays}' --output table
```

A log group showing `None` for retention is a regression — fix the template
rather than setting it by hand, or the next deploy loses it.
