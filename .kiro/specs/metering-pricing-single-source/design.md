<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Design — Metering pricing from a single source

Implements [`requirements.md`](requirements.md). Background analysis:
[`docs/plans/metering-admin-console/05-PRICING-SINGLE-SOURCE.md`](../../../docs/plans/metering-admin-console/05-PRICING-SINGLE-SOURCE.md).

## 1. Overview

```
                    AWS Price List (public HTTPS, no auth)
                    ├─ AmazonBedrock/current/<region>/index.json
                    ├─ AmazonBedrockFoundationModels/current/<region>/index.json
                    └─ AmazonBedrockService/current/<region>/index.json
                                    │  1.77 MB total, us-east-1
                    bedrock:ListFoundationModels
                                    │  modelId → modelName
                                    ▼
                    ┌───────────────────────────────┐
      EventBridge   │   pricing-refresher Lambda    │
      daily ───────▶│   parse → resolve → write     │
      admin POST    └───────────────┬───────────────┘
                                    │
                 DynamoDB (single metering table)
                 ├─ PRICING#<model_id>  sk=PUBLISHED   rate grid, AWS-sourced
                 ├─ PRICING#<model_id>  sk=OVERRIDE    operator-owned, never auto-written
                 ├─ PRICING#_ALIAS      sk=<pl_name>   operator binding
                 ├─ PRICING#_UNMATCHED  sk=<pl_name>   review queue, never prices
                 └─ PRICING#_CATALOG    sk=META        refresh marker
                                    │
                    metering/pricing/resolver.py  (shared, no AWS calls)
                       ┌────────────┴────────────┐
                       ▼                         ▼
              metering/debit (settle)   gateway/metering-interceptor (estimate)
```

One automated source, one operator source, one resolver shared by both runtime
consumers so estimates and settlement cannot disagree.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Refresh the **deployment region only** | Measured: a single region's offer files carry both in-region and global rates (1.77 MB vs 21 MB all-regions). A deployment is billed in its own region. Satisfies Req 1.7. |
| D2 | Alias bindings live in **DynamoDB** | Req 3.4 requires a binding to apply on the next refresh with no redeploy. A `config/` file would need a deploy and would not be editable from the console. |
| D3 | Overrides are **flat across tiers/routing**, per direction | Negotiated pricing is normally a per-token rate per direction. Stored under an explicit `scope` attribute so a future per-tier override needs no migration (Req 5.8, 5.9). |
| D4 | Unpriced models **still serve**, priced at $0, alarmed | Preserves existing design M3 behaviour. Blocking is a quota-policy decision, not a pricing one, and is out of scope. |
| D5 | Store rates **per 1M tokens** as published | The offer files publish per-1K or per-1M; storing the published per-1M decimal keeps the value exactly auditable against the pricing page and removes a float-dust class of drift. Per-token is derived at computation. Ledger `rate_in`/`rate_out` stay per-token so historical rows stay comparable. |
| D6 | Shared resolver copied into both assets at synth | Matches the existing pattern (`metering-stack.ts` already stages `index.py` + JSON into a temp dir; `gateway-stack.ts` already copies a config file into the interceptor asset). Avoids introducing a Lambda-layer artifact the repo has no precedent for. |
| D7 | No new GSI | The catalog is ~150 rows; the admin API's bounded `PRICING#` Scan is adequate. DynamoDB permits one GSI add per stack update, which is a documented upgrade hazard in this stack. (Refined by 06/D9: the bounded Scan became a meta-list `BatchGetItem` — same no-GSI decision, fewer read units.) |

## 3. Data model

All rates are **USD per 1,000,000 tokens**, stored as DynamoDB `N` (decimal,
exact). `_UNIT` is recorded on every row so a future unit change is detectable.

### 3.1 `PRICING#<model_id>` / `PUBLISHED`

```jsonc
{
  "pk": "PRICING#anthropic.claude-opus-5",
  "sk": "PUBLISHED",
  "model_id": "anthropic.claude-opus-5",
  "display_name": "Claude Opus 5",
  "provider": "Anthropic",
  "source": "aws-published",
  "_UNIT": "USD/1M-tokens",
  "resolved_via": "control-plane-name",   // direct-id | control-plane-name | alias
  "price_list_name": "Claude Opus 5",
  "service_code": "AmazonBedrockFoundationModels",
  "region": "us-east-1",
  "offer_version": "20260728133434",
  "effective_date": "2026-07-01T00:00:00Z",
  "refresh_generation": 41,
  "updated_at": 1785000000,
  "rates": {                              // routing → tier → context → direction
    "in_region": {
      "standard": { "default": { "input": 5.5, "output": 27.5,
                                 "cache_read": 0.55,
                                 "cache_write_5m": 6.875,
                                 "cache_write_1h": 11.0 } },
      "batch":    { "default": { "input": 2.75, "output": 13.75 } }
    },
    "global": {
      "standard": { "default": { "input": 5.0, "output": 25.0,
                                 "cache_read": 0.5,
                                 "cache_write_5m": 6.25,
                                 "cache_write_1h": 10.0 } },
      "batch":    { "default": { "input": 2.5, "output": 12.5 } }
    }
  }
}
```

`rates` is one JSON attribute (a map). Grids observed in the live data are
20-50 leaf values — far inside the 400 KB item limit — and one attribute keeps
the hot read to a single `GetItem`/`BatchGetItem` per model.

Direction keys: `input`, `output`, `cache_read`, `cache_write_5m`,
`cache_write_1h`, `cache_write_30m`, `training`. Context keys: `default`, `long`.
Tier keys: `standard`, `batch`, `flex`, `priority`, `latency_optimized`.
Routing keys: `in_region`, `global` (plus `geo` if AWS ever publishes one —
Req 7.5 needs no schema change because routing is just another map key).

### 3.2 `PRICING#<model_id>` / `OVERRIDE`

```jsonc
{
  "pk": "PRICING#anthropic.claude-opus-5", "sk": "OVERRIDE",
  "source": "override", "_UNIT": "USD/1M-tokens",
  "scope": "ALL",                    // D3: reserved for a future tier/routing qualifier
  "rates": { "input": 4.0, "output": 20.0 },
  "note": "negotiated EDP rate", "updated_by": "<sub>", "updated_at": 1785000000
}
```

Flat `rates` — no routing/tier nesting. `scope: "ALL"` makes the intent explicit
and lets a later `scope: "in_region/batch"` coexist without migrating rows.

### 3.3 `PRICING#_ALIAS` / `<price_list_name>`

```jsonc
{ "pk": "PRICING#_ALIAS", "sk": "Claude Opus 5",
  "model_id": "anthropic.claude-opus-5",
  "updated_by": "<sub>", "updated_at": 1785000000 }
```

Sentinel `pk` so the refresher loads all aliases with one `Query`.

### 3.4 `PRICING#_UNMATCHED` / `<price_list_name>`

```jsonc
{ "pk": "PRICING#_UNMATCHED", "sk": "Ministral 8B 3.0",
  "price_list_name": "Ministral 8B 3.0", "provider": "Mistral AI",
  "service_code": "AmazonBedrock", "reason": "no-control-plane-match",
  "candidate_rates": { "in_region": { "standard": { "default": { "input": 0.1, "output": 0.3 } } } },
  "refresh_generation": 41, "updated_at": 1785000000 }
```

Never read by the pricing path (Req 3.2). `reason` ∈ `no-control-plane-match`,
`ambiguous-match`, `no-token-rates`.

### 3.5 `PRICING#_CATALOG` / `META`

`offer_versions` per service code, `region`, `refresh_generation`,
`model_count`, `unmatched_count`, `alias_count`, `refreshed_at`, `duration_ms`,
`partial` (true when any offer file failed).

## 4. Model identity resolution

`metering/pricing/identity.py` — pure functions, no AWS calls, unit-testable.

### 4.1 Parsing an invoked model id

```
parse_model_ref("global.anthropic.claude-opus-5")
  → ModelRef(key="anthropic.claude-opus-5", routing="global")
parse_model_ref("us.anthropic.claude-opus-5")
  → ModelRef(key="anthropic.claude-opus-5", routing="geo")
parse_model_ref("bedrock/anthropic.claude-opus-5")
  → ModelRef(key="anthropic.claude-opus-5", routing="in_region")
```

Steps, in order:

1. Strip a gateway path prefix (`…/`) and a pipe prefix
   (`gateway_anthropic.`, `metering.`).
2. Peel a leading routing scope: `global.` → `global`; `us.` `eu.` `apac.` `ap.`
   `ca.` `sa.` → `geo`; otherwise `in_region`. Only one scope is peeled, and only
   when what follows still looks like `vendor.model` (Req 2.8).
3. The remainder is the catalog key. Two ids differing only by scope therefore
   yield the same key (Req 2.9).

`ROUTING_DEFAULT` (env, default `in_region`) applies only when step 2 finds no
prefix, satisfying Req 7.11 while keeping id-derived routing authoritative.

### 4.2 Binding a Price List rate to a model id (refresher side)

Precedence — first hit wins:

1. **Alias** (`PRICING#_ALIAS`) — operator intent outranks inference (Req 2.4).
2. **Direct id** — the usage type already contains a model id (mantle shapes such
   as `USE1-openai.gpt-oss-120b-mantle-input-tokens-standard`). Validated against
   `^[a-z0-9]+\.[a-z0-9][a-z0-9.\-]*$`.
3. **Control-plane name** — normalized `modelName` from
   `bedrock:ListFoundationModels` matched to the normalized Price List name.

Normalization for step 3 lowercases, keeps parenthesised content as ordinary
tokens (the control plane parenthesises versions — "Pixtral Large (25.02)" —
where the Price List does not), collapses `.0` version tails
("Nova 2.0 Lite" ≡ "Nova 2 Lite"), removes training/format tokens
(`instruct`, `it`, `pt`, `bf16`, `vl`, `dense`), and strips
non-alphanumerics. These are exact, deterministic equivalences — not
similarity matching. It is applied to **both** sides of a name-to-name
comparison — never to a model id.

If step 3 yields zero or more than one candidate, the entry becomes
`_UNMATCHED` (Req 2.7, 3.1).

### 4.3 Safe alias expansion

Model ids are **never** rewritten to find a match. The control-plane index is
built by expanding each catalog-side id into alias keys; lookup then requires an
exact match on the id being priced (Req 2.5).

Expansion strips only suffixes that cannot carry version meaning:

| Pattern | Example |
|---|---|
| `:N` / `:N:tag` | `anthropic.claude-…-v1:0` → `…-v1` |
| `-vN` | `anthropic.claude-opus-4-6-v1` → `…-4-6` |
| `-YYYYMMDD` | `…-haiku-4-5-20251001` → `…-haiku-4-5` |
| trailing `-N` **only after a digit+letter size token** | `openai.gpt-oss-120b-1` → `openai.gpt-oss-120b` |

The guard on the last rule is load-bearing, and implementation tightened it
from "preceded by a letter" to "preceded by a digit+letter size token"
(`…120b-1` strips; `…sonnet-5` does not): the wider letter guard reduced
`anthropic.claude-sonnet-5` to `anthropic.claude-sonnet`, and without any
guard `anthropic.claude-opus-4-7` collapses to `anthropic.claude-opus-4` and
collides with `claude-opus-4-6` — during analysis that silently produced
**Claude Opus 4.6 rates for Opus 4.7** and **Claude Sonnet 4 rates for
Sonnet 5**. Req 2.6 forbids this; §9 pins it with regression tests.

## 5. Rate selection

`metering/pricing/resolver.py`:

```python
def resolve_rate(catalog_entry, direction, tier="standard",
                 routing="in_region", context="default") -> RateResult
```

`RateResult` = `(usd_per_1m, source, matched_routing, matched_tier,
matched_context, fallback: bool)`.

1. **Override first.** If an `OVERRIDE` row carries `direction`, return it with
   `source="override"`. Flat by D3, so tier/routing/context are not consulted.
2. **Routing key.** `geo` → `in_region` (Req 7.4: AWS publishes no on-demand geo
   token rate — verified, all 612 `geo` usage types are reserved-throughput
   commitments). If a future `geo` key exists in `rates`, it is preferred
   (Req 7.5).
3. **Ladder within the routing key**, first hit wins:
   `(tier, context)` → `(tier, default)` → `(standard, context)` →
   `(standard, default)`
4. **Cross-routing fallback.** If the ladder is exhausted, repeat it under the
   other routing key and set `fallback=True` with `matched_routing` recorded.
   This covers the 15 models with global-only slices (Req 7.8).
5. Otherwise unpriced.

Per-token conversion happens once at the call site:
`usd = tokens * usd_per_1m / 1_000_000`, rounded to 8dp as today.

Cache-read and cache-write tokens are priced from their own directions where
published, falling back to `input` only if absent — recorded as a fallback.

## 6. Refresher

`metering/pricing-refresher/index.py`, rewritten. Flow:

1. Read `PRICING#_ALIAS` (one `Query`).
2. Fetch the three offer files for `REGION`. Track per-file success.
3. `bedrock:ListFoundationModels` for the region; build the alias-expanded index.
4. Parse every token-denominated price dimension into
   `(price_list_name | model_id, direction, tier, routing, context, usd_per_1m)`.
   A token usage type naming no tier is `standard` (Req 1.4). Per-1K dimensions
   are multiplied by 1000 (Req 1.3).
5. Resolve each model per §4.2; accumulate grids.
6. Write `PUBLISHED` rows with `refresh_generation = previous + 1`.
7. Write `_UNMATCHED` rows for the residue; delete `_UNMATCHED` rows that now
   resolve.
8. **Garbage-collect** (Req 10.1, constrained by Req 4.6):
   - Delete any `PUBLISHED` row whose key fails model-id validation. These are the
     legacy display-token keys (`Claude3Haiku`, `Claude4Sonnet`) that the settle
     path can never read — unconditionally safe.
   - Delete model-id-shaped `PUBLISHED` rows absent from this run **only if every
     offer file fetched successfully**. On a partial refresh, retain and log.
   - **Never** touch `OVERRIDE` or `_ALIAS` rows (Req 5.2, 10.2).
9. Write `_CATALOG/META`, emit metrics.

Idempotent: a re-run with unchanged inputs produces identical rows apart from
`refresh_generation` and `updated_at`.

## 7. Runtime consumers

### 7.1 Debit (settle)

`_rate()` is replaced by a call into the shared resolver. `_catalog_entry()`
keeps its per-container TTL cache but now fetches two sort keys
(`PUBLISHED`, `OVERRIDE`) instead of four.

Ledger row changes:

| Field | Change |
|---|---|
| `rate_in`, `rate_out` | unchanged contract — per-token, derived from per-1M |
| `price_source` | `override` \| `aws-published` \| `unpriced` only |
| `price_map_version` | now the `offer_version` of the row that supplied the rate (Req 8.3) |
| `routing` | **new** — derived routing mode (Req 7.10) |
| `rate_fallback` | **new** — set when tier/routing/context substitution occurred (Req 7.9) |

### 7.2 Interceptor (admission estimate)

Reads the same rows through the same resolver, with its own short TTL cache. The
bundled `model-prices.json` and the hardcoded `EST_FALLBACK_IN` / `EST_FALLBACK_OUT`
are removed (Req 8.5). A model with no resolvable rate estimates at $0 and is
admitted, consistent with D4. `EST_INPUT_DIVISOR` (token-count heuristic) is
unrelated to pricing and stays.

The interceptor already has metering-table access for estimate writes, so no new
IAM grant is needed.

### 7.3 Admin API

- `GET /pricing` — adds `unmatched[]`, `aliases[]`, and per-model `rates` grid;
  drops `provider_row` and `default`.
- `PUT /pricing/{model}` — unchanged shape, writes `rates` + `scope`, still
  audited. Validation bound becomes per-1M (`0 ≤ rate ≤ 1e6`).
- `DELETE /pricing/{model}` — unchanged.
- `POST /pricing/alias` — **new**, binds a Price List name to a model id, audited.
- `DELETE /pricing/alias/{name}` — **new**.
- `POST /pricing/refresh` — unchanged; returns model/unmatched counts and version.

## 8. Infrastructure

`infra/lib/metering-stack.ts`:

- Remove the synth-time price-map read and both staged copies; remove
  `PRICE_MAP_VERSION` from the admin Lambda env.
- Stage `metering/pricing/*.py` into the debit and refresher assets (D6).
- Grant the refresher `bedrock:ListFoundationModels` on `*` (a list operation with
  no resource-level scoping), and nothing else Bedrock-related (Req 11.1).
- Alarms (all namespace `Metering`, warning-surface unless noted): the
  refresh-failure and unpriced alarms, plus the coverage/observability set added
  by the gateway-pricing-coverage design
  ([`docs/plans/metering-admin-console/06-GATEWAY-PRICING-COVERAGE.md`](../../../docs/plans/metering-admin-console/06-GATEWAY-PRICING-COVERAGE.md)):
  `UnpricedGatewayModels` (Req 13.4), `PricingUnmatchedActionable` (Req 14.4,
  which replaces the earlier raw `PricingUnmatched` count alarm),
  `PricingDimensionUnclassified` (Req 14.1), and `PricingRateConflict` (Req 14.3).

`infra/lib/gateway-stack.ts`: replace the `config/model-prices.json` copy with the
shared resolver copy.

Metrics (namespace `Metering`): `PricingRefreshFailure`, `PricingRefreshModels`,
`PricingRoutingFallback`, `PricingTierFallback`, `UnpricedModel` (existing),
`UnpricedAdmission` (admission-path estimate resolved no rate, Req 14.5),
`UnpricedGatewayModels` (coverage join, Req 13.4), `PricingUnmatchedActionable`
(Req 14.4), `PricingDimensionUnclassified` (Req 14.1), and `PricingRateConflict`
(Req 14.3). The coverage item itself is `PRICING#_COVERAGE/META`; see 06 for the
join, the coverage universe, and the `GET /pricing/coverage` surface.

## 9. Testing

`metering/tests/test_pricing_resolver.py` (new) — pure-function coverage:

| Test | Requirement |
|---|---|
| override beats published; delete reverts | 12.1 |
| absent model → unpriced, never a default | 12.2 |
| `flex` falls back to `standard`, flagged | 12.3 |
| **`claude-opus-4-7` never resolves to `Claude Opus 4.6`; `claude-sonnet-5` never to `Claude Sonnet 4`** | 12.4, 2.6 |
| per-1M magnitude guard on stored rates | 12.5 |
| bare → in-region; `global.` → global; `us.` → in-region | 12.6, 7.2-7.4 |
| prefix-variant ids collapse to one key | 12.7, 2.9 |
| global-only slice → in-region request uses global, flagged | 12.8, 7.8 |
| Opus 5 (10% spread) and Sonnet 4 (identical) both correct per routing | 12.9 |
| Opus 5 in-region resolves $5.50/$27.50 per 1M | 1.5 |

`metering/tests/test_pricing_refresher.py` (new) — parser and GC against a
trimmed real offer-file fixture: marketplace `servicename` extraction, tier-less
usage type → `standard`, per-1K → per-1M, legacy-key GC, and that a simulated
partial fetch performs no deletion (4.6).

Update `test_debit_logic.py`: replace the assertion that
`anthropic.claude-sonnet-5` is unpriced (Req 12.10).

Fixtures are trimmed real responses, committed under `metering/tests/fixtures/`,
so tests stay offline.

## 10. Rollout

Each step deploys and is reversible on its own.

| Step | Change | Reversible by |
|---|---|---|
| 1 | Add `metering/pricing/` (identity + resolver) with tests. No behaviour change. | delete, unused |
| 2 | Refresher: third offer file, marketplace shapes, control-plane join, model-id keying, `_UNMATCHED`, GC. Writes new rows alongside old. | previous Lambda version |
| 3 | Debit reads through the resolver; precedence collapses to override → published → unpriced. | previous Lambda version |
| 4 | Interceptor reads the catalog; drop its bundled map and hardcoded fallbacks. | interceptor alias repoint (existing mechanism) |
| 5 | Remove price-map bundling, `PRICE_MAP_VERSION`; stamp version per row. | CDK rollback |
| 6 | Admin API alias routes; console unmatched tab and badge cleanup. | CDK rollback |
| 7 | Delete `config/model-prices.json`, `config/model-price-overrides.json`, `scripts/generate-price-map.py`. Mark plans `03`/`04` superseded; rewrite `docs/METERING.md` pricing sections and add upgrade guidance. | git |

Step 3 is the point at which chargeback figures move. Expected direction, to be
announced with the deploy (Req 10.4, 10.5):

| Model | Before (estimate) | After (published, in-region) |
|---|---|---|
| `anthropic.claude-opus-4-7` | $15 / $75 per 1M | **$5.50 / $27.50** (−63%) |
| `anthropic.claude-sonnet-5` | $3 / $15 | **$2.20 / $11.00** (−27%) |
| `anthropic.claude-haiku-4-5` | $1 / $5 | **$1.10 / $5.50** (+10%) |

Settled ledger rows are not rewritten (Req 10.3); tokens remain the invariant and
dollars stay re-derivable from `offer_version`.

## 11. Rejected alternatives

- **Fuzzy/similarity model matching to raise coverage above 60%.** Produces
  plausible wrong dollars; demonstrated during analysis. `_UNMATCHED` plus an
  operator binding is slower but correct.
- **Keep a bundled snapshot as a last-resort tier.** Reintroduces the third store
  and the untruthful version stamp that Req 8 exists to remove.
- **Lambda layer for the shared resolver.** No layer precedent in this repo;
  synth-time staging already exists for exactly this.
- **All-region refresh.** 12× the download for no benefit to a single-region
  deployment, since global rates are already in the regional file.
