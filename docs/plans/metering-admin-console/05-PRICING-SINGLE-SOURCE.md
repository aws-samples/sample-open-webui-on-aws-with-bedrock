<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Pricing refactor — one source (AWS Price List) plus operator overrides

Supersedes the multi-source design in
[`04-PROVIDER-PRICE-SOURCE.md`](04-PROVIDER-PRICE-SOURCE.md) and the seeded
estimates in [`03-PRICING-RECONCILIATION.md`](03-PRICING-RECONCILIATION.md).

**Goal:** the metering solution prices every model from the AWS Price List and
nothing else, refreshes on a schedule, and lets an operator override any rate.
Two sources total: *what AWS publishes* and *what the operator says*.

---

## 1. Why the current design has five sources

The settle path in `metering/debit/index.py` resolves a rate through six tiers:

```
operator OVERRIDE → aws-published → provider-list (LiteLLM) → seeded DEFAULT → bundled file → unpriced
```

Three of those tiers exist to work around a gap that is self-inflicted. The
premise of `04-PROVIDER-PRICE-SOURCE.md` — "AWS's Price List feed lags
newly-launched models" — is **false for the models it names**.

### Finding 1: the refresher reads two of the four Bedrock service codes

Bedrock pricing is published under four Price List service codes. The refresher
(`metering/pricing-refresher/index.py` L49) reads two:

| Service code | Read today? | Contains |
|---|---|---|
| `AmazonBedrock` | yes | Nova/Titan, open-weight models, `openai.gpt-5.x`, platform features |
| `AmazonBedrockService` | yes | Reserved throughput, cross-region rates |
| **`AmazonBedrockFoundationModels`** | **no** | **All modern Anthropic Claude**, Cohere, AI21, Stability, TwelveLabs, Writer |
| `AmazonBedrockAgentCore` | no (correct) | AgentCore runtime, not per-model tokens |

`AmazonBedrockFoundationModels` is excluded by a comment in
`scripts/generate-price-map.py` L54-57 asserting it is "marketplace-shape MP:
units / provisioned throughput, not per-model token rates". Measured against the
live `us-east-1` offer file, that file carries **238 token-priced dimensions**
(3,855 across all regions), and the current usage-type regexes match **0** of
them, because marketplace usage types look like
`USE1-MP:USE1_input_tokens_standard-Units` and carry the model in `servicename`,
not in the usage type.

Every Claude rate the seeded estimates and the LiteLLM feed were added to supply
is published by AWS in that file:

| Seeded "estimate" model | Estimate ($/1M in/out) | AWS published, us-east-1 in-region | Error |
|---|---|---|---|
| `anthropic.claude-opus-4-7` | $15.00 / $75.00 | **$5.50 / $27.50** | **+172.7%** |
| `anthropic.claude-sonnet-5` | $3.00 / $15.00 | **$2.20 / $11.00** | **+36.4%** |
| `anthropic.claude-haiku-4-5` | $1.00 / $5.00 | **$1.10 / $5.50** | **−9.1%** |

`anthropic.claude-opus-4-8` and `anthropic.claude-fable-5` are also published and
are not in the seed file at all.

The LiteLLM rate for Sonnet 5 ($2/$10, per `04`) matches AWS's **global**
routing rate, not the **in-region** rate this deployment bills under
($2.20/$11.00) — a 10% under-bill. This is the general hazard: a provider list
cannot express AWS's routing, tier, and context-length variants.

### Finding 2: the `aws-published` tier is largely unreachable

The refresher derives its DynamoDB key from the usage-type text, which yields two
incompatible key spaces:

- **mantle** usage types → real Bedrock model ids (`deepseek.v3.1`,
  `google.gemma-3-12b-it`) → these **do** match at settle time
- **classic / service** usage types → display tokens (`Claude3Haiku`,
  `Claude4Sonnet`, `Llama3-1-70B`) → these **never** match, because
  `metering/debit/index.py` `_rate()` looks up a normalized Bedrock model id

So the refresher writes 106 `PRICING#<key>/PUBLISHED` rows, but only 36 of the 47
live catalog ids can resolve one, and every `Claude*` row is dead weight the
console still displays as "AWS published". This is why Claude appeared unpriced
even after `02-PRICING-INVESTIGATION.md` widened the parser.

### Finding 3: the bundled snapshot is a third store with its own staleness

`config/model-prices.json` is read at synth time (`infra/lib/metering-stack.ts`
L77) and bundled into **two** Lambda assets (debit, metering-interceptor). It is
the ledger's `price_map_version` stamp *even when the rate came from DynamoDB*,
so provenance on ledger rows is wrong whenever the catalog served the rate. It
also drifts until someone reruns a script and redeploys.

### Finding 4: pricing is effectively untested

`metering/tests/test_debit_logic.py` has one pricing test, and it asserts
`_rate("anthropic.claude-sonnet-5", "input") == (None, "unpriced")` — it encodes
the bug as expected behaviour.

---

## 2. Target design

```
                 ┌─────────────────────────────┐
   schedule ───▶ │  pricing-refresher Lambda   │
                 │  1. Price List bulk offers  │  4 service codes, all regions
                 │  2. bedrock:ListFoundation… │  modelId → modelName join
                 └──────────────┬──────────────┘
                                │ writes
                    ┌───────────▼────────────┐
                    │ PRICING#<model_id>     │
                    │   sk=PUBLISHED         │  ← AWS, refreshed, never hand-edited
                    │   sk=OVERRIDE          │  ← operator, wins, never auto-touched
                    │   sk=UNMATCHED         │  ← needs review, priced as unpriced
                    └───────────┬────────────┘
                                │ read (cached)
                       debit ───┴─── interceptor
```

Two sources, two rows, one precedence rule:

```
operator OVERRIDE → aws-published → unpriced
```

**Removed:** `provider-list` (LiteLLM), seeded `DEFAULT`, and the bundled-file
fallback. An unpriced model records tokens, prices at $0, and alarms — which is
the existing designed behaviour (design M3) and stays.

### 2.1 Key the catalog by Bedrock model id

`PRICING#<key>` must use the same id the debit path settles under. The refresher
resolves each Price List rate to a model id by, in order:

1. **Direct** — the mantle usage type already contains the model id
   (`USE1-openai.gpt-oss-120b-mantle-input-tokens-standard`). No mapping.
2. **Control-plane join** — `bedrock:ListFoundationModels` gives an
   AWS-authoritative `modelId → modelName`, and `modelName` matches the Price
   List `servicename` (marketplace, minus ` (Amazon Bedrock Edition)`) or the
   `model` attribute. This is an AWS-to-AWS join, not a guess.
3. **Operator alias** — `config/model-price-aliases.json`, for the residue.

Measured coverage against the 47 ids in `config/model-capabilities.json`:

| Path | Models |
|---|---|
| direct model-id | 1 |
| control-plane name join | 27 |
| **resolved without human input** | **28 / 47 (60%)** |
| in Bedrock, no Price List token SKU → genuinely unpriced | 8 |
| no control-plane match in-region → needs alias or is unavailable | 11 |

60% is not the finish line; it is the floor that requires **zero** hand-maintained
mapping. The 11 unmatched are mostly `-instruct`-suffixed Qwen ids and preview
`openai.gpt-5.5`; several (`Qwen3 235B A22B 2507`, `Qwen3 Coder 480B A35B`) do
exist in the Price List and will resolve once aliased.

> **Do not add fuzzy name matching to close the gap.** While developing this
> plan, a normalizing slug join silently mapped `anthropic.claude-opus-4-7` to
> **Claude Opus 4.6** and `anthropic.claude-sonnet-5` to **Claude Sonnet 4** by
> stripping a trailing `-N`. Both look plausible and both are wrong dollars.
> Alias expansion is only safe in one direction: expand the *catalog* side into
> candidate keys and require an *exact* match on the id being priced.

### 2.2 Make "unmatched" a visible state, not a silent miss

Price List rates that resolve to no model id are written as
`PRICING#<price_list_name>/UNMATCHED` with the candidate rates and the reason.
They never price traffic. The console lists them so an operator can bind one to a
model id with a click, which writes `config`-equivalent alias state in DynamoDB.
This converts the current silent-fallthrough into a work queue.

### 2.3 Store the tier/routing/context grid, price by request shape

Marketplace and mantle models publish rates per
(direction × tier × routing × context_mode). The refresher stores the whole grid
per model and the debit path selects with explicit, logged fallback:

```
(tier, routing, context) → (tier, routing) → (tier, regional) → standard/regional
```

`routing` defaults to `regional`. The gateway invokes bare model ids — no
`global.`/`us.` inference-profile prefixes appear anywhere in `gateway/` or
`pipe/` — so in-region rates are correct today; a config switch covers a future
move to global profiles. This matters: for Claude Opus 5 the two differ by 10%
($5.50 vs $5.00 per 1M input).

### 2.4 One store, not three

DynamoDB `PRICING#` rows become the only runtime price source.

- Delete the `PRICE_MAP` bundling into the debit and interceptor assets.
- Stamp ledger `price_map_version` from the **row that supplied the rate**, not
  a synth-time constant, so provenance is truthful.
- The interceptor's admission estimate reads the same catalog (cached) instead of
  a bundled map plus hardcoded `3e-06`/`1.5e-05` fallbacks
  (`gateway/metering-interceptor/index.py` L78-85). Estimates and settlement stop
  disagreeing by construction.
- Keep `scripts/fetch-bedrock-pricing.py` as the operator/audit CSV export. It is
  not on the runtime path.

---

## 3. Work plan

Ordered so each step is independently deployable and reversible. Steps 1-3 are
correctness fixes; 4-6 are the simplification; 7-8 are cleanup.

| # | Change | Files |
|---|---|---|
| 1 | Add `AmazonBedrockFoundationModels` (+ marketplace usage-type shapes and the `servicename` model carrier) to the refresher. Default a tier-less token usage type to `standard`. | `metering/pricing-refresher/index.py` |
| 2 | Add the control-plane join: call `bedrock:ListFoundationModels`, key rows by resolved **model id**, write `UNMATCHED` rows for the residue. Grant `bedrock:ListFoundationModels` to the refresher role. | `metering/pricing-refresher/index.py`, `infra/lib/metering-stack.ts` |
| 3 | Store the full tier/routing/context grid; implement explicit selection + fallback in `_rate_from_row`. | `metering/pricing-refresher/index.py`, `metering/debit/index.py` |
| 4 | Collapse precedence to `OVERRIDE → PUBLISHED → unpriced`. Delete `_fetch_provider_rates`, `_write_provider`, `_normalize_provider_keys`, `_seed_defaults`, `_load_seed_overrides`, `PROVIDER_PRICE_*`, `_MAX_SANE_RATE`. | `metering/debit/index.py`, `metering/pricing-refresher/index.py` |
| 5 | Point the interceptor's estimate at the DynamoDB catalog; drop its bundled map and hardcoded fallback rates. | `gateway/metering-interceptor/index.py`, `infra/lib/gateway-stack.ts` L137-141 |
| 6 | Remove price-map bundling and `PRICE_MAP_VERSION` env; stamp version per-row. | `infra/lib/metering-stack.ts` L77-83, L241, ~L397-425, `metering/debit/index.py`, `metering/admin-api/index.py` |
| 7 | Console: drop the `provider-list` and `default-override` badges; add an **Unmatched** tab with bind-to-model-id. Keep override/revert/refresh as-is — that surface is already right. | `console/src/pages/PricingPage.tsx`, `console/src/types.ts` |
| 8 | Delete `config/model-price-overrides.json`, `config/model-prices.json`, `scripts/generate-price-map.py`. Mark `03`/`04` superseded. Rewrite `docs/METERING.md` L105-145 + L218-225. | as listed |

### Tests to add (`metering/tests/test_debit_logic.py`)

Pricing currently has one test, and it asserts the bug. Replace it with:

- `anthropic.claude-opus-5` resolves a rate from a `PUBLISHED` fixture; assert
  the **in-region** rate ($5.50/$27.50 per 1M), not global
- `OVERRIDE` beats `PUBLISHED`; deleting the override reverts
- a model absent from the catalog is `(None, "unpriced")` — still never a guess
- tier fallback: `flex` → `standard` when the model publishes no flex rate
- the alias expander refuses to map `claude-opus-4-7` onto `claude-opus-4-6`
  (regression guard for the silent-mismatch class above)
- per-token magnitude guard (keep the existing per-1K/per-1M assertion)

### Migration

One-way, no ledger rewrite. Existing `PRICING#Claude3Haiku/PUBLISHED`-style rows
are orphaned by the re-key, so the refresher deletes `PRICING#` rows whose `sk`
is `PUBLISHED`/`PROVIDER`/`DEFAULT` and whose key is not a resolved model id, on
its first post-deploy run. **`OVERRIDE` rows are never touched.**

Historical ledger rows keep their original `rate_in`/`rate_out`/
`price_map_version`; per `02-DESIGN.md` §backdated-repricing, tokens are the
invariant and dollars are re-derivable. Expect a visible drop in effective rates
for Claude traffic after step 1 (Opus 4.7 falls 63%, from $15/$75 to $5.50/$27.50
per 1M) — announce it, because chargeback reports will move.

### Open questions

1. **Routing default.** Confirm in-region (`regional`) is correct for the
   AgentCore gateway path. If a future change adopts `global.`/`us.` inference
   profiles, this becomes a per-deployment config value.
2. **Refresh cadence.** Daily is fine for published rates. The bulk offer files
   are ~21 MB across four service codes; fetching all regions in one Lambda needs
   ~512 MB and ~60s, or restrict to the deployment region (current behaviour) and
   accept that a region switch needs a refresh.
3. **Reserved capacity.** `AmazonBedrockService` reserved-throughput commitments
   are not per-token and are out of scope for the settle path. Keep them in the
   CSV export only.

---

## Addendum — 2026-08-20/21 re-measurement (does not rewrite the above)

The coverage and dimension figures in §2.1 above were the analysis-time floor.
They were re-measured live on 2026-08-20/21 against account `TEST_ACCOUNT_ID`,
us-east-1, table `open-webui-metering` (refresh generation 20), and against the
three live Bedrock offer files. This addendum records the current numbers; the
original figures are kept above as the reasoning that led here. The coverage
join that produced these is designed in
[`06-GATEWAY-PRICING-COVERAGE.md`](06-GATEWAY-PRICING-COVERAGE.md).

**Coverage is now per-lane, not a single "28/47 (60%)".** The gateway serves
three lanes; measured priced/unpriced by lane (served `MODEL_CAPS` LEFT JOIN the
priced catalog):

| Lane | Total | Priced | Unpriced |
|---|---|---|---|
| chat_completions | 46 | 41 | 5 |
| responses | 13 | 6 | 7 |
| messages | 5 | 5 | 0 |

The "60% (28/47)" floor no longer holds — coverage is high and lane-dependent.

**Exactly 8 models remain unpriced, and the set is not the one §2.1/`02` named.**
Every one is `row-absent` (no `PRICING#<id>` row at all) because it has **no SKU
in any of the three live offer files**, verified per model:
`openai.gpt-5.4`, `openai.gpt-5.4-2026-03-05`, `openai.gpt-5.5`,
`openai.gpt-5.5-2026-04-23`, `openai.gpt-5.6-luna`, `openai.gpt-5.6-sol`,
`openai.gpt-5.6-terra`, `zai.glm-4.6`. This is the GPT-5.x/GLM **mantle
publishing-gap family** — an AWS publishing gap, not a parser or join defect.
The search is sound, not a weak grep: the positive control `openai.gpt-oss-*`
matched **16 SKUs each** in the same files. The count "8" coincidentally equals
the earlier claim, but the *members* differ — the frontier Claude ids that `02`
called unpublished are now priced from `AmazonBedrockFoundationModels`. 7 of the
8 have AWS rates on Bedrock model-card doc pages (operator-override path);
`zai.glm-4.6` has no AWS-published rate anywhere and is refused an invented rate
(escalated as a lane-removal recommendation).

**The "238 token-priced dimensions" figure in §1 is stale.** Live counts:
`AmazonBedrockFoundationModels` **241**, `AmazonBedrock` **857**,
`AmazonBedrockService` **15** = **1113** token-priced dimensions across the three
files (the parser classifies 1096; the 17 unclassified are all intentional
exclusions — 16 Nova `custom-model`, 1 APO `optimizePrompt`). `AmazonBedrockFoundationModels`
alone carries 241, already more than the old "238" for all files combined.
