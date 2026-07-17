<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Pricing accuracy investigation — why models showed $0, and the fix

**Question raised:** "Every Bedrock model has publicly-available pricing. Why don't
we have it? We shouldn't guess or set $0 when unsure." Correct on both counts.
Everything below is grounded in **live AWS Price List Bulk API responses**
(the public offer files under `https://pricing.us-east-1.amazonaws.com/...`),
not the design doc's earlier claim.

## Root cause — two distinct causes, both proven live

### Cause 1 — our generator was too narrow (a real bug; fixable now)

`scripts/generate-price-map.py` had two limiting assumptions:

1. **It read only ONE of four Bedrock offer files.** The Price List service index
   (`/offers/v1.0/aws/index.json`) lists **four** Bedrock services, each with its
   own offer file:
   - `AmazonBedrock` — the file we read (1013 products)
   - `AmazonBedrockService` — **newer Claude 4 / 4.5 Sonnet on-demand token pricing lives here**
   - `AmazonBedrockFoundationModels` — marketplace-shape (`MP:` units), provisioned throughput, customization
   - `AmazonBedrockAgentCore` — AgentCore runtime pricing
2. **It matched only the `-mantle-` usage-type shape.** Regex
   `^…-(model)-mantle-(input|output|cache-read)-tokens-(tier)$`. But token pricing
   in the offer files takes **multiple shapes**:
   - `USE1-{model}-mantle-input-tokens-standard` — the *newer serverless "mantle" tier* (41 models: qwen, deepseek, gemma, minimax, kimi, gpt-oss, mistral, nvidia, …). **These are what our `bedrock-mantle` gateway bills under → they priced correctly.**
   - `USE1-Claude3Sonnet-input-tokens` — the **classic** on-demand shape (Claude 2/3, Llama 3.x, Gemma-3, DeepSeek-R1, Kimi-K2, GPT-OSS-Safeguard …). **75 distinct models** the generator silently skipped.
   - `USE1-Claude4Sonnet-input-tokens-cross-region-global` — Claude 4/4.5 in `AmazonBedrockService`.

**Proof (live, us-east-1):** real per-1K on-demand token rates pulled
programmatically — Claude 3 Sonnet `$0.003` in, Claude 3 Haiku `$0.00025` in,
Claude 4 Sonnet `$0.003` in / `$0.015` out. Pricing is 100% retrievable; we
weren't parsing it.

### Cause 2 — a residual few models are genuinely unpublished (need overrides)

Our **exact deployed model ids** — `anthropic.claude-sonnet-5`,
`anthropic.claude-opus-4-7`, `openai.gpt-5.6-sol`, `openai.gpt-5.6-luna` — are
**absent from all four offer files** (no `sonnet-5` / `gpt-5` family entry
anywhere, live-checked). These are pre-GA / frontier versions AWS has not yet
published mantle SKUs for. No parser can price them; they legitimately require
an **operator-entered rate** until AWS publishes — the exact "don't guess, don't
silently $0" case. This is what the pricing catalog's override lane is for.

## The fix (two parts)

1. **Widen the generator** (`generate-price-map.py`): read all Bedrock offer
   files, and match all on-demand token usage-type shapes (mantle + classic +
   cross-region), normalizing to canonical model ids and per-token USD. This
   alone prices the ~75 previously-missed models.
2. **Model Pricing Catalog in the admin console** (this is the durable answer):
   - **Default = AWS published pricing**, kept current by a scheduled refresh
     Lambda that re-parses the Price List Bulk API (verified: AWS's recommended
     programmatic source — `docs.aws.amazon.com/.../using-the-aws-price-list-bulk-api.html`).
   - **Per-model operator overrides** editable in the UI, for (a) genuinely
     unpublished models like our GPT-5.6 / Claude-5, (b) negotiated/committed
     rates, (c) a correction pending the next refresh.
   - Precedence at settle: **operator override → published rate → unpriced
     (record tokens, `usd_estimate` for display, `UnpricedModel` alarm)**. Never
     a silent $0, never a guess.
   - Drift is acceptable *with refresh cycles* (repricing can be backdated — the
     ledger stamps `price_map_version` so affected windows can be re-rated).

## Why a catalog, not just a wider generator

The generator runs at deploy/build time and covers *published* models. The
catalog closes the loop the platform's accuracy hinges on:
- **Currency:** scheduled refresh means a price change flows in without a
  redeploy (drift bounded by the refresh cadence, and dated so it's auditable).
- **Coverage of the unpublishable:** frontier models get an explicit,
  audited operator rate instead of $0.
- **Transparency:** an admin can *see* every model's rate, its source
  (AWS-published vs override), and its effective date — the thing finance needs.
