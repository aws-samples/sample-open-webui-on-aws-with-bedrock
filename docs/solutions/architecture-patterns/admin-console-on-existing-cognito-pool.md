---
title: "Standalone admin console on an existing Cognito pool (CloudFront + same-origin API + PKCE)"
module: metering-admin-console
date: "2026-07-16"
problem_type: architecture_pattern
component: tooling
severity: medium
tags:
  - cognito
  - cloudfront
  - pkce
  - dynamodb-gsi
  - cloudscape
  - lean-core
  - opt-in-module
---

# Standalone admin console on an existing Cognito pool

## Context

The metering module shipped an operator surface that was API-plus-`curl`-plus-CloudWatch
only — no human-usable way to see consumption, manage quotas, or investigate a user. The
task was to add a real web console **without** a second identity system, **without**
touching the base sample when the module is off, and deploying as part of the existing
opt-in stack. This pattern is reusable for any "admin console bolted onto an existing
Cognito-authenticated app" need.

## Guidance

### 1. Hosting: one CloudFront distribution, API mounted same-origin (zero CORS)

Serve the SPA from a **private S3 bucket via Origin Access Control**, and add a
**second origin/behavior `/api/*`** pointing at the existing API Gateway HTTP API. Give
the HTTP API a **dedicated stage whose name equals the path prefix** (`api`) so the
CloudFront path and the stage line up with no URI rewriting:

```ts
new apigwv2.HttpStage(this, 'ApiStage', { httpApi, stageName: 'api', autoDeploy: true });
// /api/* behavior → HttpOrigin(`${apiId}.execute-api.${region}.${urlSuffix}`)
//   originRequestPolicy: ALL_VIEWER_EXCEPT_HOST_HEADER  (forwards Authorization, strips Host)
//   cachePolicy: CACHING_DISABLED                       (never cache authed API responses)
```

Because the app and API are the **same origin**, there is no CORS configuration anywhere —
the browser enforces same-origin naturally and there are no `Access-Control-Allow-*`
headers to get wrong. Do SPA deep-link routing with a **CloudFront Function** that rewrites
extensionless, non-`/api/` paths to `/index.html` — **not** a distribution-wide
`errorResponses` 404→index.html rule, which would rewrite API JSON errors too.

### 2. Identity: a no-secret PKCE client on the *existing* pool, group-gated server-side

Add one `UserPoolClient` (no secret, authorization-code + PKCE, callback/logout locked to
the console origin) to the pool the app already uses. Admin capability is
**group membership checked server-side on every request**, not the client id and not the
UI. The SPA fetches a deploy-time `config.json` (pool id, client id, domain) so **no ids
are baked into the bundle**.

- Reuse the pool's Managed Login. **Every app client on a Managed-Login pool needs its own
  `CfnManagedLoginBranding`** or the hosted sign-in page returns 404 — reuse
  `useCognitoProvidedValues: true` like the base client.
- Keep the client **hosted-UI-only** (`oAuth.flows.authorizationCodeGrant`, no
  `authFlows`) so the public client never authenticates outside the redirect flow.
- The edge JWT authorizer's audience will include the app's mass-user client (so
  self-service endpoints work under one identity) — meaning the **edge authorizer is not
  the admin boundary; the Lambda group check is**. Make that explicit and don't leak admin
  metadata (e.g. the admin group names) to non-admins on any pre-gate endpoint.

### 3. Data access at scale: additive GSIs + attribute stamps, never a Scan

To render thousands of users without hammering DynamoDB, add GSIs keyed on attributes the
write path stamps — and update **every** writer that produces those rows:

- `by-window` GSI (`w` partition, `used_usd` sort): stamp `w = <window>` on counter rows at
  **every** mutation point (settle, sweep, rollup, admin reset). One Query returns
  spend-ordered users/groups for a window.
- `by-sub` GSI on ledger rows (already carry `sub`): per-user history, backfilled
  automatically by DynamoDB for existing rows.
- Reuse an existing GSI for a second purpose by stamping a sentinel: policy rows get
  `state = "POLICY"` so the sweeper's `state = "OPEN"` estimates GSI also serves the policy
  list — never colliding with the sweeper's query.
- Aggregate time-series come from **CloudWatch metrics the module already emits**
  (`GetMetricData`), not from re-aggregating the ledger.

The console read Lambda holds **no `dynamodb:*`** — table read/write is grant-scoped, and
Cognito/CloudWatch/SNS grants are read-only and resource-scoped (except `GetMetricData`,
which has no resource-level scoping).

### 4. Lean-core: construct the console only inside the opt-in stack

Put the whole console (S3, CloudFront, the app client, the API stage) in a construct
instantiated **only** within the metering-enabled path. Verify with a byte-identical
`cdk synth` comparison against `main`'s off-state for all base stacks — the console must
add zero resources when the module is off.

## Why This Matters

One identity system means an existing admin is a console admin with no new login and no
parallel user store — the single hardest requirement, satisfied by construction. Same-origin
hosting deletes the entire CORS attack/complexity surface. GSI-plus-stamp keeps reads O(query)
at 20K-user scale while leaving the enforcement hot path untouched. The lean-core gate is what
lets a security- and cost-sensitive base sample absorb a whole web app as an opt-in.

## When to Apply

Any time you need an operator/admin web console for an existing Cognito-authenticated AWS
app, especially as an **opt-in module** that must not perturb the base deployment, and the
backing store is DynamoDB single-table where console reads would otherwise Scan.

## Operational gotchas (verified this build)

- **DynamoDB allows only ONE GSI creation per stack update on an existing table.** Adding two
  console GSIs to a live table fails with `Cannot perform more than one GSI creation ... in a
  single update`. Gate the second behind a context flag and deploy in two passes
  (`-c meteringGsiPhase=1`, then without); fresh table creates take all GSIs at once.
- **Cognito Managed Login 404s for any app client without a branding style.** Add
  `CfnManagedLoginBranding` for the new client.
- **A Cloudscape/Highcharts `LineChart` with a zero-length data series renders a degenerate
  "Jan 1 2000" axis** instead of the empty state. Pass an **empty `series` array** (not a
  series whose `data` is `[]`) when there are no points, so the `empty` slot renders.
- **The heavy Cognito Managed Login page crashes minimal-library headless Chromium**
  (missing system libs cause a page crash on navigation). For visual verification use a
  managed browser service (e.g. AgentCore Browser) rather than a hand-assembled headless
  Chromium; or seed a session token directly to skip the hosted page.
- **`chart-components` (Cloudscape's newer chart package) wraps Highcharts** — commercial
  license, wrong for an MIT-0 sample. Use Cloudscape's built-in `LineChart`/`BarChart`.
- **Time-boxed overrides don't auto-expire** unless the *enforcement* reader honors the
  expiry. If you only stamp `override_until`, it's an operator-facing marker; ending the
  override means deleting the row. Auto-expiry is a one-line reader change (skip a user
  policy whose `override_until < now`) — but that's an enforcement-behavior change, scope it
  deliberately.
