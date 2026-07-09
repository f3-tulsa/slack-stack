#!/usr/bin/env bash
# One-time repo admin setup for dependabot auto-merge on test.
# Requires: gh auth with admin permissions on f3-tulsa/slack-stack
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
RULESET_ID="${TEST_RULESET_ID:-15405493}"
LOW_SHA="${LOW_SHA:-ccdbdea}"
AUTO_SHA="${AUTO_SHA:-dee6354}"
MAJOR_SHA="${MAJOR_SHA:-0231994}"

echo "Configuring ${REPO}..."

gh api "repos/${REPO}" -X PATCH -f allow_auto_merge=true
echo "Enabled allow_auto_merge"

gh api "repos/${REPO}/rulesets/${RULESET_ID}" > /tmp/test-ruleset.json
python3 <<'PY'
import json
with open("/tmp/test-ruleset.json") as f:
    data = json.load(f)
checks = [
    {"context": "test"},
    {"context": "pip-audit"},
    {"context": "sam-lint"},
    {"context": "requirements-sync"},
    {"context": "weaselbot-requirements-sync"},
    {"context": "pre-commit"},
]
rules = [r for r in data["rules"] if r["type"] != "required_status_checks"]
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
with open("/tmp/test-ruleset-patch.json", "w") as f:
    json.dump(payload, f)
PY

gh api "repos/${REPO}/rulesets/${RULESET_ID}" -X PUT --input /tmp/test-ruleset-patch.json
echo "Updated Test Set ruleset with required CI checks"

LOW_RISK=(7 8 9 10 11 12 14 15 16 18 19 21 22 23 25 26 29 31 32 34 35 36 59 67 69)
for n in "${LOW_RISK[@]}"; do
  gh pr close "$n" --repo "$REPO" \
    --comment "Superseded by low-risk consolidation on test (${LOW_SHA}, ${AUTO_SHA})." \
    --delete-branch || true
done

HIGH_RISK=(13 20 33 50 66 70)
if [[ -n "$MAJOR_SHA" ]]; then
  for n in "${HIGH_RISK[@]}"; do
    gh pr close "$n" --repo "$REPO" \
      --comment "Superseded by test-first majors PR (${MAJOR_SHA})." \
      --delete-branch || true
  done
fi

echo "Done. Open/merge PRs:"
echo "  1. chore/deps-consolidation-and-automation -> test"
echo "  2. chore/deps-major-bumps -> test (after #1 merges)"
