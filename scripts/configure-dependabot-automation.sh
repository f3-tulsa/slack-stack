#!/usr/bin/env bash
# One-time repo admin setup for dependabot auto-merge on main/test/prod.
# Requires: gh auth with admin permissions on f3-tulsa/slack-stack
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
MAIN_RULESET_ID="${MAIN_RULESET_ID:-15405638}"
PROD_RULESET_ID="${PROD_RULESET_ID:-15405317}"
TEST_RULESET_ID="${TEST_RULESET_ID:-15405493}"

echo "Configuring ${REPO}..."

gh api "repos/${REPO}" -X PATCH -f allow_auto_merge=true
echo "Enabled allow_auto_merge"

patch_ruleset() {
  local ruleset_id="$1"
  local ruleset_name="$2"
  gh api "repos/${REPO}/rulesets/${ruleset_id}" > "/tmp/ruleset-${ruleset_id}.json"
  python3 <<PY
import json

ruleset_id = "${ruleset_id}"
ruleset_name = "${ruleset_name}"
with open(f"/tmp/ruleset-{ruleset_id}.json") as f:
    data = json.load(f)

checks = [
    {"context": "test"},
    {"context": "pip-audit"},
    {"context": "sam-lint"},
    {"context": "requirements-sync"},
    # Kept until Weaselbot is removed from main; CI emits a no-op job with this name.
    {"context": "weaselbot-requirements-sync"},
    {"context": "pre-commit"},
]

keep_types = {"deletion", "non_fast_forward", "pull_request"}
rules = [r for r in data["rules"] if r["type"] in keep_types and r["type"] != "required_status_checks"]
rules = [r for r in rules if r["type"] != "required_status_checks"]

if not any(r["type"] == "pull_request" for r in rules):
    rules.append({
        "type": "pull_request",
        "parameters": {
            "allowed_merge_methods": ["merge", "squash", "rebase"],
            "dismiss_stale_reviews_on_push": True,
            "dismissal_restriction": {"allowed_actors": [], "enabled": False},
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_approving_review_count": 0,
            "required_review_thread_resolution": False,
            "required_reviewers": [],
        },
    })

rules.append({
    "type": "required_status_checks",
    "parameters": {
        "strict_required_status_checks_policy": False,
        "required_status_checks": checks,
    },
})

payload = {
    "name": data["name"],
    "target": data["target"],
    "enforcement": data["enforcement"],
    "conditions": data["conditions"],
    "rules": rules,
}
with open(f"/tmp/ruleset-patch-{ruleset_id}.json", "w") as f:
    json.dump(payload, f)
print(f"Prepared {ruleset_name} ({ruleset_id})")
PY
  gh api "repos/${REPO}/rulesets/${ruleset_id}" -X PUT --input "/tmp/ruleset-patch-${ruleset_id}.json"
  echo "Updated ${ruleset_name} ruleset (${ruleset_id})"
}

patch_ruleset "$MAIN_RULESET_ID" "Main Set"
patch_ruleset "$PROD_RULESET_ID" "Prod Set"
patch_ruleset "$TEST_RULESET_ID" "Test Set"

gh label create dependency-major --repo "$REPO" --color "B60205" --force 2>/dev/null || true
echo "Ensured dependency-major label exists"

echo "Done. Next steps:"
echo "  1. Merge chore/reconcile-main-trunk -> main"
echo "  2. Align prod and test to main via promotion PRs"
echo "  3. Validate minor/patch auto-merge and major test-first flow"
