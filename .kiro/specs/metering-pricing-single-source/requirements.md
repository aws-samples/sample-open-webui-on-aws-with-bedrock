<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Requirements — Metering pricing from a single source

## Introduction

The metering solution currently resolves a per-token rate through six precedence
tiers drawing on five distinct sources: operator overrides, AWS Price List rows,
a pinned LiteLLM community price feed, hand-seeded "default" estimates, and a
deploy-time JSON snapshot bundled into two Lambda assets.

Investigation (`docs/plans/metering-admin-console/05-PRICING-SINGLE-SOURCE.md`)
established that three of those sources exist to compensate for a gap in how the
AWS Price List is read, not a gap in what AWS publishes:

- The refresher reads 2 of the 4 Bedrock Price List service codes. The excluded
  `AmazonBedrockFoundationModels` file carries 238 token-priced dimensions in
  us-east-1 (3,855 across all regions) and holds all modern Anthropic Claude
  pricing. The current usage-type patterns match none of them.
- Catalog rows are keyed from usage-type text, which yields Bedrock model ids for
  "mantle" usage types but display tokens (`Claude3Haiku`, `Claude4Sonnet`) for
  the others. The settle path looks up model ids, so those rows are written and
  never read.
- Consequently the seeded estimates and provider feed price Claude traffic, and
  they are materially wrong where AWS does publish: `claude-opus-4-7` was
  estimated at $15/$75 per 1M against a published $5.50/$27.50 (+172.7%).

This feature replaces that with two sources — what AWS publishes, and what the
operator says — refreshed on a schedule, with any rate overridable by an operator.

### Scope

In scope: the pricing refresh path, the pricing catalog data contract, rate
resolution in the settle and admission paths, routing-mode-aware pricing for
cross-region and global inference profiles, the operator override surface, and
migration of existing catalog rows.

Out of scope: quota/policy enforcement logic, the ledger and counter transaction
model, reconciliation against Cost Explorer, and reserved-throughput commitment
pricing (not per-token; remains export-only). Changing how the gateway invokes
Bedrock is also out of scope — this feature makes pricing correct for
inference-profile routing ahead of any such change, but does not introduce it.

### Terminology

- **Model id** — the Bedrock identifier the gateway invokes and the debit path
  settles under, e.g. `anthropic.claude-opus-5`.
- **Price List name** — the model label as published in a Price List offer file,
  either the `model` attribute or the marketplace `servicename` minus the
  ` (Amazon Bedrock Edition)` suffix.
- **Rate grid** — the set of published rates for one model across
  direction × tier × routing mode × context mode.
- **Routing mode** — how a request reaches the model, inferred from the invoked
  model id: `in_region` (bare id, e.g. `anthropic.claude-opus-5`), `geo` (a
  geographic cross-region inference profile, e.g. `us.anthropic.claude-opus-5`),
  or `global` (a global inference profile, e.g.
  `global.anthropic.claude-opus-5`).
- **Unpriced** — a model with no resolvable rate. Existing designed behaviour:
  record tokens, price at $0, raise an alarm. Never guess a rate.

### Observed routing facts (measured, us-east-1 offer files)

These shape Requirements 6 and 7 and are recorded so the design is not
re-derived. Today the gateway invokes bare model ids over the Bedrock mantle
endpoint through AgentCore, so all current traffic is in-region; these
requirements make the other routing modes correct in advance.

- On-demand token rates are published for two routing modes only: in-region and
  global. **No on-demand token rate is published for `geo` routing** — all 612
  `geo` usage types in the catalog are reserved-throughput commitments
  (`1M TPM Hour` / `1K TPM Hour`), which are out of scope.
- 15 models publish a distinct global on-demand token rate; 119 are in-region
  only. Some (direction, tier, region) slices are published as global only.
- Where a model publishes both, in-region carries a consistent 10% premium
  (Claude Opus 5: $5.50 in-region vs $5.00 global per 1M input). For a few
  (Claude Sonnet 4, Nova 2.0 Lite, Nova 2.0 Omni) the two are identical.

---

## Requirements

### Requirement 1 — Comprehensive coverage from the AWS Price List

**User Story:** As a finance operator, I want every Bedrock model the deployment
serves to carry the rate AWS actually publishes, so that chargeback figures
reconcile to the AWS invoice without manual estimates.

#### Acceptance Criteria

1. WHEN the pricing refresh runs THEN the system SHALL read the
   `AmazonBedrock`, `AmazonBedrockFoundationModels`, and `AmazonBedrockService`
   Price List offer files.
2. WHERE a Price List product carries a token-denominated price dimension, the
   system SHALL extract that rate regardless of whether the model is identified
   by the `model` attribute or by the marketplace `servicename` attribute.
3. WHEN a token rate is published per 1K tokens and another per 1M tokens THEN
   the system SHALL normalize both to a single internal unit before storage.
4. WHEN a token usage type names no inference tier THEN the system SHALL treat
   that rate as the `standard` on-demand tier.
5. WHEN the refresh completes for a us-east-1 deployment THEN the catalog SHALL
   contain an in-region standard rate of $5.50 per 1M input tokens and $27.50 per
   1M output tokens for `anthropic.claude-opus-5`.
6. The system SHALL NOT read pricing from any source other than the AWS Price
   List and operator-entered overrides.
7. The system SHALL read the offer files for the deployment's own region, which
   carry both in-region and global routing rates for that region.
8. IF the deployment region changes THEN a refresh SHALL repopulate the catalog
   for the new region without a code change.

### Requirement 2 — Catalog keyed by Bedrock model id

**User Story:** As an operator, I want the pricing catalog keyed by the same
model identifier the metering path settles under, so that a published rate is
actually applied instead of silently ignored.

#### Acceptance Criteria

1. WHEN the system writes a published rate THEN it SHALL key that row by resolved
   Bedrock model id.
2. WHEN a usage type already contains a Bedrock model id THEN the system SHALL use
   it directly without a mapping step.
3. WHEN a Price List name must be mapped to a model id THEN the system SHALL
   resolve it using the `bedrock:ListFoundationModels` `modelId`→`modelName`
   mapping.
4. WHERE an operator-supplied alias exists for a Price List name, the system SHALL
   prefer that alias over an automatic match.
5. WHEN matching a model id against candidate keys THEN the system SHALL expand
   aliases on the catalog side only and require an exact match on the model id
   being priced.
6. The system SHALL NOT resolve a model id to a Price List name by fuzzy,
   similarity, or truncating-suffix matching. Specifically, the system SHALL NOT
   map `anthropic.claude-opus-4-7` to `Claude Opus 4.6` or
   `anthropic.claude-sonnet-5` to `Claude Sonnet 4`.
7. IF a resolution attempt is ambiguous between two or more Price List names THEN
   the system SHALL treat the model as unresolved rather than selecting one.
8. WHEN an invoked model id carries an inference-profile prefix THEN the system
   SHALL strip that prefix to derive the catalog key AND SHALL retain the prefix
   as the request's routing mode for rate selection.
9. WHERE two model ids differ only by inference-profile prefix, the system SHALL
   resolve them to the same catalog key.

### Requirement 3 — Unresolved rates are visible, not silent

**User Story:** As an operator, I want to see published rates that could not be
bound to a model id, so that coverage gaps become a work queue instead of silent
$0 traffic.

#### Acceptance Criteria

1. WHEN a published rate cannot be resolved to a model id THEN the system SHALL
   record it in a distinct reviewable state that includes the Price List name, the
   candidate rates, and the reason resolution failed.
2. WHILE a rate is in the unresolved state the system SHALL NOT use it to price
   any request.
3. WHEN an operator views the pricing surface THEN the system SHALL list
   unresolved entries separately from priced models.
4. WHEN an operator binds an unresolved entry to a model id THEN the system SHALL
   persist that binding and apply it on the next refresh without a redeploy.
5. WHEN a model the deployment serves has no resolvable rate THEN the system SHALL
   report it as unpriced on the pricing surface.

### Requirement 4 — Scheduled and on-demand refresh

**User Story:** As an operator, I want pricing to stay current automatically, so
that a published price change is reflected without a code change or redeploy.

#### Acceptance Criteria

1. The system SHALL refresh the pricing catalog on a recurring schedule without
   operator action.
2. WHEN an operator requests a refresh from the admin surface THEN the system
   SHALL run a refresh and return the resulting model count and source version.
3. WHEN a refresh succeeds THEN the system SHALL record the source offer-file
   version, the resolved model count, the unresolved count, and the completion
   time.
4. WHEN a refresh fails THEN the system SHALL emit a failure metric, raise an
   alarm, and leave the previously stored rates intact.
5. WHEN a refresh writes new rates THEN the settle path SHALL observe them within
   a bounded cache interval without redeployment.
6. IF one offer file is unavailable THEN the system SHALL continue processing the
   remaining files and SHALL NOT delete rates sourced from the unavailable file.

### Requirement 5 — Operator override capability

**User Story:** As a finance operator with negotiated or custom pricing, I want to
set my own rate for any model, so that metering reflects what my organization
actually pays.

#### Acceptance Criteria

1. WHEN an operator sets an override for a model THEN the system SHALL use that
   rate in preference to the AWS-published rate.
2. WHEN a refresh runs THEN the system SHALL NOT modify or delete any operator
   override.
3. WHEN an operator removes an override THEN the system SHALL revert the model to
   its AWS-published rate, or to unpriced if none exists.
4. WHEN an operator submits an override THEN the system SHALL validate the rate is
   a non-negative number within a plausible per-token bound and SHALL reject it
   otherwise.
5. WHEN an override is created, changed, or removed THEN the system SHALL write an
   audit record identifying the actor, the target model, and the before and after
   values.
6. WHERE an override is in effect, the system SHALL label the rate's source as an
   operator override on the pricing surface and on ledger rows it prices.
7. The system SHALL allow an override to specify rates per direction, and SHALL
   allow an override for a model that has no AWS-published rate.
8. WHERE an override specifies no tier, routing mode, or context qualifier, it
   SHALL apply to every tier, routing mode, and context for that model.
9. The override record SHALL be stored in a shape that can carry an optional
   tier/routing qualifier later without migrating existing overrides.

### Requirement 6 — Rate selection matches request shape

**User Story:** As a finance operator, I want batch, cached, and long-context
requests priced at their published rates, so that costs are not systematically
over- or under-stated.

#### Acceptance Criteria

1. The system SHALL store published rates per direction, inference tier, routing
   mode, and context mode.
2. WHEN pricing a request THEN the system SHALL select the rate matching the
   request's direction, tier, routing mode, and context mode.
3. IF no rate exists for the exact combination THEN the system SHALL fall back in
   a defined, documented order and SHALL record which combination supplied the
   rate.
4. WHEN a model publishes distinct cache-read and cache-write rates THEN the
   system SHALL retain them as separately addressable rates.
5. WHEN a model publishes distinct long-context rates THEN the system SHALL retain
   them as separately addressable rates.

### Requirement 7 — Routing mode is derived per request

**User Story:** As an operator who may move the gateway onto cross-region
inference profiles, I want pricing to follow the routing each request actually
used, so that adopting `us.` or `global.` profiles does not silently mis-bill and
does not require a pricing change.

#### Acceptance Criteria

1. The system SHALL derive a request's routing mode from the invoked model id
   rather than from a fixed deployment-wide assumption.
2. WHEN an invoked model id carries no inference-profile prefix THEN the system
   SHALL price the request as `in_region`.
3. WHEN an invoked model id carries a `global.` prefix THEN the system SHALL price
   the request at the model's published global rate.
4. WHEN an invoked model id carries a geographic prefix (`us.`, `eu.`, `apac.`,
   or another AWS geo scope) THEN the system SHALL price the request at the
   model's published in-region rate, because AWS publishes no on-demand token
   rate for geo routing.
5. IF AWS begins publishing an on-demand token rate for geo routing THEN the
   system SHALL prefer that rate over the in-region rate for geo-routed requests,
   without requiring a schema change.
6. The system SHALL ingest and retain published rates for every routing mode a
   model offers, including models the deployment does not currently invoke through
   an inference profile.
7. IF a request's routing mode has no published rate for the model THEN the system
   SHALL fall back to another published routing mode for that model and SHALL
   record which routing mode supplied the rate.
8. WHERE a model publishes a global rate but no in-region rate for the requested
   direction and tier, the system SHALL use the global rate rather than treating
   the model as unpriced.
9. WHEN the routing mode used to price a request differs from the routing mode the
   request actually used THEN the system SHALL mark that record as a routing
   fallback so the substitution is auditable.
10. The system SHALL expose the derived routing mode on priced records and on the
    pricing surface, so an operator can tell an in-region charge from a global one.
11. WHERE a deployment routes globally by a mechanism that does not appear in the
    model id, a configurable default routing mode SHALL apply to ids that carry no
    prefix; the id-derived mode SHALL take precedence over this default.
12. The system SHALL NOT require a code change to price a model id whose only
    difference from an already-priced id is an inference-profile prefix.

### Requirement 8 — One runtime store with truthful provenance

**User Story:** As an auditor, I want each priced ledger row to state which rate
source and version produced it, so that historical dollars can be explained and
re-derived.

#### Acceptance Criteria

1. The system SHALL resolve rates at runtime from a single store.
2. The system SHALL NOT bundle a price snapshot into any deployment artifact for
   use as a runtime rate source.
3. WHEN the system prices a request THEN it SHALL stamp the resulting record with
   the source and version of the row that supplied the rate.
4. WHEN the admission-time estimate and the settled charge price the same model
   THEN they SHALL draw from the same store and resolution rules.
5. The system SHALL NOT use a hardcoded fallback rate in the admission path.
6. WHEN a rate cannot be resolved THEN the system SHALL record token counts,
   price the request at zero, mark the record unpriced, and emit an unpriced
   metric identifying the model.

### Requirement 9 — Removal of secondary price sources

**User Story:** As a maintainer, I want exactly one automated price source, so
that there is no third-party dependency on a billing path and no ambiguity about
which tier produced a rate.

#### Acceptance Criteria

1. The system SHALL NOT fetch pricing from any third-party or community-maintained
   price list.
2. The system SHALL NOT ship hand-authored estimated rates as a fallback tier.
3. The rate precedence SHALL be exactly: operator override, then AWS-published,
   then unpriced.
4. WHEN the refactor is complete THEN the codebase SHALL contain no remaining
   provider-feed fetch, seeded-default, or bundled-map fallback code paths.
5. The operator-facing surface SHALL present only the source labels that remain
   reachable.

### Requirement 10 — Migration without corrupting history

**User Story:** As an operator upgrading an existing deployment, I want the
re-keying to clean up stale rows while preserving my overrides and my historical
ledger, so that the upgrade is safe.

#### Acceptance Criteria

1. WHEN the refactored refresh first runs THEN the system SHALL remove
   AWS-sourced catalog rows whose key is not a resolved model id.
2. WHEN migration runs THEN the system SHALL NOT modify, delete, or re-key any
   operator override row.
3. The system SHALL NOT retroactively alter rates or dollar amounts on previously
   settled ledger rows.
4. WHEN previously-estimated models gain a published rate THEN the system SHALL
   make the change observable to operators, given that effective rates will move
   materially for affected models.
5. The migration SHALL be documented in the upgrade guidance, including the
   expected direction and magnitude of rate changes.

### Requirement 11 — Least-privilege access

**User Story:** As a security reviewer, I want the pricing path to hold only the
permissions it needs, so that a billing-adjacent component cannot be misused.

#### Acceptance Criteria

1. WHERE the refresh path requires Bedrock model metadata, it SHALL be granted
   read-only listing permission and no inference or mutation permissions.
2. The refresh path SHALL access the Price List over public HTTPS endpoints
   without credentials.
3. The system SHALL restrict override mutation to authenticated operators holding
   an administrative group claim.
4. The system SHALL scope pricing data access to the metering table.

### Requirement 12 — Verification

**User Story:** As a maintainer, I want the pricing rules covered by tests, so
that a future change cannot silently reintroduce a mis-pricing defect.

#### Acceptance Criteria

1. Tests SHALL assert an override takes precedence over a published rate, and
   that removing it reverts the model.
2. Tests SHALL assert a model absent from the catalog resolves as unpriced rather
   than to any default rate.
3. Tests SHALL assert tier fallback behaviour when a requested tier is not
   published.
4. Tests SHALL assert that alias resolution refuses the truncating-suffix
   mismatches named in Requirement 2.6.
5. Tests SHALL assert stored rates are in the expected per-token magnitude,
   guarding the per-1K versus per-1M normalization.
6. Tests SHALL assert routing derivation for all three modes: a bare model id
   prices in-region, a `global.`-prefixed id prices at the global rate, and a
   `us.`-prefixed id prices at the in-region rate.
7. Tests SHALL assert that ids differing only by inference-profile prefix resolve
   to the same catalog key.
8. Tests SHALL assert a routing fallback is recorded when the requested routing
   mode has no published rate, including the global-only case in Requirement 7.8.
9. Tests SHALL assert that a model publishing identical in-region and global rates
   (e.g. Claude Sonnet 4) and one publishing a 10% spread (e.g. Claude Opus 5)
   both price correctly per routing mode.
10. The existing assertion that `anthropic.claude-sonnet-5` is unpriced SHALL be
    replaced, since that model is published.

---

## Traceability

| Finding (05-PRICING-SINGLE-SOURCE.md) | Requirements |
|---|---|
| `AmazonBedrockFoundationModels` never read | 1.1, 1.2, 1.5 |
| Tier-less marketplace usage types unclassified | 1.4, 6.1 |
| Mixed per-1K / per-1M units | 1.3, 12.5 |
| Two incompatible key spaces; published rows unreachable | 2.1–2.3, 10.1 |
| Silent wrong match via suffix stripping | 2.5, 2.6, 12.4 |
| Estimates materially wrong vs published | 1.5, 9.2, 10.4 |
| Provider feed cannot express routing variants | 6.2, 7.1, 7.6, 9.1 |
| No on-demand geo token SKU exists | 7.4, 7.5 |
| In-region carries a 10% premium over global | 7.3, 7.10, 12.9 |
| Inference-profile prefixes unhandled at settle time | 2.8, 2.9, 7.2, 12.6, 12.7 |
| Three price stores; version stamp untruthful | 8.1–8.3 |
| Interceptor estimate diverges from settlement | 8.4, 8.5 |
| Pricing effectively untested | 12.1–12.10 |
