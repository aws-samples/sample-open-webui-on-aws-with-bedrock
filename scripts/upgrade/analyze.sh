#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Analyze the gap between fork and upstream. Read-only. Prints upgrade scope.
#
# Usage:
#   scripts/upgrade/analyze.sh [target-ref]
#
# target-ref defaults to latest upstream tag. Pass "origin/main" to target HEAD.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

MANIFEST="scripts/upgrade/fork-manifest.json"
UPSTREAM="$(jq -r .upstream_remote "$MANIFEST")"
MAIN="$(jq -r .fork_main_branch "$MANIFEST")"

echo "→ Fetching upstream tags from $UPSTREAM..."
git fetch "$UPSTREAM" --tags --quiet

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
	TARGET="$(git tag -l 'v*' --sort=-v:refname | head -1)"
	[ -z "$TARGET" ] && { echo "No tags found. Pass a ref explicitly."; exit 1; }
fi

MERGE_BASE="$(git merge-base "$MAIN" "$TARGET")"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Upgrade Analysis"
echo "════════════════════════════════════════════════════════════"
echo "  Fork branch:     $MAIN ($(git rev-parse --short $MAIN))"
echo "  Upgrade target:  $TARGET ($(git rev-parse --short $TARGET))"
echo "  Merge base:      $(git rev-parse --short $MERGE_BASE)"
echo "  Behind target:   $(git rev-list --count "$MAIN".."$TARGET") commits"
echo "  Ahead of target: $(git rev-list --count "$TARGET".."$MAIN") commits"
echo ""

echo "── Conflict-risk files (modified upstream files you also changed) ──"
jq -r '.modified_upstream_files[] | "  [\(.conflict_risk | ascii_upcase)]\t\(.path)\t→ \(.reason)"' "$MANIFEST" | column -t -s $'\t'
echo ""

echo "── Which of those did upstream actually change? ──"
CHANGED_UPSTREAM=0
while IFS= read -r path; do
	if git diff --quiet "$MERGE_BASE".."$TARGET" -- "$path" 2>/dev/null; then
		echo "  ✓ $path (upstream unchanged — your edits apply cleanly)"
	else
		lines=$(git diff "$MERGE_BASE".."$TARGET" -- "$path" | wc -l)
		echo "  ⚠ $path (upstream changed ~$lines diff lines — review merge)"
		CHANGED_UPSTREAM=$((CHANGED_UPSTREAM + 1))
	fi
done < <(jq -r '.modified_upstream_files[].path' "$MANIFEST")
echo ""
echo "  → $CHANGED_UPSTREAM of $(jq '.modified_upstream_files | length' "$MANIFEST") customized files have upstream changes."
echo ""

echo "── New upstream Alembic migrations ──"
NEW_MIGS=$(git diff --name-only --diff-filter=A "$MERGE_BASE".."$TARGET" -- backend/open_webui/migrations/versions/ | wc -l)
echo "  $NEW_MIGS new migrations to absorb"
git diff --name-only --diff-filter=A "$MERGE_BASE".."$TARGET" -- backend/open_webui/migrations/versions/ | sed 's|^|  + |'
echo ""

echo "── Dependency changes to watch ──"
if ! git diff --quiet "$MERGE_BASE".."$TARGET" -- pyproject.toml backend/requirements.txt; then
	echo "  ⚠ Python deps changed — check aiohttp pin"
	git diff "$MERGE_BASE".."$TARGET" -- pyproject.toml backend/requirements.txt | grep -iE '^\+.*aiohttp' || true
fi
if ! git diff --quiet "$MERGE_BASE".."$TARGET" -- package.json; then
	echo "  ⚠ package.json changed — lockfile regeneration required"
fi
echo ""

echo "── Sync → async signature changes in upstream Models/* ──"
# Any model helper that changed from `def foo` to `async def foo` between
# merge-base and target will break fork code that calls it without await.
SIGCHANGES=$(git diff "$MERGE_BASE".."$TARGET" -- backend/open_webui/models/ \
	| grep -E '^\+\s+async def [a-z_]+\(' \
	| sort -u || true)
if [ -n "$SIGCHANGES" ]; then
	echo "  ⚠ These upstream methods are (now) async. Grep fork files for callers without 'await':"
	echo "$SIGCHANGES" | sed -E 's/^\+\s+async def ([a-z_]+)\(.*/    \1/' | sort -u
else
	echo "  ✓ no new async method signatures in models/"
fi
echo ""

echo "── Release notes hint ──"
git log --oneline "$MERGE_BASE".."$TARGET" --grep='^[0-9]\+\.[0-9]\+\.' | head -10 | sed 's|^|  |'
echo ""
echo "Next: scripts/upgrade/upgrade.sh $TARGET"
