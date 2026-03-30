#!/usr/bin/env bash
# Local SAM deploy for one or all stacks. Mirrors .github/workflows/deploy.yml.
# Usage:
#   ./deploy.sh --env test              # deploy all stacks
#   ./deploy.sh --env prod --stack paxminer
#   ./deploy.sh --env test --build-only
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ENV_NAME=""
STACK="all"
BUILD_ONLY=false
CONFIRM=false

usage() {
  echo "Usage: $0 --env test|prod [--stack paxminer|weaselbot|slackblast|qsignups] [--build-only] [--confirm]"
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

ENV_FILE="$ROOT/.env.$ENV_NAME"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.example and fill in values."
  exit 1
fi
# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

: "${AWS_REGION:?Set AWS_REGION in $ENV_FILE}"
: "${STAGE:?Set STAGE (test or prod) in $ENV_FILE}"
: "${DATABASE_HOST:?}"
: "${DATABASE_PORT:?}"
: "${DATABASE_USER:?}"
: "${DATABASE_PASSWORD:?}"
: "${DB_ENCRYPTION_KEY:?}"
: "${PAXMINER_SCHEMA:?}"
: "${WEASELBOT_SCHEMA:?}"
: "${SLACKBLAST_SCHEMA:?}"
: "${QSIGNUPS_SCHEMA:?}"
: "${IMAGE_S3_BUCKET:?}"

: "${SB_SLACK_TOKEN:?}"
: "${SB_SLACK_SIGNING_SECRET:?}"
: "${SB_SLACK_CLIENT_SECRET:?}"
: "${SB_STRAVA_CLIENT_ID:?}"
: "${SB_STRAVA_CLIENT_SECRET:?}"

: "${QS_SLACK_TOKEN:?}"
: "${QS_SLACK_SIGNING_SECRET:?}"
: "${QS_SLACK_CLIENT_SECRET:?}"
: "${QS_GOOGLE_CLIENT_ID:?}"
: "${QS_GOOGLE_CLIENT_SECRET:?}"

export AWS_DEFAULT_REGION="${AWS_REGION}"

SAM_DEPLOY_EXTRA=()
if [[ "$CONFIRM" == false ]]; then
  SAM_DEPLOY_EXTRA+=(--no-confirm-changeset)
fi
SAM_DEPLOY_EXTRA+=(--no-fail-on-empty-changeset --capabilities CAPABILITY_IAM)

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
    --resolve-s3 \
    --parameter-overrides \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      "PaxminerSchema=${PAXMINER_SCHEMA}_${STAGE}" \
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
    --parameter-overrides \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      "PaxminerSchema=${PAXMINER_SCHEMA}_${STAGE}" \
      "WeaselbotSchema=${WEASELBOT_SCHEMA}_${STAGE}" \
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
    --resolve-s3 \
    --parameter-overrides \
      "Stage=${STAGE}" \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DatabaseSchema=${SLACKBLAST_SCHEMA}_${STAGE}" \
      "PaxminerSchema=${PAXMINER_SCHEMA}_${STAGE}" \
      "SlackToken=${SB_SLACK_TOKEN}" \
      "SlackSigningSecret=${SB_SLACK_SIGNING_SECRET}" \
      "SlackClientSecret=${SB_SLACK_CLIENT_SECRET}" \
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
  sam deploy \
    --stack-name "qsignups-${STAGE}" \
    "${SAM_DEPLOY_EXTRA[@]}" \
    --resolve-s3 \
    --parameter-overrides \
      "Stage=${STAGE}" \
      "DatabaseHost=${DATABASE_HOST}" \
      "DatabasePort=${DATABASE_PORT}" \
      "DatabaseUser=${DATABASE_USER}" \
      "DatabasePassword=${DATABASE_PASSWORD}" \
      "DatabaseSchema=${QSIGNUPS_SCHEMA}_${STAGE}" \
      "SlackToken=${QS_SLACK_TOKEN}" \
      "SlackSigningSecret=${QS_SLACK_SIGNING_SECRET}" \
      "SlackClientSecret=${QS_SLACK_CLIENT_SECRET}" \
      "DbEncryptionKey=${DB_ENCRYPTION_KEY}" \
      "GoogleClientId=${QS_GOOGLE_CLIENT_ID}" \
      "GoogleClientSecret=${QS_GOOGLE_CLIENT_SECRET}" \
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

log_receipt "Done."
exit 0
