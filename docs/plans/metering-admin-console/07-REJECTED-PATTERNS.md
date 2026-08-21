<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Rejected patterns — what the pricing subsystem deliberately does not do

Companion to [`05-PRICING-SINGLE-SOURCE.md`](05-PRICING-SINGLE-SOURCE.md) and
[`06-GATEWAY-PRICING-COVERAGE.md`](06-GATEWAY-PRICING-COVERAGE.md). Each entry is a
design path that was considered and rejected on evidence, recorded here so a
future change does not relitigate it or "fix" a non-bug. Nothing below is a TODO.

## 1. GetProducts (Price List Query API) fallback

**Rejected — closes nothing here.** The reference implementation adds a
`GetProducts` Query-API fallback for models missing from the bulk offer files.
Our only current gaps (the GPT-5.x/GLM family, measured 2026-08-21) are absent
from the Price List **entirely** — bulk *and* query — so a Query fallback would
price zero of them. The reference repo's own notes document the cost: per-model
API throttling and, in a 15-minute Lambda, silent truncation of large result
sets (you cannot tell an empty result from a truncated one). Revisit only with a
named model the Query API would actually price that the bulk files do not.

## 2. Hand-maintained alias / usage-type-prefix maps

**Rejected — maintenance debt with a silent-wrong-rate failure mode.** The
reference implementation carries `MODEL_ALIASES` and `USAGETYPE_PREFIX` tables;
its own docs call them debt. A stale or wrong hand-entry produces a plausible
but wrong dollar amount with no signal. Our path instead does an AWS-to-AWS
exact join (`bedrock:ListFoundationModels` `modelId`→`modelName` against the
Price List name) plus an operator-editable `PRICING#_ALIAS` binding for the
residue — auditable, no code deploy, and unresolved entries are a visible
`_UNMATCHED` queue rather than a silent guess.

## 3. `skuPrecedence` tier ladder

**Rejected — our grid already keys tiers as axes (see 06/D7).** The reference
implementation ranks overlapping SKUs with a hand-ordered precedence ladder.
Because our catalog stores routing × tier × context × direction as explicit map
axes, distinct tiers never collide — only a genuine same-leaf conflict remains.
For those we keep the **maximum** rate (conservative for quota admission) and
record a `rate_conflict` signal (metric `PricingRateConflict`). Max+signal is
honest without a hand-ranked ladder nobody can audit against AWS.

## 4. Pricing `zai.glm-4.6` from a non-AWS source

**Rejected — prohibited; it would invent a number.** `zai.glm-4.6` is served on
Bedrock but has **no AWS-published rate anywhere**: absent from all three offer
files, from the Bedrock pricing page's Z AI section, from the Z.AI doc model
index, and its direct model-card URL 404s (checked 2026-08-21). Substituting
GLM 4.7 ($0.60/$2.20) or GLM 5 ($1.00/$3.20) would bill a different model's rate.
It stays visible as invokable-unpriced (the `UnpricedGatewayModels` alarm names
it — by design) and is escalated as a lane-removal decision, not silently priced.

## 5. Blocking unpriced admissions

**Rejected here — it changes the enforcement contract.** Refusing to admit a
request for a model with no resolvable rate would make pricing gaps
availability-affecting. The module's posture is availability-first (design D4):
an unpriced model records tokens, prices at $0, and alarms — it does not block.
Flipping that is a deliberate product decision about the enforcement contract,
not a pricing change, and is out of scope for this work.

## 6. "~9% below" vs "10% premium" — the same fact, not a bug to reconcile

The Fable brief ([`../metering-pricing-catalog/00-FABLE-BRIEF.md`](../metering-pricing-catalog/00-FABLE-BRIEF.md))
says Anthropic frontier bills **Global ~9% below** regional; `05` and the spec
say in-region carries a **10% premium** over global. These are the **same
relationship expressed from opposite ends**: if in-region = 1.10 × global, then
global = 1/1.10 ≈ 0.909 × in-region, i.e. ~9.1% below. Concretely, Claude Opus 5
is $5.50 in-region vs $5.00 global per 1M input. **Do not "correct" either
number to match the other** — both are right; changing one to read like the
other would make the doc wrong.
