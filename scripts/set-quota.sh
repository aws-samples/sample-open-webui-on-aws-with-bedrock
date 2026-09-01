#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Operator CLI for the metering admin API (docs/METERING.md).
#
#   set-quota.sh policy DEFAULT --hard 5 --soft 4 --rpm 30
#   set-quota.sh policy 'GROUP#power-users' --hard 25
#   set-quota.sh usage <cognito-sub> [--window YYYY-MM]
#   set-quota.sh override <cognito-sub> --hard 20
#   set-quota.sh reset <cognito-sub> [--window YYYY-MM]
#
# Auth: a Cognito access token for a user in an admin group, via $METERING_TOKEN
# or --token. The API URL comes from the OpenWebUI-Metering stack output
# AdminApiUrl, via $METERING_API or --api.
set -euo pipefail

API="${METERING_API:-}"
TOKEN="${METERING_TOKEN:-}"
CMD="${1:-}"; shift || true
TARGET="${1:-}"; [[ "$TARGET" == --* ]] || shift || true

HARD=""; SOFT=""; RPM=""; WINDOW=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --hard)   HARD="$2"; shift 2;;
    --soft)   SOFT="$2"; shift 2;;
    --rpm)    RPM="$2"; shift 2;;
    --window) WINDOW="$2"; shift 2;;
    --api)    API="$2"; shift 2;;
    --token)  TOKEN="$2"; shift 2;;
    *) echo "unknown option: $1" >&2; exit 1;;
  esac
done

[[ -n "$API" && -n "$TOKEN" ]] || { echo "need METERING_API + METERING_TOKEN (or --api/--token)" >&2; exit 1; }
hdr=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")

body_policy() {
  local json="{\"hard_limit_usd\": ${HARD:?--hard required}"
  [[ -n "$SOFT" ]] && json+=", \"soft_limit_usd\": $SOFT"
  [[ -n "$RPM" ]] && json+=", \"rpm_limit\": $RPM"
  echo "$json}"
}

case "$CMD" in
  policy)
    scope=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$TARGET")
    if [[ -n "$HARD" ]]; then
      curl -sf "${hdr[@]}" -X PUT "$API/policy/$scope" -d "$(body_policy)"
    else
      curl -sf "${hdr[@]}" "$API/policy/$scope"
    fi ;;
  usage)
    curl -sf "${hdr[@]}" "$API/usage/$TARGET${WINDOW:+?window=$WINDOW}" ;;
  override)
    curl -sf "${hdr[@]}" -X POST "$API/override" -d "{\"sub\": \"$TARGET\", \"hard_limit_usd\": ${HARD:?--hard required}}" ;;
  reset)
    curl -sf "${hdr[@]}" -X POST "$API/counter-reset" -d "{\"sub\": \"$TARGET\"${WINDOW:+, \"window\": \"$WINDOW\"}}" ;;
  *)
    grep '^#   ' "$0" | sed 's/^#   //'; exit 1 ;;
esac
echo
