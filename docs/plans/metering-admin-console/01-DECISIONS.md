<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Metering Admin Console — architecture decision record

Companion to [`docs/METERING.md`](../../METERING.md) (the module this console
fronts) and [`../metering-enforcement/02-DESIGN.md`](../metering-enforcement/02-DESIGN.md)
(the data model it renders). Decided autonomously per the build brief; each
decision records the options weighed and why the pick won.

## What it is

A standalone admin web application for the metering module: monitor
consumption, investigate users and groups, manage quota policies, grant
time-boxed overrides, reset counters, read the audit trail, and watch the
module's own health (canaries, degraded checks, alarms) — with the same
Cognito sign-in the sample already uses. Deploys only with `--metering`;
the base sample stays byte-identical when the module is off (G0 gate).

## D1 — Hosting: CloudFront + private S3 + the existing admin HTTP API as a second origin

**Pick:** one CloudFront distribution owned by `MeteringStack`: default
behavior → private S3 bucket (Origin Access Control) serving a static SPA;
`/api/*` behavior → the module's existing API Gateway HTTP API via a new
`api` stage (so no path rewriting at all). SPA deep links are handled by a
viewer-request CloudFront Function that rewrites extensionless paths to
`/index.html`.

- Same-origin API ⇒ **zero CORS surface** (no `Access-Control-Allow-*`
  anywhere; browsers enforce same-origin naturally).
- No `errorResponses` SPA fallback — that would rewrite API 403/404 JSON
  bodies into `index.html` distribution-wide. The CF-Function rewrite keeps
  API error bodies intact (the console renders them).
- Security headers (CSP, HSTS, nosniff, frame-ancestors 'none') via a
  `ResponseHeadersPolicy` on the S3 behavior.

**Rejected:** Amplify Hosting (a second deploy system next to `deploy.sh`;
violates "deploys as part of the metering stack"); Fargate/App Runner (a
server for a static app); S3 website endpoints (no OAC, HTTP-only origin);
CloudFront Function URI-strip of `/api` (the extra `api` stage on the
existing HTTP API achieves it declaratively — the `$default` stage stays for
`set-quota.sh` compatibility).

## D2 — Identity: a new no-secret SPA app client on the *existing* pool, PKCE code flow

**Pick:** `MeteringStack` adds one `UserPoolClient` (`metering-console`,
no secret, authorization-code + PKCE, callback/logout = the console's own
CloudFront URL) to the pool the sample already deploys. The SPA uses
`react-oidc-context`/`oidc-client-ts` against the pool's Managed Login
domain. The API authorizer's audience list gains this client id. Admin =
membership in the same groups the admin API already trusts
(`admins`/`webui-admins`/`admin`) — read from the **access token's**
`cognito:groups` claim server-side on every call.

- One identity system, by construction: an OWUI admin *is* a console admin;
  nobody gets a second password. Signing in re-uses the pool's Managed
  Login (already branded for the sample).
- The OWUI app client can't be reused: it has a client secret and
  OWUI-owned callback URLs — a public SPA must not embed a secret. A
  no-secret PKCE client is the AWS-documented pattern for SPAs.
- Lean-core holds: the client is a metering-stack resource; destroying the
  stack removes it; metering-off synth never touches the auth stack.
- Tokens: kept in `sessionStorage` (oidc-client-ts default WebStorageStore),
  sent as `Bearer` to the same-origin API. Mitigations for the XSS-steals-
  token risk: strict CSP (`script-src 'self'`, no inline script), zero
  third-party script origins, React's default output encoding, short
  (60-min) access tokens with refresh rotation, and revocation-enabled
  client. **Rejected:** a cookie/BFF token-handler (HttpOnly cookies +
  session store + CSRF machinery — meaningfully more infrastructure, and
  the existing admin API contract is bearer-JWT; the sample's threat model
  is served by CSP + no-third-party-script).

## D3 — API: extend the existing admin Lambda; no second service

**Pick:** the console calls the module's existing admin API, extended with
read/aggregate routes (below). Same Lambda, same table, same
admin-group gate, same audit rules — the console adds **no second policy
engine and no second auth model**.

New routes (all admin-gated except `/usage/me` and `/config`):

| Route | Purpose |
|---|---|
| `GET /config` | pool/client ids for the SPA, enforce mode, price-map version, defaults (public-safe subset served to authenticated users) |
| `GET /users?window=&limit=&cursor=` | top spenders + near-limit listing from the new `by-window` GSI |
| `GET /users/search?q=` | Cognito email/name/sub search → sub + groups (ListUsers) |
| `GET /user/{sub}` | full drill-in: counter, resolved policy + which scope won, groups, Cognito profile, open estimates |
| `GET /user/{sub}/ledger?limit=&cursor=` | per-user call history (new `by-sub` GSI on ledger rows) |
| `GET /groups?window=` | group rollup counters + group policies |
| `GET /policies` | every explicit policy row + the implicit env-default |
| `DELETE /policy/{scope}` | remove a policy row (audited; DEFAULT delete = revert to env defaults; self-target rejected) |
| `GET /audit?days=&actor=` | audit rows, newest first |
| `GET /activity?limit=` | recent ledger feed (today + yesterday) |
| `GET /metrics?set=ops` | CloudWatch `GetMetricData` proxy for the fixed `Metering/*` series (spend, denies, degraded, sweeper, canaries) |
| `GET /alarms` · `GET/POST /alert-subscriptions` | alarm states for the module's own alarms; SNS email subscription list/add on the alerts topic |
| `GET /estimates` | open admission estimates (the estimates GSI), oldest first |

- Mutations stay exactly the four that exist (`PUT /policy`, `POST
  /override`, `POST /counter-reset`, + new `DELETE /policy`) — all audited,
  all self-target-rejected. The console UI *shows* the self-target rule
  (disabled action + explanation) rather than hiding it.
- **Rejected:** a separate "console BFF" Lambda (second policy surface to
  keep in sync); AppSync/GraphQL (nothing here needs subscriptions badly
  enough to buy a schema layer); direct-from-browser DynamoDB via Identity
  Pools (would put table IAM in the browser and bypass the audit writer).

## D4 — Rendering consumption at scale: two additive GSIs + attribute stamps, no scans

The table holds millions of ledger rows at design scale — the console must
never scan.

- **`by-window` GSI** (`w`, `used_usd`): counter items get a `w =
  <window>` stamp from the *metering-plane* writers (debit settle,
  threshold check, sweeper resolve, rollup, admin reset). One query per
  window returns user AND group counters ordered by spend — top-spenders
  and near-limit lists are single Queries. Policy rows are stamped
  `w = "POLICY"` so `GET /policies` is also one Query.
  The **enforcement path (interceptor) is untouched** — pre-console counter
  rows created only by floor-debits enter the GSI on their first settle,
  sweep, or threshold check (≤15 min behind; documented).
- **`by-sub` GSI** (`sub`, `sk`): ledger rows already carry `sub`; DynamoDB
  backfills the GSI for existing rows, so per-user history works
  retroactively. Time-ordered because the ledger `sk` starts with the epoch.
- Aggregate time-series come from **CloudWatch metrics the module already
  publishes** (`SettledUSD`, `DenyDecisions`, `DegradedChecks`, …) via
  `GetMetricData` — not from re-aggregating the ledger.
- The users list is paginated (cursor = DynamoDB `LastEvaluatedKey`), and
  the Lambda memoizes hot reads (users/groups/policies) for 15 s per
  container as a cheap stampede guard.

## D5 — Frontend: React + Vite + Cloudscape Design System

**Pick:** TypeScript React SPA, Vite-built, using
[Cloudscape](https://cloudscape.design) (`@cloudscape-design/components`,
Apache-2.0 — including its built-in Line/Bar charts; the newer
`chart-components` package was rejected because it wraps Highcharts, whose
commercial license doesn't belong in an MIT-0 sample) with
`react-oidc-context` for the PKCE flow and React Router for navigation.

- Cloudscape is AWS's own open-source design system, purpose-built for
  exactly this artifact class (operator consoles): tables with server-side
  pagination/filtering/sorting, form fields with validation, flash/alert
  patterns, dashboards, dark mode and density modes, WCAG accessibility —
  for free, and visually "the art of the possible" for an AWS sample.
- **Rejected:** hand-rolled Tailwind UI (weeks of table/form/a11y work to
  reach the same bar); Next.js (SSR server for what is a static app);
  Svelte (fine, but Cloudscape is React and is the differentiator);
  AWS Amplify UI (auth-widget-shaped, not console-shaped).

## D6 — Build/deploy: prebuilt SPA asset, deployed by the stack

`deploy.sh --metering` builds the SPA (`npm ci && npm run build` under
`console/`) exactly like it already vendors the provisioner's boto3 — then
`MeteringStack` ships `console/dist` to the bucket with `BucketDeployment`
plus a deploy-time-resolved `/config.json` (`Source.jsonData`) carrying the
pool id, client id, Managed Login domain, and region. No Docker requirement,
no second deploy step, no drift between infra and app config. Synth with
`metering=on` fails with a clear message if `console/dist` is missing.

## D7 — Non-admin experience: self-service "My usage" only

Any authenticated pool user may sign in and see **their own** counter,
limits, and recent calls (`GET /usage/me`, extended with their ledger
history) — nothing else; the admin nav never renders and, decisively, the
Lambda rejects every admin route with 403 regardless of what the client
does. Rationale: self-service transparency ("why was I blocked?") removes
the #1 support ticket the module generates, uses an endpoint that already
exists, and adds zero privileged surface. The alternative — hard-deny
non-admins at the door — was rejected because it wastes the cheapest
trust-building surface the module has.

## D8 — Demo data: explicit, manifest-tracked seeding

`scripts/seed-demo-metering-data.py` writes clearly-labeled synthetic
users/counters/ledger/audit rows for evaluating the console, records every
key it writes to a local manifest, and `--cleanup` deletes exactly those
keys (never by prefix). Ships in-repo because "evaluate the console without
20K real users" is a real operator need.

## Non-goals (deliberate)

User provisioning/SCIM (the pool owns identity); invoice generation (Cost
Explorer/CUR is the financial truth — the console links out); mid-stream
cutoff controls (out of module scope by design); multi-region aggregation;
mobile layouts (Cloudscape degrades acceptably; operators use desktops).

## Accepted residual risks (from the security review)

- **The edge JWT authorizer is not the admin boundary — the Lambda group
  check is.** The authorizer's `jwtAudience` includes the OWUI app client (the
  token every chat user holds) plus the console + canary clients, because the
  same API serves OWUI users their self-service `/usage/me` and admins their
  full surface under one identity system (D2/D3). Admin isolation therefore
  rests entirely on the server-side `_is_admin()` group check that guards every
  admin route. This is deliberate; the mitigations are that the check is one
  well-tested chokepoint applied uniformly, all admin reads/mutations sit
  behind it, `/config` no longer discloses the admin group names to
  non-admins, and mutations are additionally audited and self-target-rejected.
  A deployment that wants edge-level separation can split the admin API onto a
  console-only audience and route self-service through a separate path.
- **The HTTP API's `$default` stage is public** (kept for `set-quota.sh`
  compatibility) alongside the CloudFront-fronted `api` stage. The Cognito JWT
  authorizer is attached per-route, so it applies to *both* stages — there is
  no auth bypass — but the raw `execute-api` endpoint skips the CloudFront CSP/
  HSTS/TLS-floor. Non-browser callers with a valid pool token can reach it
  directly; browsers cannot (no CORS). Acceptable for a CLI-and-console sample.
- **Login redirect uses top-level navigation**, so the `form-action 'self'`
  CSP directive does not block sign-in (verified end-to-end in the browser;
  oidc-client-ts navigates via `window.location`, not a form POST).
- **Time-boxed overrides do not auto-expire.** The console writes an
  `override_until` marker on the `USER#` policy row, but the enforcement
  interceptor applies the row whenever it exists — so an override ends only
  when an operator deletes the row (the Policies page flags past-date rows red
  for exactly this). Auto-expiry would be a one-line interceptor change
  (skip a `USER#` row whose `override_until < now`, falling through to
  `DEFAULT`), deliberately NOT made here because it alters the module's
  *enforcement* behavior — out of scope for building the console on top of the
  module. Recommended as a fast-follow if operators want hands-off expiry. The
  console and docs state the manual-cleanup semantics plainly so nobody
  mistakes the marker for auto-expiry.
- **Self-target rejection guards the per-user scope, not org-wide scopes.** An
  admin cannot edit/override/reset their *own* `USER#<self>` policy or counter
  (a second admin must act). They *can* edit `DEFAULT` or a `GROUP#` they
  belong to, which also raises their own effective limit — this is not a
  bypass: default/team policy is a legitimate, audited, org-wide admin action
  (any admin can already set any user's limit), and every such change writes
  an actor-stamped audit row. The four-eyes control is specifically about an
  admin quietly lifting *their own individual* ceiling; org-wide changes are
  visible by construction (they affect everyone or the whole team, and are audited).
  Deployments wanting four-eyes on default/group policy too can extend the
  same rejection to those scopes.

## Security posture summary

Admin-group gate enforced server-side per request (never client-side
only) · all mutations audited with before/after (existing mechanism) ·
self-target rejection preserved and surfaced in the UI · same-origin API
(no CORS) · strict CSP, HSTS, nosniff, `frame-ancestors 'none'` ·
private-S3 + OAC (no public bucket) · no-secret PKCE client with
revocation + 60-min access tokens · new IAM grants are read-only and
resource-scoped (pool ARN, alerts-topic ARN, module alarm prefix;
`GetMetricData` is `*` because CloudWatch offers no resource-level scoping
there) · no account ids, ARNs, or emails baked into the SPA (config.json is
deploy-time) · Lambda continues to hold no `dynamodb:*`.
