# Metering, consumption governance, and quotas

[Documentation home](README.md) · [Architecture](GATEWAY_INTEGRATION_GUIDE.md) ·
[Deployment guide](AWS_DEPLOYMENT_GUIDE.md) · [Cost planning](COSTS.md)

The opt-in metering module adds a user-aware consumption control plane around
the unmodified Open WebUI application. It can answer three operational
questions the base sample cannot answer by itself:

1. What token usage and estimated model cost did Open WebUI persist for each
   Cognito user?
2. Should the next request be admitted under that user's monthly USD and RPM
   policy?
3. Which gateway-available models are missing a defensible price, and what
   accounting or capture paths are unhealthy?

Enable it with `./deploy.sh --metering`. It is off by default.

> [!IMPORTANT]
> This module is an **availability-first governance sample**, not an exact
> billing system or an unconditional cost ceiling. Read the enforcement
> contract and failure posture below before relying on a quota.

## Is this module a fit?

Use it to evaluate:

- per-Cognito-subject monthly USD and request-rate controls at the inference
  boundary;
- pre-request reservations followed by provider-usage settlement;
- an append-only usage ledger and user counters;
- best-effort team allocation through Bedrock Project/workspace headers;
- a single pricing store refreshed from the AWS Price List plus operator
  overrides;
- proactive gateway↔pricing coverage and operational alarms; and
- separate admin and self-service Cloudscape views.

Do **not** treat it as:

- a monthly or daily token-quota engine;
- enforced group/team quota policy;
- an atomic RPM limiter or zero-overshoot hard limit;
- mid-stream cutoff;
- guaranteed capture for every model/API/upstream release;
- durable billing for direct-to-gateway callers under the default refund mode;
- exact deployment-scoped invoice reconciliation; or
- a reason to call the sample production-ready.

## Lifecycle

![Optional metering lifecycle showing real-time admission, asynchronous settlement, pricing coverage, estimate recovery, operator controls, and assurance signals.](images/metering-flow-light.svg#gh-light-mode-only)
![Optional metering lifecycle showing real-time admission, asynchronous settlement, pricing coverage, estimate recovery, operator controls, and assurance signals.](images/metering-flow-dark.svg#gh-dark-mode-only)

The editable source is [`diagrams/metering-flow.mmd`](diagrams/metering-flow.mmd).
This is a mechanism sequence; the canonical system topology remains in the
[gateway guide](GATEWAY_INTEGRATION_GUIDE.md#canonical-architecture).

## Enforcement contract

### Scope and policy precedence

Admission keys usage by Cognito `sub` and UTC calendar month. The effective
policy is:

1. `USER#<sub>` policy, when present;
2. explicit `DEFAULT` policy, when present; or
3. deployment defaults.

Group policies and group counters are stored and displayed as advisory team
ceilings. The gateway admission path does not read them.

Default interceptor values are:

| Control | Default | Semantics |
|---|---:|---|
| Hard limit | USD 5 per user/month | A request is denied when previously recorded `used_usd + max(est_usd, 0)` is already at or above the limit. |
| Soft limit | USD 4 | The seeded Open WebUI filter can show a throttled warning toast from the user's counter snapshot. |
| Request rate | 30 requests/minute | Per-user UTC-minute bucket; the read and increment are not one atomic operation. |
| Output clamp | 8,192 tokens | Applied to the lane's output-token field before forwarding. |
| Store-degradation grace | 10 requests/user/5-minute Lambda-container window | In ENFORCE mode, subsequent failed store checks return 429 until the local grace window resets. Scale-out creates more than one local grace budget. |

A hard or RPM value at or below zero disables that check.

### What happens before a request

For a parseable inference request with a locally verified Cognito subject, the
interceptor:

1. performs one eventually consistent batch read for the user counter, user and
   default policies, and RPM bucket;
2. estimates input tokens from serialized request bytes and output tokens from
   the clamped request limit;
3. resolves the current model rates from DynamoDB;
4. decides using **already recorded** spend and RPM usage;
5. writes an RPM tick and a conditional OPEN estimate; and
6. injects the lane-appropriate Bedrock Project/workspace header when a project
   mapping exists.

A request that crosses the limit is admitted; the next request sees the updated
counter and can be denied. Concurrent admissions and eventually consistent RPM
reads can increase that overshoot. The estimate is conservative bookkeeping,
not provider tokenization.

In ENFORCE mode, a deny returns HTTP 429 with an OpenAI-shaped JSON error. The
live block canary verifies the transport-level Chat Completions response. This
repository does not contain browser automation proving the exact presentation
for every Open WebUI release and all three lanes.

### OBSERVE mode

ENFORCE is the metering default. To evaluate decisions without denying for
healthy over-limit/RPM reads, use the supported deploy flag:

```bash
./deploy.sh --metering --metering-mode observe \
  --profile YOUR_PROFILE --region us-east-1
```

You can instead set `METERING_MODE=observe` in the ignored `.env` file and keep
using `./deploy.sh --metering`. OBSERVE still writes RPM and reservation state;
it changes healthy deny decisions to log-and-forward. The accepted values are
`enforce` and `observe`.

### Legacy console-GSI upgrade

Fresh metering deployments create both console indexes in one pass. A metering
table created before the console indexes may require two CloudFormation updates
because DynamoDB permits one GSI addition per table update:

```bash
# First pass: add by-window only
./deploy.sh --metering --metering-gsi-phase 1 \
  --profile YOUR_PROFILE --region us-east-1

# After that update completes: add by-sub
./deploy.sh --metering \
  --profile YOUR_PROFILE --region us-east-1
```

Use this only after confirming the live table lacks both indexes. Do not leave
the phase-1 flag in routine deploy configuration.

## Usage capture and settlement

When metering is enabled, the Compute stack delivers and seeds
[`pipe/metering_filter.py`](../pipe/metering_filter.py). It is a global Open
WebUI filter with two fail-soft hooks:

- **inlet:** reads the user's counter for a rate-limited soft-limit warning;
- **outlet:** reads normalized usage persisted on the latest assistant message
  and emits one EventBridge usage event.

The interceptor forces `stream_options.include_usage=true` only for streamed
Chat Completions. Responses and Messages depend on provider/Open WebUI usage
normalization. If no nonzero usage is persisted, the outlet emits nothing.

The debit Lambda prices the event and attempts one DynamoDB transaction that:

- inserts a unique `LEDGER#<day>` row;
- adds actual USD/input/output/request counts to the monthly user counter;
- subtracts the matched reservation; and
- changes the matched estimate from OPEN to SETTLED.

Provider response ID is the preferred idempotency input. Duplicate ledger puts
are counted and skipped. Estimate matching uses the oldest OPEN estimate for the
same subject and canonical model from a bounded query; concurrent requests can
associate an event with the wrong same-model estimate even though the actual
usage still lands once.

Ledger rows expire after roughly 15 months. User counters and policies do not
use the same TTL. Resetting a counter does not delete ledger history or close
OPEN estimates; it is an enforcement reset, not an accounting correction.

## Orphaned estimates and direct callers

A five-minute sweeper looks for OPEN estimates older than 15 minutes:

- **`refund` (deployed default):** close as REFUNDED and subtract the estimate;
- **`settle`:** close at the estimate and create an estimate-based ledger row.

The stack currently hard-codes `refund`; it exposes no `deploy.sh` flag for
strict settlement. A direct caller can receive inference through the gateway
without producing an Open WebUI filter event, so its reservation is normally
refunded after the stale window. Do not describe default direct-gateway traffic
as durably metered.

## Pricing and coverage

### One runtime price store

Admission and settlement import the same resolver from [`metering/pricing/`](../metering/pricing/).
Rate precedence is:

1. model-specific operator `OVERRIDE` row;
2. AWS-published `PUBLISHED` row; then
3. unpriced.

The scheduled/on-demand refresher reads three regional AWS Price List offer
files, Bedrock control-plane model metadata, the Mantle catalog, the served
capability snapshot, and operator aliases. It writes model-keyed rates,
unmatched-review rows, refresh metadata, and `PRICING#_COVERAGE`.

A new deployment does not synchronously seed the pricing catalog. After the
first metering deploy, sign in to the console as an admin and select **Model
pricing → Refresh from AWS**, or wait for the daily schedule, before treating
USD policies as meaningful.

### Unpriced models remain available

If either input or output rate cannot be resolved, the call remains available,
its tokens are recorded, and its authoritative settled cost is `$0` with
`unpriced: true`. The coverage join and reactive metrics surface the gap; they
do not invent a price or remove the model.

Use [`scripts/diagnose-model-pricing.py`](../scripts/diagnose-model-pricing.py)
to distinguish parser/join failures from an AWS publishing gap. Add an operator
override only from a defensible source and record that provenance in the note.
Do not copy a number from a dated planning document.

### Coverage is an observation, not an invocation test

The coverage item joins:

- lane membership served by the interceptor;
- model IDs currently reported available by the Mantle catalog;
- Bedrock control-plane presence; and
- resolvable published/override rates.

It does not invoke every model. A Mantle-catalog fetch failure writes partial
coverage with an error; inspect refresh health as well as the unpriced gauge.

## Team attribution

[`config/metering-groups.json`](../config/metering-groups.json) defines ordered
Cognito groups. The Gateway stack provisions a Mantle Project per configured
group plus a catch-all and freezes the group→project map into the live
interceptor version. The interceptor chooses the first configured group and
adds:

- `OpenAI-Project` for Chat Completions/Responses; or
- `anthropic-workspace-id` for Messages.

This creates an attribution path, not an automatic finance guarantee. Activate
the project cost-allocation tags in Billing, account for propagation delay and
non-retroactivity, and validate the resulting CUR/Cost Explorer dimensions.
Requests with no configured group use the catch-all. DynamoDB group rollups are
asynchronous and advisory; exact application usage comes from ledger rows, and
billed cost comes from AWS billing data.

## Console and API

The metering deployment builds a React/Cloudscape SPA and serves it from a
private S3 origin through a separate CloudFront distribution. A dedicated
public Cognito app client uses authorization code + PKCE. API Gateway validates
JWT audiences; the Lambda separately checks Cognito group claims for admin
routes.

- **Any authenticated pool user:** module config, own current usage, own ledger.
- **Members of `admin`, `admins`, or `webui-admins`:** users/groups, policies,
  audit, estimates, metrics, alarms, subscriptions, pricing/coverage, counter
  resets, and pricing refresh.

Client-side navigation mirrors these roles, but the API is the authorization
boundary. Admin mutations are audited. Self-targeted user policy changes,
overrides, and counter resets are rejected so a second admin must act.

### Policy caveats

- `until` is a future-date marker for operator cleanup; enforcement does not
  ignore or delete an expired policy automatically.
- `GROUP#...` policy rows are advisory, not admission inputs.
- A counter reset does not rewrite the ledger or resolve reservations.
- The pricing surface has a bounded full-table-scan fallback for older
  installations whose catalog metadata lacks the current key inventory.

For a populated, non-sensitive evaluation view, follow the authorized
seed/capture/cleanup procedure in
[`images/SCREENSHOT-SPEC.md`](images/SCREENSHOT-SPEC.md). The demo script creates
no Cognito user, uses a unique conditional namespace, and blocks a second run
until its manifest is cleaned up.

## Assurance and failure posture

| Mechanism or failure | What it proves or does | What it does not prove |
|---|---|---|
| Bad/unknown local JWT verification | Interceptor passes through and emits a degraded signal; AgentCore remains the upstream JWT authorizer | That an invalid JWT can bypass AgentCore |
| DynamoDB read failure | Requests pass within the per-container grace budget; ENFORCE returns 429 after that local budget is exhausted | Permanent fail-open or one account-wide grace count |
| Reservation/metric/filter failure | Logs/signals and normally preserves chat | Complete accounting for the affected request |
| Missing price | Admits and settles at $0, records tokens, emits coverage/traffic signals | Free inference or a hard pricing gate |
| Block canary | Real Cognito auth + real gateway Chat Completions path returns the expected quota 429 | Browser rendering or Responses/Messages blocking UX |
| Capture canary | Synthetic EventBridge event reaches debit/counter settlement | Open WebUI filter health, real model usage, or estimate matching |
| Disable seeded filter | Stops new Open WebUI usage events while chat continues | Capture canary failure—the canary bypasses the filter |
| Nightly reconciliation | Compares D-2 ledger token totals with matching account Cost Explorer usage types above a volume floor | Deployment/project-isolated invoice accuracy |
| Group stream rollup | Provides a low-cost advisory aggregate | Exactly-once team accounting under stream redelivery |

The CloudWatch dashboard includes degraded checks, but the stack does not create
a dedicated `DegradedChecks` alarm. Canary alarms treat missing data as
not-breaching, so schedule/function health also deserves operator review.

## Enable and validate

```bash
./deploy.sh --metering --profile YOUR_PROFILE --region us-east-1
```

After deployment:

1. record the `ConsoleUrl`, `AdminApiUrl`, and alerts-topic outputs;
2. add the operator to a recognized Cognito admin group and sign in;
3. refresh pricing and inspect `PRICING#_COVERAGE` on **Model pricing**;
4. set an explicit DEFAULT policy only after confirming priced coverage;
5. activate and validate project cost-allocation tags if team attribution is
   required;
6. subscribe a verified endpoint to the alert topic;
7. verify block/capture canary metrics after their schedules run; and
8. test each enabled lane with non-sensitive prompts while inspecting ledger
   rows and reservations.

Keep `--metering` on every full deploy that should retain the integration.
Omitting it removes metering wiring from newly synthesized Gateway/Compute
resources but does not automatically delete an already-existing metering stack.

## Common operations

### Review module health

Use **Module health** in the console for alarms, metrics, and SNS subscriptions.
Also inspect:

- debit DLQ depth;
- pricing refresh status and coverage timestamp/error;
- unpriced invocation/admission metrics;
- OPEN estimate age/count;
- block and capture canary datapoints; and
- reconciliation scope before interpreting drift.

### Redrive debit failures

Failed events land in the metering debit DLQ. Settlement ledger puts are
idempotent, but redrive only after fixing the failure and reviewing the event
shape. Verify `DuplicateSettles` and user counters after replay.

### Roll back enforcement

Redeploy with `--metering --metering-mode observe`. This preserves resources
and accounting while changing healthy over-limit decisions to log-and-forward.

### Disable capture only

Deactivating the seeded metering function in Open WebUI stops new filter events
but leaves admission active. The capture canary remains green because it begins
at EventBridge; watch for missing real ledger activity and later reconciliation
signals.

### De-wire the module

A full deploy without `--metering` restores the base gateway interceptor and
omits metering assets/environment from the new task definition. The existing
Metering stack is not automatically destroyed and can keep billing. Resource
deletion is a separate, destructive operation; follow the deployment guide's
[cleanup](AWS_DEPLOYMENT_GUIDE.md#cleanup) warnings.

## Known limits

- Per-user USD/RPM policy only; no enforced token or group quota.
- Next-request enforcement with concurrency/consistency overshoot.
- Input estimate uses serialized bytes rather than provider tokenization.
- All-lane persisted usage is not guaranteed by the interceptor.
- Direct gateway traffic normally refunds after 15 minutes.
- Estimate matching is oldest same-subject/same-model within a bounded query.
- Missing rates settle at $0 and do not block.
- Group rollup can double-add after partial stream redelivery.
- Reconciliation is account-wide and D-2, not an invoice subsystem.
- Policy expiry is manual.
- No browser test in this repository proves all-lane quota-error presentation.
- The base sample's metering-off isolation is implemented through conditional
  CDK wiring, but no committed synth-diff test currently gates byte identity.

## Implementation map

| Area | Source |
|---|---|
| Admission and request mutation | [`gateway/metering-interceptor/`](../gateway/metering-interceptor/) |
| Usage capture and seeding | [`pipe/metering_filter.py`](../pipe/metering_filter.py), [`pipe/metering_seed.py`](../pipe/metering_seed.py) |
| Settlement | [`metering/debit/`](../metering/debit/) |
| Pricing and coverage | [`metering/pricing/`](../metering/pricing/), [`metering/pricing-refresher/`](../metering/pricing-refresher/) |
| Recovery and derived data | [`metering/sweeper/`](../metering/sweeper/), [`metering/rollup/`](../metering/rollup/), [`metering/reconciler/`](../metering/reconciler/) |
| Assurance | [`metering/canary/`](../metering/canary/) |
| API and UI | [`metering/admin-api/`](../metering/admin-api/), [`console/`](../console/) |
| Infrastructure | [`infra/lib/metering-stack.ts`](../infra/lib/metering-stack.ts), [`infra/lib/metering-console.ts`](../infra/lib/metering-console.ts) |

The dated design investigations and rejected alternatives are indexed under
[`plans/`](plans/README.md). They are historical evidence, not current operator
guidance.
