<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Ecosystem adversarial review workflow

- Date: 2026-08-10
- Repository: `sample-open-webui-on-aws-with-bedrock`
- Baseline: `main` at `83bc9100710b8442ed1756925cdcc38835fbcf97`
- Sibling baseline: `sample-agentcore-gateway-governance` `main` at `48d2d8cd605551f1fff00c40214c70b8e53f913f`

## Objective

Review both governance samples as one product ecosystem. Produce evidence-backed findings, audience analysis, and three prioritized roadmaps. Ship only low-risk defects whose behavior and live deployment can be verified without deciding a roadmap question.

## Locked workflow

### Phase 0: safety and provenance

Completion gate:

- Record exact local and remote `main` revisions for both repositories.
- Preserve all pre-existing untracked and local-only files.
- Work on a dedicated review branch in each repository.
- Restrict all AWS work for both deployments to the named `test-web-app` profile in `us-east-1`.
- Keep raw output, endpoints, identities, and screenshots local and untracked; commit only sanitized evidence.

Observed at start:

- Both local revisions matched GitHub `main` on 2026-08-10.
- The user confirmed both projects are deployed to the same account in `us-east-1`.
- The `test-web-app` profile authenticated successfully on 2026-08-10.
- Every AWS command must pass `--profile test-web-app --region us-east-1` explicitly; no default or alternate profile is permitted.

### Phase 1: friendly architecture review

Run separate read-only reviewers for:

1. Open WebUI metering, enforcement, model intelligence, IAM, console, and documentation.
2. Standalone admission, settlement, recovery, pricing, IAM, console, and coding-agent onboarding.
3. Cross-repository pricing-package parity, integration drift, and Git/ADR intent.
4. Product posture, audience journeys, canary coverage, and evidence status.

Completion gate: every architectural statement has a source path and line, a test result, a Git/PR record, or is labeled as requiring live verification.

### Phase 2: deterministic local validation

Run each repository's owned test/build gates without editing generated output:

- Python tests, including focused pricing, interceptor, debit, admin API, canary, and token-helper cases.
- TypeScript compilation and CDK synthesis.
- Console tests/build where present.
- Cross-repository package comparisons and targeted repros for candidate defects.
- A deterministic governance-cost model using measured code-path operation counts and current published AWS prices; assumptions remain explicit.

Completion gate: candidate defects are reproduced or withdrawn. A green command alone is not proof; the observed output must match the claim.

### Phase 3: separated adversarial review

Fresh reviewers that did not perform Phase 1 attack these hypotheses:

- under-billing, over-consumption, attribution evasion, and another-user quota exhaustion;
- alias poisoning, partial refresh, concurrent generation publication, and wrong-price assignment;
- reservation/settlement races, replay, DLQ/redrive, sweep/cancel/refund, and rollup drift;
- attacker-controlled JWT, headers, request bodies, usage events, and console inputs;
- non-admin console access and enterprise evaluation failure modes.

Every adversarial item receives one status:

- `DEMONSTRATED`: repeatable test, local proof, or sanitized live reproduction;
- `SUSPECTED`: plausible but not proven, with the missing proof named;
- `RETRACTED`: attempted and disproved, with the counter-evidence retained.

Completion gate: no claimed bypass remains speculation presented as fact.

### Phase 4: live verification

Use only designated test deployments. Exercise buffered and streamed requests, trusted-emitter and no-emitter paths, over-quota and unpriced behavior, canaries, alarms, catalog/unmatched state, admin/non-admin sessions, and coding-agent clients. Record elapsed onboarding time and governance-service resource use per governed request.

Safety gate:

- Never use `prod-web`, `org-master`, a default profile, or any unnamed account.
- Never delete by prefix or wildcard.
- Teardown only a stack deployed by this run or an individually recorded resource created by this run.
- Do not alter a live shared test stack until `cdk diff` identifies the exact impact.

If credentials remain blocked, preserve static and local findings, mark every live claim `UNVERIFIED`, and do not infer deployment state from source.

### Phase 5: synthesis

Write:

- this repository's findings register and evidence appendix;
- a cross-link to the standalone repository's product analysis and roadmaps;
- a clear shipped-versus-proposed record;
- maintainer decisions with a recommendation and the work each decision unlocks.

The standalone repository is the home for cross-ecosystem product analysis and the governance, model-intelligence, and UI/adoption roadmaps.

Completion gate: every finding includes severity, status, evidence, blast radius, remediation, and effort; every roadmap item includes dependencies and rejected alternatives.

### Phase 6: fix gate and delivery

A change may be self-merged only when all are true:

1. The defect is unambiguous and does not choose a roadmap direction.
2. The fix is small and reversible.
3. Targeted tests plus the repository baseline gates pass.
4. The changed behavior is live-verified when it has a live surface.
5. The PR contains no identifiers, endpoints, credentials, or raw captures.

Use the Compound Engineering LFG pipeline for qualifying fixes. Roadmap-shaping changes remain designs or open, unmerged PRs. If no candidate clears the gate, ship no code and state why.

### Phase 7: claim audit and compounding

A fresh final pass checks source lines, links, test transcripts, sanitization, cross-repository consistency, and original success criteria. Durable maintenance lessons are saved under `docs/solutions/` through `ce-compound`.

## Re-planning triggers

Re-plan rather than forcing the workflow when:

- source behavior contradicts an ADR or merged-PR rationale;
- a candidate finding fails its repro;
- live deployment differs from checked-in `main`;
- a test requires production-like credentials or an account or profile other than `test-web-app`;
- a proposed fix changes failure posture, tenancy, pricing semantics, or evidence policy;
- governance-cost measurement cannot isolate the governed request from background schedules.

## Planned artifacts

- `FINDINGS.md`
- `EVIDENCE.md`
- `SHIPPED-AND-PROPOSED.md`
- cross-link to the standalone `PRODUCT-ANALYSIS.md`, `ROADMAP-GOVERNANCE.md`, `ROADMAP-MODEL-INTELLIGENCE.md`, `ROADMAP-UI-ADOPTION.md`, and `DECISIONS.md`

Raw captures use a `.local.*` suffix and remain untracked.