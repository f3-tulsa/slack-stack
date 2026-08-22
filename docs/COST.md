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

## What actually costs money

Measured from Cost Explorer 2026-08-21, usage records only (credits excluded):

| Month | Total | Lambda SnapStart | ECR storage | S3 |
| --- | --- | --- | --- | --- |
| 2026-05 | $3.82 | $2.02 | $1.77 | $0.04 |
| 2026-06 | $3.80 | $1.95 | $1.81 | $0.04 |
| 2026-07 | $4.06 | $2.02 | $1.99 | $0.05 |

**Promotional credits are currently zeroing this out**, so the invoice reads $0
while real usage is roughly **$3.85/month**. When the credits lapse, that is the
bill. Two line items account for essentially all of it.

### 1. An orphaned Lambda SnapStart snapshot (~$2.00/month)

Usage type `Lambda-SnapStart-Cached-GB-S`, about 1.3 million GB-seconds a month.

`slackblast-prod-SlackblastFunction` **version 1**, published 2026-04-01, has
`SnapStart.ApplyOn=PublishedVersions` and `OptimizationStatus=On` at 512 MB —
roughly half a gigabyte of snapshot cached continuously.

SnapStart was added in commit `e9c4cd1` and later dropped from the template, but
**removing SnapStart from a template does not delete already-published
versions**, and a published version keeps its own configuration forever. `$LATEST`
reports `ApplyOn=None` while version 1 quietly keeps billing.

Nothing uses version 1: there are no aliases, and both the Function URL and the
resource policy target the unqualified ARN, which resolves to `$LATEST`.
Deleting that version stops the charge, and it cannot come back because no
template enables SnapStart now.

**Rule going forward:** if a function ever publishes versions, deleting the
feature from the template is not enough — the old versions have to be deleted
too.

### 2. ECR image storage (~$1.80-2.00/month)

Private ECR gives **500 MB-month free, and only for the first 12 months** — not
an always-free allowance. Past that it is **$0.10/GB-month**.

Billed storage is about **20 GB-months**. Note that summing `imageSizeInBytes`
across images gives ~87 GB, which is misleading: **ECR bills unique layers**, and
these images share base layers heavily. Trust the Cost Explorer quantity, not
the image-size sum.

**Why it grows.** `deploy.sh` and CI both pass `--resolve-image-repos`, so SAM
creates the repositories in a `*-CompanionStack` and every deploy pushes a new
image without deleting the old one. A day of iteration adds several images per
function.

**The fix is a lifecycle policy per repository.** This is **set-once, not a
recurring chore** — a policy stays on the repository, SAM reuses the same
repositories across deploys, and ECR enforces it continuously. You only need to
re-run it when a *new* image function is added:

```bash
./scripts/prune-ecr.sh --preview   # show what would be expired
./scripts/prune-ecr.sh             # apply the policy
```

Keeping several images matters: **Lambda needs the image its deployed version
references to still exist in ECR**, so never trim below the live image plus
rollback headroom.

**Dead weight to delete outright:** the four `weaselbot*` repositories, the four
`weaselbot-*` Lambda functions, and the `weaselbot-test` / `weaselbot-prod`
stacks plus their companion stacks. That app no longer deploys. It is on the
cutover teardown list in the program plan.

## Everything else

| Service | Free allowance | How this stack uses it |
| --- | --- | --- |
| Lambda | 1M requests + 400,000 GB-seconds per month, always free | Four PAXMiner functions plus slackblast and qsignups. The keep-warm ping is every 5 minutes (~8,600/month) and the schedule tick every 15 minutes (~2,900/month). Invocation and duration have never appeared on the bill. **SnapStart cache is billed separately and is not covered by the free tier** — see above. |
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

Start with the bill, not with resource inventories. Cost Explorer answers
"what is charging" in one call; guessing from resource sizes led to a wrong
conclusion once already. Note that CE itself charges $0.01 per request.

```bash
# What is actually charging, by service and usage type. Usage records only,
# so promotional credits do not hide the real number.
aws ce get-cost-and-usage --region us-east-1 \
  --time-period Start=$(date -u -v-2m +%Y-%m-01),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY --metrics UnblendedCost \
  --filter '{"Dimensions":{"Key":"RECORD_TYPE","Values":["Usage"]}}' \
  --group-by Type=DIMENSION,Key=SERVICE --output table

# Swap the group-by for USAGE_TYPE once you know the service.
```

```bash
# Any Lambda version with SnapStart still enabled anywhere.
for r in us-east-1 us-east-2; do
  aws lambda list-functions --region $r --query 'Functions[].FunctionName' --output text |
  tr '\t' '\n' | while read -r f; do
    aws lambda list-versions-by-function --region "$r" --function-name "$f" \
      --query "Versions[?SnapStart.ApplyOn!='None'].[?Version!='\$LATEST'] | []" --output text
  done
done

# Confirm retention is still set on every log group.
aws logs describe-log-groups --region us-east-1 \
  --query 'logGroups[].{name:logGroupName,retention:retentionInDays}' --output table
```

A log group showing `None` for retention is a regression — fix the template
rather than setting it by hand, or the next deploy loses it.

## Other stacks in this account

The account also carries `syncbot-test` / `syncbot-prod` in **us-east-2**, which
are not part of slack-stack. They contribute the small `USE2-*` S3 lines. Free
tier does not pool across regions, so a second region starts its own meters.
