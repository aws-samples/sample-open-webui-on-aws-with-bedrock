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

- **Admin API** (stack output `AdminApiUrl`; Cognito JWT; mutating routes
  require membership in an admin group — `admins`, `webui-admins`, or `admin`):
  - `GET/PUT /policy/{scope}` — scope `DEFAULT`, `GROUP#<name>`, `USER#<sub>`;
    body `{"hard_limit_usd": 10, "soft_limit_usd": 8, "rpm_limit": 30}`
  - `GET /usage/{sub}?window=YYYY-MM`, `GET /usage/me`
  - `POST /override` `{"sub": "...", "hard_limit_usd": 20}` ·
    `POST /counter-reset` `{"sub": "..."}`
  - Every mutation writes an audit row; **self-targeted resets/overrides are
    rejected** — a second admin must act.
- **`scripts/set-quota.sh`** — thin CLI over the same API.
- **CloudWatch dashboard** `open-webui-metering` — spend rate, denies,
  degraded checks, sweeper refunds, canary status, reconciliation drift.
- **SNS topic** (output `MeteringAlertsTopicArn`) — subscribe for: user at
  80%/100% of quota, DLQ depth, unpriced-model, canary failures, drift >5%.

## Accuracy & reconciliation (what to tell finance)

Per-call numbers are **provider-reported token counts** priced by a versioned
map generated from the AWS Price List (`scripts/generate-price-map.py`;
regenerate monthly — the `PriceMapVersion` stack output tells you what's
deployed). A nightly reconciler compares the ledger against Cost Explorer's
per-model mantle usage types (unit = 1K tokens) at D-2 and publishes
`Metering/ReconciliationDriftPct`; **the measured 30-day drift is the accuracy
claim** — no number is promised up front. Notes:

- **Unpriced models** (today: Anthropic Claude and GPT-5.x have no mantle SKUs
  in the offer file): usage is recorded in tokens, priced at $0, and the
  `UnpricedModel` alarm fires. Add operator rates under `"overrides"` in
  `config/model-prices.json` to bring them under dollar quotas.
- Activate the projects' **cost-allocation tags** (Billing console → Cost
  allocation tags) after first deploy; tags take ≤24 h and are never
  retroactive.
- The reconciler intentionally queries **all** services (not just
  `Amazon Bedrock`) because Claude-on-Bedrock can invoice under the Anthropic
  marketplace entity.

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
- The capability matrix and price map drift with the Bedrock catalog —
  regenerate `config/model-capabilities.json` (probe script) and
  `config/model-prices.json` (price script) on a schedule; both are inputs
  to the interceptor asset, so redeploy the gateway stack after.
