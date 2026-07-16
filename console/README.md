<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Metering Admin Console

The web operator surface for the opt-in metering module
([`docs/METERING.md`](../docs/METERING.md)): monitor consumption, investigate
users and teams, set quota policies, grant time-boxed overrides, reset
counters, read the audit trail, and watch the module's own health — signed in
with the same Cognito pool (and admin groups) the sample already uses.

Architecture and decision record:
[`docs/plans/metering-admin-console/01-DECISIONS.md`](../docs/plans/metering-admin-console/01-DECISIONS.md).

## Deploy

Nothing extra — `./deploy.sh --metering` builds this app and ships it inside
the `OpenWebUI-Metering` stack. The stack output `ConsoleUrl` is the address.
Sign in with any pool user; members of an admin group (`admin`,
`admins`, `webui-admins`) get the full console, everyone else gets a
self-service "My usage" view.

## Local development

```bash
cd console
npm install
npm run dev          # http://localhost:5173
```

Local dev needs a deployed metering stack to talk to. Copy
`public/config.json` from a deployed site (or craft one):

```json
{
  "region": "<region>",
  "userPoolId": "<pool id>",
  "clientId": "<console client id — stack output ConsoleClientId>",
  "cognitoDomain": "<managed login domain>",
  "apiBase": "/api"
}
```

and proxy `/api` to the deployed admin API's `api` stage in
`vite.config.ts` (`server.proxy`), plus temporarily add
`http://localhost:5173/auth/callback` to the console app client's callback
URLs. None of this is required for the deployed path.

## Stack

React 18 + TypeScript + Vite; [Cloudscape Design System](https://cloudscape.design)
(Apache-2.0); `react-oidc-context`/`oidc-client-ts` (authorization-code +
PKCE against the pool's Managed Login). The build ships as a private-S3 +
CloudFront static site with the admin API mounted same-origin under `/api`.
