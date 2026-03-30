#!/usr/bin/env bash
# Local SAM deploy for one or all stacks. Mirrors .github/workflows/deploy.yml.
#
# Usage:
#   ./deploy.sh --env test                    # deploy all stacks
#   ./deploy.sh --env prod --stack paxminer
#   ./deploy.sh --env test --build-only
#   ./deploy.sh --env test --bootstrap      # OIDC + deploy bucket (then deploy)
#   ./deploy.sh --env test --setup-github   # after deploy: push env to GitHub
#   ./deploy.sh --env test --bootstrap --setup-github
#
# Env file: .env.deploy.<env> (e.g. .env.deploy.test). Copy from .env.deploy.example.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ENV_NAME=""
STACK="all"
BUILD_ONLY=false
CONFIRM=false
DO_BOOTSTRAP=false
DO_SETUP_GITHUB=false

usage() {
  cat <<EOF
Usage: $0 --env test|prod [options]

Options:
  --stack paxminer|weaselbot|slackblast|qsignups   default: all
  --build-only
  --confirm              prompt for SAM changeset confirmation
  --bootstrap            deploy infra/template.bootstrap.yaml (OIDC + SAM artifact bucket), then continue
  --setup-github         create/update GitHub environment (same name as --env) with vars/secrets from this env file (needs gh CLI)

Env file: .env.deploy.<env>  (copy from .env.deploy.example)
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENV_NAME="${2:-}"
      shift 2
      ;;
    --stack)
      STACK="${2:-}"
      shift 2
      ;;
    --build-only)
      BUILD_ONLY=true
      shift
      ;;
    --confirm)
      CONFIRM=true
      shift
      ;;
    --bootstrap)
      DO_BOOTSTRAP=true
      shift
      ;;
    --setup-github)
      DO_SETUP_GITHUB=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $1"
      usage
      ;;
  esac
done

[[ -n "$ENV_NAME" ]] || usage

ENV_FILE="$ROOT/.env.deploy.$ENV_NAME"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.deploy.example and fill in values."
  exit 1
fi
# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

case "$ENV_NAME" in
  test|prod) ;;
  *)
    echo "Error: --env must be test or prod (got '$ENV_NAME')" >&2
    exit 1
    ;;
esac
export STAGE="$ENV_NAME"

BOOTSTRAP_STACK_NAME="${BOOTSTRAP_STACK_NAME:-slack-stack-bootstrap}"
CREATE_OIDC_PROVIDER="${CREATE_OIDC_PROVIDER:-true}"
DEPLOY_BUCKET_PREFIX="${DEPLOY_BUCKET_PREFIX:-slack-stack-deploy}"

prereq_hint() {
  local cmd="$1"
  case "$cmd" in
    aws)
      echo "Install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
      ;;
    sam)
      echo "Install AWS SAM CLI: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
      ;;
    docker)
      echo "Install Docker (required for Weaselbot image builds): https://docs.docker.com/get-docker/"
      ;;
    python3)
      echo "Install Python 3 (used for manifest URL substitution)."
      ;;
    gh)
      echo "Install GitHub CLI: https://cli.github.com/"
      ;;
  esac
}

require_cmd() {
  local c="$1"
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "Error: required command '$c' not found." >&2
    prereq_hint "$c" >&2
    exit 1
  fi
}

# owner/repo from git remote origin (https or ssh)
github_owner_repo_from_origin() {
  local git_dir="$1"
  local url or
  url="$(git -C "$git_dir" remote get-url origin 2>/dev/null || true)"
  [[ -n "$url" ]] || return 1
  url="${url%.git}"
  url="${url%/}"
  if [[ "$url" =~ ^git@github\.com:([^/]+)/(.+)$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    return 0
  fi
  if [[ "$url" =~ ^ssh://git@github\.com/([^/]+)/(.+)$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    return 0
  fi
  if [[ "$url" =~ ^https://([^/@]+@)?github\.com/([^/]+)/([^/]+)$ ]]; then
    echo "${BASH_REMATCH[2]}/${BASH_REMATCH[3]}"
    return 0
  fi
  return 1
}

cf_output() {
  local stack="$1" key="$2"
  aws cloudformation describe-stacks --stack-name "$stack" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue | [0]" --output text 2>/dev/null || true
}

run_bootstrap() {
  local github_repo tmpl
  tmpl="$ROOT/infra/template.bootstrap.yaml"
  if [[ ! -f "$tmpl" ]]; then
    echo "Error: bootstrap template not found: $tmpl" >&2
    exit 1
  fi
  if ! github_repo="$(github_owner_repo_from_origin "$ROOT")"; then
    echo "Error: could not parse owner/repo from git remote 'origin'. Set a GitHub remote or run from the repo root." >&2
    exit 1
  fi
  echo "=== Bootstrap (CloudFormation) ==="
  echo "GitHub repository (OIDC trust): $github_repo"
  echo "Stack: $BOOTSTRAP_STACK_NAME  Region: $AWS_REGION"
  aws cloudformation deploy \
    --template-file "$tmpl" \
    --stack-name "$BOOTSTRAP_STACK_NAME" \
    --parameter-overrides \
      "GitHubRepository=$github_repo" \
      "CreateOIDCProvider=$CREATE_OIDC_PROVIDER" \
      "DeploymentBucketPrefix=$DEPLOY_BUCKET_PREFIX" \
      "AppImageBucketName=${IMAGE_S3_BUCKET:-}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --region "$AWS_REGION"

  local role bucket
  role="$(cf_output "$BOOTSTRAP_STACK_NAME" "GitHubDeployRoleArn")"
  bucket="$(cf_output "$BOOTSTRAP_STACK_NAME" "DeploymentBucketName")"
  echo "Bootstrap outputs:"
  echo "  GitHubDeployRoleArn: $role"
  echo "  DeploymentBucketName: $bucket"
  if [[ -n "$bucket" && "$bucket" != "None" ]]; then
    export _BOOTSTRAP_DEPLOY_BUCKET="$bucket"
  fi
  if [[ -n "$role" && "$role" != "None" ]]; then
    export _BOOTSTRAP_ROLE_ARN="$role"
  fi
}

run_setup_github() {
  local repo role_arn
  require_cmd gh
  if ! gh auth status >/dev/null 2>&1; then
    echo "Error: gh is not authenticated. Run: gh auth login" >&2
    exit 1
  fi
  if ! repo="$(github_owner_repo_from_origin "$ROOT")"; then
    echo "Error: could not parse owner/repo from git remote 'origin'." >&2
    exit 1
  fi
  role_arn="${AWS_ROLE_ARN:-}"
  if [[ -z "$role_arn" ]]; then
    role_arn="$(cf_output "$BOOTSTRAP_STACK_NAME" "GitHubDeployRoleArn")"
  fi
  if [[ -z "$role_arn" || "$role_arn" == "None" ]]; then
    echo "Error: AWS_ROLE_ARN not set and bootstrap stack output GitHubDeployRoleArn not found." >&2
    exit 1
  fi

  echo "=== GitHub environment: $STAGE (repo $repo) ==="
  gh api -X PUT "repos/$repo/environments/$STAGE" >/dev/null
  echo "Ensured environment '$STAGE'."

  gh variable set STAGE --env "$STAGE" --body "$STAGE" -R "$repo"
  gh variable set AWS_REGION --env "$STAGE" --body "$AWS_REGION" -R "$repo"
  gh variable set PAXMINER_SCHEMA --env "$STAGE" --body "$PAXMINER_SCHEMA" -R "$repo"
  gh variable set WEASELBOT_SCHEMA --env "$STAGE" --body "$WEASELBOT_SCHEMA" -R "$repo"
  gh variable set SLACKBLAST_SCHEMA --env "$STAGE" --body "$SLACKBLAST_SCHEMA" -R "$repo"
  gh variable set QSIGNUPS_SCHEMA --env "$STAGE" --body "$QSIGNUPS_SCHEMA" -R "$repo"
  gh variable set IMAGE_S3_BUCKET --env "$STAGE" --body "$IMAGE_S3_BUCKET" -R "$repo"
  gh variable set DATABASE_TLS_ENABLED --env "$STAGE" --body "$DATABASE_TLS_ENABLED" -R "$repo"
  gh variable set F3_REGION_NAME --env "$STAGE" --body "$F3_REGION_NAME" -R "$repo"
  gh variable set F3_REGION_SLACK_TEAM_ID --env "$STAGE" --body "$F3_REGION_SLACK_TEAM_ID" -R "$repo"
  echo "Set GitHub Actions variables for environment '$STAGE'."

  gh secret set AWS_ROLE_ARN --env "$STAGE" --body "$role_arn" -R "$repo"
  gh secret set DATABASE_HOST --env "$STAGE" --body "$DATABASE_HOST" -R "$repo"
  gh secret set DATABASE_PORT --env "$STAGE" --body "$DATABASE_PORT" -R "$repo"
  gh secret set DATABASE_USER --env "$STAGE" --body "$DATABASE_USER" -R "$repo"
  gh secret set DATABASE_PASSWORD --env "$STAGE" --body "$DATABASE_PASSWORD" -R "$repo"
  gh secret set DB_ENCRYPTION_KEY --env "$STAGE" --body "$DB_ENCRYPTION_KEY" -R "$repo"
  gh secret set PM_SLACK_TOKEN --env "$STAGE" --body "$PM_SLACK_TOKEN" -R "$repo"
  gh secret set WB_SLACK_TOKEN --env "$STAGE" --body "$WB_SLACK_TOKEN" -R "$repo"
  gh secret set SB_SLACK_TOKEN --env "$STAGE" --body "$SB_SLACK_TOKEN" -R "$repo"
  gh secret set SB_SLACK_SIGNING_SECRET --env "$STAGE" --body "$SB_SLACK_SIGNING_SECRET" -R "$repo"
  gh secret set SB_SLACK_CLIENT_ID --env "$STAGE" --body "$SB_SLACK_CLIENT_ID" -R "$repo"
  gh secret set SB_SLACK_CLIENT_SECRET --env "$STAGE" --body "$SB_SLACK_CLIENT_SECRET" -R "$repo"
  gh secret set SB_STRAVA_CLIENT_ID --env "$STAGE" --body "$SB_STRAVA_CLIENT_ID" -R "$repo"
  gh secret set SB_STRAVA_CLIENT_SECRET --env "$STAGE" --body "$SB_STRAVA_CLIENT_SECRET" -R "$repo"
  gh secret set QS_SLACK_TOKEN --env "$STAGE" --body "$QS_SLACK_TOKEN" -R "$repo"
  gh secret set QS_SLACK_SIGNING_SECRET --env "$STAGE" --body "$QS_SLACK_SIGNING_SECRET" -R "$repo"
  gh secret set QS_SLACK_CLIENT_ID --env "$STAGE" --body "$QS_SLACK_CLIENT_ID" -R "$repo"
  gh secret set QS_SLACK_CLIENT_SECRET --env "$STAGE" --body "$QS_SLACK_CLIENT_SECRET" -R "$repo"
  [[ -n "${QS_GOOGLE_CLIENT_ID:-}" ]] && gh secret set QS_GOOGLE_CLIENT_ID --env "$STAGE" --body "$QS_GOOGLE_CLIENT_ID" -R "$repo"
  [[ -n "${QS_GOOGLE_CLIENT_SECRET:-}" ]] && gh secret set QS_GOOGLE_CLIENT_SECRET --env "$STAGE" --body "$QS_GOOGLE_CLIENT_SECRET" -R "$repo"
  echo "Set GitHub Actions secrets for environment '$STAGE'."
}

: "${AWS_REGION:?Set AWS_REGION in $ENV_FILE}"

require_cmd aws
require_cmd sam
require_cmd python3

needs_docker() {
  case "$STACK" in
    all|weaselbot) return 0 ;;
    *) return 1 ;;
  esac
}

if needs_docker; then
  require_cmd docker
fi

if [[ "$DO_BOOTSTRAP" == true ]]; then
  run_bootstrap
fi

: "${DATABASE_HOST:?}"
: "${DATABASE_PORT:?}"
: "${DATABASE_USER:?}"
: "${DATABASE_PASSWORD:?}"
: "${DATABASE_TLS_ENABLED:?}"
: "${DB_ENCRYPTION_KEY:?}"
if [[ "${#DB_ENCRYPTION_KEY}" -lt 16 ]]; then
  echo "ERROR: DB_ENCRYPTION_KEY must be at least 16 characters (got ${#DB_ENCRYPTION_KEY})" >&2
  exit 1
fi
: "${PAXMINER_SCHEMA:?}"
: "${WEASELBOT_SCHEMA:?}"
: "${SLACKBLAST_SCHEMA:?}"
: "${QSIGNUPS_SCHEMA:?}"
: "${IMAGE_S3_BUCKET:?}"

: "${SB_SLACK_TOKEN:?}"
: "${SB_SLACK_SIGNING_SECRET:?}"
: "${SB_SLACK_CLIENT_ID:?}"
: "${SB_SLACK_CLIENT_SECRET:?}"
: "${SB_STRAVA_CLIENT_ID:?}"
: "${SB_STRAVA_CLIENT_SECRET:?}"

: "${QS_SLACK_TOKEN:?}"
: "${QS_SLACK_SIGNING_SECRET:?}"
: "${QS_SLACK_CLIENT_ID:?}"
: "${QS_SLACK_CLIENT_SECRET:?}"

: "${F3_REGION_NAME:?}"
: "${PM_SLACK_TOKEN:?}"
: "${WB_SLACK_TOKEN:?}"
: "${F3_REGION_SLACK_TEAM_ID:?}"

export AWS_DEFAULT_REGION="${AWS_REGION}"

SAM_DEPLOY_EXTRA=()
if [[ "$CONFIRM" == false ]]; then
  SAM_DEPLOY_EXTRA+=(--no-confirm-changeset)
fi
SAM_DEPLOY_EXTRA+=(--no-fail-on-empty-changeset --capabilities CAPABILITY_IAM)

# SAM artifact bucket: bootstrap output this run, or DEPLOYMENT_S3_BUCKET from env, else --resolve-s3
SAM_S3_BUCKET_ARGS=()
if [[ -n "${DEPLOYMENT_S3_BUCKET:-}" ]]; then
  SAM_S3_BUCKET_ARGS=(--s3-bucket "$DEPLOYMENT_S3_BUCKET")
elif [[ -n "${_BOOTSTRAP_DEPLOY_BUCKET:-}" ]]; then
  SAM_S3_BUCKET_ARGS=(--s3-bucket "$_BOOTSTRAP_DEPLOY_BUCKET")
else
  SAM_S3_BUCKET_ARGS=(--resolve-s3)
fi

mkdir -p "$ROOT/receipts"
RECEIPT_FILE="$ROOT/receipts/deploy-${STAGE}-$(date +%Y%m%d-%H%M%S).txt"

log_receipt() {
  echo "$@" | tee -a "$RECEIPT_FILE"
}

{
  echo "=== Deploy receipt ==="
  echo "Started (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Stage: ${STAGE}"
  echo "AWS region: ${AWS_REGION}"
  echo "Env file: ${ENV_FILE}"
  echo "Stack selection: ${STACK}"
  echo "Build only: ${BUILD_ONLY}"
  echo "Bootstrap: ${DO_BOOTSTRAP}"
  echo "Setup GitHub: ${DO_SETUP_GITHUB}"
  echo ""
} | tee "$RECEIPT_FILE"

# -1 = not run, 0 = success, >0 = failure
PAX_RC=-1
WEASEL_RC=-1
SB_RC=-1
QS_RC=-1

get_stack_output() {
  local stack_name="$1" output_key="$2"
  local val
  val="$(aws cloudformation describe-stacks --stack-name "$stack_name" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue | [0]" --output text 2>/dev/null || true)"
  if [[ -z "$val" || "$val" == "None" ]]; then
    echo ""
  else
    echo "$val"
  fi
}

# Full events URL from CF -> API base (scheme + host + stage), e.g. .../Prod
api_base_from_events_url() {
  local url="$1"
  [[ -z "$url" ]] && { echo ""; return; }
  # Strip /slack/events and any trailing slashes
  url="${url%%/slack/events*}"
  while [[ "$url" == */ ]]; do url="${url%/}"; done
  echo "$url"
}

write_stage_manifest_copy() {
  local app_rel="$1"
  local src="$ROOT/$app_rel/manifest.json"
  local dst="$ROOT/$app_rel/manifest-${STAGE}.json"
  [[ -f "$src" ]] || return 1
  cp "$src" "$dst"
  log_receipt "Wrote stage manifest (copy): $dst"
}

write_stage_manifest_subst() {
  local app_rel="$1"
  local base_url="$2"
  local src="$ROOT/$app_rel/manifest.json"
  local dst="$ROOT/$app_rel/manifest-${STAGE}.json"
  [[ -f "$src" ]] || return 1
  [[ -n "$base_url" ]] || return 1
  python3 - "$src" "$dst" "$base_url" <<'PY'
import pathlib
import sys

src, dst, base = sys.argv[1], sys.argv[2], sys.argv[3]
pathlib.Path(dst).write_text(pathlib.Path(src).read_text(encoding="utf-8").replace("__HOSTNAME__", base))
PY
  log_receipt "Wrote stage manifest (URLs): $dst"
}

deploy_paxminer() {
  sam build -t PAXminer/template.yaml 2>&1 | tee -a "$RECEIPT_FILE"
  local brc="${PIPESTATUS[0]}"
  if [[ "$brc" -ne 0 ]]; then return "$brc"; fi
  [[ "$BUILD_ONLY" == true ]] && return 0
  sam deploy \
    --stack-name "paxminer-${STAGE}" \
    "${SAM_DEPLOY_EXTRA[@]}" \
    "${SAM_S3_BUCKET_ARGS[@]}" \
    --parameter-overrides \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DatabaseTlsEnabled=${DATABASE_TLS_ENABLED}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      "PaxminerSchema=${PAXMINER_SCHEMA}_${STAGE}" \
      "Stage=${STAGE}" \
      "F3RegionName=${F3_REGION_NAME}" \
      "PmSlackToken=${PM_SLACK_TOKEN}" \
    2>&1 | tee -a "$RECEIPT_FILE"
  return "${PIPESTATUS[0]}"
}

deploy_weaselbot() {
  sam build -t weaselbot/template.yaml 2>&1 | tee -a "$RECEIPT_FILE"
  local brc="${PIPESTATUS[0]}"
  if [[ "$brc" -ne 0 ]]; then return "$brc"; fi
  [[ "$BUILD_ONLY" == true ]] && return 0
  sam deploy \
    --stack-name "weaselbot-${STAGE}" \
    "${SAM_DEPLOY_EXTRA[@]}" \
    --resolve-image-repos \
    "${SAM_S3_BUCKET_ARGS[@]}" \
    --parameter-overrides \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DatabaseTlsEnabled=${DATABASE_TLS_ENABLED}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      "PaxminerSchema=${PAXMINER_SCHEMA}_${STAGE}" \
      "WeaselbotSchema=${WEASELBOT_SCHEMA}_${STAGE}" \
      "Stage=${STAGE}" \
      "F3RegionName=${F3_REGION_NAME}" \
      "WbSlackToken=${WB_SLACK_TOKEN}" \
      "F3RegionSlackTeamId=${F3_REGION_SLACK_TEAM_ID}" \
    2>&1 | tee -a "$RECEIPT_FILE"
  return "${PIPESTATUS[0]}"
}

deploy_slackblast() {
  sam build -t slackblast/template.yaml 2>&1 | tee -a "$RECEIPT_FILE"
  local brc="${PIPESTATUS[0]}"
  if [[ "$brc" -ne 0 ]]; then return "$brc"; fi
  [[ "$BUILD_ONLY" == true ]] && return 0
  sam deploy \
    --stack-name "slackblast-${STAGE}" \
    "${SAM_DEPLOY_EXTRA[@]}" \
    "${SAM_S3_BUCKET_ARGS[@]}" \
    --parameter-overrides \
      "Stage=${STAGE}" \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DatabaseTlsEnabled=${DATABASE_TLS_ENABLED}" \
      "DatabaseSchema=${SLACKBLAST_SCHEMA}_${STAGE}" \
      "PaxminerSchema=${PAXMINER_SCHEMA}_${STAGE}" \
      "SlackToken=${SB_SLACK_TOKEN}" \
      "SlackSigningSecret=${SB_SLACK_SIGNING_SECRET}" \
      "SlackClientSecret=${SB_SLACK_CLIENT_SECRET}" \
      "SlackClientId=${SB_SLACK_CLIENT_ID}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      "StravaClientID=${SB_STRAVA_CLIENT_ID}" \
      "StravaClientSecret=${SB_STRAVA_CLIENT_SECRET}" \
      "ImageBucketName=${IMAGE_S3_BUCKET}" \
    2>&1 | tee -a "$RECEIPT_FILE"
  return "${PIPESTATUS[0]}"
}

deploy_qsignups() {
  sam build -t qsignups/template.yaml 2>&1 | tee -a "$RECEIPT_FILE"
  local brc="${PIPESTATUS[0]}"
  if [[ "$brc" -ne 0 ]]; then return "$brc"; fi
  [[ "$BUILD_ONLY" == true ]] && return 0
  local google_overrides=""
  [[ -n "${QS_GOOGLE_CLIENT_ID:-}" ]] && google_overrides+=" GoogleClientId=${QS_GOOGLE_CLIENT_ID}"
  [[ -n "${QS_GOOGLE_CLIENT_SECRET:-}" ]] && google_overrides+=" GoogleClientSecret=${QS_GOOGLE_CLIENT_SECRET}"
  sam deploy \
    --stack-name "qsignups-${STAGE}" \
    "${SAM_DEPLOY_EXTRA[@]}" \
    "${SAM_S3_BUCKET_ARGS[@]}" \
    --parameter-overrides \
      "Stage=${STAGE}" \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DatabaseTlsEnabled=${DATABASE_TLS_ENABLED}" \
      "DatabaseSchema=${QSIGNUPS_SCHEMA}_${STAGE}" \
      "SlackToken=${QS_SLACK_TOKEN}" \
      "SlackSigningSecret=${QS_SLACK_SIGNING_SECRET}" \
      "SlackClientSecret=${QS_SLACK_CLIENT_SECRET}" \
      "SlackClientId=${QS_SLACK_CLIENT_ID}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      ${google_overrides} \
    2>&1 | tee -a "$RECEIPT_FILE"
  return "${PIPESTATUS[0]}"
}

case "$STACK" in
  all)
    deploy_paxminer; PAX_RC=$?
    deploy_weaselbot; WEASEL_RC=$?
    deploy_slackblast; SB_RC=$?
    deploy_qsignups; QS_RC=$?
    ;;
  paxminer)
    deploy_paxminer; PAX_RC=$?
    ;;
  weaselbot)
    deploy_weaselbot; WEASEL_RC=$?
    ;;
  slackblast)
    deploy_slackblast; SB_RC=$?
    ;;
  qsignups)
    deploy_qsignups; QS_RC=$?
    ;;
  *)
    echo "Unknown stack: $STACK"
    usage
    ;;
esac

if [[ "$BUILD_ONLY" == true ]]; then
  log_receipt ""
  log_receipt "=== Summary (build-only) ==="
  log_receipt "Receipt file: ${RECEIPT_FILE}"
  log_receipt "Build-only complete."
  if [[ "$DO_SETUP_GITHUB" == true ]]; then
    run_setup_github 2>&1 | tee -a "$RECEIPT_FILE"
    gh_setup_rc="${PIPESTATUS[0]}"
    [[ "$gh_setup_rc" -eq 0 ]] || exit "$gh_setup_rc"
  fi
  exit 0
fi

log_receipt ""
log_receipt "--- Stack outputs (CloudFormation) ---"
for name in "paxminer-${STAGE}" "weaselbot-${STAGE}" "slackblast-${STAGE}" "qsignups-${STAGE}"; do
  if aws cloudformation describe-stacks --stack-name "$name" --region "$AWS_REGION" &>/dev/null; then
    log_receipt "# $name"
    aws cloudformation describe-stacks --stack-name "$name" --region "$AWS_REGION" \
      --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text 2>/dev/null | tee -a "$RECEIPT_FILE" || true
  fi
done

log_receipt ""
log_receipt "--- Stage-specific Slack manifests ---"
SB_API_URL=""
QS_API_URL=""
if [[ "$SB_RC" -eq 0 ]]; then
  SB_FULL="$(get_stack_output "slackblast-${STAGE}" "SlackblastApi")"
  SB_API_URL="$(api_base_from_events_url "$SB_FULL")"
  if [[ -n "$SB_API_URL" ]]; then
    write_stage_manifest_subst "slackblast" "$SB_API_URL" || log_receipt "WARN: could not write slackblast manifest-${STAGE}.json"
  else
    log_receipt "WARN: SlackblastApi output missing; skip slackblast manifest generation"
  fi
fi
if [[ "$QS_RC" -eq 0 ]]; then
  QS_FULL="$(get_stack_output "qsignups-${STAGE}" "QSignupsApi")"
  QS_API_URL="$(api_base_from_events_url "$QS_FULL")"
  if [[ -n "$QS_API_URL" ]]; then
    write_stage_manifest_subst "qsignups" "$QS_API_URL" || log_receipt "WARN: could not write qsignups manifest-${STAGE}.json"
  else
    log_receipt "WARN: QSignupsApi output missing; skip qsignups manifest generation"
  fi
fi
if [[ "$PAX_RC" -eq 0 ]]; then
  write_stage_manifest_copy "PAXminer" || log_receipt "WARN: could not write PAXminer manifest-${STAGE}.json"
fi
if [[ "$WEASEL_RC" -eq 0 ]]; then
  write_stage_manifest_copy "weaselbot" || log_receipt "WARN: could not write weaselbot manifest-${STAGE}.json"
fi

log_receipt ""
log_receipt "=== Deploy summary ==="
printf '%-22s %s\n' "Stack" "Status" | tee -a "$RECEIPT_FILE"
printf '%-22s %s\n' "--------------------" "------" | tee -a "$RECEIPT_FILE"
summarize_row() {
  local label="$1" rc="$2"
  local st
  if [[ "$rc" -lt 0 ]]; then st="(not run)"; elif [[ "$rc" -eq 0 ]]; then st="success"; else st="FAILED (exit $rc)"; fi
  printf '%-22s %s\n' "$label" "$st" | tee -a "$RECEIPT_FILE"
}
summarize_row "paxminer-${STAGE}" "$PAX_RC"
summarize_row "weaselbot-${STAGE}" "$WEASEL_RC"
summarize_row "slackblast-${STAGE}" "$SB_RC"
summarize_row "qsignups-${STAGE}" "$QS_RC"
log_receipt ""
log_receipt "API base URLs (for Slack manifests):"
log_receipt "  slackblast: ${SB_API_URL:-n/a}"
log_receipt "  qsignups:   ${QS_API_URL:-n/a}"
log_receipt ""
log_receipt "Receipt file: ${RECEIPT_FILE}"

ANY_FAIL=0
for rc in "$PAX_RC" "$WEASEL_RC" "$SB_RC" "$QS_RC"; do
  if [[ "$rc" -gt 0 ]]; then ANY_FAIL=1; break; fi
done

if [[ "$ANY_FAIL" -eq 1 ]]; then
  log_receipt "Done with failures."
  exit 1
fi

if [[ "$DO_SETUP_GITHUB" == true ]]; then
  run_setup_github 2>&1 | tee -a "$RECEIPT_FILE"
  gh_setup_rc="${PIPESTATUS[0]}"
  [[ "$gh_setup_rc" -eq 0 ]] || exit "$gh_setup_rc"
fi

log_receipt "Done."
exit 0
