<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Gateway pricing coverage — join the gateway catalog to the price catalog

Builds on [`05-PRICING-SINGLE-SOURCE.md`](05-PRICING-SINGLE-SOURCE.md) (the
single-source rate architecture, unchanged here). This design adds the missing
**join** between what the gateway serves and what the pricing store prices, makes
unpriced-but-invokable a first-class alarmed condition, simplifies the pricing
package against live-data evidence, and reconciles the docs.

Design date: 2026-08-21. All numbers below were measured live on 2026-08-20/21
against account `TEST_ACCOUNT_ID`, us-east-1, table `open-webui-metering`
(evidence: session artifacts; re-derivable via `scripts/diagnose-model-pricing.py`
after this change).

## Measured baseline (fresh, not inherited)

| Lane | Total | Priced | Unpriced |
|---|---|---|---|
| chat_completions | 46 | 41 | 5 |
| responses | 13 | 6 | 7 |
| messages | 5 | 5 | 0 |

- **8 distinct unpriced gateway models**, all `row-absent`:
  `openai.gpt-5.4`, `openai.gpt-5.4-2026-03-05`, `openai.gpt-5.5`,
  `openai.gpt-5.5-2026-04-23`, `openai.gpt-5.6-luna`, `openai.gpt-5.6-sol`,
  `openai.gpt-5.6-terra`, `zai.glm-4.6`.
- **All 8 are absent from all three live offer files** (`AmazonBedrockFoundationModels`,
  `AmazonBedrock`, `AmazonBedrockService`; positive control: `openai.gpt-oss-*`
  matched 16 SKUs each in the same search). This is an **AWS publishing gap**, not
  a parser or join defect. A 4th service code (`AmazonBedrockAgentCore`) exists but
  carries no model-token SKUs.
- 7 of the 8 have AWS-published rates on Bedrock **model-card doc pages** (not the
  Price List): the operator-override path applies. `zai.glm-4.6` has **no AWS-published
  rate anywhere** (pricing page, Z.AI doc index, direct model-card URL 404).
- Parser drops: 17/1113 token dimensions, **all intentional** (16 `custom-model`,
  1 APO `optimizePrompt`). The drop *mechanism* is still silent for unknown
  qualifiers — that is the defect fixed here.
- Unmatched: 49 entries, all legacy display-name products with no
  control-plane twin (Claude 2.x-era). They hold `PricingUnmatchedAlarm` in ALARM.
- Live row shapes: 164 `PUBLISHED` rows + 1 meta (`PRICING#_CATALOG`/`META`,
  generation 20) + 1 alias. **Zero rows** carry a non-null `tiers` value or the
  flat `input`/`output` legacy shape — both compatibility branches are provably
  dead. Rows carry vestigial `"tiers": null, "input": null, "output": null`.
- Prior doc claims re-derived: "60% coverage (28/47)" — no longer true (85%+ and
  lane-dependent). "8 genuinely unpriced" — the *count* coincidentally matches
  today, but the set is the GPT-5.x/GLM mantle family with per-model offer-file
  evidence, not the inherited list. "238 token-priced dimensions" — stale (live:
  241 + 857 + 15 = 1113).

## Decisions

**D1 — The join lives in the pricing-refresher.** At the end of every refresh run
(24h schedule + admin-API `POST /pricing/refresh`), the refresher fetches the
gateway catalog (same SigV4 GET as `gateway/refresher/probe_core.fetch_catalog()`,
`status == "available"`), reads the served `MODEL_CAPS` from the interceptor
Lambda's configuration (same pattern as `gateway/refresher/index.py::_current_caps`),
resolves every model in the **union** through the production resolver, and writes
one coverage item: `pk=PRICING#_COVERAGE, sk=META` — per-model
`{id, lanes, listed, catalog_available, priced, source, reason}` plus counts and
`computed_at`. Rationale: the refresher already owns the pricing write path, the
schedule, and the alarm surface; DynamoDB stays the one store. Rejected: computing
in the admin API only (not alarmable when idle), a new Lambda (ceremony), the
gateway model-refresher (metering must stay optional and decoupled).

**D2 — Coverage universe is the union of served lanes and the live catalog.** The
metering interceptor estimates and admits any model id it sees, so a
catalog-available model that is not in any lane is still invokable by a crafted
request (`listed: false`). A lane model no longer catalog-available is stale caps
(`catalog_available: false`) — visible but not a quota risk. The alarmed count is
**invokable-unpriced**: `catalog_available AND NOT priced`.

**D3 — New metric + alarm `UnpricedGatewayModels`** (gauge emitted by the
refresher; alarm at >= 1). This is the proactive, named condition. The reactive
settle-path `UnpricedModel` metric stays. The admission path additionally emits
`UnpricedAdmission` when an estimate resolves no rate. **Admission behavior does
not change** (availability-first posture preserved; unpriced admission remains
allowed but becomes visible and, post-overrides, empty).

**D4 — Publishing-gap models get operator OVERRIDE rows with provenance.** The
override write path gains an optional `note` (provenance string). The 7
GPT-5.x models are overridden with the AWS model-card rates (in-region 272K-context
standard tier for input/output/cache axes as published; long-context published
tiers recorded in the note). Dated snapshot ids (`-2026-03-05`, `-2026-04-23`)
get their **own** override rows citing the base model-card page (AWS publishes no
per-snapshot rate; no alias guessing). `zai.glm-4.6` has no AWS-published rate:
**overriding it would invent a number — refused.** *Decision recorded
2026-08-21:* the operator chose availability-always — every actually-invokable
gateway model stays available; unpriceable ones are flagged (this alarm), and a
manual override with a documented note is the optional resolution. Lane removal
and a capability denylist were considered and rejected as overcomplication.

**D5 — One usage-type grammar with an explicit exclusion list and a loud
unknown-bucket.** `offers.py`'s four grammars (`_classify_snake`, `_classify_camel`,
`_classify_mantle`, `_LEGACY_DIR_RE`) collapse into one canonical
tokenizer (lowercase, region-prefix strip, split on `-`/`_`/camel boundaries) and
**one** qualifier vocabulary spelled once. Known-and-deliberate exclusions
(`custom-model`, APO `optimizePrompt`) are matched by name and recorded as
`excluded`. Anything else unclassifiable is recorded as `unclassified` with the
verbatim usagetype, persisted on the refresh meta, surfaced in the admin API, and
emitted as `PricingDimensionUnclassified` (alarm >= 1). A `None`-return silent
drop is no longer possible.

**D6 — Resolver sheds its dead compatibility branches.** One `_grid(row)`
normalizer shared by override and published paths; the `tiers`->grid lift and the
flat `input`/`output` tolerance are deleted (zero live rows use them; the current
writer never produces them). The refresher stops writing the vestigial null
fields. The 36-lookup fallback matrix with three boolean flags becomes a single
generator that yields an explicit, ordered candidate-key chain (same effective
order — verified by golden tests), so the chain is readable and testable as data.

**D7 — Deterministic merge.** `_merge_rate`'s `cell.setdefault()`
(first-wins-by-file-order — the exact fragility the reference implementation
engineered away) becomes: identical duplicate values merge silently; conflicting
values keep the **maximum** (conservative for quota admission) and record a
`rate_conflict` entry (surfaced like `unclassified`). Rejected: their
`skuPrecedence()` tier ladder — our grid already keys tiers as axes, so only
true same-leaf conflicts remain, and max+signal is honest without a hand-ranked
SKU ladder.

**D8 — Unmatched queue is classified, and the alarm tracks only actionable
entries.** `no-match` (no current control-plane twin) = historical: kept,
counted, collapsed by default in the console, **not** alarmed. `ambiguous`
(multiple candidate twins — refresher refused to guess) = actionable: alarmed
(`PricingUnmatchedActionable` >= 1, rewiring the existing alarm). Today's 49 are
all `no-match`, so the alarm goes green on deploy — a live validation signal.

**D9 — Scans become targeted reads.** The catalog meta item already carries the
model list; `_gc` diffs the prior list against the new run (legacy full Scan only
if meta is absent), and the admin API's `_pricing_rows` reads the meta list +
`BatchGetItem` instead of a capped 5000-item Scan.

**D10 — One cache TTL.** The 60s (estimate) / 300s (settle) split for the same
rows becomes a single shared 300s TTL constant in `metering/pricing`.

**D11 — Console renders server-computed truth.** The admin API `_catalog`
response merges coverage (`gateway: {available, listed, lanes}`) and includes the
resolver-computed `effective` grid per model; `PricingPage.tsx` drops `gridStd()`
(its local resolver re-implementation) and renders the served grid. `_pricing_meta`
serves `model_id_pattern` (from `identity.MODEL_ID_RE`); the BindModal literal is
removed. New coverage summary strip: *invokable & priced / invokable & UNPRICED /
listed-but-unavailable*, with the unpriced set called out.

**D12 — Two operator scripts, both stdlib, both read-only.**
`scripts/diagnose-model-pricing.py` (--model/--all): runs the production parse +
join + resolve against live offer files and prints per-model outcomes, so "why is
this model unpriced" is one command. `scripts/pricing-rate-diff.py`: per-model,
per-leaf effective-rate diff between two snapshots (live table vs local compute,
or two table dumps) — the pre-deploy gate. A rate diff, not a gap count, is what
catches a matcher that matches more by being less exact.

## Rejected (recorded so the next run does not relitigate)

- **GetProducts Query API fallback** — rejected for now even as a gap-closer: the
  only current gaps are absent from the Price List entirely, so it closes nothing
  here; the reference implementation documents throttling + 15-minute-Lambda
  silent truncation. Revisit only with a named model it would actually price.
- **Hand-maintained alias/prefix maps** (reference `MODEL_ALIASES` /
  `USAGETYPE_PREFIX`) — their own docs call them maintenance debt; our exact-join
  plus operator `_ALIAS` binding stays.
- **skuPrecedence tier ladder** — see D7.
- **Pricing zai.glm-4.6 from any non-AWS source** — prohibited (no invented rates).
- **Blocking unpriced admissions** — changes the availability-first enforcement
  contract; needs an explicit product decision, not this change.

## Validation plan

Golden-rate tests are written **before** the refactor (capture current resolver
outputs for the full fixture matrix; assert byte-identical after D5–D7, except
deltas explicitly justified). New tests: the coverage join (caps x catalog x
rows), the gpt-5.6 publishing-gap regression (real trimmed offer fixtures ->
named condition, never silent), unclassified-dimension signal, merge determinism,
unmatched classification, scan-replacement equivalence. Gates:
`python -m pytest metering/tests -q`; `npx tsc --noEmit` + `npx cdk synth --quiet`
(infra/); `npm run build` (console/); `scripts/pricing-rate-diff.py` against the
live table pre/post deploy (expect: zero unintended deltas; additions = the 7
overrides); live: refresher run, `GET /pricing/coverage` showing
`invokable_unpriced == [zai.glm-4.6]` pre-decision (or `[]` if removal approved),
`PricingUnmatchedActionable` alarm OK, console rendering coverage.
