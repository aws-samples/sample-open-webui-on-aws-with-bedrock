# Metering, Consumption Tracking & Quota Enforcement (opt-in module)

Per-user token/dollar metering, per-team cost attribution, and operator-set
quotas that actually block — delivered entirely as AWS infrastructure around
the **unmodified official Open WebUI image**. Off by default; the base
three-lane sample is bit-identical when disabled.

Design rationale, evidence, and rejected alternatives:
[`docs/plans/metering-enforcement/`](plans/metering-enforcement/) (the
architecture is `02-DESIGN.md`; every load-bearing behavior below was either
live-spike-proven or doc-verified there).

## Enable / disable

```bash
./deploy.sh --metering            # deploy with the module (adds 1 stack: OpenWebUI-Metering)
./deploy.sh                       # without the flag: base sample, no metering resources
```

- **Off (default):** the five base stacks synth **byte-identical** to a tree
  without this module (the lean-core gate — verified in CI/PR by comparing
  `cdk synth` output with and without the module's files).
- **On:** adds the `OpenWebUI-Metering` stack and switches the gateway's
  REQUEST interceptor to the quota-enforcing v2 (behind a Lambda alias with
  CodeDeploy canary deploys). Disabling again (`deploy without --metering`)
  points the gateway back at the base capability-filter interceptor.
- **Enforcement ramp:** deploy with `-c meteringMode=observe` to run the
  interceptor in OBSERVE (log-only decisions, nothing blocked) and flip to
  ENFORCE by redeploying without it.
- **With the model refresher (`enableModelRefresh`):** the metering interceptor
  also holds the `MODEL_CAPS` model list, and the gateway invokes it through the
  `live` **alias** pinned to a published (frozen-env) version. So the refresher
  can't just update `$LATEST` — it publishes a new version and repoints the
  alias, or new models would list-and-route at the gateway yet never reach the
  served version. The stack passes the refresher `INTERCEPTOR_ALIAS=live` and
  grants it `lambda:PublishVersion` + `lambda:UpdateAlias` automatically when
  both modules are on; with metering off, the refresher targets `$LATEST`
  directly. (Repointing the alias outside a CodeDeploy deployment window is the
  supported way to ship a config-only change — CodeDeploy owns the alias only
  during an interceptor *code* roll.)

## What it does

| Capability | Mechanism |
|---|---|
| **Meter** | The stream's own `usage` block, captured server-side by a seeded global filter (all three lanes; the interceptor force-injects `stream_options.include_usage` so capture never depends on per-model UI flags) → EventBridge → transactional settle into a DynamoDB ledger + per-user counters |
| **Track** | Per-user monthly counters; per-group rollups (async, ceilings); **Bedrock Projects** per cost-center group — the interceptor injects `OpenAI-Project` / `anthropic-workspace-id` per request, so team dollars land in Cost Explorer/CUR via the projects' cost-allocation tags |
| **Enforce** | Pre-request at the gateway interceptor: over hard limit or RPM bucket exhausted → **HTTP 429** with an OpenAI-shaped error **that renders in the chat window**; an admission floor-debit makes quotas bind even for direct-to-gateway callers; blocked users regain access when the window resets or an operator raises the limit |

Defaults (env on the interceptor; change per deployment): hard **$5/user/month**,
soft-warn **$4** (toast in the UI), **30 requests/min**, max-tokens clamp **8192**.

## Operator surface (outside Open WebUI)

- **Admin console** (stack output `ConsoleUrl`) — the primary operator
  surface: a standalone web app (CloudFront + private S3 + the admin API
  same-origin under `/api`) where an admin can monitor live/aggregate
  consumption, drill into any user or team, set and edit quota policies,
  grant time-boxed overrides, reset counters, read the audit trail, watch
  module health (canaries, degraded checks, alarms), and manage alert
  subscriptions. Sign-in is the sample's **existing Cognito pool** via a
  dedicated no-secret PKCE app client (output `ConsoleClientId`) — an OWUI
  admin (member of `admin`/`admins`/`webui-admins`) is a console admin
  automatically; every other pool user gets a self-service "My usage" view
  only. Architecture + decision record:
  [`docs/plans/metering-admin-console/01-DECISIONS.md`](plans/metering-admin-console/01-DECISIONS.md).
  Evaluating without traffic: `scripts/seed-demo-metering-data.py` seeds
  labeled demo rows (`--cleanup` removes exactly them).
- **Admin API** (stack output `AdminApiUrl`; Cognito JWT; mutating routes
  require membership in an admin group — `admins`, `webui-admins`, or `admin`):
  - `GET/PUT/DELETE /policy/{scope}` — scope `DEFAULT`, `GROUP#<name>`,
    `USER#<sub>`; body `{"hard_limit_usd": 10, "soft_limit_usd": 8,
    "rpm_limit": 30, "until": <epoch>, "note": "..."}`
  - `GET /usage/{sub}?window=YYYY-MM`, `GET /usage/me`, `GET /user/me/ledger`
  - `POST /override` `{"sub": "...", "hard_limit_usd": 20, "until": ...}` ·
    `POST /counter-reset` `{"sub": "..."}`
  - Console read routes: `/users`, `/users/search`, `/user/{sub}`,
    `/user/{sub}/ledger`, `/groups`, `/policies`, `/audit`, `/activity`,
    `/estimates`, `/metrics`, `/alarms`, `/alert-subscriptions`, `/config`
    — all admin-gated (except `/config` + the self-service pair), all
    GSI-backed (no table scans).
  - Every mutation writes an audit row; **self-targeted policy changes,
    overrides, and resets are rejected** — a second admin must act.
  - A time-boxed override's `until` is an **operator-facing expiry marker**,
    not an auto-expiry: the enforcement interceptor applies a `USER#` policy
    row whenever it exists, so ending an override means **deleting the row**
    (the console's Quota policies page flags past-date rows red for cleanup).
    Making the interceptor drop expired rows on read is a small,
    deliberately-deferred enforcement-plane change (see the console decision
    record's residual-risks section).
- **`scripts/set-quota.sh`** — thin CLI over the same API (`$default` stage).
- **CloudWatch dashboard** `open-webui-metering` — spend rate, denies,
  degraded checks, sweeper refunds, canary status, reconciliation drift.
- **SNS topic** (output `MeteringAlertsTopicArn`) — subscribe for: user at
  80%/100% of quota, DLQ depth, the pricing/coverage alarm set (see the pricing
  runbook below), canary failures, reconciliation drift >5% (or manage
  subscriptions from the console's Module health page).

**Upgrading a pre-console metering deployment:** DynamoDB allows one GSI
creation per stack update and the console adds two. Deploy once with
`-c meteringGsiPhase=1`, then again without it. Fresh deploys need one pass.
Counters written before the upgrade enter the console's by-window listing on
their first settle/sweep/reset (≤15 min of traffic); ledger history backfills
automatically.

## Accuracy & reconciliation (what to tell finance)

Per-call numbers are **provider-reported token counts** priced from the
**single-source pricing catalog** in DynamoDB — one automated source (the AWS
Price List) plus operator overrides, nothing else. A nightly reconciler
compares the ledger against Cost Explorer's per-model mantle usage types
(unit = 1K tokens) at D-2 and publishes `Metering/ReconciliationDriftPct`;
**the measured 30-day drift is the accuracy claim** — no number is promised up
front. Notes:

- **Model pricing catalog** (console **Model pricing** page + `GET /pricing`):
  a scheduled refresher (`…-pricing-refresher`, daily + on-demand via "Refresh
  from AWS") parses the **three Bedrock offer files** for the deployment
  region (`AmazonBedrockFoundationModels` — the marketplace file that carries
  modern Anthropic/OpenAI grids — plus `AmazonBedrock` and
  `AmazonBedrockService`), joins each token rate to a **Bedrock model id** via
  `bedrock:ListFoundationModels` (operator alias → id embedded in the usage
  type → exact normalized-name match; anything else lands in the **Unmatched**
  review queue, never a guess), and writes `PRICING#<model_id>/PUBLISHED` rows
  keyed by every id the model is invocable under. Rates are stored **USD per
  1M tokens** exactly as published, as a full grid: routing (in-region /
  global) × tier (standard / batch / flex / priority / latency-optimized) ×
  context (default / long) × direction (input / output / cache read / cache
  write). Operators set per-model **overrides**
  (`PRICING#<model_id>/OVERRIDE`, audited, USD per 1M) in the console. The
  settle path and the admission estimate resolve through the **same shared
  resolver** (`metering/pricing/`): **override → AWS-published → unpriced**,
  cached ≤5 min. Cross-region **routing** is derived from the invoked id
  (`global.…` → global rate; `us.…` geo profiles price at the in-region rate —
  AWS publishes no on-demand geo token SKU) and stamped on every ledger row;
  a documented substitution (e.g. a flex request for a model publishing only
  standard) sets `rate_fallback`. Each settled row records `price_source` and
  the supplying row's offer version in `price_map_version`. Design:
  [`.kiro/specs/metering-pricing-single-source/design.md`](../.kiro/specs/metering-pricing-single-source/design.md).
- **Gateway↔pricing coverage join** (`PRICING#_COVERAGE`, console **Model
  pricing** coverage strip + `GET /pricing/coverage`): at the end of every
  refresh, the refresher joins what the gateway actually serves (the live
  catalog + the interceptor's `MODEL_CAPS`) against what the catalog prices, and
  writes one coverage item — per-model `{listed, catalog_available, priced,
  source, reason}` plus counts. This turns "a model is invokable but has no
  price" from a silent gap into a **named, alarmed** condition
  (`UnpricedGatewayModels`). Baseline measured 2026-08-20/21 (us-east-1, refresh
  generation 20): chat 41/46 priced, responses 6/13 priced, messages 5/5 priced;
  8 distinct invokable-unpriced models, all the GPT-5.x/GLM mantle
  publishing-gap family. *Expected* after this change deploys and the 7
  override-able models are entered: `invokable_unpriced` narrows to
  `[zai.glm-4.6]` (or `[]` if its lane is removed) — the parent records the live
  post-deploy count.
- **Unmatched Price List entries** (`PRICING#_UNMATCHED`, console Unmatched
  queue): AWS publishes a token rate but no model id could be resolved without
  guessing. These are classified by `reason` and split for alerting:
  - `no-control-plane-match` = **historical**: a legacy display name with no
    current control-plane twin (retired/marketing names — Claude 2.x-era). Kept
    and counted, collapsed by default in the console, but **not** alarmed — it is
    expected residue, not a work item. Baseline measured 2026-08-20/21: **49
    entries, all `no-control-plane-match`**.
  - `ambiguous-match` = **actionable**: the refresher found more than one
    candidate twin and refused to guess. This is what the `PricingUnmatchedActionable`
    alarm tracks (fires at ≥ 1). Resolve it by binding the name to a model id in
    the console (audited, `PRICING#_ALIAS`) and refreshing — bindings outrank
    automatic matching. Because today's 49 are all `no-control-plane-match`, the
    alarm is *expected* to read OK after this change deploys (a live validation
    signal); the parent records the post-deploy state.
- **Unpriced models** (no AWS-published rate and no override): usage is
  recorded in tokens, priced at $0, and the model surfaces three ways —
  proactively as `UnpricedGatewayModels` (the coverage join names it even before
  anyone calls it), and reactively as `UnpricedModel` (a settle-path invocation
  of an unpriced model) and `UnpricedAdmission` (the admission estimate resolved
  no rate). Set an operator override in the console's Model pricing page (USD
  per 1M tokens) to bring them under dollar quotas — never a silent guess.
  Settled ledger rows for unpriced models carry `unpriced: true` plus
  `usd_estimate` (the interceptor's admission-estimate dollars) so the console
  shows "~$X est." rather than a bare `$0` — a call that cost money reads as
  cost-unknown, not free. The `usd_estimate` is display-only and is never summed
  into the enforced counter (`used_usd`). **Admission is not blocked** for an
  unpriced model — the availability-first posture is preserved; the model just
  becomes visible and, once overridden, priced.
- **Call `lane`** (chat/completions · responses · messages) on a *settled*
  ledger row is filled from the matched admission estimate (the gateway
  interceptor observed the actual lane); the seeded filter also emits its
  best-effort lane so filter-only rows (e.g. the capture canary) aren't
  `unknown`. Direct-to-gateway callers produce an estimate but no filter
  event, so their spend appears as an OPEN reservation that the sweeper
  resolves — it never becomes a settled ledger row.
- Activate the projects' **cost-allocation tags** (Billing console → Cost
  allocation tags) after first deploy; tags take ≤24 h and are never
  retroactive.
- The reconciler intentionally queries **all** services (not just
  `Amazon Bedrock`) because Claude-on-Bedrock can invoice under the Anthropic
  marketplace entity.

## Pricing & coverage alarms — what each means and what to do

All are namespace `Metering`, alarm names prefixed with the deployment's env
prefix (default `open-webui-metering-…`), routed to the alerts SNS topic and the
console Module health page. Baselines are the 2026-08-20/21 measurement (refresh
generation 20); post-deploy states are *expected* until the parent records live
values.

| Alarm (`metricName`) | Fires when | Baseline | What to do |
|---|---|---|---|
| `UnpricedGatewayModels` | a model the gateway serves is invokable but has no resolvable rate (coverage join, ≥ 1) | 8 (the GPT-5.x/GLM family) | Run the override runbook below for the 7 model-card-published ids; escalate `zai.glm-4.6` (no AWS rate — do **not** invent one). *Expected* → 1 (`zai.glm-4.6`) or 0. |
| `UnpricedModel` | an unpriced model was actually **settled** (reactive) | OK | Same fix as above; this fires only on live traffic to an unpriced model. |
| `UnpricedAdmission` | the admission estimate resolved no rate for a request | — | Same underlying gap; confirms an unpriced model is being called, not just listed. |
| `PricingUnmatchedActionable` | an **ambiguous** Price List name (>1 candidate twin) is queued (≥ 1) | OK (all 49 unmatched are historical `no-control-plane-match`) | Open the console Unmatched queue, bind the ambiguous name to the correct model id (audited `PRICING#_ALIAS`), refresh. |
| `PricingDimensionUnclassified` | the parser saw a token usage type it could neither classify nor match to the exclusion list (≥ 1) | 0 (17 drops today, all intentional exclusions) | Inspect the `unclassified` list on `GET /pricing` / refresh meta; a new AWS usage-type shape needs a parser update — file it, don't hand-edit rows. |
| `PricingRateConflict` | two SKUs gave conflicting rates for the same model+leaf (kept the max, recorded the conflict) | 0 | Review the `rate_conflicts` entries on the refresh meta; usually a transient AWS publishing overlap — confirm the kept (max) rate is acceptable. |
| `PricingRefreshFailure` | a refresh run failed | OK | Old rates are retained (Req 4.4); check the refresher logs. A partial fetch (one offer file down) does **not** delete that file's rows. |
| `PricingCoverageComputed` (stale alarm) | **no** coverage computation datapoint for 2 daily periods — the refresher is not running at all, or dies before the coverage join | OK | The value alarms above stay quiet on missing data, so this heartbeat's *absence* is the signal. Check the EventBridge schedule and the refresher function; trigger a manual refresh (`POST /pricing/refresh`) and confirm the alarm clears. |

`PricingRoutingFallback` / `PricingTierFallback` are emitted (not alarmed) when a
request is priced from a substitute routing mode or tier; they surface on ledger
rows as `rate_fallback` for audit.

## Publishing-gap override runbook

When `UnpricedGatewayModels` names a model AWS serves but has not put in the
Price List bulk offer files (the GPT-5.x mantle family is the current example):

1. **Diagnose** — `scripts/diagnose-model-pricing.py --model <id>` runs the
   production parser + identity join + resolver against the live offer files and
   prints, per model, the SKUs found (or `0 hits`), the join outcome, and the
   named unpriced reason. `0 hits` in all three files = an AWS **publishing gap**,
   not a parser/join defect (the tool's own positive control is the priced
   `openai.gpt-oss-*` family).
2. **Find the rate from an AWS source only** — Bedrock **model-card doc pages**
   publish rates that are absent from the bulk API (e.g.
   `docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-sol.html`).
   Use the us-east-1 **In-Region**, Standard tier, short-context row. If AWS
   publishes no rate anywhere (as with `zai.glm-4.6`), **stop** — overriding it
   would invent a number; escalate as a lane-removal decision instead.
3. **Override with provenance** — in the console Model pricing page (or
   `PUT /pricing/{model}`), enter the per-1M input/output (and cache axes where
   published) and record the source page in the override `note`. Dated snapshot
   ids (`…-2026-03-05`) get their **own** override citing the base model-card —
   AWS publishes no per-snapshot rate, so no alias guessing.
4. **Gate before deploy** — `scripts/pricing-rate-diff.py` compares the effective
   per-model, per-leaf rates between two snapshots (live table vs about-to-ship
   compute, or two dumps). A clean run — or only the intended override additions —
   is the go signal. A rate *diff*, not a gap *count*, is what catches a matcher
   that "improved coverage" by matching less exactly.

Both scripts are Python-stdlib-only, read-only, and run the **production**
`metering/pricing` code — so "why is this model unpriced?" and "did any rate
move?" are each one command.

## Failure posture (chosen defaults, and the valves)

- **Fail-open with a grace budget:** if the quota store is unreachable, chats
  keep working — up to ~10 requests/user per degraded window — while
  `Metering/DegradedChecks` alarms. A 429 is only ever returned on a
  positively-read exceeded counter. (The platform default is the opposite:
  an interceptor crash blocks the request — this module inverts it in code.)
- **Sweeper `refund` mode (default):** an admission estimate that never
  settles (aborted stream, lost event, direct caller) is refunded after
  15 minutes — a lost usage event must not permanently consume quota. Strict
  deployments set `SWEEPER_MODE=settle` on the sweeper Lambda to charge the
  estimate instead.
- **Overage bound:** between crossing the limit and the next-call block, a
  user can finish in-flight requests — bounded by the max-tokens clamp to
  roughly cents per user per window.
- **Two canaries, hourly:** a block canary (over-quota synthetic user must
  get 429) and a capture canary (a usage event must settle). Enforcement and
  metering regress independently; each direction is alarmed.

## Rollbacks

| Layer | Rollback |
|---|---|
| Interceptor deploy | CodeDeploy canary auto-rolls-back on error-rate alarm; manual: redeploy the previous version through the alias |
| Enforcement itself | redeploy with `-c meteringMode=observe` (log-only) — no resource changes |
| Whole module | deploy without `--metering`: gateway returns to the base interceptor; metering stack can then be destroyed (`npx cdk destroy OpenWebUI-Metering -c metering=on`) |
| Filter only | deactivate the `metering` function in **Admin Panel → Functions** (capture stops; enforcement remains) |

## DLQ redrive

Failed debit events land in the `…-metering-debit-dlq` SQS queue (alarmed).
Replay is safe — settlement is idempotent (first-writer-wins on the ledger
key): use the Lambda console's SQS redrive, or
`aws sqs start-message-move-task --source-arn <dlq-arn>`. Verify zero
double-debits by checking `Metering/DuplicateSettles` (duplicates are counted
and skipped, not applied).

## Chaos drills (expected signals)

| Drill | Expected |
|---|---|
| Break the table name on the interceptor | chats fine; `DegradedChecks` rises; block canary FAILS within an hour |
| Deactivate the seeded filter | chats fine; capture canary fails; drift rises at D+2 |
| Kill the debit Lambda's permissions | DLQ depth alarm within minutes; redrive after fixing |
| Remove the block canary's policy row | canary re-creates it next run (self-healing) |

## Cost of the module itself

Sample scale (≈0.5M calls/month): interceptor + Lambdas + DynamoDB on-demand +
EventBridge + dashboard/alarms ≈ **$15–25/month** — noise against the Bedrock
token spend it governs (see `docs/COST_ANALYSIS_20K_USERS.md`).

## Known limits (deliberate)

- Quotas key off the user's **Cognito access-token claims**; local-password
  users (no OAuth session) are governed per-request but attribute as their
  gateway JWT — with the sample's SSO-only default this is everyone.
- Group rollups are advisory ceilings (async, seconds of lag); **exact
  chargeback always comes from the ledger / Cost Explorer**, never counters.
- Mid-stream cutoff is out of scope (the managed gateway owns the stream;
  no industry gateway does this either) — the bound is next-call blocking +
  the max-tokens clamp.
- The capability matrix drifts with the Bedrock catalog — regenerate
  `config/model-capabilities.json` (probe script) on a schedule; it is an
  input to the interceptor asset, so redeploy the gateway stack after.
  Pricing does NOT need a redeploy: the catalog refreshes itself daily from
  the AWS Price List (or on demand from the console).
