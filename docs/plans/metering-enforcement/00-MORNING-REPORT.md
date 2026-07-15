<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# 00 — Morning Report: metering / tracking / enforcement design run (overnight 2026-07-14 → 15)

## 1. Outcome and recommendation, in two sentences

The research-and-design run is complete: four evidence tracks (AWS primitives, prior
art, gateway internals, reconciliation), a disposable live spike that closed every
design-gating unknown (torn down to zero residuals), a full design that survived three
adversarial reviews (FinOps / security / SRE — all material findings folded in), and a
phased build runbook — all on branch `analysis/metering-enforcement`, nothing pushed,
nothing permanent deployed. **Recommended architecture (HUMAN SIGN-OFF REQUIRED):
meter from the stream's own `usage` block captured server-side by a seeded Open WebUI
global filter (+ the Claude pipe we already own); enforce pre-request at the gateway
REQUEST interceptor (429 short-circuit + estimate-at-admission floor debit, settle
async, block the *next* call); attribute per-team with Bedrock **Projects** headers the
interceptor injects; reconcile nightly against Cost Explorer/CUR mantle usage types;
backstop with mantle TPM quotas + AWS Budgets — shipped as an opt-in, default-off CDK
module inside this repo whose off-state is bit-identical to today's sample.**

## 2. Top design tensions and how they resolved

- **Token source of truth vs the invoice.** No single source on our path is per-call
  accurate *and* invoice-authoritative: gateway response interception doesn't exist for
  streams (docs now say it explicitly), Bedrock invocation logging doesn't cover
  mantle, and `AWS/BedrockMantle` CloudWatch metrics turned out to be unreliable in
  practice (our spike-night traffic never appeared, even 4 h later). Resolution: a
  three-layer ladder — per-call truth from the provider's own `usage` block (spike-
  proven to survive gateway streaming on all three lanes), per-team dollars from
  Project cost-allocation tags in CE/CUR, and a nightly ledger-vs-invoice reconciler
  whose *measured* drift is the accuracy claim (we deleted the "1–2%" assertion —
  finance gets a measured number, not folklore).
- **Pre-request deny vs cost-known-only-at-the-end.** Industry consensus (LiteLLM,
  Cloudflare, Kong, Envoy) is check-then-debit with next-call blocking; nobody cuts
  streams mid-flight. We adopt that, but the security review exposed the hole in pure
  post-debit: a direct-to-gateway caller (any valid Cognito token, no Open WebUI) would
  never generate a debit — quotas would be fiction. Resolution: the interceptor writes
  an **estimated floor debit at admission** (bytes/4 input + per-lane max-tokens clamp),
  settled asynchronously against the real usage event via an idempotent transaction,
  with a 15-minute sweeper auto-refunding orphaned estimates — the reservation *shape*
  prior art keeps leaking, adopted deliberately with the two leak-killers (idempotent
  estimates, sweeper) it was missing. Worst-case overage is enforced, not estimated:
  `concurrent × (input + clamp)` ≈ a few tens of cents per user per window.
- **Fail-open vs fail-closed.** The spike proved the platform is **fail-closed**: an
  interceptor crash blocks the request (and leaks stack traces unless handled). Our
  code inverts that with a **grace-budgeted fail-open**: quota-store errors allow ~10
  requests/subject/window (not unbounded — the security review's attacker-induced-
  degradation case), alarmed; a 429 only ever comes from a positively-read exceeded
  counter; strict-mode valve for compliance deployments; an enforcement canary (1-token
  user must get 429) plus a capture canary (ample-quota user must produce a ledger row)
  guard both silent-regression directions.

## 3. What I worked around (dead avenues, drift, blockers — stated plainly)

- **Dead avenues verified dead:** Application Inference Profiles (bedrock-runtime only
  — AWS's docs literally say "use Projects instead" for mantle), model-invocation
  logging (doesn't capture mantle), Cedar policy as quota engine (no usage state, MCP-
  tool-shaped), gateway token-limit policies (referenced in docs, **absent from the
  API** — the linked page 404s), per-user CloudWatch dims / gateway USAGE_LOGS
  (don't exist), API Gateway usage plans (request-count units, wrong path).
- **Docs-vs-reality drift found twice:** the `AWS/BedrockMantle` namespace is
  documented as covering all mantle traffic but empirically missed all of tonight's
  calls; and the mantle offer file + this account's CE have **no Claude or GPT-5.x
  mantle SKUs** despite those being the headline models — so the design treats
  unpriced models as a blocking onboarding state and the runbook front-loads a billing
  probe. GPT-5.5 also dropped off `/v1/responses` since 07-09 (capability drift within
  5 days) — the scheduled re-probe job got promoted from hygiene to load-bearing.
- **Workflow/infra friction:** the research workflow and all three adversarial
  reviewers were interrupted by session restarts / API timeouts; everything was
  resumed to completion (workflow resume cache + agent transcripts). Track D completed
  on retry. No research was lost.
- **CloudWatch propagation prevented one same-session proof:** the Project-tag →
  Cost Explorer flow (docs-asserted) needs 24–48 h to observe; it's marked
  `[unverified-live]` and gated in Phase 3 of the runbook rather than assumed.

## 4. What needs your decision (each with my recommendation)

1. **Architecture sign-off** (`02-DESIGN.md` §4) — recommend **approve**.
2. **In-repo opt-in module vs companion repo** (§5) — recommend **in-repo, default-off**
   with the `cdk diff` bit-identity gate; the module is made of the sample's own moving
   parts and a companion repo would fork-track three of them.
3. **Fail-open default with grace budget** (§4.2) — recommend **fail-open**; strict
   valve documented for compliance postures (it converts metering outages into chat
   outages — say so out loud).
4. **Default policy seed** — recommend shipping `DEFAULT: monthly, $5 hard / $4 warn`
   so enabling metering is safe-by-default, not unlimited-by-default.
5. **Sweeper default** (refund vs settle-at-estimate for orphaned floor debits) —
   recommend **refund**.
6. **Headline-model pricing** — Claude/GPT-5.x are unpriced in the offer file today;
   run the billing probe during the build run (recommended) or hold them out of the
   metered launch until rates are known.

## 5. Evidence trail (what moved the design, what remains unverified)

- **Live spike (all torn down, zero residuals — `04-SPIKE-FINDINGS.md`):** REQUEST
  interceptor receives the user's raw JWT on every call; 429 short-circuit with custom
  body works (~216 ms); DynamoDB quota read in-path costs 3–5 ms warm; the `usage`
  block survives gateway streaming on all three lanes (chat-completions only with
  `stream_options.include_usage` — which the interceptor can **inject by rewriting the
  body**, also proven); Bedrock **Projects** exist on mantle with per-request headers
  (`OpenAI-Project` / `anthropic-workspace-id` — the latter discovered via a live error
  message), validated server-side, injectable at the interceptor through the gateway
  E2E; interceptor crash = fail-closed + stack-trace leak (defaulted exceptionLevel).
- **Current-docs citations that decided things (all retrieved 2026-07-14/15, URLs in
  `01-LANDSCAPE.md` + `research/`):** "Interceptors are not yet supported in streaming
  mode" (the RESPONSE-interceptor trap, now explicit); "use Projects instead" (AIP
  exclusion); "not currently captured by invocation logging" (mantle); mantle TPM
  quotas incl. mid-generation output cutoff; Budgets 8–24 h latency + monthly-only
  actions; CE usage-type unit = 1K tokens (confirmed against live rows to the cent);
  offer-file `effectiveDate` proves **backdated repricing** is real.
- **Prior art (`research/track-b-prior-art.md`):** adopted check-then-debit +
  next-call blocking, fail-open-with-strict-mode, single serialized counter with one
  write path, discovery-path exemption, and enforcement canaries — each mapped to a
  documented LiteLLM production failure (#26672, #30460, #27639, #31078, #30776).
  Rejected: mid-stream cutoff (nobody does it), Redis counter hierarchies, blind
  reservations.
- **Explicitly unverified (carried as such in the design):** Project-tag→CE flow
  (docs-asserted, 24–48 h to observe); where Claude/GPT-5 mantle usage actually bills;
  `AWS/BedrockMantle` emission criteria; gateway retry/timeout semantics on interceptor
  failure and UpdateGateway propagation latency (pre-prod probes in the runbook);
  OWUI Responses-lane usage persistence into `message.usage`; Cost Anomaly Detection
  coverage of Anthropic-entity line items; abort *rate* for real users (the
  reconciler's unsettled-estimate bucket is the measurement).

## 6. Where everything lives

`docs/plans/metering-enforcement/` on branch `analysis/metering-enforcement`
(branched from `feat/gateway-first`; not pushed; no existing file modified):
`01-LANDSCAPE.md` (verified primitives + prior art), `02-DESIGN.md` (the architecture,
options, rejected register, sign-off items), `03-IMPLEMENTATION-RUNBOOK.md` (Phases 0–5
with gates G0–G5), `04-SPIKE-FINDINGS.md` (probes + teardown), `research/track-{a,b,c,d}-*.md`
(the raw evidence). All real account IDs and live resource identifiers redacted.
