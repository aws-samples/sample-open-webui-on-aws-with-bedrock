<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Tasks — Metering pricing from a single source

Implements [`design.md`](design.md) against [`requirements.md`](requirements.md).

Sequenced so each top-level task leaves the tree building and testable. Tasks 1-5
add new code with no behaviour change; 6-9 switch the runtime over; 10-12 remove
the old paths.

Validation commands (per steering):

```bash
uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
cd infra && npx tsc --noEmit && npx cdk synth --quiet
```

---

- [x] 1. Create the shared pricing package and test fixtures
- [x] 1.1 Add `metering/pricing/__init__.py`, `identity.py`, and `resolver.py` with
  MIT-0 headers, dataclasses (`ModelRef`, `RateResult`), and typed function
  signatures raising `NotImplementedError`.
  - _Requirements: 8.1_
- [x] 1.2 Add `metering/tests/fixtures/` containing trimmed real responses: one
  marketplace offer-file excerpt (Claude Opus 5, Claude Sonnet 4, Claude Haiku 4.5),
  one `AmazonBedrock` excerpt (a mantle model plus a legacy `InputTokenCount`-style
  entry), and a `ListFoundationModels` excerpt. Keep them offline-loadable.
  - _Requirements: 12.5_
- [x] 1.3 Add `metering/tests/test_pricing_resolver.py` and
  `test_pricing_refresher.py` following the existing `importlib` loader pattern in
  `test_admin_api.py`, with the fixtures wired and all cases marked xfail.
  - _Requirements: 12.1_

- [x] 2. Implement model reference parsing in `identity.py`
- [x] 2.1 Implement `parse_model_ref` to strip gateway/pipe prefixes, peel exactly
  one routing scope (`global.` → `global`; `us.`/`eu.`/`apac.`/`ap.`/`ca.`/`sa.` →
  `geo`; else `in_region`), and return `(key, routing)`. Peel only when the
  remainder still matches `vendor.model`.
  - _Requirements: 2.8, 7.1, 7.2, 7.3, 7.4_
- [x] 2.2 Support a `ROUTING_DEFAULT` override applied only when no prefix is
  present, with id-derived routing taking precedence.
  - _Requirements: 7.11_
- [x] 2.3 Write tests: bare id → `in_region`; `global.` → `global`; `us.` → `geo`;
  `bedrock/` and `gateway_anthropic.` prefixes stripped; ids differing only by
  scope produce an identical key.
  - _Requirements: 12.6, 12.7, 2.9_

- [x] 3. Implement safe alias expansion and name normalization in `identity.py`
- [x] 3.1 Implement `id_aliases` stripping only `:N[:tag]`, `-vN`, `-YYYYMMDD`, and
  a trailing `-N` **only when preceded by a digit+letter size token** (e.g.
  `…-120b-1` strips; `…-sonnet-5` does not — the tightened guard that keeps
  `claude-sonnet-5` and `claude-opus-4-7` from collapsing, per design §4.3).
  - _Requirements: 2.5, 2.6_
- [x] 3.2 Implement `normalize_name` (lowercase, drop parenthesised qualifiers,
  remove `instruct`/`it`/`pt`/`bf16`/`vl`/`dense`, strip non-alphanumerics), applied
  only to name-to-name comparisons, never to a model id.
  - _Requirements: 2.3_
- [x] 3.3 Implement `build_index` expanding the catalog side into alias keys and
  requiring an exact match on the queried id; return no match rather than a guess
  when a key is ambiguous.
  - _Requirements: 2.5, 2.7_
- [x] 3.4 Write the regression guard: `anthropic.claude-opus-4-7` must not resolve
  to `Claude Opus 4.6`, and `anthropic.claude-sonnet-5` must not resolve to
  `Claude Sonnet 4`. Assert `openai.gpt-oss-120b-1:0` still aliases to
  `openai.gpt-oss-120b`.
  - _Requirements: 2.6, 12.4_

- [x] 4. Implement rate resolution in `resolver.py`
- [x] 4.1 Implement `resolve_rate(entry, direction, tier, routing, context)`
  returning `RateResult`, checking the flat `OVERRIDE` rates first.
  - _Requirements: 5.1, 5.8_
- [x] 4.2 Map `geo` to the `in_region` routing key, preferring a `geo` key if one
  is ever present in `rates`.
  - _Requirements: 7.4, 7.5_
- [x] 4.3 Implement the in-routing ladder `(tier, context)` → `(tier, default)` →
  `(standard, context)` → `(standard, default)`, recording which combination
  matched.
  - _Requirements: 6.2, 6.3_
- [x] 4.4 Implement cross-routing fallback with `fallback=True` and
  `matched_routing` recorded; return unpriced only when both routing keys are
  exhausted.
  - _Requirements: 7.7, 7.8, 7.9_
- [x] 4.5 Price cache-read and cache-write directions from their own rates, falling
  back to `input` only when absent and flagging the substitution.
  - _Requirements: 6.4, 6.5_
- [x] 4.6 Add `per_token()` conversion from the stored per-1M value.
  - _Requirements: 1.3_
- [x] 4.7 Write tests: override beats published and reverts on delete; absent model
  is unpriced with no default; `flex` falls back to `standard` flagged; global-only
  slice serves an `in_region` request flagged; Claude Opus 5 (10% spread) and
  Claude Sonnet 4 (identical) both correct per routing; Opus 5 in-region resolves
  $5.50/$27.50 per 1M.
  - _Requirements: 12.1, 12.2, 12.3, 12.8, 12.9, 1.5_

- [x] 5. Implement offer-file parsing into the rate grid
- [x] 5.1 Add a parser that walks products and OnDemand price dimensions, keeping
  only token-denominated units, and emits
  `(identity, direction, tier, routing, context, usd_per_1m)`.
  - _Requirements: 1.2_
- [x] 5.2 Extract model identity from either the `model` attribute or the
  marketplace `servicename` minus ` (Amazon Bedrock Edition)`.
  - _Requirements: 1.2, 2.2_
- [x] 5.3 Classify direction, tier, routing, and context from the usage type;
  default a tier-less token usage type to `standard`.
  - _Requirements: 1.4, 6.1_
- [x] 5.4 Normalize per-1K dimensions to per-1M; exclude commitment and
  non-token dimensions.
  - _Requirements: 1.3_
- [x] 5.5 Write tests against the fixtures: marketplace `servicename` extraction,
  tier-less → `standard`, per-1K → per-1M, and a per-1M magnitude guard on stored
  rates.
  - _Requirements: 12.5, 1.4_

- [x] 6. Rewrite the pricing refresher
- [x] 6.1 Fetch the three offer files for the deployment region, tracking per-file
  success and setting `partial` when any fetch fails.
  - _Requirements: 1.1, 1.7, 4.6_
- [x] 6.2 Call `bedrock:ListFoundationModels` and build the alias-expanded index;
  load `PRICING#_ALIAS` rows with one Query.
  - _Requirements: 2.3, 2.4_
- [x] 6.3 Resolve each parsed model via alias → direct id → control-plane name and
  accumulate the routing/tier/context/direction grid.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 7.6_
- [x] 6.4 Write `PUBLISHED` rows keyed by model id with `rates`, `_UNIT`,
  `resolved_via`, `offer_version`, `refresh_generation`, and provider metadata.
  - _Requirements: 2.1, 8.3_
- [x] 6.5 Write `_UNMATCHED` rows with `reason` and `candidate_rates`; delete
  `_UNMATCHED` rows that now resolve.
  - _Requirements: 3.1, 3.2, 2.7_
- [x] 6.6 Implement garbage collection: unconditionally delete `PUBLISHED` rows
  whose key fails model-id validation; delete model-id-shaped rows absent from the
  run only when no offer file failed; never touch `OVERRIDE` or `_ALIAS`.
  - _Requirements: 10.1, 10.2, 4.6, 5.2_
- [x] 6.7 Write `_CATALOG/META` with offer versions, counts, region, generation,
  and `partial`; emit `PricingRefreshModels`, `PricingUnmatched`, and
  `PricingRefreshFailure` metrics.
  - _Requirements: 4.3, 4.4_
- [x] 6.8 Delete `_fetch_provider_rates`, `_write_provider`,
  `_normalize_provider_keys`, `_seed_defaults`, `_load_seed_overrides`,
  `PROVIDER_PRICE_REF`, `PROVIDER_PRICE_URL`, and `_MAX_SANE_RATE`.
  - _Requirements: 9.1, 9.2, 9.4_
- [x] 6.9 Write tests: legacy display-token keys are collected; a simulated partial
  fetch performs no deletion; a re-run is idempotent apart from generation and
  timestamps.
  - _Requirements: 4.6, 10.1_

- [x] 7. Switch the debit Lambda onto the resolver
- [x] 7.1 Replace `_rate` and `_rate_from_row` with resolver calls; reduce
  `_catalog_entry` to fetching `PUBLISHED` and `OVERRIDE` only.
  - _Requirements: 8.1, 9.3_
- [x] 7.2 Derive the routing mode with `parse_model_ref` in `_settle` and pass
  tier, routing, and context into the resolver.
  - _Requirements: 7.1, 7.2, 2.8_
- [x] 7.3 Stamp `price_map_version` from the `offer_version` of the row that
  supplied the rate, and add `routing` and `rate_fallback` to ledger rows.
  - _Requirements: 8.3, 7.9, 7.10_
- [x] 7.4 Keep unpriced behaviour: record tokens, price zero, mark unpriced, emit
  `UnpricedModel`. Emit `PricingRoutingFallback` and `PricingTierFallback` when a
  substitution occurred.
  - _Requirements: 8.6_
- [x] 7.5 Remove `_load_price_map`, the `PRICE_MAP` env path, and the bundled-file
  fallback tier.
  - _Requirements: 8.2, 9.3, 9.4_
- [x] 7.6 Update `test_debit_logic.py`: replace the assertion that
  `anthropic.claude-sonnet-5` is unpriced, and drop the bundled-map shape test.
  - _Requirements: 12.10_

- [x] 8. Switch the metering interceptor onto the catalog
- [x] 8.1 Read `PRICING#` rows through the shared resolver with a short per-container
  TTL cache, replacing the bundled `PRICE_MAP` read.
  - _Requirements: 8.4_
- [x] 8.2 Remove `EST_FALLBACK_IN` and `EST_FALLBACK_OUT` and their use; estimate an
  unresolvable model at zero and admit it. Leave `EST_INPUT_DIVISOR` intact.
  - _Requirements: 8.5, 9.4_
- [x] 8.3 Derive routing with `parse_model_ref` so the estimate and the settle path
  price the same request identically.
  - _Requirements: 8.4, 7.1_
- [x] 8.4 Update `test_interceptor.py` for the new estimate path.
  - _Requirements: 12.1_

- [x] 9. Extend the admin API
- [x] 9.1 Rework `_catalog` to emit the `rates` grid, `unmatched[]`, and
  `aliases[]`, and drop `provider_row` and `default`.
  - _Requirements: 3.3, 3.5, 9.5_
- [x] 9.2 Update `_put_price_override` to write flat `rates` plus `scope: "ALL"`
  and `_UNIT`, with a per-1M validation bound.
  - _Requirements: 5.4, 5.7, 5.8, 5.9_
- [x] 9.3 Add `POST /pricing/alias` and `DELETE /pricing/alias/{name}` with model-id
  validation, admin-group gating, and audit records.
  - _Requirements: 3.4, 5.5, 11.3_
- [x] 9.4 Remove `PRICE_MAP_VERSION` from `GET /config`, reporting the catalog
  version from `_CATALOG/META` instead.
  - _Requirements: 8.3_
- [x] 9.5 Extend `test_admin_api.py` for override validation bounds and alias
  route authorization.
  - _Requirements: 5.4, 11.3_

- [x] 10. Update the console
- [x] 10.1 Update `PriceRow`/`PricingCatalog` in `console/src/types.ts` for the
  `rates` grid, `routing`, `unmatched`, and `aliases`.
  - _Requirements: 3.3_
- [x] 10.2 Reduce `SourceBadge` to `override`, `AWS published`, and `unpriced`.
  - _Requirements: 9.5_
- [x] 10.3 Add an Unmatched view listing unresolved Price List entries with a
  bind-to-model-id action calling the alias route.
  - _Requirements: 3.3, 3.4_
- [x] 10.4 Surface routing mode and per-routing rates on the pricing table, and
  show `rate_fallback` on ledger rows.
  - _Requirements: 7.10_

- [x] 11. Update the CDK wiring
- [x] 11.1 Remove the synth-time price-map read, both staged `model-prices.json`
  copies, and `PRICE_MAP_VERSION` from the admin Lambda environment.
  - _Requirements: 8.2_
- [x] 11.2 Stage `metering/pricing/*.py` into the debit and refresher assets, and
  into the interceptor asset in `gateway-stack.ts` in place of the config copy.
  - _Requirements: 8.1, 8.4_
- [x] 11.3 Grant the refresher `bedrock:ListFoundationModels` and no other Bedrock
  action; keep table access scoped to the metering table.
  - _Requirements: 11.1, 11.2, 11.4_
- [x] 11.4 Add the `PricingUnmatchedAlarm`; keep the existing refresh-failure and
  unpriced alarms and the daily schedule.
  - _Requirements: 4.1, 4.4_
- [x] 11.5 Run `npx tsc --noEmit` and `npx cdk synth --quiet` and resolve any
  findings.
  - _Requirements: 8.1_

- [x] 12. Remove legacy sources and update documentation
- [x] 12.1 Delete `config/model-prices.json`, `config/model-price-overrides.json`,
  and `scripts/generate-price-map.py`.
  - _Requirements: 9.1, 9.2, 9.4_
- [x] 12.2 Rewrite the pricing sections of `docs/METERING.md`: two sources, the
  precedence chain, routing modes, the unmatched queue, and override behaviour.
  - _Requirements: 10.5_
- [x] 12.3 Add upgrade guidance to `docs/UPGRADE_RUNBOOK.md` covering the re-key,
  the GC of legacy rows, preservation of overrides, and the expected rate movement
  (Opus 4.7 −63%, Sonnet 5 −27%, Haiku 4.5 +10%).
  - _Requirements: 10.4, 10.5_
- [x] 12.4 Mark `docs/plans/metering-admin-console/03-PRICING-RECONCILIATION.md`
  and `04-PROVIDER-PRICE-SOURCE.md` superseded by `05-PRICING-SINGLE-SOURCE.md`,
  noting the corrected premise.
  - _Requirements: 9.1_
- [x] 12.5 Note in `scripts/fetch-bedrock-pricing.py` that it is an operator/audit
  export and not on the runtime pricing path.
  - _Requirements: 8.1_

- [x] 13. Gateway-to-pricing coverage join and pricing observability
  Implemented under the gateway-pricing-coverage design
  ([`docs/plans/metering-admin-console/06-GATEWAY-PRICING-COVERAGE.md`](../../../docs/plans/metering-admin-console/06-GATEWAY-PRICING-COVERAGE.md)),
  which extends this spec. Tasks recorded here so the new requirements trace to code.
- [x] 13.1 At the end of the refresh, join served `MODEL_CAPS` + the live gateway
  catalog against the resolved catalog and write `PRICING#_COVERAGE/META` with
  per-model `{listed, catalog_available, priced, source, reason}` and counts;
  record a partial result (never fail the refresh) if the catalog fetch fails.
  - _Requirements: 13.1, 13.2, 13.3, 13.5_
- [x] 13.2 Emit `UnpricedGatewayModels` and add its alarm; keep admission
  unblocked for invokable-unpriced models.
  - _Requirements: 13.4, 13.6_
- [x] 13.3 Record unclassifiable token dimensions in an `unclassified` set and emit
  `PricingDimensionUnclassified`; match named exclusions separately.
  - _Requirements: 14.1, 14.2_
- [x] 13.4 Replace first-wins merge with max+signal (`rate_conflicts`), emit
  `PricingRateConflict`; make the merge order-independent.
  - _Requirements: 14.3_
- [x] 13.5 Classify `_UNMATCHED` by reason and rewire the alarm to
  `PricingUnmatchedActionable` (ambiguous only, not historical no-match).
  - _Requirements: 14.4_
- [x] 13.6 Emit `UnpricedAdmission` on the admission path so proactive coverage
  and reactive settle/admission signals are distinguishable; expose
  `GET /pricing/coverage`.
  - _Requirements: 14.5, 13.3_
