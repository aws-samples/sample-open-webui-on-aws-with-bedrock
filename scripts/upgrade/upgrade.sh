#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Perform an upstream merge onto a feature branch. Resolves safe cases automatically,
# flags complex ones for manual review. Does NOT touch main.
#
# Usage:
#   scripts/upgrade/upgrade.sh <target-ref>
#
# Example:
#   scripts/upgrade/upgrade.sh v0.9.2
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

TARGET="${1:-}"
[ -z "$TARGET" ] && { echo "Usage: $0 <target-ref>"; exit 1; }

MANIFEST="scripts/upgrade/fork-manifest.json"
UPSTREAM="$(jq -r .upstream_remote "$MANIFEST")"
MAIN="$(jq -r .fork_main_branch "$MANIFEST")"

# Safety checks
[ -n "$(git status --porcelain)" ] && { echo "ERROR: Working tree not clean. Commit or stash first."; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" != "$MAIN" ] && { echo "ERROR: Must start from $MAIN branch."; exit 1; }

echo "→ Fetching upstream..."
git fetch "$UPSTREAM" --tags --quiet

# Resolve target to a real commit (tag or branch)
TARGET_SHA="$(git rev-parse --verify "$TARGET" 2>/dev/null || git rev-parse --verify "$UPSTREAM/$TARGET" 2>/dev/null || true)"
[ -z "$TARGET_SHA" ] && { echo "ERROR: Cannot resolve $TARGET"; exit 1; }

# Derive branch name
SLUG="${TARGET//\//-}"
SLUG="${SLUG// /-}"
BRANCH="feat/upgrade-${SLUG}"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Upgrade: $MAIN → $TARGET"
echo "  Branch:  $BRANCH"
echo "════════════════════════════════════════════════════════════"
echo ""

# Create / reset branch
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
	echo "⚠ Branch $BRANCH exists. Reset? [y/N]"
	read -r reply
	[ "$reply" != "y" ] && exit 1
	git branch -D "$BRANCH"
fi
git checkout -b "$BRANCH"

echo ""
echo "→ Merging $TARGET (no commit yet)..."
if git merge --no-commit --no-ff "$TARGET_SHA" 2>&1; then
	echo "✓ Merge completed without conflicts."
else
	echo ""
	echo "⚠ Merge has conflicts. Applying resolution guide..."
fi

echo ""
echo "── Conflicts (if any) ──"
CONFLICTS=$(git diff --name-only --diff-filter=U || true)
if [ -z "$CONFLICTS" ]; then
	echo "  (none)"
else
	echo "$CONFLICTS" | sed 's|^|  ⚠ |'
fi

echo ""
echo "── Resolution guide per file ──"
if [ -n "$CONFLICTS" ]; then
	while IFS= read -r file; do
		guidance=$(jq -r --arg p "$file" '.modified_upstream_files[] | select(.path == $p) | .reason' "$MANIFEST")
		if [ -n "$guidance" ]; then
			echo "  $file"
			echo "    Keep your addition: $guidance"
		else
			echo "  $file (not in manifest — inspect manually)"
		fi
	done <<< "$CONFLICTS"
fi

echo ""
echo "── Required post-merge actions ──"
echo "  1. Resolve the conflicts above (see docs/UPGRADE_RUNBOOK.md)"
echo "  2. Rebase Bedrock migration's down_revision onto new upstream head:"
echo "     scripts/upgrade/fix-migration-chain.sh"
echo "  3. Regenerate package-lock.json if package.json changed:"
echo "     docker run --rm -v \$(pwd):/app -w /app node:22-alpine3.20 \\"
echo "       sh -c 'rm -rf node_modules package-lock.json && npm install --force'"
echo "     sudo rm -rf node_modules"
echo "  4. Format: black backend/open_webui/ && npx prettier --write src/"
echo "  5. Verify: scripts/upgrade/verify.sh"
echo "  6. Complete merge: git commit --no-edit"
echo "  7. Push branch: git push origin $BRANCH"
echo "  8. Open PR → merge to main → pipeline handles Dev → Approval → Prod"
