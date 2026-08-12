# Open WebUI Upstream Upgrade Runbook

Repeatable process for upgrading (or pinning, or rolling back) the upstream
Open WebUI release this sample deploys.

## How this sample tracks upstream

This repository vendors **no upstream source** and builds **no image**. The
deployed application is the **completely unmodified official Open WebUI image**
pulled from `ghcr.io/open-webui/open-webui`. The Amazon Bedrock integration is
delivered entirely as AWS infrastructure + runtime configuration — an
[AgentCore inference gateway](GATEWAY_INTEGRATION_GUIDE.md)
plus a small [Claude pipe](../pipe/gateway_anthropic_pipe.py) and two OpenAI
connections that [`pipe/seed.py`](../pipe/seed.py) writes into the app database
at container start.

**Which version runs is decided in one place**: the `OPEN_WEBUI_IMAGE` variable
in `.env` (untracked operator state, not a source file):

| `OPEN_WEBUI_IMAGE` | What deploys |
|---|---|
| unset *(the default)* | The **latest official Open WebUI release**. `deploy.sh` discovers the newest release tag and resolves it to its immutable `@sha256:` index digest at deploy time ([`scripts/resolve-owui-image.py`](../scripts/resolve-owui-image.py)). Requires reach to `api.github.com` and `ghcr.io` from the deploy machine; fails with an actionable error otherwise. |
| a release tag, e.g. `ghcr.io/open-webui/open-webui:v0.11.0` | That release, resolved to its digest at deploy time. If `ghcr.io` is unreachable from the deploy machine, the tag is passed through unresolved with a loud warning (see the caution in step 3). |
| a digest, e.g. `ghcr.io/open-webui/open-webui@sha256:…` | Exactly that image, verbatim — no network needed at resolution time. This is the reproducible form: use it for anything you care about, and for rollback. |

Because the task definition carries a **digest** (on every path except the
unreachable-registry fallback), every task launch — autoscaling, crash
replacement, `--force-new-deployment` — runs byte-identical software. Nothing
changes version until you deliberately run a deploy. A same-`.env` redeploy
*is* an upgrade when the variable is unset, because the deploy re-resolves
"latest release" at that moment; pin a digest if you don't want that.

Avoid `:latest` as a value: on ghcr it is upstream's **main-branch build**, not
the newest release.

So "upgrading" is just **deploying with a newer version selected**. There is no
source to vendor, no diffs to re-apply, and no image to build. The real risks
are (a) an upstream release changing something the seeder or pipe depends on
(step 2) and (b) upstream's own database migrations (step 3).

Run **v0.10.2 (2026-07-01) or newer** — that release carries upstream security
and access-control fixes. The unset default (latest release) always satisfies
this.

## 1. Record what you're running, then choose the target

Before changing anything, record the current image — it is your rollback
target:

```bash
aws cloudformation describe-stacks --stack-name OpenWebUI-Compute \
  --query "Stacks[0].Outputs[?OutputKey=='AppImageUri'].OutputValue" --output text
# e.g. ghcr.io/open-webui/open-webui@sha256:9fcea9c6…   ← keep this
```

(If earlier deploys used a tag rather than a digest, get the *running* digest
from `aws ecs describe-tasks` → `containers[0].imageDigest` instead.)

Then choose the target version:

- **Track the latest release** — leave `OPEN_WEBUI_IMAGE` unset in `.env`.
  Preview what "latest" currently is before deploying:

  ```bash
  python3 scripts/resolve-owui-image.py
  # [resolve-owui-image] latest official release: vX.Y.Z
  # [resolve-owui-image] resolved vX.Y.Z -> sha256:…
  ```

- **Pin a specific release** — set the tag (resolved to a digest at deploy
  time) or run the resolver yourself and set the digest form:

  ```bash
  python3 scripts/resolve-owui-image.py ghcr.io/open-webui/open-webui:vX.Y.Z
  # → ghcr.io/open-webui/open-webui@sha256:…   ← put this in .env
  ```

Release notes live at <https://github.com/open-webui/open-webui/releases>.
Prefer release tags (`vX.Y.Z`) — never `main`, and not `:latest` (which is
main).

## 2. Check for breaking changes that affect the integration

Because the app is unmodified, the **only** things an upstream release can break
are the surfaces this sample plugs into. Skim the release notes and diff
`<running version>..<target version>` for material changes to:

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
your main environment** (deploy the target version to a scratch environment and
confirm the pipe + both connections install and work — see step 5). If nothing
relevant changed, the upgrade is a config-only version change.

## 3. Snapshot the database — upstream migrates it on start

Upstream ships its own schema migrations, and **they run automatically the
moment a task with a newer release starts** — there is no separate "migrate"
step you control, and no built-in downgrade path. Some releases (v0.10.2 among
them) ship schema changes with an explicit upstream warning against running two
application versions against one database simultaneously. Treat every release
boundary as a database event:

- **Snapshot Aurora first, every time.** Not optional:

  ```bash
  aws rds create-db-cluster-snapshot \
    --db-cluster-identifier <cluster-id> \
    --db-cluster-snapshot-identifier pre-owui-<target-version>-$(date +%Y%m%d)
  aws rds wait db-cluster-snapshot-available \
    --db-cluster-snapshot-identifier pre-owui-<target-version>-$(date +%Y%m%d)
  ```

  Wait for `available` **before** the new image starts. A migration that goes
  wrong is otherwise unrecoverable short of point-in-time restore.

- **Minimize the old+new window.** The rolling ECS deployment briefly runs old
  and new tasks side by side against one database. For releases whose notes
  flag schema changes, don't accept that window: scale the service to 0, deploy,
  scale back up — so only one app version ever touches the DB during the
  migration.

- **Know what protects you (and what doesn't).** Because the task definition
  pins a digest, ordinary task churn can never start a newer release or run a
  migration you didn't plan — version changes happen only at deploys. The one
  exception: if the resolver warned it could not resolve your tag and passed it
  through (restricted-egress deploy machine), the task definition floats and
  any task launch may pull a newer build **and migrate the database
  unplanned**. If you saw that warning and are running with a floating tag,
  either accept that risk deliberately or switch to a digest pin now.

- **Rollback ≠ downgrade.** Rolling the *image* back (step 6) does not roll the
  *schema* back. Old app code usually tolerates additive migrations, but the
  snapshot is your real undo. Fresh installs are unaffected by all of this.

## 4. (Optional) Refresh the model capability matrix

New Bedrock models surface in the dropdown **only** when they're added to
[`config/model-capabilities.json`](../config/model-capabilities.json), which is
the gateway interceptor's input. To pick up newly available models, regenerate
it and redeploy the Gateway stack:

```bash
uv run --no-project --with boto3 python scripts/probe-model-capabilities.py
# review + commit the updated config/model-capabilities.json, then:
cd infra && npx cdk deploy OpenWebUI-Gateway
```

This is independent of the image upgrade — do it whenever you want new models,
not only at upgrade time.

## 5. Deploy and smoke-test

```bash
./deploy.sh          # or your own CI running: cd infra && npx cdk deploy --all
```

The deploy log prints the exact image it resolved — record it next to the
rollback target from step 1:

```
[→] Open WebUI image: ghcr.io/open-webui/open-webui@sha256:…
```

The only stack that changes for a version bump is the Compute stack (new image
reference → new task definition → rolling deploy). The service is protected by
a deployment circuit breaker, a healthy-host deployment alarm, and a 5-minute
bake window — a deployment that fails to stabilize rolls back to the previous
task definition automatically. Then verify manually:

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

## 6. Roll back if anything fails

The rollback target is the digest you recorded in step 1 (or the previous
deploy's log line). In `.env`:

```bash
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui@sha256:<previous digest>
```

then `./deploy.sh` again. There is no "pin commit" to revert — the selection
lives in untracked `.env`, so rollback is an explicit redeploy of the recorded
digest. (A deployment that never stabilized will usually have rolled itself
back already via the circuit breaker/alarm; the manual path is for regressions
that surface after the deployment completed.) Remember from step 3: this rolls
the image back, not the schema — if the bad release migrated the database and
the old version can't read it, restore the snapshot.

## Recommended cadence

- **Every deploy is an upgrade opportunity.** With `OPEN_WEBUI_IMAGE` unset,
  any full `./deploy.sh` re-resolves the latest release — so upgrades happen on
  your schedule, at deploy time, never behind your back.
- **Monthly** — deploy to absorb the newest release, following steps 1-5.
- **Out-of-band** — on upstream security advisories (like v0.10.2's).
- **For environments you care about**, pin the resolved digest in `.env` and
  move it deliberately; the unset default is the right choice for fresh
  evaluations and demos.
- Release tags only — never `main`, and `:latest` *is* main on ghcr.

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
