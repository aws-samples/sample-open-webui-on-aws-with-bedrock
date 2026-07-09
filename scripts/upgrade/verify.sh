#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Verify that the sample's customizations survived an upstream version bump.
# Run from a workbench where the pinned upstream tree has the overlay files
# copied in and the patches applied (see docs/UPGRADE_RUNBOOK.md).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

MANIFEST="scripts/upgrade/fork-manifest.json"
FAIL=0

# python3-only hosts have no bare `python`.
PYTHON="$(command -v python || command -v python3)"

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

echo "── Sample-only files still present ──"
while IFS= read -r entry; do
	if [[ "$entry" == *"*"* ]]; then
		if compgen -G "$entry" > /dev/null; then pass "$entry"; else fail "$entry MISSING"; fi
	else
		if [[ -e "$entry" ]]; then pass "$entry"; else fail "$entry MISSING"; fi
	fi
done < <(jq -r '.fork_only_files[]' "$MANIFEST")
echo ""

echo "── Required markers in patched upstream files ──"
check_marker() {
	local file="$1" marker="$2"
	if grep -qF "$marker" "$file" 2>/dev/null; then pass "$file: '$marker'"; else fail "$file: missing '$marker'"; fi
}

while IFS= read -r m; do check_marker backend/open_webui/main.py "$m"; done < <(jq -r '.invariants.required_main_py_markers[]' "$MANIFEST")
while IFS= read -r m; do check_marker backend/open_webui/utils/models.py "$m"; done < <(jq -r '.invariants.required_models_py_markers[]' "$MANIFEST")
while IFS= read -r m; do check_marker backend/open_webui/utils/chat.py "$m"; done < <(jq -r '.invariants.required_chat_py_markers[]' "$MANIFEST")
echo ""

echo "── Python syntax checks ──"
for f in backend/open_webui/main.py backend/open_webui/utils/models.py \
	backend/open_webui/utils/chat.py backend/open_webui/routers/bedrock.py \
	backend/open_webui/utils/bedrock.py \
	backend/open_webui/config.py backend/open_webui/env.py; do
	if "$PYTHON" -m py_compile "$f" 2>/dev/null; then pass "compile: $f"; else fail "compile: $f"; fi
done
echo ""

echo "── Async hygiene (sample backend files must await upstream async methods) ──"
# Upstream v0.9.2+ converted many *Table helpers from sync to async. Overlay code
# calling them without `await` returns coroutines (e.g. the infamous
# 'coroutine' object has no attribute 'base_model_id'). Grep the overlay backend
# files for the common culprits used without await.
ASYNC_TABLES='Models|Groups|Users|Chats|Auths|Files|Folders|Knowledges|Prompts|Tools|Functions|Feedbacks|Memories|Messages|Channels|Notes|OAuthSessions|AccessGrants'
SAMPLE_BACKEND_FILES=(
	backend/open_webui/routers/bedrock.py
	backend/open_webui/utils/bedrock.py
)
suspect=0
for f in "${SAMPLE_BACKEND_FILES[@]}"; do
	[ -f "$f" ] || continue
	matches=$(grep -nE "(^|[^_a-zA-Z])(${ASYNC_TABLES})\.[a-z_]+\(" "$f" 2>/dev/null \
		| grep -vE "await (${ASYNC_TABLES})\." \
		| grep -vE "async def [a-z_]+|^[^:]+:\s*#" || true)
	if [ -n "$matches" ]; then
		echo "  ⚠ $f has possibly-sync calls to upstream tables:"
		echo "$matches" | sed 's/^/      /'
		suspect=$((suspect + 1))
	fi
done
if [ "$suspect" -eq 0 ]; then
	pass "no un-awaited calls to upstream *Table helpers in overlay files"
else
	fail "$suspect overlay file(s) may have un-awaited async calls — review the lines above"
fi
echo ""

echo "── Migration chain intact (sample ships none; upstream's own must be single-head) ──"
if python3 -c "
import os, re, sys
d = 'backend/open_webui/migrations/versions'
revs = {}
for f in os.listdir(d):
	if not f.endswith('.py'): continue
	txt = open(os.path.join(d, f)).read()
	r = re.search(r\"^revision[^=]*=\s*['\\\"]([a-f0-9]+)['\\\"]\", txt, re.M)
	if not r: continue
	tm = re.search(r\"^down_revision[^=]*=\s*\(([^)]+)\)\", txt, re.M)
	sm = re.search(r\"^down_revision[^=]*=\s*(?:['\\\"]([a-f0-9]+)['\\\"]|None)\", txt, re.M)
	if tm:
		revs[r.group(1)] = set(re.findall(r\"['\\\"]([a-f0-9]+)['\\\"]\", tm.group(1)))
	elif sm and sm.group(1):
		revs[r.group(1)] = {sm.group(1)}
	else:
		revs[r.group(1)] = set()
referenced = set()
for s in revs.values(): referenced |= s
heads = [r for r in revs if r not in referenced]
if len(heads) == 1: print('ok'); sys.exit(0)
print('heads:', heads); sys.exit(1)
"; then pass "single Alembic head"; else fail "multiple Alembic heads — merge them via tuple down_revision"; fi
echo ""

echo "════════════════════════════════════════════════════════════"
if [ "$FAIL" -eq 0 ]; then
	echo "  ✓ ALL CHECKS PASSED"
	echo "════════════════════════════════════════════════════════════"
	exit 0
else
	echo "  ✗ $FAIL CHECK(S) FAILED"
	echo "════════════════════════════════════════════════════════════"
	exit 1
fi
