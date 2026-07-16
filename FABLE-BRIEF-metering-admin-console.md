<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# FABLE BRIEF — Metering Admin Console (standalone web UI for the metering module)

*Fully autonomous overnight-style brief for a standalone Fable session. Design →
implement → deploy → test, end to end, unsupervised, to a working implementation I can
test when I return. Non-prescriptive by intent: the substrate facts below are the ground
truth you must integrate with; every product, UX, and tech decision is yours to reason out,
lock, and defend — then build.*

---

## The problem I want you to solve

The sample's opt-in **metering/enforcement module** (`docs/METERING.md`,
`docs/plans/metering-enforcement/`) already meters per-user LLM token/dollar consumption,
attributes cost per team, and enforces quotas at the AgentCore gateway. But the entire
**operator surface today is a raw HTTP admin API + a `curl` wrapper (`scripts/set-quota.sh`)
+ a CloudWatch dashboard**. There is no human-usable way for an enterprise administrator to
*see* consumption, *manage* quotas, *investigate* a user, or *respond* to an alert. An
admin who wants to raise one user's monthly ceiling has to hand-craft a signed API call.

That is the gap: **a full-featured, professionally-built standalone admin web console** for
everything an enterprise admin needs when governing LLM token consumption across a large
user base — monitoring, managing, setting, and overriding quotas, and everything adjacent
that a real operator would reach for.

## The result I want

A polished, production-grade **Metering Admin Console** — a fully working, deployed,
tested implementation I can log into and use when I return — that:

- is a **standalone web application** (its own URL / distribution — NOT a modification to
  Open WebUI; the zero-OWUI-touch mandate is absolute), and
- **reuses the exact Cognito user pool this sample already deploys for OWUI sign-in**, and
  the **same groups** — an OWUI admin (member of the admin group the module already trusts:
  `admins` / `webui-admins` / `admin`) is automatically a metering/quota admin, with **no
  separate identity system, no second login**, and
- ships **inside this repo and deploys as part of the metering stack** — i.e. it appears
  when (and only when) the metering module is enabled, and the base sample remains
  byte-identical when metering is off (the lean-core gate is sacred — see below), and
- lets an admin do **everything an enterprise LLM-cost operator would want**: at minimum
  monitor live/aggregate consumption, drill into a user or a team/cost-center, set and edit
  quota policies (default / group / per-user), grant time-boxed overrides, reset counters,
  see who's approaching or over their limit, read the audit trail, and act on the alerts
  the module already emits — plus whatever else your research and first-principles view of
  the enterprise-admin job says belongs here.

"Good" looks like: an admin lands on the console, is already authenticated via the org's
existing SSO, and within a minute can answer "who is burning budget, how much is left, and
change a limit" — without touching a terminal, a CloudFront distribution, or the AWS
console. Visually and functionally it should feel like a product, not a sample scaffold —
"the art of the possible."

## Use these sources (the ground truth to integrate with)

Read before designing — these are facts, not suggestions, and getting them wrong breaks
the integration:

- **This repo** `sample-open-webui-on-aws-with-bedrock`, branch **`main`** (base your work
  on a fresh branch off `main`). The metering module is live in-tree and deploy-verified.
- **`docs/METERING.md`** — the operator-surface contract as it exists today: the admin HTTP
  API (routes, Cognito-JWT auth, admin-group gate, audit rows, self-target rejection), the
  `AdminApiUrl` / `MeteringAlertsTopicArn` / `PriceMapVersion` stack outputs, the CloudWatch
  dashboard, the failure posture, and the data the console will render.
- **`docs/plans/metering-enforcement/02-DESIGN.md`** — the data model (the single DynamoDB
  table: `POLICY#…`, `USE#<sub>#<window>` counters, `GROUP#…` rollups, `LEDGER#…`,
  `AUDIT#…`, `EST#…`), the quota policy model (windows, tiers, soft-warn vs hard-block,
  precedence/inheritance, per-model weighting, operator overrides), and the multi-tenant
  identity model. This is your domain spec.
- **`infra/lib/metering-stack.ts`** — how the module is wired today (the admin API Lambda +
  API Gateway HTTP API + Cognito JWT authorizer, the table, the dashboard). The console's
  infrastructure extends this stack; the console deploys with it.
- **`infra/lib/auth-stack.ts`** + `infra/bin/app.ts` — the Cognito pool, the app client, the
  `metering` context flag, the `--metering` deploy path. The console must consume the *same*
  pool. Note the pool's app clients and their auth flows (there's an OWUI client and a
  canary/CLI client already); decide what the console needs and justify it.
- **`metering/admin-api/index.py`** — the current API surface the console will call (and
  which you may extend; extending it is in scope, changing its auth model needs a flag — see
  constraints).
- **Live AWS**, `test` CLI profile (see constraints) — inspect the real deployed metering
  stack, exercise the real API, read the real table, drive the real Cognito flow.
- **The AWS docs / what's-new feed** — before you build any building block (hosting, auth
  wiring, data access, charts), check whether AWS has shipped something (incl. preview) that
  does it better or cheaper than a hand-roll. Do not reinvent a primitive AWS now provides.
  Verify applicability against current docs (cite URL + date for anything load-bearing).
- **The Fable prompt library** (`/mnt/s3files/tools/prompt-library/every-fable5/`) and
  **Compound Engineering** skills — use the LFG pipeline for the software work, the
  visual-verification loop for the UI, and `ce-compound` at the end.

## Important constraints (the hard boundaries)

1. **Zero Open WebUI source modification.** No fork, patch, image rebuild, or upstream edit.
   The console is a *separate* app; it does not live inside, embed into, or alter OWUI.
2. **One identity system.** Reuse the sample's existing Cognito pool and its groups. An
   OWUI admin is a metering admin by group membership — no new user store, no parallel
   role model, no second sign-in. Non-admins must not reach admin functions; decide how a
   non-admin (ordinary user) experience, if any, is scoped, and defend it.
3. **Lean-core / opt-in, unchanged.** The console is part of the metering module: it
   deploys **only** when metering is enabled (`--metering` / `-c metering=on`) and adds
   **zero** resources and **zero** behavior when metering is off. Re-run and preserve the
   existing off-state gate: with the flag off, `cdk synth` of the base stacks stays
   byte-identical. If your design would perturb that, the design is wrong — find another.
4. **Deploys as part of the metering stack deployment.** `./deploy.sh --metering` (or the
   documented deploy path) brings the console up alongside the rest of the module — no
   separate manual deploy step for the operator. Fold the console's infra into the existing
   `MeteringStack` (or a clearly-owned sub-construct of it) and its deploy flow.
5. **Deploy + test target: the `test` AWS CLI profile, its account, that region.** You have
   **full rein to deploy, exercise, iterate, and tear down within the test account** — this
   is net-new work in a non-production account, so deploy freely and autonomously. Do
   **not** touch the live Dev/Prod OWUI fleet, their Cognito pools, or any resource outside
   the test account. **Never delete or force-delete resources by name-prefix/glob** — an
   earlier run destroyed live secrets that way; delete only specific resources you created
   this run, and prefer teardown by stack. Clean up anything you stand up that isn't part
   of the shippable module.
6. **Full commit/push/merge authority on this repo.** Work on a feature branch, commit at
   logical steps, push, run your own adversarial review, open a PR against `main`, and
   **self-merge it once your own review + the live test-account verification pass** (do not
   block the merge on GitHub CI). Deploy/test from your branch as you go so the build isn't
   gated on the merge; land it to `main` when it's real and green.
7. **Publishable hygiene** (this is `aws-samples`-bound): MIT-0 + SPDX headers on all
   AWS-authored files, a clean NOTICE/third-party story for any dependency you add, and
   **no real account IDs, ARNs, emails, or domains** committed anywhere (placeholders only;
   redact anything real you must reference).
8. **Security is a first-class deliverable, not an afterthought.** Every admin action is
   privileged. Carry the existing module's posture forward: admin-group-gated, audited,
   self-target-rejected mutations. Think about the console's own attack surface (token
   handling, CORS, XSS, the hosting model, least-privilege for anything it calls) and treat
   a weakness there as a bug, not a nicety.

**This run is fully autonomous — do NOT pause for a plan-approval checkpoint.** Lock your
own architecture and feature set from your research and first-principles reasoning, record
the decision and the options you rejected, and **continue straight through design → build →
test → deploy** to a working implementation. I want to come back to something I can log
into and test, not a plan to review.

**Deliberately not prescribed** (your call, decided and defended in writing, then built):
the frontend framework and design system; the hosting/serving model for a static or
dynamic app in private-or-public networking; how the browser obtains and refreshes a
Cognito token; whether the console calls the existing admin API as-is or needs new
read/aggregate endpoints (and if so, their shape); how you render consumption at scale
(thousands of users) without hammering DynamoDB; what "everything an enterprise admin
wants" fully includes beyond the floor above; and the visual language. Where a choice is
consequential, briefly record the options and tradeoffs and your pick — but then keep
moving; don't wait on me.

**Any AWS service is on the table** — use whatever managed AWS primitive best fits
(including newer/preview services), with no cost gate; cost is not a constraint for this
run. Prefer fully-managed AWS over hand-rolled where it's the better fit.

**Pause only for** (the genuine hard stops): a destructive or irreversible action outside
the test-account/stack teardown path; a change to the metering module's *enforcement*
behavior or auth model (as opposed to building the console on top of it); a real change to
this brief's scope; missing credentials or an AWS quota block you can't work around; or a
decision only I can make. For everything else — including adding any AWS service or
third-party library the console needs — apply a sensible default, record it, and continue.

## How to work

Use dynamic workflows, subagents, loops, and the installed skills where they fit. For the
software build, use **Compound Engineering's LFG pipeline** (brainstorm → plan → build →
review → test) — run it end to end without stopping for my sign-off. For the UI, run a
**visual-verification loop**: exercise the real console against the test-account
deployment with representative data, capture screenshots of every screen and state
(including empty, error, over-quota, and permission-denied states), record a short video
of the primary admin flow, then review your own captures for layout shifts, broken states,
and anything a real admin would hit that a passing test wouldn't catch. Iterate on what you
find; don't ship a rough edge you can fix.

Commit and push at logical checkpoints. Open the PR (base `main`) when the work is real and
verified. Deploy to the test account and drive the full end-to-end test yourself.

Before treating anything as done, **audit every claim against a tool result or a live test
from this session** — a real browser check for every UI-facing claim, a real API call for
every backend claim, a real `cdk synth`/deploy for every infra claim. If something isn't
verified, say so plainly.

## By morning, leave me a working implementation and this briefing

1. **The outcome in one sentence**, and exactly **how to test it** — the console URL and
   which test-account admin user (+ how to get its credentials/SSO) so I can log in
   immediately.
2. **What you built** — the console's feature set, and the most important decisions
   (architecture, hosting, auth wiring, data access) with the reasoning and the options you
   rejected.
3. **Evidence it works** — the screenshot gallery, the flow video, the deploy/synth output,
   the off-state lean-core re-verification, and the admin-vs-non-admin auth checks.
4. **The security posture** — how admin identity, token handling, and the console's own
   surface are protected, and any residual risk.
5. **What you worked around, what you deferred, and what still needs my decision** — each
   with your recommendation.
6. **The merged PR** (base `main`) with a description I can read after the fact, the merge
   commit, and confirmation the test-account deployment is live (or torn down, if you chose
   to leave it clean — state which, and how I re-stand it up if torn down).

After a meaningful run, use **Compound Engineering (`ce-compound`)** to save the durable
lessons — the admin-console architecture, the Cognito-reuse pattern, and anything about the
metering data model the next run should start from.
