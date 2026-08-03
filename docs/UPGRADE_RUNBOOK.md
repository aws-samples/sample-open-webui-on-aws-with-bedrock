# Open WebUI Upstream Upgrade Runbook

Repeatable process for bumping this sample's pinned upstream Open WebUI release.

## How this sample tracks upstream

This repository vendors **no upstream source** and builds **no image**. The
deployed application is the **completely unmodified official Open WebUI image**,
pinned by digest and pulled from `ghcr.io/open-webui/open-webui` at deploy time.
The Amazon Bedrock integration is delivered entirely as AWS infrastructure +
runtime configuration — an
[AgentCore inference gateway](GATEWAY_INTEGRATION_GUIDE.md)
plus a small [Claude pipe](../pipe/gateway_anthropic_pipe.py) and two OpenAI
connections that [`pipe/seed.py`](../pipe/seed.py) writes into the app database
at container start.

So "upgrading" is just **moving the pin**. There is no source to vendor, no
diffs to re-apply, no image to build, and no database migration chain to
maintain — the app carries its own schema and applies its own migrations on
start. The only real risk is that an upstream release changes something the
seeder or the pipe depends on; step 2 covers exactly what to check.

The current pin is **v0.10.2** (2026-07-01), a release with upstream security
and access-control fixes — pin at or above it.

## 1. Bump the pinned image

The digest lives in **exactly one code location**: the `OFFICIAL_IMAGE` constant
near the top of [`infra/lib/compute-stack.ts`](../infra/lib/compute-stack.ts).

Pick the new upstream **release tag** (`vX.Y.Z`, **not** `main`) and resolve its
**multi-arch (index) digest**:

```bash
# With Docker/buildx available:
docker buildx imagetools inspect ghcr.io/open-webui/open-webui:vX.Y.Z
# → read the "Digest:" of the manifest list (the sha256 for the tag itself).
```

If Docker isn't available, read the digest straight from the ghcr manifest API
with the anonymous pull-token flow (curl + jq):

```bash
TAG=vX.Y.Z
# 1. Anonymous pull token for the public repo:
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:open-webui/open-webui:pull&service=ghcr.io" | jq -r .token)

# 2. HEAD the manifest and read the digest from the response header:
curl -sI \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://ghcr.io/v2/open-webui/open-webui/manifests/$TAG" \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
# → sha256:… — this is the same index digest buildx would report.
```

Update the pin to the resolved digest, then refresh **every version reference**:

```
infra/lib/compute-stack.ts   OFFICIAL_IMAGE = 'ghcr.io/open-webui/open-webui@sha256:…'
                             (and the "(v0.10.2)" note in its doc comment)
README.md                    the "(currently vX.Y.Z)" / "pin is vX.Y.Z" mentions
docs/UPGRADE_RUNBOOK.md       this file's "current pin" line
```

Pin by **digest**, not by tag — the digest is what ECS actually pulls, and it's
immutable.

## 2. Check for breaking changes that affect the integration

Because the app is unmodified, the **only** things an upstream release can break
are the surfaces this sample plugs into. Skim the release notes and diff
`v0.10.2..vX.Y.Z` for material changes to:

- **`backend/open_webui/models/config.py`** — the seeder writes per-key rows to
  the `config` table: `openai.api_base_urls`, `openai.api_keys`,
  `openai.api_configs`, `openai.enable`. A schema/key-name change here breaks the
  two OpenAI connections.
- **`backend/open_webui/models/functions.py`** — the seeder inserts the Claude
  pipe row into the `function` table. A schema change breaks pipe installation.
- **`backend/open_webui/routers/openai.py`** — the OpenAI connection contract.
  The connections rely on the `api_config` keys `prefix_id`, `model_ids`,
  `connection_type`, `auth_type`, `headers` (the `x-models-flavor` header), and
  `api_type` (`responses` for the `gwr` lane). If any key is renamed or its
  semantics change, the lanes mis-route.
- **`backend/open_webui/utils/oauth.py`** — the `auth_type: system_oauth`
  behavior and the `oauth_manager` / `oauth_session` token path the Claude pipe
  reads (`app.state.oauth_manager.get_oauth_token`, `OAuthSessions`, the
  `oauth_session_id` cookie). If this path changes, the pipe can't obtain the
  user's bearer token.

If any of these changed materially, **test the seeder against a dev deploy before
prod** (deploy the new pin to a dev environment and confirm the pipe + both
connections install and work — see step 5). If nothing relevant changed, the
bump is a config-only pin change.

## 3. DB schema changes / no rolling old+new

Upstream ships its own migrations, which run automatically on container start.
Some releases (v0.10.2 among them) ship **schema changes with a warning against
running old and new application versions against one database simultaneously**.
Keep this caution for every bump:

- **Snapshot the Aurora database** before deploying across a schema-changing
  release.
- Accept the **brief ECS rolling window**, or stop the old tasks first
  (scale the service to 0, deploy, scale back up) so only one app version ever
  touches the DB during the migration.

Fresh installs are unaffected.

## 4. (Optional) Refresh the model capability matrix

New Bedrock models surface in the dropdown **only** when they're added to
[`config/model-capabilities.json`](../config/model-capabilities.json), which is
the gateway interceptor's input. To pick up newly available models, regenerate
it and redeploy the Gateway stack:

```bash
uv run --no-project --with boto3 python scripts/probe-model-capabilities.py
# review + commit the updated config/model-capabilities.json, then:
cd infra && npx cdk deploy '*GatewayStack*'
```

This is independent of the image bump — do it whenever you want new models, not
only at upgrade time.

## 5. Deploy and smoke-test

```bash
./deploy.sh          # or your own CI running: cd infra && npx cdk deploy --all
```

The only stack that changes for a pin bump is the Compute stack (new image
digest → new task definition → rolling deploy; the service auto-rolls-back if the
new tasks don't stabilize). Then verify manually:

- [ ] **OIDC login** — Cognito SSO completes at `/auth` and redirects home.
- [ ] **Model dropdown populates from all three lanes** — Chat Completions and
      Responses (the two native OpenAI connections) plus Claude (the pipe's own
      discovered models). If a lane is empty, that connection or the pipe failed
      to seed (check the container logs for the seeder output).
- [ ] **A streamed chat works on each lane** — send a real message to a
      Chat-Completions model, a Responses model, and a Claude model. HTTP 200 on
      the model-list endpoint is **not** sufficient; only an actual streamed
      response proves the connection/pipe contract still holds against the new
      release.

If anything fails, revert the pin commit and redeploy the previous digest (ECS
keeps the prior task-definition revision, and the deployment circuit breaker
rolls back automatically on a failed stabilize).

## Recommended cadence

- **Monthly** — absorb the latest release tag (`vX.Y.Z`), not `main` HEAD.
- **Out-of-band** — on upstream security advisories (like v0.10.2's).
- Pin **tags, not `main`**; tags are stable, reproducible targets.

## Metering module: upgrading through the single-source pricing change

Deployments created before the single-source pricing redesign
(`.kiro/specs/metering-pricing-single-source/`) carry the legacy four-tier
catalog in the metering table (`PROVIDER` / `DEFAULT` rows, display-token-keyed
`PUBLISHED` rows like `PRICING#Claude3Haiku`, per-token override rows) and a
bundled `config/model-prices.json` snapshot. Upgrading is one deploy plus one
refresh; the migration is self-executing:

1. **Deploy** (`./deploy.sh --metering`). The debit/interceptor/admin Lambdas
   switch to the shared resolver immediately. Legacy rows keep pricing in the
   deploy-to-first-refresh window: the resolver reads old per-token `PUBLISHED`
   `tiers` shapes and per-token `OVERRIDE` rows in place.
2. **Refresh** (console "Refresh from AWS", or wait for the daily schedule).
   The refresher writes the model-id-keyed catalog, then garbage-collects the
   legacy rows: display-token `PUBLISHED` keys the settle path can never read
   are deleted unconditionally; `PROVIDER` and `DEFAULT` rows (retired source
   tiers) are deleted; stale model-id rows are deleted only when all three
   offer files fetched successfully. **Operator `OVERRIDE` rows and `_ALIAS`
   bindings are never touched.**
3. **Review the Unmatched queue** (console → Model pricing). Entries AWS
   publishes a rate for but that no model id could be resolved to without
   guessing land here (with their published rates); bind the ones you serve.
4. **Announce the chargeback shift.** Settle now prices from AWS-published
   rates instead of the retired estimate tiers, so per-model dollars move at
   the deploy boundary (frontier Claude models drop 27-63%; some models gain
   a real rate for the first time). Settled ledger rows are never rewritten;
   tokens remain the cross-boundary invariant, and each row's
   `price_map_version` records the offer version that priced it.

Notes:
- Overrides entered BEFORE the upgrade were per-token; they keep working
  as-is. Overrides entered after are USD per 1M tokens (the console and
  `PUT /pricing/{model}` validate the new unit).
- `PRICE_MAP_VERSION` is gone from stack env/outputs; catalog freshness now
  comes from `GET /config` → `pricing` (generation, counts, refreshed_at).
- If the first refresh reports `partial: true`, an offer file was unreachable;
  stored rates are kept and stale-row GC is skipped until a clean run.
