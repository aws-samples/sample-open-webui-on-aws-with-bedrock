#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ============================================================
# set-model-access.sh — Bulk manage Bedrock model access by group
#
# Usage:
#   ./set-model-access.sh --url URL --token TOKEN --group GROUP --pattern PATTERN [--permission read|write]
#   ./set-model-access.sh --url URL --token TOKEN --list-groups
#   ./set-model-access.sh --url URL --token TOKEN --list-models
#   ./set-model-access.sh --url URL --token TOKEN --show-access MODEL_ID
#
# Examples:
#   # Grant basic-users read access to all Nova models
#   ./set-model-access.sh --url https://oui.example.com --token $TOKEN \
#       --group basic-users --pattern "us.amazon.nova*"
#
#   # Grant power-users read access to ALL Bedrock models
#   ./set-model-access.sh --url https://oui.example.com --token $TOKEN \
#       --group power-users --pattern "*"
#
#   # List all groups and their IDs
#   ./set-model-access.sh --url https://oui.example.com --token $TOKEN --list-groups
#
#   # List all available models
#   ./set-model-access.sh --url https://oui.example.com --token $TOKEN --list-models
#
#   # Show current access grants for a model
#   ./set-model-access.sh --url https://oui.example.com --token $TOKEN \
#       --show-access "us.anthropic.claude-sonnet-4-5-v2:0"
#
# Notes:
#   - TOKEN is an admin user's JWT (copy from browser cookie or API key)
#   - Pattern uses shell glob matching (fnmatch): * matches anything
#   - Access grants are ADDITIVE — running twice for different groups adds both
#   - To get a token: copy the 'token' cookie value from your browser after SSO login
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
BOLD='\033[1m'

usage() {
    sed -n '2,36p' "$0" | sed 's/^# \?//'
    exit 1
}

# Parse args
URL="" TOKEN="" GROUP="" PATTERN="" PERMISSION="read"
LIST_GROUPS=false LIST_MODELS=false SHOW_ACCESS=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --url) URL="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --group) GROUP="$2"; shift 2 ;;
        --pattern) PATTERN="$2"; shift 2 ;;
        --permission) PERMISSION="$2"; shift 2 ;;
        --list-groups) LIST_GROUPS=true; shift ;;
        --list-models) LIST_MODELS=true; shift ;;
        --show-access) SHOW_ACCESS="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

[[ -z "$URL" || -z "$TOKEN" ]] && { echo "Error: --url and --token are required"; usage; }

API="$URL/api/v1"
AUTH="Authorization: Bearer $TOKEN"

api_get() { curl -sf "$API/$1" -H "$AUTH" -H "Content-Type: application/json"; }
api_post() { curl -sf "$API/$1" -H "$AUTH" -H "Content-Type: application/json" -d "$2"; }

# ── List Groups ──
if $LIST_GROUPS; then
    echo -e "${BOLD}Groups:${NC}"
    api_get "groups/" | python3 -c "
import sys, json
groups = json.load(sys.stdin)
for g in groups:
    members = len(g.get('user_ids', []))
    print(f\"  {g['name']:20s}  id={g['id']}  members={members}\")
" 2>/dev/null || echo "  (no groups found or API error)"
    exit 0
fi

# ── List Models ──
if $LIST_MODELS; then
    echo -e "${BOLD}Bedrock Models:${NC}"
    api_get "bedrock/models" | python3 -c "
import sys, json
data = json.load(sys.stdin).get('data', [])
for m in sorted(data, key=lambda x: x['id']):
    print(f\"  {m['id']}\")
print(f\"\n  Total: {len(data)} models\")
" 2>/dev/null || echo "  (error fetching models)"
    exit 0
fi

# ── Show Access ──
if [[ -n "$SHOW_ACCESS" ]]; then
    echo -e "${BOLD}Access grants for: ${SHOW_ACCESS}${NC}"
    # Get access grants via the models API
    api_get "models/" | python3 -c "
import sys, json
models = json.load(sys.stdin).get('data', [])
model_id = '$SHOW_ACCESS'
for m in models:
    if m.get('id') == model_id:
        grants = m.get('access_grants', [])
        if not grants:
            print('  (no access grants — model is public)')
        for g in grants:
            print(f\"  {g.get('principal_type','?'):6s} {g.get('principal_id','?'):40s} {g.get('permission','?')}\")
        sys.exit(0)
print(f'  Model {model_id} not found')
" 2>/dev/null
    exit 0
fi

# ── Set Model Access ──
[[ -z "$GROUP" || -z "$PATTERN" ]] && { echo "Error: --group and --pattern are required"; usage; }

echo -e "${BOLD}Setting model access${NC}"
echo -e "  Group:      ${GREEN}$GROUP${NC}"
echo -e "  Pattern:    ${GREEN}$PATTERN${NC}"
echo -e "  Permission: ${GREEN}$PERMISSION${NC}"
echo ""

# Resolve group name to ID
GROUP_ID=$(api_get "groups/" | python3 -c "
import sys, json
groups = json.load(sys.stdin)
name = '$GROUP'
for g in groups:
    if g['name'] == name:
        print(g['id'])
        sys.exit(0)
print('NOT_FOUND')
" 2>/dev/null)

if [[ "$GROUP_ID" == "NOT_FOUND" || -z "$GROUP_ID" ]]; then
    echo -e "${RED}Error: Group '$GROUP' not found${NC}"
    echo "Available groups:"
    api_get "groups/" | python3 -c "
import sys, json
for g in json.load(sys.stdin):
    print(f\"  {g['name']}\")
" 2>/dev/null
    exit 1
fi

echo -e "  Group ID:   ${YELLOW}$GROUP_ID${NC}"
echo ""

# Get all Bedrock models and filter by pattern
MATCHING_MODELS=$(api_get "bedrock/models" | python3 -c "
import sys, json, fnmatch
data = json.load(sys.stdin).get('data', [])
pattern = '$PATTERN'
for m in sorted(data, key=lambda x: x['id']):
    if fnmatch.fnmatch(m['id'], pattern):
        print(m['id'])
" 2>/dev/null)

if [[ -z "$MATCHING_MODELS" ]]; then
    echo -e "${RED}No models matching pattern '$PATTERN'${NC}"
    exit 1
fi

MODEL_COUNT=$(echo "$MATCHING_MODELS" | wc -l)
echo -e "Matching models (${BOLD}$MODEL_COUNT${NC}):"
echo "$MATCHING_MODELS" | while read -r mid; do echo "  $mid"; done
echo ""

# For each matching model, get existing grants and add the new one
UPDATED=0
FAILED=0

echo "$MATCHING_MODELS" | while read -r MODEL_ID; do
    # Get existing access grants for this model
    EXISTING_GRANTS=$(api_get "models/" | python3 -c "
import sys, json
models = json.load(sys.stdin).get('data', [])
mid = '$MODEL_ID'
for m in models:
    if m.get('id') == mid:
        print(json.dumps(m.get('access_grants', [])))
        sys.exit(0)
print('[]')
" 2>/dev/null || echo "[]")

    # Build new grants list: existing + new group grant (deduplicated)
    NEW_GRANTS=$(python3 -c "
import json
existing = json.loads('$EXISTING_GRANTS')
new_grant = {'principal_type': 'group', 'principal_id': '$GROUP_ID', 'permission': '$PERMISSION'}
# Deduplicate
seen = set()
result = []
for g in existing + [new_grant]:
    key = (g.get('principal_type'), g.get('principal_id'), g.get('permission'))
    if key not in seen:
        seen.add(key)
        result.append(g)
print(json.dumps(result))
")

    # Update model access
    PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'id': '$MODEL_ID',
    'name': '$MODEL_ID',
    'access_grants': $NEW_GRANTS
}))
")

    RESULT=$(api_post "models/model/access/update" "$PAYLOAD" 2>/dev/null) && {
        echo -e "  ${GREEN}✓${NC} $MODEL_ID"
    } || {
        echo -e "  ${RED}✗${NC} $MODEL_ID (failed)"
    }
done

echo ""
echo -e "${GREEN}Done.${NC} Grant '${PERMISSION}' access to '${GROUP}' for ${MODEL_COUNT} models."
