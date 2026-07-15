<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# 02 — Design: Token Metering, Consumption Tracking, and Quota Enforcement

> **Status: PROPOSED — HUMAN SIGN-OFF REQUIRED.**
> This document recommends one architecture and records the alternatives it rejected.
> Nothing here is built. The recommendation, the in-repo-vs-companion decision, and the
> fail-open stance are decisions the operator (you) must ratify before the build run.

Evidence keys used throughout: `[S#]` = live spike finding ([`04-SPIKE-FINDINGS.md`](04-SPIKE-FINDINGS.md)),
`[A]`/`[B]`/`[C]`/`[D]` = research tracks ([`research/`](research/)), `[repo]` = file in this
repository, `[owui@v0.10.2]` = upstream source at the pinned tag, `[unverified]` = explicitly
not proven. Account IDs in examples are placeholders (`123456789012`).

---

## 1. The problem, restated as constraints

An enterprise runs the unmodified official Open WebUI image for thousands of Cognito users.
Every chat turn becomes one or more streamed Bedrock calls through the AgentCore inference
gateway. Requirements: **meter** (who consumed what, per call, durable + queryable),
**track** (rollups per user/group/cost-center, operator-visible, alerting), **enforce**
(operator-set quotas that actually block). Constraints that shape everything:

1. **Zero Open WebUI modification** — capture and enforcement must live in AWS infra,
   gateway config, seeded DB config, or the pipe we own [repo README].
2. **Streaming SSE everywhere** — cost is known only at end-of-stream, and the gateway's
   RESPONSE interceptors do not run for streaming responses on inference targets
   (docs: "Interceptors are not yet supported in streaming mode" [C]). There is **no
   server-side hook of ours inside the response stream.**
3. **Identity is already in-path** — every call carries the user's own Cognito access
   token; the REQUEST interceptor receives it verbatim (`passRequestHeaders: true`) [S1].
4. **The bill is the truth** — finance trusts Cost Explorer/CUR, which carries per-model
   mantle usage types (`USE1-{model}-mantle-{input|output}-tokens-standard`, unit = 1K
   tokens) [A][live CE probe 2026-07-14].

## 2. What the substrate actually offers (verified inventory)

The design space collapsed considerably once each candidate primitive was tested against
*our* path (gateway → `bedrock-mantle`), not Bedrock-in-general:

| Primitive | Verdict for our path | Evidence |
|---|---|---|
| REQUEST interceptor: sees every call, user JWT, full body; can **mutate body+headers**; can **short-circuit any status (429 proven)**; fail-closed on crash | ✅ the universal control point | [S1][S2][S5][S7][C] |
| RESPONSE interceptor on streaming | ❌ not invoked | [C] docs quote |
| `usage` in the stream, all 3 lanes, **through** the gateway | ✅ survives passthrough (chat needs `stream_options.include_usage`; Responses/Messages automatic) | [S4][D] |
| Interceptor can force `include_usage` server-side | ✅ body-rewrite proven E2E | [S5] |
| Open WebUI persists per-message `usage` + runs **global filter functions** (inlet/outlet) on every model; both seeded via DB, zero image change | ✅ palette-approved capture + UX surface | [owui@v0.10.2 middleware.py, filter.py, functions.py][repo pipe/seed.py] |
| Bedrock **Projects** (mantle-native): per-request attribution header, validated server-side; interceptor can inject it; CW `Project` dimension + cost-allocation tags → CE/CUR | ✅ per-*team* attribution spine (≤1000 projects/acct) | [S6][A] |
| Application Inference Profiles | ❌ bedrock-runtime only; AWS says "use Projects" for mantle | [A] doc quote |
| Bedrock model-invocation logging | ❌ does not capture mantle traffic | [A] doc quote |
| `AWS/BedrockMantle` CW metrics (tokens by Project×Model) | ⚠️ documented but unreliable in practice — spike-night traffic absent 4 h later, only 3 of 38+ billed models ever present; best-effort alarms only | [S8][A] |
| Gateway CW metrics / vended logs / Cedar policy for inference | ❌ no token metrics, no per-user dims, no usage state in Cedar, "token limit policy" referenced in docs but absent from the API | [C] |
| mantle per-model TPM service quotas (account-wide) | ✅ coarse backstop; no RPM quotas; output-TPM even cuts generation mid-stream | [A] |
| AWS Budgets (+ Actions), Cost Anomaly Detection, CUR 2.0 | ✅ finance layer; 8–24 h latency class; Actions can attach deny-policies | [A] |
| AWS Price List API carries mantle SKUs (per-1K-token USD) | ✅ machine-readable price map source | [live GetProducts probe 2026-07-14] |
| DynamoDB atomic `ADD` + `ConditionExpression` | ✅ serialized counter with threshold-at-write; 3–5 ms in-path | [S3][B] |

And from prior art [B], the patterns that survive: **pre-request check + async post-debit
(next-call enforcement)** is the industry default (LiteLLM, Cloudflare, Kong, Envoy);
**mid-stream cutoff is rare and non-default** everywhere; **fail-open on metering-infra
failure** is the availability default with fail-closed as an explicit strict mode;
counter+cache hierarchies (LiteLLM's Redis+DB) produced their worst production bugs —
prefer one serialized counter with one write path.

## 3. Architecture options considered

### Option 1 — "Own the stream": replace the connector with a self-hosted streaming proxy

Swap the gateway's `bedrock-mantle` connector target for a **provider target** pointed at
our own proxy (Lambda URL/ALB+Fargate) that forwards to mantle with SigV4 and tees the SSE
stream, capturing `usage` server-side for every call and even cutting streams mid-flight.

- **Pros:** perfect per-call capture on all lanes, immune to client aborts; true mid-stream
  enforcement; one place for everything (this is the LiteLLM shape — AWS's own
  `genai-gateway` sample ships exactly this [B]).
- **Cons:** we now own a **streaming proxy on the hot path of every model call** —
  availability, scaling, TLS, SSE-keepalive, latency (+1 hop), and its failure takes down
  all chat. It duplicates what the managed gateway exists to do, forfeits the
  gateway-native governance story the sample is built around, and roughly doubles the
  moving parts of the sample. Also the provider-target path re-opens the model-routing
  and signing questions the connector solved.
- **Verdict: REJECTED** as the default architecture — disproportionate operational burden
  for a *sample*, and it abandons the substrate's thesis (managed gateway). Recorded as
  the known escape hatch if AWS never ships response-side interception and per-call
  server-side truth becomes a hard requirement. (If an enterprise needs exactly this,
  AWS already publishes `aws-samples/genai-gateway`; our sample should not become it.)

### Option 2 — "AWS-native only": Projects + CloudWatch + Budgets, no custom ledger

Interceptor injects a per-team Project header; consumption is read from
`AWS/BedrockMantle` metrics and CE/CUR by project tag; enforcement = mantle TPM quotas +
AWS Budgets Actions attaching a deny-policy to the gateway role at thresholds.

- **Pros:** near-zero custom code; finance-grade attribution by construction; no
  counter store to operate.
- **Cons — disqualifying:** granularity stops at ~team (1000-project cap vs 20K users;
  per-user projects impossible) [A]; metrics observed **≥2.5 h delayed and incomplete
  across models** [S8]; Budgets evaluate ~3×/day with 8–24 h data lag and Actions can't
  do daily windows [A]; a Budgets deny-policy on the gateway role blocks **everyone**,
  not the offending user. "Per-user monthly quota that actually blocks" is simply not
  expressible in this stack today. Negative finding recorded.
- **Verdict: REJECTED as the whole answer — adopted as two layers** (attribution spine +
  catastrophic backstop) of the recommendation.

### Option 3 — "App-side only": seeded filter meters + enforces inside Open WebUI

A seeded **global filter function** (same delivery as the pipe: `seed.py` upserts a row —
palette-approved) meters from `outlet()` (which receives per-message `usage`
[owui@v0.10.2]) and enforces from `inlet()` (raise → error renders in chat).

- **Pros:** zero new infra on the request path; the best UX surface (inlet raise renders a
  friendly message; outlet can toast at 80%); usage capture is server-side *within our
  trust boundary* (the ECS task consumes the stream even if the browser goes away, except
  on explicit user stop).
- **Cons — disqualifying alone:** the enforcement lives in the *client* of the gateway.
  Any user with their own Cognito token can `curl` the gateway directly and bypass the
  filter entirely (the gateway is a public regional endpoint; CUSTOM_JWT admits any valid
  pool token) — the quota would be advisory, not real. Filter execution can also be
  bypassed by internal task flows (`bypass_filter`) and doesn't govern non-OWUI clients
  at all. Fails requirement 3 ("actually prevent").
- **Verdict: REJECTED alone — adopted as the metering capture + soft-tier UX layer** of
  the recommendation.

### Option 4 — "Gateway-enforced hybrid" ✅ RECOMMENDED

Enforce at the **gateway REQUEST interceptor** (the one point every lane and every client
must pass); meter from the **stream's own usage tail** captured by a seeded global filter
(+ the pipe, which already emits usage); attribute AWS-natively with **per-team Projects**;
reconcile against **CE/CUR**; backstop with **TPM quotas + Budgets**. Detailed below.

- **Why this wins:** every component is an existing palette item; nothing sits in the
  response stream (so chat latency/availability are untouched — the only in-path addition
  is ~2–5 ms of Lambda+DynamoDB, spike-measured [S2][S3]); enforcement is at the only
  chokepoint that governs *all* clients; metering uses the most accurate per-call source
  that exists on this path (the provider's own usage block); and finance gets numbers that
  reconcile to the invoice by construction (same usage types, daily job).

### Option 5 — Wait for AWS ("token limit policies")

Gateway docs already *reference* per-target token-limit policies and the June-2026 release
notes tease per-model token limits, but the control-plane API has no such field and the
linked docs page 404s [C]. Cedar policy cannot read usage state [A][C]. IAM principal
billing attribution for mantle is "coming soon" [A].
- **Verdict: REJECTED as a plan** ("coming soon" is not a design), but the runbook's final
  phase includes a re-check gate: if AWS ships native token limits/usage plans, layers of
  this design (notably E1's custom counter) get deleted, not extended. Design for
  replaceability.

## 4. The recommended architecture (Option 4, in full)

```
                                End users
                                    │ HTTPS
                            ┌───────▼────────┐
                            │   CloudFront    │
                            └───────┬────────┘
┌───────────────────────────────────┼─────────────────────────────────────────────┐
│ VPC                        ┌──────▼───────┐        Open WebUI DB (Aurora)        │
│                            │ ECS Fargate   │   chat.message.usage  ◄─────────┐   │
│                            │ UNMODIFIED    │                                 │   │
│                            │ Open WebUI    │  seeded at boot (pipe/seed.py): │   │
│                            │               │   • Claude pipe (exists today)  │   │
│                            │               │   • metering FILTER (new):      │   │
│                            │               │     inlet  → soft-warn/deny UX  │   │
│                            │               │     outlet → usage → EventBridge├───┼──► M1
│                            └──────┬───────┘                                      │
└───────────────────────────────────┼──────────────────────────────────────────────┘
                 user's own JWT     │  3 lanes (gw / gwr / pipe), streaming SSE
                        ┌───────────▼─────────────────────────┐
                        │  AgentCore inference gateway         │
                        │  CUSTOM_JWT (Cognito) ── validates   │
                        │  REQUEST interceptor Lambda (E1):    │
                        │   1. /v1/models → capability filter  │  ◄── exists today
                        │   2. decode JWT → user + groups      │
                        │   3. DynamoDB quota read (~4 ms)     │──► over hard limit?
                        │   4. over → SHORT-CIRCUIT 429 + msg  │      429 renders in chat
                        │   5. under → inject include_usage,   │
                        │      clamp max_tokens,               │
                        │      inject OpenAI-Project /         │
                        │      anthropic-workspace-id (M2)     │
                        └───────────┬─────────────────────────┘
                        ┌───────────▼───────────┐
                        │ Amazon Bedrock (mantle)│ usage tail rides the SSE stream back
                        └───────────┬───────────┘ through gateway+app untouched [S4]
                                    │
   ══════════════ metering & control plane (new, opt-in CDK stack) ═══════════════
                                    │
   M1 usage events ──► EventBridge bus ──► debit Lambda ──► DynamoDB:
                                    │         • usage_ledger (append, TTL 15 mo)
                                    │         • usage_counter (atomic ADD per window)
                                    │         • threshold events → SNS (80%/100%)
   M2 attribution  ──► Bedrock Projects (per group/cost-center) ──► CW BedrockMantle
                       metrics by Project × Model + cost-allocation tags
   M3 reconciliation ─► nightly Lambda: Σ ledger vs CE/CUR mantle usage types
                       → drift metric + alarm (>5%)
   E4 backstops   ──► mantle TPM quotas (exist) · AWS Budgets (+optional Action:
                       deny bedrock-mantle:CreateInference on gateway role) · CAD
   Operator surface ─► admin API (API GW + Lambda, Cognito-admin-gated) + CLI
                       script + CloudWatch dashboard + SNS alerts + canary
```

### 4.1 The metering plane — hard question 1 & 2 (meter where; source of truth)

**Meter from the stream's own `usage` block, captured server-side in the app tier;
attribute and reconcile from AWS billing telemetry.** Three sources, three jobs — because
no single source on this path is simultaneously per-call-accurate, per-user, real-time,
and invoice-authoritative:

- **M1 — operational per-call ledger (primary).** The provider's `usage` block is the only
  per-call token truth available on this path (gateway response interception is
  impossible for streams [C]; invocation logging doesn't cover mantle [A]). It already
  reaches the app on all three lanes [S4], and Open WebUI normalizes + persists it
  per message [owui@v0.10.2 `middleware.py`]. Capture = a **seeded global filter
  function** (delivered by `seed.py` exactly like the pipe — DB row, no image change):
  `outlet()` receives `messages[].usage` for every model and forwards
  `{user, group(s), model, chat_id, message_id, input_tokens, output_tokens,
  cached_tokens, ts}` to EventBridge. The Claude pipe already emits OpenAI-shape usage
  (`EMIT_USAGE`) [repo pipe/gateway_anthropic_pipe.py], so the same filter covers it.
  Two hardening moves close the capture gaps: the **interceptor injects
  `stream_options.include_usage`** into chat-completions bodies that lack it [S5]
  (so capture never depends on Open WebUI's per-model `usage` capability flag), and the
  seeder sets that capability flag on seeded models anyway (belt + braces, and it gives
  users the native per-message token display).
- **M2 — AWS-native attribution spine (per team/cost-center).** The interceptor maps the
  caller's `cognito:groups` → a **Bedrock Project** and injects `OpenAI-Project`
  (chat/completions, responses) or `anthropic-workspace-id` (messages) [S6]. The
  load-bearing payoff is **billing**: the project's cost-allocation tags put per-team
  dollars in Cost Explorer and CUR 2.0 with zero custom accounting [A, docs; tag flow
  itself `[unverified-live]` — CE lags ~24 h, a Phase-3 gate]. The `AWS/BedrockMantle`
  CW `Project` dimension is treated as **best-effort only**: the spike's tagged traffic
  never appeared in the namespace even 4 h later [S8], so no design layer depends on it.
  Projects cap at 1000/account [A] — deliberately per-*team*, not per-user; per-user
  lives in M1.
- **M3 — financial truth + reconciliation.** CE/CUR mantle usage types are the invoice
  (unit = **1K tokens** — confirmed two ways: the price dimension's declared unit and
  exact cost math on live rows, e.g. 4.77 × $0.00015 = $0.0007155 [D]). A nightly Lambda
  (run ~T+26 h) sums M1's ledger by (model, direction, tier, day) and compares to CE
  ×1000 (monthly close against CUR 2.0); publishes `metering/ReconciliationDriftPct`;
  recommended thresholds: page at |drift| > 5% or > $X absolute, ticket at > 2% sustained
  3 days, floor at 100K tokens/day below which drift is statistically meaningless — these
  are recommendations, not citations; no published industry drift SLO exists [D]. The
  **price map** is a versioned snapshot of the AWS Price List offer file
  (332 mantle usage types across 41 models live-parsed [D]), keyed
  **(region, model, direction, tier)** — tier is part of the usage type
  (`-standard|-batch|-flex|-priority`). Two hard-won rules: (1) **repricing can be
  backdated** (offer terms carry `effectiveDate` days before `publicationDate` [D]), so
  ledger rows store *tokens* as the invariant and stamp `price_map_version` — dollars are
  re-derivable — the ledger row therefore carries `{tokens, tier, rate_used,
  price_map_version, state}` so a backdated reprice triggers a re-rate of the affected
  window plus a documented counter-adjustment, not a mystery mismatch; (2) the map needs
  an explicit **`unpriced` state — and it is not an edge case**: Claude and GPT-5.x, the
  sample's headline models, sit in the live mantle catalog but have **no mantle SKUs in
  the offer file and none in this account's CE month-to-date** [D]. Policy: an unpriced
  model is a **blocking onboarding state** — it stays out of quota-governed lanes until
  the operator enters a rate or the SKU appears (never silently priced at $0 or a
  guess); a pre-GA probe (one billed Claude/GPT-5 mantle call → re-query CE + offer file
  at T+24–48 h) resolves where they actually bill `[unverified until run]`. Related
  trap: Claude-on-Bedrock spend can invoice under the **Anthropic legal entity** rather
  than `Service="Amazon Bedrock"` [A] — the M3 query must include those line items or
  the biggest spend bucket silently exits the comparison; (3) **tier discipline**: the
  interceptor clamps `service_tier` to `standard` by default (body rewrite [S5],
  documented), so ledger, price map, and invoice stay on one tier; deployments enabling
  flex/priority carry `tier` through all three instead.

**Why the app-tier capture is trustworthy enough to bill against internally:** the ECS
task — inside our trust boundary — consumes the SSE stream server-side and persists usage
even when the browser disappears; the loss cases are (a) explicit user "stop generation"
(task cancelled before the usage tail arrives — the provider's own contract warns "If the
stream is interrupted or cancelled, you may not receive the final usage chunk", while AWS
bills every token generated to that point [D]), (b) internal task calls (title/tag
generation) that bypass filters, and (c) **non-OWUI clients calling the gateway directly
with their own token** — all three are *undercounts*, never double-counts, and the M3
reconciler is designed to *bucket* the residue (token-count match first to isolate
price-map error, then the abort/unmetered attribution) rather than just alarm on it [D].
(c) is also independently rate-limited and attributable (below). **Idempotency:** every
capture writes to one ledger with first-writer-wins (`attribute_not_exists`) on a
deterministic key — preference order: **provider response id** (`chatcmpl-…` / `resp_…` /
`msg_…`, present on all three lanes' usage events [D]), fallback
`(chat_id, message_id)`, last-resort gateway trace id. That single rule removes the
pipe-lane double-count risk (the pipe's emitted usage and OWUI's persisted usage are the
same event → same key → one row) and keeps one write path to the counter, the LiteLLM
lesson [B][D].

**Accuracy ladder (what finance is told):** per-call M1 numbers are provider-reported
token counts priced by our versioned map — **estimates of the invoice with no promised
accuracy number**: the honest claim is "accuracy is whatever M3 measures; expect a
calibration period, after which the published 30-day measured drift *is* the accuracy
statement" (no spike measured end-to-end ledger-vs-invoice accuracy, and no industry
drift SLO exists to cite [D] — asserting "1–2%" here would be the exact credibility
defect a FinOps reviewer rejects). Per-team M2 dollars in CE are *the invoice* (daily
grain, once tags verify); M3 continuously proves the first against the second and pages
when they diverge. We never ask finance to trust a tokenizer — or an unevidenced
percentage.

**Chargeback attribution rules (FinOps review):** each debit carries exactly one
**`billing_group`** — resolved by documented precedence from `cognito:groups` (the same
rule drives the project-header injection), so per-group invoices sum to the bill; the
other group counters are **ceiling-only** and excluded from chargeback. Ledger
attribution is **frozen at debit time** — a user changing groups mid-month moves their
*future* spend; historical rows are never re-attributed. Tag activation is a **go-live
gate**: activate cost-allocation tags at deploy and verify a tagged line item in CE
within 48 h before finance onboards; until then M3 reconciles account-wide (shared-
account confound documented).

### 4.2 The enforcement plane — hard questions 3 & 4 (where/when; fail posture)

**Pre-request deny at the gateway interceptor; debit asynchronously; block the *next*
call — with explicit, bounded overage.**

- **E1 — the hard wall (all lanes, all clients).** On every inference POST
  (`/v1/chat/completions`, `/v1/responses`, `/v1/messages` — `GET /v1/models` and other
  discovery paths are explicitly exempt, the LiteLLM-#31078 lesson [B]): decode the JWT
  (signature-verify against cached Cognito JWKS, **refresh-on-unknown-`kid`** so a
  routine Cognito key rotation degrades to one refresh, not fleet-wide fail-open;
  defense in depth — the gateway already validated the token [S1]), resolve the
  subject's quota + RPM state from DynamoDB (eventually-consistent `GetItem`, 3–5 ms
  warm [S3]), and if `used_est ≥ hard_limit` **or** the per-minute rate bucket is
  exhausted, **short-circuit HTTP 429** with an OpenAI-shaped error body [S2] (+
  `Retry-After` on rate-limit blocks):
  `{"error":{"message":"Monthly AI budget reached (resets Aug 1). Contact <admin> to raise it.","type":"quota_exceeded","code":"quota_exceeded"}}`.
  E1 additionally writes a **floor debit** (below) — the security review's fix for the
  direct-caller hole.
- **E2 — the debit: estimate-at-admission, settle-at-capture.** Be honest about the
  shape: this **is** a reservation pattern — the one prior art shows leaks (LiteLLM
  phantom-blocking [B]) — adopted anyway because it is the only way quotas bind on
  direct-to-gateway callers (the security review's #1 finding), and adopted **with the
  leak paths engineered out** rather than pretended away:
  - *Admission (interceptor):* after an allow decision, `UpdateItem ADD est_usd`
    (input ≈ bytes/4, output = the per-lane max-tokens clamp, priced) on the subject's
    counter + `ADD 1` on the per-minute bucket `RPM#<sub>#<yyyymmddHHMM>` (TTL 2 min).
    Idempotency key for the estimate row: a **deterministic hash of
    (sub, body-hash, minute)** — NOT the platform request id, because the gateway may
    retry interceptor invocations and nothing guarantees id stability across retries
    [C]; a duplicate hash is a no-op. ~5 ms alongside the read [S3]. This write is what
    makes **every** caller's counter move — including direct-to-gateway clients that
    never produce an M1 event.
  - *Settlement (debit Lambda, async):* on the M1 usage event,
    **`TransactWriteItems { Put ledger-row IF attribute_not_exists(idempotency key),
    Update user-counter ADD (actual − estimate) }`** — guard and increment atomic, so a
    crash between them cannot drop or double-apply (at-least-once EventBridge + DLQ with
    a documented redrive procedure; a G1 gate replays a DLQ batch and asserts zero
    double-debits). **Group rollups are NOT in the transaction** — a hot `GROUP#` item
    under the 9 am burst would serialize thousands of settles (per-item ~1000 WCU,
    2× transactional cost, cancellation storms); rollups build asynchronously from
    DynamoDB Streams instead.
  - *The sweeper (anti-phantom-block):* a scheduled Lambda resolves any estimate
    unsettled after max-stream-duration (15 min). Default: **refund** (user-friendly —
    a lost usage event must not permanently consume quota; refunded-estimate volume is
    itself a metric and *is* the abort/internal-call/direct-caller measurement [D]).
    Strict-mode valve: **settle-at-estimate** instead (spend-conservative — deployments
    that would rather over-charge than under-count flip one flag). Either way the
    estimate rows make every loss channel *visible per-request*, not just as aggregate
    drift; M3 alarms on the **settled-vs-billed residue** with %, absolute-$, and
    volume-floor thresholds so a constant baseline (title/tag internal calls, aborts)
    calibrates out instead of masking regressions. This sweeper is the mechanism
    LiteLLM's leaked reservations were missing.
- **Worst-case overage (hard question 3, answered with numbers).** Between crossing the
  line and the next-call block, a user can complete their in-flight requests:
  `overage ≤ concurrent_streams × (est_input + effective_max_tokens) × price`, where
  `effective_max_tokens` is the request's `max_tokens` **or the clamp the interceptor
  injects when absent** (body rewrite, proven [S5]; default ceiling 8K). This makes the
  bound *enforced, not estimated* — no tokenizer needed in-path (input estimated at
  bytes/4; a real tokenizer would cost ~88 MB + 250 ms cold and is only correct for
  OpenAI-family models anyway — ±15–40% across our 40-model catalog [D]). Per-subject
  requests/minute limiting (same DynamoDB row) bounds `concurrent_streams`. Concretely:
  a user at their limit running 3 parallel Claude Sonnet chats with an 8K clamp can
  overshoot by ≤ 3 × (~10K in + 8K out) ≈ 54K tokens ≈ **$0.15–0.40** — per user, once
  per window, at frontier-model prices. E2's floor debit tightens even this: parallel
  call #2 already sees call #1's estimate on the counter, so racing past the ceiling
  requires the calls to land inside the same few-millisecond read window. For 20K users
  whose quotas are single-digit dollars/month, that is enterprise-acceptable.
- **E3 — the soft tier (UX, zero-touch, warn-only).** The seeded filter's `inlet()`
  reads a cached quota snapshot (refreshed out-of-band; never a blocking read on the
  chat path): at ≥ warn threshold it emits a `notification` event — verified to render
  as a toast at v0.10.2 (`Chat.svelte` handles `type: notification` → `toast.warning`)
  — e.g. "82% of your monthly AI budget used". **The inlet never hard-raises**: a raise
  from a stale snapshot would keep blocking after an operator reset with no
  invalidation hook (SRE finding), and the gateway 429 already renders in-chat (§4.4).
  One wall, owned by the gateway; the filter is UX only (Option 3's lesson).
- **E4 — backstops.** Account/mantle per-model TPM quotas (already enforced by AWS,
  adjustable [A]); AWS Budgets monthly alerts + optional Budget Action attaching
  `Deny bedrock-mantle:CreateInference` to the gateway role (the everything-off
  kill-switch, 8–24 h latency class [A]); Cost Anomaly Detection on the project tags
  (noting the documented Marketplace/Anthropic coverage caveat [A]); CloudWatch alarm on
  `AWS/BedrockMantle TotalOutputTokens` rate as the fastest coarse tripwire.
- **Fail posture (hard question 4): fail-open on infrastructure with a bounded grace
  budget, fail-closed on verified breach — implemented in our code because the platform
  default is the opposite.** The spike proved an interceptor crash **blocks the request**
  (fail-closed platform behavior [S7]); therefore the interceptor wraps all metering
  logic in a catch-all — plus a **bare outermost handler** returning a fixed opaque error
  body, and the gateway's `exceptionLevel` pinned to non-DEBUG, because the crash path
  otherwise leaks stack traces to the client [S7]. Degraded behavior is **not**
  unbounded: quota-store timeout/error → allow **up to a per-subject grace budget**
  (default 10 requests per degradation window, tracked best-effort in the same counter
  row or in-memory per Lambda env when DDB is fully down), emit `metering/DegradedChecks`
  and alarm on its rate; JWKS unknown-`kid` → one bounded refresh, then (only if Cognito
  itself is unreachable) allow under a small **global** grace counter with
  `attribution=unknown`. A 429 is returned only on a positively-read exceeded counter.
  Rationale: a metering outage that silently blocks every chat for 20K users is a worse
  incident than a grace-bounded window of unmetered spend under E4's account quotas —
  and the industry default agrees [B]. Deployments with a compliance mandate can flip a
  single valve to strict mode (missing quota state → 429), accepting that strict mode
  converts metering outages into chat outages. An **enforcement canary** (scheduled
  synthetic user with a 1-token quota; alarm if its call is *not* 429'd) guards the
  block path itself — budget enforcement silently regressing while metering keeps
  working is a documented failure mode in prior art (LiteLLM #26672) [B].

### 4.3 The multi-tenant identity & quota model — hard question 5

Identity arrives free on every call: `sub`, `username`, `cognito:groups` [S1]. The policy
model (DynamoDB, single table `metering_policy` + `usage_counter` + `usage_ledger`):

```
policy items    pk=POLICY#<scope>        scope ∈ {DEFAULT, GROUP#<name>, USER#<sub>}
  { window: MONTHLY|DAILY|ROLLING_30D, unit: USD|TOKENS,
    soft_limit, hard_limit, models_weighting: implicit-via-price-map,
    action_soft: WARN, action_hard: BLOCK, rpm_limit, effective_from, override_until? }

counters        pk=USE#<sub>#<window-start>   (+ mirrored GROUP# rollups)
  { used_usd, est_usd, used_input_tokens, used_output_tokens, req_count, updated_at, ttl }
rate buckets    pk=RPM#<sub>#<yyyymmddHHMM>   { n, ttl 2min }   ← the RPM primitive

ledger          pk=LEDGER#<yyyy-mm-dd>, sk=<ts>#<idempotency-key>
  { key: provider-response-id | chat/message-id | hash(sub,body,minute),
    sub, billing_group, groups[], project, model, lane, tier,
    in, out, cached, rate_used, price_map_version, usd,
    state: ESTIMATE|SETTLED|REFUNDED, source: FILTER|PIPE|INTERCEPTOR, ttl 15mo }

audit records   pk=AUDIT#<yyyy-mm-dd>, sk=<ts>#<actor-sub>
  { actor, action: PUT_POLICY|OVERRIDE|COUNTER_RESET, target, before, after }
```

IAM boundaries (stated so the sample teaches least-privilege): interceptor =
`GetItem` on `POLICY#*` + read/est-write scoped to `USE#`/`RPM#` leading keys; debit
Lambda = transact-write on counters+ledger only; admin Lambda = policy+audit+reset only;
no Lambda holds `dynamodb:*`. Admin API: **self-targeting `counter-reset`/`override` is
rejected** (a second admin must act), every mutation writes an audit record — the
usage ledger is not the audit trail.

- **Resolution precedence:** `USER# override` → most-specific `GROUP#` (ties: the most
  generous, so adding a user to a project group never *reduces* access — operator-visible
  choice, configurable to most-restrictive) → `DEFAULT`. Resolved at debit time and
  cached in the interceptor (60 s TTL) so the hot path stays one `GetItem`.
- **Windows:** calendar-month is the enterprise default (finance-aligned); daily and
  rolling-30-day supported by the same counter scheme (window key in the pk). Resets are
  *implicit* — a new window key starts at zero; there is no reset job to break (a
  recurring prior-art bug class [B]). Operator overrides: write a `USER#` policy with
  `override_until`, or zero a counter via the admin API (both audited to the ledger).
- **Units:** **USD is the canonical unit** — per-model weighting then comes free from the
  price map (a frontier-model token simply debits ~20× more dollars than a small-model
  token), which answers "per-model weighting" without a second mechanism. Token-unit
  quotas remain available for deployments that prefer them.
- **Group rollups / cost centers:** every debit increments the subject's group counters
  **asynchronously via DynamoDB Streams** (ceiling checks tolerate seconds of lag; a hot
  `GROUP#` item must not sit inside the settle transaction). `GROUP#` policies give
  per-team ceilings; the single `billing_group` (precedence-resolved, §4.1) is what
  chargeback sums — multi-group membership inflates ceilings only, never invoices.
  M2's Projects give the same teams *invoice-grade* dollars in CE/CUR. Group membership
  comes from the JWT (`cognito:groups`), so org changes propagate with the token, not
  with our config; ledger rows are frozen at debit time (never re-attributed).

### 4.4 Blocked-user experience — hard question 6 (through a UI we cannot modify)

Verified signal path, no upstream change:

1. **Soft warn:** filter `inlet`/`outlet` emits a status/toast event — renders natively.
2. **Hard block, chat lanes:** interceptor 429 with OpenAI-error body → Open WebUI's
   openai router returns the upstream JSON error (`JSONResponse(status, error_json)`
   [owui@v0.10.2 `routers/openai.py`]) → the chat middleware writes `error.content`
   into the message and emits it → **the user sees the quota message in the chat window**
   with the reset date and contact. (The pipe lane raises the same message text.)
   The sample already relies on this rendering path for pipe errors in production.
3. **Model list stays visible** (`/v1/models` exempt) so the UI never half-breaks [B].
4. The message itself is the operator's escalation path ("contact …") — configured text,
   set with the quota policy.

### 4.5 Operator control surface — hard question 7 (also outside OWUI)

- **Admin API + CLI:** a small API Gateway (HTTP API) + Lambda, authenticated by Cognito
  (admin group), exposing `GET/PUT policy`, `GET usage?subject=…&window=…`,
  `POST override`, `POST counter-reset`; plus a `scripts/set-quota.sh` in the style of
  the sample's existing `set-model-access.sh`. (Deliberately API+CLI first; a console UI
  is a later nicety — QuickSight/dashboards carry the visual load.)
- **Dashboards:** a CloudWatch dashboard from the custom `Metering/*` metrics (aggregate
  spend rate, top-N groups, degraded-checks, canary, drift) + `AWS/BedrockMantle`
  Project×Model views; optional QuickSight on CUR 2.0 for finance.
- **Alerts:** SNS topic; per-subject 80%/100% events from the debit Lambda; ops alarms
  (DegradedChecks rate, ReconciliationDrift, canary failure, interceptor errors).
  Per-user CloudWatch *metrics* are deliberately **not** published (20K-user cardinality
  = cost explosion); user-level data stays in DynamoDB, queried via the API.

### 4.6 Lean-core proof — hard question 8 (off = untouched)

The whole system is one **opt-in CDK stack** (`MeteringStack`, default **off** via CDK
context flag `metering=on|off`) plus two conditionals:

- Off (default): `MeteringStack` isn't synthesized; the gateway keeps today's
  `ModelsFilter` interceptor Lambda; `seed.py` seeds exactly today's rows. **Zero new
  resources, zero behavior delta** — the three-lane experience is bit-identical.
- On: the stack deploys the tables/bus/Lambdas/API/dashboard; the gateway stack swaps its
  interceptor ARN to the metering interceptor (a superset that retains the capability
  filter — same file family under `gateway/interceptor/`); `seed.py` additionally upserts
  the metering filter function row. Both toggles are single-property diffs in existing
  stacks, verified by `cdk diff` showing no change when the flag is off (a runbook gate).

### 4.7 Cost of the metering system itself

At the cost-analysis baseline (20K users, ~400M tokens, ~0.5M model calls/month
[repo docs/COST_ANALYSIS_20K_USERS.md]): interceptor Lambda ~0.5M × ~10 ms × 256 MB ≈
**<$1**; DynamoDB on-demand ~1.5M ops ≈ **~$2**; EventBridge 0.5M events ≈ **$0.50**;
debit/reconcile/price-refresh Lambdas ≈ **<$1**; API GW admin traffic ≈ **<$1**;
CloudWatch (dashboard + ~20 custom aggregate metrics + alarms + logs) ≈ **$10–15**;
SNS/canary ≈ **<$1**. **Total ≈ $15–25/month** — noise against the $2.8K–45K/month
Bedrock spend it governs, and it scales sub-linearly (per *call*, not per token).
QuickSight, if enabled, adds its own per-author/reader pricing.

### 4.8 Security & failure analysis

- **Spoofing:** quota identity comes from the JWT the *gateway* validated; the
  interceptor re-verifies signature/issuer/aud from cached JWKS before trusting claims
  for enforcement [S1]. Attribution can't be client-spoofed the way LiteLLM's `user`
  param can [B]. Project headers are injected (client-supplied ones overwritten) —
  a client cannot re-bill their traffic to another team [S6].
- **Bypass surface (the honest one):** any pool user can call the gateway directly,
  outside Open WebUI. E1 governs them (the interceptor is at the gateway) **because the
  interceptor itself writes the floor debit at admission** — without that write, a
  direct caller's counter would never move and "quotas bind" would be false (the
  security review's #1 finding; E2's estimate-at-admission is the fix). Their actual
  usage never settles (no M1 event), so they run on *estimates* — conservative ones
  (`bytes/4 + max_tokens clamp` ≥ actual in the common case) — and their unsettled rows
  are individually visible to the reconciler, not just in aggregate. RPM buckets bound
  their burst. Residual risk: estimate error on input for non-OpenAI tokenizers;
  bounded, documented.
- **Interceptor availability = chat availability** (fail-closed platform [S7]) — treated
  as a *lifecycle* problem, not a footnote: (a) **deploys ship behind a Lambda alias
  with CodeDeploy canary traffic-shifting** (10%/5 min, auto-rollback on error-rate or
  `DegradedChecks` alarms) — pointing the gateway at `$LATEST` would make every deploy
  an unrehearsed 100%-of-chat gamble; (b) **provisioned/reserved concurrency sized from
  measured peak RPS** (runbook load test moves *before* ENFORCE) with alarms at 60%
  utilization and on any throttle — a Lambda **throttle** is itself fail-closed
  (`[unverified how the gateway surfaces it; pre-prod probe in the runbook]`), so
  under-provisioning is the outage; (c) small artifact (stdlib+boto3, no VPC) for
  cold-start; (d) last-resort rollback = point the gateway back at `ModelsFilter`
  (UpdateGateway propagation latency measured in pre-prod, `[unverified today]`).
- **Every deny is debuggable:** the interceptor writes one structured decision record
  per 429 (and sampled allows) — sub, counter value read, policy scope+version, cache
  age, request id — to its log group with retention, plus a canned Logs Insights query
  in `docs/METERING.md`; "user says they were wrongly blocked at 2 am" resolves from
  one query, not from aggregates.
- **Capture-liveness canary (the other half of silent regression):** the 1-token canary
  proves the *block* path; a second synthetic user with ample quota proves the *meter*
  path (its hourly call must produce a settled ledger row within N minutes), plus an
  alarm on debit-events/min ÷ gateway `InboundAuthorizationSuccess` ratio — a seeder
  regression or upstream outlet-contract change that silently stops capture would
  otherwise surface only as ~26 h-later drift.
- **Counter integrity:** one writer (debit Lambda), serialized `ADD`s, idempotency by
  `message_id`, no cache-hierarchy — the specific bug class that burned prior art [B].
  DynamoDB throttle/failure on the read path → fail-open per 4.2.
- **Privacy:** the ledger stores token counts and ids, never message content; TTL 15
  months (one budget year + audit margin); the admin API is Cognito-admin-gated, writes
  audit records (actor/action/before/after), and rejects self-targeted resets/overrides.
  CloudTrail data events for mantle (optional phase) note that request `metadata` is
  logged verbatim — we send none.
- **The seeded filter is code running inside the app** (full message-body access, like
  every Open WebUI function — including the Claude pipe the sample already ships). Its
  integrity is part of the threat model: the build pins the seeded row's content hash
  (seeder re-asserts on boot and emits a drift metric if the DB row diverges), the S3
  seed assets are deploy-role-writable only, and a tampered filter's worst case for
  *metering* is undercount — surfaced by unsettled-estimate rows and drift. Content
  exfiltration via a tampered function is an app-tier risk the sample inherits from
  Open WebUI's function mechanism itself, not new to metering; called out in
  `docs/METERING.md` hardening guidance.
- **What can still go wrong (residual, accepted):** usage-tail semantics on mantle are
  provider-implemented and could drift per model family (mitigation: canary + drift
  alarm); `AWS/BedrockMantle` completeness may improve/change (we treat it as
  non-authoritative anyway); "token limit policies" may ship and obsolete parts of E1
  (tracked re-check gate); Cost Anomaly Detection may not cover Anthropic-entity line
  items [A][unverified].

## 5. Where it lives — in-repo module vs companion repo

**Recommendation: in-repo, opt-in module** (`infra/lib/metering-stack.ts` +
`gateway/interceptor/` extension + `pipe/metering_filter.py` + `scripts/set-quota.sh` +
`docs/METERING.md`), default-off with the `cdk diff`-clean gate of §4.6.

- For: the module is made of the sample's own moving parts (the interceptor file family,
  the seeder, the CDK app) — a companion repo would have to fork-track all three, the
  exact rot pattern the sample was refactored to escape; discoverability ("cost controls"
  is the #1 enterprise question this sample gets — the cost doc already apologizes for
  its absence [repo docs/COST_ANALYSIS_20K_USERS.md]); one `cdk deploy` story.
- Against (acknowledged): repo surface grows (~6 files + one stack); the README's
  "lean and mean" claim now needs one sentence ("optional metering module, off by
  default"). The off-state bit-identity gate is what makes this acceptable.
- **HUMAN SIGN-OFF REQUIRED** on this trade specifically — it is a taste call about the
  sample's identity, not a technical one.

## 6. Rejected-options register (one line each)

| Option | Why rejected |
|---|---|
| Self-hosted streaming proxy (Opt 1) | owns every chat's availability + duplicates the managed gateway; escape hatch only |
| AWS-native-only (Opt 2) | cannot express per-user quotas or block in <8 h; adopted as spine+backstop layers |
| App-side-only (Opt 3) | gateway is directly reachable → quota advisory, not enforced; adopted as capture+UX layer |
| Wait for AWS (Opt 5) | referenced features not in the API today; re-check gate instead |
| Per-user Bedrock Projects | 1000-project cap vs 20K users; per-team only [A] |
| Application Inference Profiles | bedrock-runtime only; AWS directs mantle users to Projects [A] |
| Bedrock invocation logging as meter | does not capture mantle traffic [A] |
| Cedar policy as quota engine | no usage/dynamic state in Cedar; MCP-tool-shaped today [A][C] |
| CloudWatch metrics as real-time meter | ≥2.5 h lag + incomplete coverage observed [S8] |
| Blind reserve-then-reconcile (no sweeper) | leakiest pattern in prior art (phantom blocks) [B]; E2 adopts the reservation *shape* for direct-caller coverage but pairs it with idempotent estimates + a 15-min auto-refund sweeper — the missing mechanisms in the LiteLLM failures |
| Mid-stream cutoff | impossible without owning the stream; industry doesn't do it either [B][C] |
| Redis/ElastiCache counter store | second infra + the counter+cache bug class; DynamoDB suffices at 3–5 ms [S3][B] |
| Per-user CloudWatch metrics | 20K-metric cardinality cost; DynamoDB + API instead |
| API Gateway usage plans | request-count units, wrong data path [B] |

## 7. Open questions for the operator (with recommendations)

1. **Architecture sign-off** (§4) — recommend approve.
2. **In-repo module vs companion repo** (§5) — recommend in-repo, off by default.
3. **Fail-open default** (§4.2) — recommend fail-open + strict-mode valve; if your
   compliance posture demands fail-closed by default, it is a one-line change with the
   availability consequences stated there.
4. **Default policy seed** — recommend shipping with `DEFAULT: monthly, USD 5 hard /
   USD 4 warn` and README guidance, so enabling metering is safe-by-default rather than
   unlimited-by-default.
5. **Group-precedence tie-break** (most-generous vs most-restrictive, §4.3) — recommend
   most-generous, configurable. The same precedence list defines each user's single
   `billing_group` for chargeback.
6. **Sweeper default for unsettled estimates** (§4.2 E2: refund after 15 min vs
   settle-at-estimate) — recommend refund (user-friendly; the strict valve exists for
   spend-conservative deployments).
7. **Headline-model pricing** — Claude/GPT-5.x are unpriced in the offer file today
   [D]; until the pre-GA billing probe resolves where they bill, enabling them under
   quota requires an operator-entered rate (design M3). Decide whether to run the probe
   during the build run (recommended) or hold those models out of the metered launch.
