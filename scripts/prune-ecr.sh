#!/usr/bin/env bash
# Keep ECR inside the AWS free tier by expiring superseded Lambda images.
#
# SAM owns these repositories (deploy.sh and CI both pass --resolve-image-repos)
# and never deletes anything, so every deploy leaves its predecessor behind.
# Private ECR is $0.10/GB-month after a 12-month 500 MB allowance, which is the
# one place this stack has actually leaked money. See docs/COST.md.
#
# Lambda needs the image its deployed version references to still exist, so this
# keeps several recent images rather than trimming to one.
#
# Usage:
#   ./scripts/prune-ecr.sh --preview        # what would be expired, no changes
#   ./scripts/prune-ecr.sh                  # apply the policy
#   KEEP=10 ./scripts/prune-ecr.sh          # keep more per repository
#   REPO_FILTER=paxminer ./scripts/prune-ecr.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
KEEP="${KEEP:-5}"
REPO_FILTER="${REPO_FILTER:-}"
PREVIEW=false
[[ "${1:-}" == "--preview" ]] && PREVIEW=true

POLICY=$(cat <<EOF
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Expire untagged images after 1 day.",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 1
      },
      "action": { "type": "expire" }
    },
    {
      "rulePriority": 2,
      "description": "Keep the ${KEEP} most recent images; older ones are superseded deploys.",
      "selection": {
        "tagStatus": "any",
        "countType": "imageCountMoreThan",
        "countNumber": ${KEEP}
      },
      "action": { "type": "expire" }
    }
  ]
}
EOF
)

# Read into an array without mapfile: macOS still ships bash 3.2.
REPOS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && REPOS+=("$line")
done < <(
  aws ecr describe-repositories --region "$REGION" \
    --query 'repositories[].repositoryName' --output text 2>/dev/null | tr '\t' '\n' | sort
)

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "No ECR repositories found in ${REGION}." >&2
  exit 0
fi

total_before=0
total_expiring=0

for repo in "${REPOS[@]}"; do
  [[ -n "$REPO_FILTER" && "$repo" != *"$REPO_FILTER"* ]] && continue

  bytes=$(aws ecr describe-images --region "$REGION" --repository-name "$repo" \
    --query 'sum(imageDetails[].imageSizeInBytes)' --output text 2>/dev/null || echo 0)
  [[ "$bytes" == "None" || -z "$bytes" ]] && bytes=0
  bytes=${bytes%.*}
  count=$(aws ecr describe-images --region "$REGION" --repository-name "$repo" \
    --query 'length(imageDetails)' --output text 2>/dev/null || echo 0)
  total_before=$((total_before + bytes))

  if [[ "$PREVIEW" == true ]]; then
    id=$(aws ecr start-lifecycle-policy-preview --region "$REGION" \
      --repository-name "$repo" --lifecycle-policy-text "$POLICY" \
      --query 'status' --output text 2>/dev/null || echo FAILED)
    for _ in {1..10}; do
      status=$(aws ecr get-lifecycle-policy-preview --region "$REGION" \
        --repository-name "$repo" --query 'status' --output text 2>/dev/null || echo FAILED)
      [[ "$status" != "IN_PROGRESS" ]] && break
      sleep 1
    done
    expiring=$(aws ecr get-lifecycle-policy-preview --region "$REGION" \
      --repository-name "$repo" --query 'length(previewResults)' --output text 2>/dev/null || echo 0)
    [[ "$expiring" == "None" || -z "$expiring" ]] && expiring=0
    total_expiring=$((total_expiring + expiring))
    printf '%-58s %3s images %6s MB  -> would expire %s\n' \
      "$repo" "$count" "$((bytes / 1048576))" "$expiring"
  else
    aws ecr put-lifecycle-policy --region "$REGION" --repository-name "$repo" \
      --lifecycle-policy-text "$POLICY" >/dev/null
    printf '%-58s %3s images %6s MB  -> policy applied (keep %s)\n' \
      "$repo" "$count" "$((bytes / 1048576))" "$KEEP"
  fi
done

echo "-----"
echo "Current ECR storage: $((total_before / 1048576)) MB"
if [[ "$PREVIEW" == true ]]; then
  echo "Images that would be expired: ${total_expiring}"
  echo "Re-run without --preview to apply."
else
  echo "Policies applied. ECR evaluates them asynchronously, usually within 24 hours."
fi
