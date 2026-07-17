<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Pricing reconciliation — the honest final answer on the $0 models

Follow-up to [`02-PRICING-INVESTIGATION.md`](02-PRICING-INVESTIGATION.md). The
first investigation fixed the generator (41→106 models) but a few headline
models still settled at $0. This documents *exactly why*, verified against
**three independent AWS sources**, and what we ship to fix it.

## What actually settles at $0 (live table scan, source=FILTER = real OWUI traffic)

    anthropic.claude-sonnet-5     ← frontier
    anthropic.claude-opus-4-7     ← frontier
    anthropic.claude-haiku-4-5    ← frontier
    openai.gpt-5.6-sol / -luna    ← frontier

These arrive through the metering filter from the Open WebUI deployment (they
are the *fleet's* served model ids). `openai.gpt-oss-*` — the OpenAI models
this sample's own gateway actually serves — **are** priced (mantle SKUs).

## What AWS actually publishes (verified in three places, 2026-07)

Cross-checked the same question against every AWS pricing source:

1. **Price List Bulk API** offer files (`AmazonBedrock`, `AmazonBedrockService`,
   `AmazonBedrockFoundationModels`, `AmazonBedrockAgentCore`).
2. The **pricing webpage** `https://aws.amazon.com/bedrock/pricing/` prose.
3. The pricing page's own **calculator data feed**
   (`b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrock/USD/current/bedrock.json`).

All three agree. The newest Anthropic models AWS publishes pricing for are:

    Claude 2.0, 2.1, Instant, 3 Haiku, 3 Sonnet, Sonnet 4, Sonnet 4.5

There is **no** `Claude Sonnet 5`, `Opus 4.7`, `Haiku 4.5`, or `GPT-5.6` in any
AWS pricing source. They are **genuinely unpublished** — the deployment is
running model versions ahead of AWS's published price list. This is NOT a
parser bug (the generator now catches everything AWS publishes) and NOT a
name-mapping bug (there is no differently-named entry to map to — the versions
simply are not there yet).

> Correcting my earlier hand-wave: the claim "every Bedrock model has published
> pricing" is true for every model AWS has *released pricing for*, but the
> pricing page and API lag the newest frontier/preview versions by design.
> When those versions get official SKUs, the daily refresher picks them up
> automatically and the override becomes redundant.

## The fix: seeded default overrides for known frontier models

Leaving well-known models at $0 until an operator hand-types a rate is a poor
default. So we ship a small, **clearly-sourced** set of default price overrides
for the frontier models the fleet runs, in `config/model-price-overrides.json`.
The pricing refresher seeds any of these that are (a) not already an operator
override and (b) still unpublished by AWS — marking them `source=default-override`
with the rate's provenance. Precedence is unchanged:

    operator override → AWS-published → seeded default-override → bundled file → unpriced

Properties:
- **Not $0, not a silent guess:** each seeded rate cites its public source
  (Anthropic / OpenAI list price for the nearest equivalent tier) and is
  surfaced in the console with a distinct "default (est.)" badge + the note.
- **Auto-heals:** the moment AWS publishes a real SKU, `aws-published` outranks
  the seed and the estimate stops being used.
- **Operator-editable:** an admin override still wins over the seed, and the
  seed file is documented so operators can correct a rate the day a model ships.
- **Honest to finance:** these rows reconcile as *estimates* (the module's
  accuracy ladder already distinguishes estimate vs invoice-settled), never as
  authoritative AWS pricing.

Rates are approximations of published Anthropic/OpenAI list prices for the
nearest released tier, chosen conservatively; operators MUST review them
against their negotiated Bedrock rates before trusting the dollars. The point
is "a defensible non-zero estimate with a paper trail," not "the exact invoice."
