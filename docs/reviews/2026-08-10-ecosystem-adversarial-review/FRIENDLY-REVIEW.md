<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Open WebUI friendly architecture review

- Date: 2026-08-10
- Source baseline: `83bc9100710b8442ed1756925cdcc38835fbcf97`
- Status: intermediate review evidence; candidate findings require the proof named below before promotion to `FINDINGS.md`

## Scope and evidence standard

This pass traced the tracked source, tests, infrastructure, product documentation, and relevant Git intent. It did not use the untracked Fable brief, review artifacts, `.kiro/` additions, or compiled Python extension as product evidence. A source-level defect is not automatically a demonstrated live failure. Each candidate is therefore labeled with the local or live proof still required.

## End-to-end architecture

1. **Product boundary.** The application runs the official Open WebUI v0.10.2 image pinned by digest; integration is supplied through runtime assets and environment configuration rather than an application fork (`infra/lib/compute-stack.ts:19-29`, `infra/lib/compute-stack.ts:91-95`, `infra/lib/compute-stack.ts:162-220`).
2. **Private request path.** CloudFront fronts a VPC origin and internal ALB; ECS, Aurora PostgreSQL/pgvector, Redis, and S3 form the private application/data plane (`infra/lib/compute-stack.ts:337-429`, `infra/lib/data-stack.ts:31-130`).
3. **Identity.** Cognito issues user tokens. Native Chat Completions and Responses connections use Open WebUI `system_oauth`; the Claude pipe retrieves the user's OAuth session at invocation time (`pipe/seed.py:109-153`, `pipe/gateway_anthropic_pipe.py:107-130`).
4. **Model discovery.** The request interceptor returns generated capability-filtered model lists for the two native lanes (`gateway/interceptor/index.py:25-58`). Claude pipe discovery separately lists available Anthropic models (`pipe/gateway_anthropic_pipe.py:52-104`). The optional refresher probes compatibility, rejects lane collapses, updates the interceptor, re-snapshots the connector, and promotes the metering alias only after connector healing (`gateway/refresher/index.py:128-256`).
5. **Admission.** The metering interceptor verifies the Cognito JWT locally, reads user policy/counter/RPM state, estimates the request from request size and output cap using the shared pricing resolver, and either observes or enforces quota/RPM decisions (`gateway/metering-interceptor/index.py:98-165`, `gateway/metering-interceptor/index.py:278-336`, `gateway/metering-interceptor/index.py:429-519`).
6. **Reservation and mutation.** An admitted request increments RPM, writes an OPEN estimate, increments `est_usd`, clamps output tokens, forces streamed Chat Completions usage, and injects the group-derived Bedrock Project/workspace header (`gateway/metering-interceptor/index.py:348-426`, `gateway/metering-interceptor/index.py:466-500`).
7. **Capture.** A seeded global Open WebUI filter reads provider-normalized usage and emits a compact EventBridge event. It retries failed entries once and deliberately does not break chat on capture failure (`pipe/metering_seed.py:44-107`, `pipe/metering_filter.py:91-116`, `pipe/metering_filter.py:170-247`).
8. **Settlement.** Debit resolves live pricing, matches the oldest OPEN estimate for the same subject/model, and atomically inserts the first-writer-wins ledger row, adds actual usage/cost, and transitions the estimate. It retries actual settlement without estimate consumption when sweep/settle races (`metering/debit/index.py:133-337`). EventBridge retries twice and uses a 14-day SQS DLQ (`infra/lib/metering-stack.ts:137-189`).
9. **Recovery and accounting.** The five-minute sweeper refunds stale reservations by default or can settle at estimate in strict mode (`metering/sweeper/index.py:5-18`, `metering/sweeper/index.py:52-154`). DynamoDB Streams asynchronously create advisory group rollups (`metering/rollup/index.py:4-79`). Nightly D-2 reconciliation compares ledger tokens with Cost Explorer Mantle usage (`metering/reconciler/index.py:1-138`).
10. **Pricing.** One resolver implements `OVERRIDE -> AWS-published -> unpriced`, including routing/tier/context ladders and flagged cache-direction fallback (`metering/pricing/resolver.py:172-259`). The refresher joins three offer files to Bedrock model IDs, materializes unambiguous keys, writes unmatched rows, garbage-collects stale rows after complete runs, and publishes catalog metadata (`metering/pricing-refresher/index.py:249-459`).
11. **Administration.** A separate CloudFront/S3 SPA uses a Cognito PKCE client and same-origin HTTP API (`infra/lib/metering-console.ts:28-218`). API Gateway verifies the JWT; Lambda centrally requires an exact admin-group claim for privileged routes, while self-service usage/ledger routes remain available (`metering/admin-api/index.py:117-139`, `metering/admin-api/index.py:978-1022`).
12. **Operational surface.** The stack creates debit DLQ and unpriced alarms, block/capture canaries, reconciliation, pricing refresh/failure/unmatched signals, and a dashboard (`infra/lib/metering-stack.ts:137-416`, `infra/lib/metering-stack.ts:520-537`). The existence of a resource is not treated as proof that the signal is live or trustworthy.

## Evidence-backed strengths

- The official-image boundary is clean and upgradeable; no upstream application fork is introduced.
- End-user identity reaches each inference lane rather than collapsing to one shared application credential.
- The public edge does not directly expose the ALB, ECS tasks, or data stores.
- Final settlement is transactionally idempotent across ledger, counter, and estimate state (`metering/debit/index.py:262-337`).
- Unknown pricing is explicit: token usage is retained, dollar cost becomes unpriced/$0, and a metric is emitted rather than inventing a rate (`metering/pricing/resolver.py:216-259`, `metering/debit/index.py:153-164`).
- Model refresh has a collapse guard and repairs the connector snapshot before alias promotion (`gateway/refresher/index.py:175-256`).
- Privileged console authorization is enforced in Lambda, not trusted to SPA navigation (`metering/admin-api/index.py:117-119`, `metering/admin-api/index.py:978-1148`).
- Metering remains an opt-in domain stack; base deployments keep the upstream integration without governance resources (`infra/bin/app.ts:38-41`, `infra/bin/app.ts:100-158`).

## Deliberate posture, not automatically defects

- Availability-first fail-open is intentional. The review question is whether the actual bound matches the documented bound.
- Refund is the default sweeper policy and strict settlement is available. Product claims must state the resulting direct-caller behavior accurately.
- Group rollups are advisory; the ledger and Cost Explorer are authoritative.
- Unpriced means known tokens and unknown dollars, not a guessed rate.
- Model capability refresh and metering are default-off to keep the base sample lean.
- `bedrock-mantle:*` is documented as a current service/IAM constraint; it remains a residual least-privilege risk rather than an accidental wildcard.
- Filter capture must not break chat. Missing loss detection, not the availability choice, is the concern.

## Candidate findings awaiting proof

| ID | Provisional severity | Candidate claim and source evidence | Proof required |
|---|---:|---|---|
| OW-F01 | P1 | Both hourly canaries conditionally create their policy on every run without handling an existing row; later invocations can fail before testing or emitting a metric. Failure-only alarms treat missing as healthy (`metering/canary/index.py:94-110`, `metering/canary/index.py:132-213`, `infra/lib/metering-stack.ts:283-324`). | Invoke each handler twice against the same synthetic policy or add a focused unit test; inspect live invocation/error and metric timestamps. |
| OW-F02 | P1 | Debit catches pricing BatchGet failures, ignores unprocessed keys, caches an empty catalog entry, and can settle a priced call permanently at $0 (`metering/debit/index.py:67-103`, `metering/debit/index.py:133-164`, `metering/debit/index.py:226-323`). | Inject exception and `UnprocessedKeys`; prove whether a ledger row is acknowledged rather than retried/DLQ'd. |
| OW-F03 | P1 | Reservation uses separate RPM update, conditional estimate insert, and counter update. Failure after estimate creation can leave an estimate that was never added to the counter; duplicate retry does not repair it (`gateway/metering-interceptor/index.py:348-395`, `gateway/metering-interceptor/index.py:494-500`). | Failure-injection test after PutItem, retry same request, then settle/sweep and inspect `est_usd`. |
| OW-F04 | P1 / decision | Under default refund mode, a direct gateway caller with no Open WebUI filter event is refunded after 15 minutes and does not accumulate monthly settled spend (`metering/sweeper/index.py:5-18`, `metering/sweeper/index.py:112-129`, `infra/lib/metering-stack.ts:191-212`). | Live direct call through one sweep window and comparison against the documented direct-caller claim. Maintainer must choose refund, strict settlement, or a narrower promise. |
| OW-F05 | P1 | A partial pricing refresh skips stale-row deletion but still unconditionally replaces resolved rows, so a surviving service can replace a previously complete overlapping grid with partial data (`metering/pricing-refresher/index.py:295-327`, `metering/pricing-refresher/index.py:401-452`). | Focused fixture with one failed overlapping offer source and an existing complete row. |
| OW-F06 | P1 at scale | Pricing garbage collection and admin pricing reads use filtered full-table scans over the shared ledger/counter table (`metering/pricing-refresher/index.py:346-388`, `metering/admin-api/index.py:747-766`). | Seed a large non-pricing corpus or deterministically model RCUs/latency; confirm no key-bounded query exists. |
| OW-F07 | P1 | The capture canary injects directly into EventBridge and does not exercise the Open WebUI filter. The filter's capture-failure helper logs but does not publish the documented metric (`metering/canary/index.py:168-203`, `pipe/metering_filter.py:25-31`, `pipe/metering_filter.py:230-247`). | Disable/break filter in the test deployment and show the canary remains healthy; inspect metric publication after forced PutEvents failures. |
| OW-F08 | P1/P2 | Resolver supports cache directions, but settlement records cached tokens while charging all input at the input rate and never resolves cache-read/cache-write rates (`pipe/metering_filter.py:198-215`, `metering/debit/index.py:146-164`). | End-to-end debit fixture with total input, cache-read, and cache-write usage and known distinct rates. |
| OW-F09 | P2 | Claude/Messages pipe discovery lists available Anthropic models independently of generated `messages` capability data (`gateway/interceptor/index.py:25-58`, `pipe/gateway_anthropic_pipe.py:52-104`). | Compare live pipe listing to generated `messages`; attempt a probed-incompatible/account-gated Anthropic model if one exists. |
| OW-F10 | P2 | Reconciliation queries ledger rows only, making its unsettled-estimate baseline ineffective, and its Cost Explorer regex omits cache directions/hyphenated tiers (`metering/reconciler/index.py:40-97`). | Fixture containing OPEN estimates and representative cache/hyphenated usage types. |
| OW-F11 | P2 | Refresh generation is metadata rather than an atomic snapshot: rows are exposed by sequential PutItem before META, and concurrent runs can publish the same next generation (`metering/pricing-refresher/index.py:295-344`, `metering/pricing-refresher/index.py:390-459`). | Interleave two refreshes or fail midway and inspect reader-visible mixed generations. |
| OW-F12 | P2 | Admin accepts one-direction overrides; debit can mix sources while recording one source/version, and any missing direction makes the whole call $0 (`metering/admin-api/index.py:839-884`, `metering/pricing/resolver.py:216-259`, `metering/debit/index.py:150-164`). | Resolver/debit tests for input-only and output-only overrides plus mixed provenance. |
| OW-F13 | P2 | Grace is per execution environment and resets on cold start/window; local JWT verification failure bypasses reservation/quota without consuming grace (`gateway/metering-interceptor/index.py:98-106`, `gateway/metering-interceptor/index.py:133-165`, `gateway/metering-interceptor/index.py:446-478`). | Concurrency/cold-start model plus forced JWKS failure against an already gateway-authenticated request. |
| OW-F14 | P2 | Admin mutation and audit writes are separate operations, allowing a successful unaudited mutation if audit persistence fails (`metering/admin-api/index.py:121-139`, `metering/admin-api/index.py:228-249`, `metering/admin-api/index.py:839-934`). | Inject audit failure after policy/pricing/reset mutation and inspect durable state and response. |
| OW-F15 | P2 onboarding | Fresh metering deploys schedule pricing refresh but do not initialize it, default to ENFORCE, and primary docs disagree about seeding, stack count, and whether metering exists (`infra/lib/metering-stack.ts:387-390`, `infra/lib/gateway-stack.ts:154-160`, `docs/AWS_DEPLOYMENT_GUIDE.md:322-353`, `docs/AWS_DEPLOYMENT_GUIDE.md:487-494`). | Inspect current stack/catalog timestamps and execute a fresh-deploy walkthrough; reconcile all primary docs against source. |

Residual hardening candidates include non-idempotent group rollup after partial batch writes, no rollup failure destination, minute/body-hash estimate collisions, oldest-subject/model estimate matching, and ID-less same-second debit idempotency collisions (`metering/rollup/index.py:11-18`, `infra/lib/metering-stack.ts:213-241`, `gateway/metering-interceptor/index.py:348-387`, `metering/debit/index.py:105-114`, `metering/debit/index.py:176-220`).

## Product and first-hour observations

The sample serves two audiences at once: an application team wanting an unmodified private Open WebUI deployment, and a platform/FinOps team evaluating opt-in governance. The first audience receives a strong upstream boundary and familiar Cognito/Open WebUI experience. The second currently has too many readiness steps hidden outside the main path:

- console administration requires Cognito admin-group membership, not merely Open WebUI application administration;
- pricing must be refreshed before dollar quotas are meaningful;
- all three lanes need a real streamed smoke test because model listing, connector snapshot, and pipe translation are different contracts;
- operations must subscribe alerts and inspect DLQ/canary freshness before ENFORCE;
- Bedrock Project cost-allocation tags must be activated separately;
- current primary documents disagree about stack count, seed timing, and whether application quotas exist (`README.md:128-140`, `README.md:173-179`, `docs/AWS_DEPLOYMENT_GUIDE.md:322-353`, `docs/AWS_DEPLOYMENT_GUIDE.md:487-494`, `.env.example:64-68`).

The product bar should be one explicit readiness state: deployed, pricing initialized, models probed and promoted, each lane invoked, capture path fresh, alarms receiving heartbeats, admin access verified, and governance mode intentionally selected. Until that state is machine-checkable, the metering module should remain clearly labeled opt-in/preview.

## Next verification batch

1. Run existing Python, console, TypeScript, and CDK baseline gates.
2. Reproduce OW-F01, OW-F02, OW-F03, OW-F05, OW-F08, OW-F10, OW-F12, and OW-F14 with focused local tests or deterministic harnesses.
3. Compare the two repositories' pricing package copies and refresher behavior byte-for-byte and semantically.
4. Inspect the live shared account for stack outputs, function freshness, alarms, DLQs, catalog generation, model snapshots, and console auth without mutating resources.
5. Run bounded live calls only after identifying exact test users/endpoints and a cleanup record; capture raw evidence locally and commit only sanitized summaries.
