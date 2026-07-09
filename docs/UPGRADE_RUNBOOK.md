# Open WebUI Upstream Upgrade Runbook

Repeatable process for bumping this sample's pinned upstream Open WebUI release
while preserving the Bedrock provider and AWS deployment customizations.

## How this sample tracks upstream

This repository is **not a fork** — it never vendors upstream source. The
deployed application is the official `ghcr.io/open-webui/open-webui` image at a
pinned tag, extended at Docker-build time by:

- **3 overlay files** (new code, copied in): `overlay/backend/open_webui/routers/bedrock.py`,
  `overlay/backend/open_webui/utils/bedrock.py`, and (full target only)
  `overlay/src/lib/apis/bedrock/index.ts`.
- **7 patches** (small attributed diffs applied to upstream files):
  5 backend (`patches/backend/`) + 2 frontend (`patches/frontend/`, full target
  only). See `patches/README.md` for the per-patch inventory.

That set — **7 patches + 3 overlay files** — is the entire upgrade surface.
The sample ships **zero alembic migrations and zero Python dependency
changes**, so there is no migration chain or requirements file to maintain
across bumps.

## Why the current pin is v0.10.2

Upstream's v0.10.2 release (2026-07-01) contains a **security advisory**
("security and access-control fixes… update production deployments at your
earliest convenience") — pin at or above it.

v0.10.2 also ships **database schema changes** with an upstream warning against
rolling updates: do not run old and new application versions against the same
database simultaneously while upgrading. Irrelevant for fresh installs; for an
existing deployment, stop the old tasks (or accept the ECS rolling window is
brief) and take a DB snapshot before bumping across it.

## Phase 1 — Analyze (read-only)

Check what the new tag changes in the 7 patched files:

```bash
git clone https://github.com/open-webui/open-webui /tmp/owui && cd /tmp/owui
git diff v0.10.2 vNEW --stat -- \
  backend/open_webui/config.py backend/open_webui/env.py backend/open_webui/main.py \
  backend/open_webui/utils/chat.py backend/open_webui/utils/models.py \
  src/lib/components/admin/Settings/Connections.svelte src/lib/constants.ts
```

Also read the release notes for: security advisories, DB schema changes (and
any no-rolling-update warnings), and changes to provider dispatch or model
listing (`utils/chat.py` / `utils/models.py` are the highest-drift-risk
targets).

**Decision point:** if upstream radically refactored `main.py`, `utils/chat.py`
or `utils/models.py`, budget extra time — those patches may need manual
re-anchoring rather than a clean apply.

## Phase 2 — Re-apply and re-baseline the patches

```bash
cd /tmp/owui && git checkout vNEW

# 1. Check which patches still apply clean:
OPEN_WEBUI_VERSION=vNEW /path/to/repo/docker/apply-patches.sh

# 2. For each patch that applies: apply it, then re-emit so it carries the
#    new tag's context lines and blob hashes:
git apply /path/to/repo/patches/backend/0001-config-bedrock-defaults.patch
git diff -- backend/open_webui/config.py > /path/to/repo/patches/backend/0001-config-bedrock-defaults.patch
git checkout -- backend/open_webui/config.py
# ... repeat per patch; restore each patch's 4-line attribution preamble
#     (git ignores preamble text before the first 'diff --git' line).

# 3. For each patch that FAILS: open the rejected hunks, re-anchor the same
#    added lines onto the new upstream code by hand, then re-emit as above.
```

Never regenerate patches by diffing a fork tree against upstream — re-apply
the existing scoped patches and re-emit, so upstream's own changes between
tags are not accidentally reverted.

### Overlay-file drift

The overlay modules call upstream internals (`Config.get/upsert`,
`Models.get_model_by_id`, `utils.payload` helpers). After a bump, check the
release notes/diff for signature changes to those, and remember upstream has a
history of **converting sync table helpers to async without warning** —
un-awaited calls surface as `'coroutine' object has no attribute ...` at
runtime, not at import. `scripts/upgrade/verify.sh` greps the overlay files
for un-awaited table calls.

## Phase 3 — Update the pin everywhere

- `Dockerfile`: `ARG OPEN_WEBUI_VERSION=vNEW` + refresh the recorded index
  digest (`docker buildx imagetools inspect ghcr.io/open-webui/open-webui:vNEW`)
- `docker/apply-patches.sh`: default `OPEN_WEBUI_VERSION`
- `patches/README.md`, `README.md`, this file: version references
- `scripts/upgrade/fork-manifest.json`: `pinned_upstream_tag`

## Phase 4 — Verify

```bash
# Patches apply against a pristine clone at the new tag:
OPEN_WEBUI_VERSION=vNEW docker/apply-patches.sh

# Both image targets build and boot:
docker build --target backend -t owui-sample:backend .
docker build --target full    -t owui-sample:full .
docker run -d -e WEBUI_AUTH=False -e WEBUI_SECRET_KEY=smoke -p 8081:8080 owui-sample:backend
curl -sf http://localhost:8081/health && curl -sf http://localhost:8081/api/config

# Import smoke + the sample ships no migrations (head must be upstream's own):
docker run --rm -e WEBUI_SECRET_KEY=smoke owui-sample:backend \
  python -c "import open_webui.routers.bedrock, open_webui.utils.bedrock; print('OK')"

# Infra still compiles:
cd infra && npx tsc --noEmit && npx cdk synth --quiet
```

## Phase 5 — Deploy and smoke-test

Deploy to a dev/test environment first (`./deploy.sh` or the pipeline). The
pipeline's smoke stage (`buildspec-smoke.yml`) checks `/health`, `/api/config`,
and the WebSocket upgrade path automatically. Then verify manually:

- [ ] `/auth` — Cognito SSO login completes, redirects home
- [ ] Bedrock models appear in the model dropdown
- [ ] **Send an actual chat message to a Bedrock model** (streaming). This is
      the canary for async-hygiene regressions — HTTP 200 on the model list
      endpoint is NOT sufficient.
- [ ] Per-response token usage displays on assistant messages
- [ ] Upload file → RAG retrieval in chat
- [ ] Admin config persists across an ECS task restart
- [ ] Container startup logs show upstream's migrations applying cleanly (no
      `alembic.util.exc` errors)

If any fail → roll back (previous image digest is retained in the CDK asset
ECR repo; `git revert` the bump commit and redeploy).

## Recovery

### A patch no longer applies

Re-anchor by hand (Phase 2 step 3). The patches are small (+85 backend lines
total) and additive — every hunk inserts lines; none rewrites upstream logic —
so re-anchoring is a matter of finding where the insertion point moved.

### Multiple Alembic heads after a bump

The sample ships no migrations, so multi-head states can only come from
upstream itself (it occasionally ships parallel migration lineages that it
merges in a later release). If the container errors with `Multiple head
revisions`, pin one release later, or hold the bump until upstream merges its
heads. Forks that add their own migrations can use
`scripts/upgrade/fix-migration-chain.sh` (set `FORK_MIGRATION_GLOB`).

### Revision-ID reuse (forks with migrations only)

Upstream has reused migration revision IDs across releases. If your fork adds
migrations and a deployed DB is stamped at an ID that now belongs to a
different upstream migration, the boot migration becomes a silent no-op and
the app reads tables that were never created. Diagnose by comparing
`SELECT version_num FROM alembic_version;` against what the schema actually
contains, and restamp to the last truly-applied common revision before
deploying the new image.

## Recommended cadence

- **Monthly** — absorb the latest tag (`vX.Y.Z`), not `main` HEAD
- **Out-of-band** — security advisories (like v0.10.2's) or critical fixes
- Don't chase every commit; tags are stable targets

## Maintaining the manifest

`scripts/upgrade/fork-manifest.json` is the source of truth for
`scripts/upgrade/verify.sh`. Update it when:

- You patch a new upstream file → add to `modified_upstream_files`
- You add a sample-only file or directory → add to `fork_only_files`
- You change Bedrock marker strings → update `invariants.required_*_markers`

Keep it honest and the upgrade scripts stay useful.
