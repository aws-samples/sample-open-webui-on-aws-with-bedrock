#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# NOTE: this sample ships no alembic migrations; this helper is retained for
# forks that add their own. It re-points a fork migration's down_revision to
# the current upstream Alembic head(s). Handles three cases:
#   1. Single upstream head → down_revision = 'head_sha'
#   2. Multiple upstream heads → down_revision = ('head1', 'head2', ...)
#      (Alembic merge migration pattern; required because the app calls
#      `alembic upgrade head` singular which errors on multi-head state)
#   3. Revision ID collision with upstream → rename the fork migration to a
#      fresh hex ID and update the filename
# Idempotent. Run after merging upstream migrations.
#
# Usage: FORK_MIGRATION_GLOB='*_my_fork_feature.py' scripts/upgrade/fix-migration-chain.sh
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

MIG_DIR="backend/open_webui/migrations/versions"
FORK_MIGRATION_GLOB="${FORK_MIGRATION_GLOB:-}"
if [ -z "$FORK_MIGRATION_GLOB" ]; then
	echo "This sample ships no alembic migrations — nothing to do."
	echo "Forks that add migrations: set FORK_MIGRATION_GLOB to your migration's filename pattern."
	exit 0
fi

FORK_MIG=$(ls "$MIG_DIR"/$FORK_MIGRATION_GLOB 2>/dev/null | head -1)
[ -z "$FORK_MIG" ] && { echo "ERROR: fork migration matching '$FORK_MIGRATION_GLOB' not found in $MIG_DIR"; exit 1; }
echo "→ Fork migration: $FORK_MIG"

CURRENT_REV=$(grep -E "^revision" "$FORK_MIG" | sed -E "s/.*['\"]([a-f0-9]+)['\"].*/\1/")
echo "→ Current revision: $CURRENT_REV"

# Detect collision: any OTHER migration uses the same revision ID?
COLLIDES=$(grep -l "revision.*['\"]${CURRENT_REV}['\"]" "$MIG_DIR"/*.py 2>/dev/null | grep -v "$(basename "$FORK_MIG")" || true)
if [ -n "$COLLIDES" ]; then
	echo "⚠ Revision ID collision with: $COLLIDES"
	NEW_REV=$(python3 -c "import secrets; print(secrets.token_hex(6))")
	echo "→ Generating new revision ID: $NEW_REV"
	NEW_FILENAME="$MIG_DIR/${NEW_REV}_$(basename "$FORK_MIG" | sed -E 's/^[a-f0-9]+_//')"
	sed -i.bak -E "s/^(revision[^=]*=\s*['\"])([a-f0-9]+)(['\"])/\1$NEW_REV\3/" "$FORK_MIG"
	rm -f "${FORK_MIG}.bak"
	git mv "$FORK_MIG" "$NEW_FILENAME" 2>/dev/null || mv "$FORK_MIG" "$NEW_FILENAME"
	FORK_MIG="$NEW_FILENAME"
	CURRENT_REV="$NEW_REV"
	echo "✓ Renamed migration → $NEW_FILENAME"
fi

# Find all current heads in the upstream lineage (excluding the fork migration
# itself, which is being rebased).
readarray -t HEADS < <(FORK_MIG_BASENAME="$(basename "$FORK_MIG")" python3 - <<'EOF'
import os, re
d = 'backend/open_webui/migrations/versions'
skip = os.environ.get('FORK_MIG_BASENAME', '')
revs = {}
dates = {}
for f in sorted(os.listdir(d)):
	if not f.endswith('.py'): continue
	if f == skip: continue
	txt = open(os.path.join(d, f)).read()
	rm = re.search(r"^revision[^=]*=\s*['\"]([a-f0-9]+)['\"]", txt, re.M)
	tm = re.search(r"^down_revision[^=]*=\s*\(([^)]+)\)", txt, re.M)
	sm = re.search(r"^down_revision[^=]*=\s*(?:['\"]([a-f0-9]+)['\"]|None)", txt, re.M)
	if not rm: continue
	deps = set()
	if tm:
		deps = set(re.findall(r"['\"]([a-f0-9]+)['\"]", tm.group(1)))
	elif sm and sm.group(1):
		deps = {sm.group(1)}
	revs[rm.group(1)] = deps
	dm = re.search(r"Create Date:\s*(\S+)", txt)
	dates[rm.group(1)] = dm.group(1) if dm else ''
referenced = set()
for s in revs.values(): referenced |= s
heads = [r for r in revs if r not in referenced]
# Sort by Create Date (newest first) so primary head is first if only single used
heads.sort(key=lambda h: dates.get(h, ''), reverse=True)
for h in heads: print(h)
EOF
)

if [ "${#HEADS[@]}" -eq 0 ]; then
	echo "ERROR: No upstream heads found"; exit 1
fi

echo "→ Detected ${#HEADS[@]} upstream head(s): ${HEADS[*]}"

# Build the new down_revision value
if [ "${#HEADS[@]}" -eq 1 ]; then
	NEW_DOWN_VALUE="\"${HEADS[0]}\""
	NEW_DOWN_TYPE="Union[str, None]"
	EXPECTED="down_revision.*=\s*['\"]${HEADS[0]}['\"]"
else
	# Tuple of all heads; Alembic merges them via this migration
	TUPLE_INNER=""
	for h in "${HEADS[@]}"; do
		[ -n "$TUPLE_INNER" ] && TUPLE_INNER+=", "
		TUPLE_INNER+="\"$h\""
	done
	NEW_DOWN_VALUE="($TUPLE_INNER)"
	NEW_DOWN_TYPE="Union[str, tuple, None]"
	EXPECTED="down_revision.*=\s*\("
fi

# Check if already at desired state
if grep -qE "$EXPECTED" "$FORK_MIG" 2>/dev/null && [ "${#HEADS[@]}" -eq 1 ]; then
	echo "✓ down_revision already at ${HEADS[0]}"
	exit 0
fi

# Rewrite the down_revision line using Python. Pass values via env to avoid
# heredoc quoting issues.
FORK_MIG="$FORK_MIG" NEW_DOWN_TYPE="$NEW_DOWN_TYPE" NEW_DOWN_VALUE="$NEW_DOWN_VALUE" \
python3 <<'PYEOF'
import os, re
p = os.environ["FORK_MIG"]
new_line = f"down_revision: {os.environ['NEW_DOWN_TYPE']} = {os.environ['NEW_DOWN_VALUE']}"
txt = open(p).read()
# Match multi-line down_revision (tuple on multiple lines) or single-line form,
# stopping at the next assignment or comment
new = re.sub(
    r"^down_revision[^\n]*(?:\n[ \t]+[^\n]*)*?(?=\n(?:branch_labels|[a-zA-Z_#]))",
    new_line,
    txt,
    count=1,
    flags=re.M,
)
if new == txt:
    new = re.sub(
        r"^down_revision[^=]*=\s*[^\n]+",
        new_line,
        txt,
        count=1,
        flags=re.M,
    )
open(p, "w").write(new)
PYEOF

# Verify result
if ! grep -q "down_revision" "$FORK_MIG"; then
	echo "ERROR: down_revision rewrite lost the line"; exit 1
fi

echo "✓ Rebased fork migration onto upstream head(s):"
grep -E "^(revision|down_revision)" "$FORK_MIG" | sed 's/^/    /'
echo ""
echo "Verify: scripts/upgrade/verify.sh"
