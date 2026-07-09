# Patches

This directory contains the complete set of modifications this sample makes to
the official [Open WebUI](https://github.com/open-webui/open-webui) release,
pinned to **v0.10.2**. Everything else the sample adds lives in new files under
`overlay/` — upstream source is never vendored into this repository.

## Attribution

The added lines in these patches are Copyright Amazon.com, Inc. or its
affiliates, licensed MIT-0 (see `LICENSE`). The quoted upstream context lines
within each unified diff remain the property of their respective copyright
holders under the Open WebUI License (see `THIRD-PARTY-LICENSES.md`) and are
reproduced solely to identify the location of the modifications. See `NOTICE`
for the full framing.

## Backend patches (applied in both image targets)

| # | Patch | Target file | Purpose | Size |
|---|-------|-------------|---------|------|
| 0001 | `backend/0001-config-bedrock-defaults.patch` | `backend/open_webui/config.py` | Seed Bedrock first-boot config defaults (`bedrock.*` keys in `DEFAULT_CONFIG`) | +22 |
| 0002 | `backend/0002-env-bedrock-vars.patch` | `backend/open_webui/env.py` | Read the 4 `BEDROCK_*` environment variables | +11 |
| 0003 | `backend/0003-main-bedrock-registration.patch` | `backend/open_webui/main.py` | Register the Bedrock router and model cache | +13 |
| 0004 | `backend/0004-chat-bedrock-dispatch.patch` | `backend/open_webui/utils/chat.py` | Dispatch `owned_by == 'bedrock'` chat completions to the Bedrock provider | +10 |
| 0005 | `backend/0005-models-bedrock-listing.patch` | `backend/open_webui/utils/models.py` | List Bedrock models alongside OpenAI/Ollama models | +18/−4 |

## Frontend patches (opt-in `full` image target only)

| # | Patch | Target file | Purpose | Size |
|---|-------|-------------|---------|------|
| 0101 | `frontend/0101-connections-bedrock-section.patch` | `src/lib/components/admin/Settings/Connections.svelte` | Add an "Amazon Bedrock API" section to the admin Connections panel | +74 |
| 0102 | `frontend/0102-constants-bedrock-base-url.patch` | `src/lib/constants.ts` | Add the `BEDROCK_API_BASE_URL` constant | +1 |

The frontend pair applies only to the opt-in `full` image target (which
rebuilds the UI from upstream source at the pinned tag). The default `backend`
target ships the official image's UI unchanged; all Bedrock configuration is
still available through environment variables and the
`GET/POST /api/v1/bedrock/config*` admin API.

## Regenerating / verifying

The patches were generated against a clean checkout of upstream at tag
`v0.10.2` and carry that tag's blob hashes. To verify they still apply:

```bash
git clone --depth 1 --branch v0.10.2 https://github.com/open-webui/open-webui /tmp/owui
cd /tmp/owui
git apply --check /path/to/this/repo/patches/backend/*.patch
git apply --check /path/to/this/repo/patches/frontend/*.patch
```

`docker/apply-patches.sh` automates this check (CI runs it on every build).

When bumping the upstream pin, re-apply each patch onto the new tag, resolve
any drift, and re-emit with `git diff` so the shipped patches carry the new
tag's context — see `docs/UPGRADE_RUNBOOK.md`.
