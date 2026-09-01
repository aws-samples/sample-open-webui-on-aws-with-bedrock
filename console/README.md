<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Metering admin and self-service console

[Documentation home](../docs/README.md) · [Metering contract](../docs/METERING.md) ·
[Safe screenshot specification](../docs/images/SCREENSHOT-SPEC.md)

This React/TypeScript/Cloudscape SPA is the browser surface for the opt-in
metering module. `./deploy.sh --metering` builds it and packages it in the
`OpenWebUI-Metering` stack; no additional console deployment command is needed.

## Authorization model

The deployed SPA uses authorization code + PKCE against a dedicated public app
client in the existing Cognito user pool. It stores the OIDC session in browser
`sessionStorage` and calls the admin API under the same CloudFront origin.

- Any authenticated pool user can view **My usage** and their own ledger.
- Members of `admin`, `admins`, or `webui-admins` can access users, teams,
  policies, pricing, audit, health, subscriptions, and admin mutations.
- The API Lambda enforces the same boundary; hidden navigation is not the
  security control.
- Self-targeted policy changes, overrides, and counter resets are rejected.

Group policies/counters are advisory. A policy's `until` value is a cleanup
marker, not automatic expiry; an operator must delete the policy when it ends.
A counter reset does not rewrite ledger history or close estimates.

## Deployed build

From `console/`, the deploy script runs the locked install/build:

```bash
npm ci
npm run build
```

The resulting `dist/` directory is generated and ignored. CDK fails synthesis
for a metering deployment when the expected build output is absent.

## Local development

```bash
npm install
npm run dev
```

Local development requires a deployed non-production metering API and Cognito
client configuration. Create `public/config.json` locally (it is not a
committed deployment artifact) with the region, pool ID, console client ID,
Managed Login domain, and `/api` base; proxy `/api` to the test HTTP API in
`vite.config.ts`.

A localhost OIDC callback requires a temporary change to the **test** console
app client. Record the existing callbacks, add
`http://localhost:5173/auth/callback`, and restore the original set when the
session ends. Do not make this change in an account you have not verified or in
an environment where localhost callbacks are prohibited.

## Pages

| Route | Audience | Purpose |
|---|---|---|
| `/` | Admin | Monthly KPI/metric dashboard |
| `/users`, `/users/:sub` | Admin | User counters, effective policy, ledger |
| `/groups` | Admin | Advisory group rollups |
| `/policies` | Admin | Default/user/advisory-group policies and cleanup markers |
| `/pricing` | Admin | Published/override rates, aliases, unmatched rows, gateway coverage |
| `/audit` | Admin | Mutation audit trail |
| `/health` | Admin | Metrics, alarms, and SNS subscriptions |
| `/me` | Any authenticated user | Own quota/usage/ledger |

## Validation

```bash
npm run build
```

For deployed behavior, test one ordinary user and a different admin. Confirm
server-side 403 responses for admin routes, self-target mutation rejection,
light/dark layout, pagination, empty/error/loading states, and no sensitive
identifiers in screenshots.

Historical UI decisions are indexed under
[`../docs/plans/README.md`](../docs/plans/README.md); current behavior belongs
in [`../docs/METERING.md`](../docs/METERING.md).
