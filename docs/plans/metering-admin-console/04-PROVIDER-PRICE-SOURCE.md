<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Provider price-list source — filling the frontier gap with real rates

Follow-up to [`03-PRICING-RECONCILIATION.md`](03-PRICING-RECONCILIATION.md).

## The insight (operator, verified correct)

For third-party models, **Bedrock's per-token price equals the model
provider's public list price** — AWS resells Anthropic/OpenAI/etc. at their
published rates. So when AWS's own Price List feed lags a newly-launched model
(e.g. `claude-sonnet-5`, `claude-haiku-4-5`, `gpt-5.6-*`), the provider's price
list is a legitimate authority for that gap.

## What is actually fetchable (probed live, no assertions)

| Source | No-auth? | Machine-readable? | Has our frontier ids + rates? |
|---|---|---|---|
| `api.anthropic.com/v1/models`, `api.openai.com/v1/models` | ❌ 401 (needs key) | — (no prices anyway) | — |
| Anthropic / OpenAI pricing **web pages** | ✅ | ❌ HTML only (brittle scrape) | yes (visually) |
| **LiteLLM `model_prices_and_context_window.json`** | ✅ HTTP 200 | ✅ JSON, 2968 models | ✅ **yes, with real Bedrock-namespaced keys** |

The providers publish **no** structured no-auth price endpoint. The de-facto
machine-readable price list the whole LLM-tooling ecosystem uses is LiteLLM's
JSON (MIT-licensed, community-maintained, updated within days of launches).
Live-confirmed it carries our exact ids under Bedrock keys with current rates:

    anthropic.claude-sonnet-5              $2/M in,  $10/M out   (bedrock_converse)
    anthropic.claude-haiku-4-5-…-v1:0      $1/M in,  $5/M out
    anthropic.claude-opus-4-5-…-v1:0       $5/M in,  $25/M out
    azure/gpt-5.6-sol, gpt-5.6-luna        present

356 Bedrock-namespaced entries carry `input_cost_per_token` / `output_cost_per_token`
(plus cache-read/write). These are *more* accurate than the hand-seeded
estimates in `03` (which guessed Sonnet 5 at $3/$15; the real rate is $2/$10).

## Decision: add a PROVIDER-LIST source tier, AWS still wins

New settle-time precedence (debit Lambda):

    operator override
      → AWS-published (Price List Bulk API — authoritative for the actual bill)
      → provider-list (LiteLLM JSON — real provider rates for the AWS gap)
      → seeded default (config/model-price-overrides.json — last-resort safety net)
      → unpriced

The **pricing refresher** gains a second fetch: after the AWS offer files, it
pulls the LiteLLM JSON and writes `PRICING#<model>/PROVIDER` rows **only for
models AWS did not publish** (AWS always wins where it has a SKU — it is the
source of the invoice). Rows carry `source=provider-list` + `source_ref` +
the upstream key matched, so every rate is traceable.

### Why AWS-published still outranks the provider list

The customer's **invoice** comes from AWS, priced by the AWS Price List. For a
model AWS prices, that number is authoritative even if the provider's list
differs (regional adjustment, promo, marketplace terms). The provider list is
the best available proxy *only* for models AWS hasn't published yet — exactly
the frontier gap. When AWS later publishes the SKU, `aws-published` outranks
`provider-list` automatically and the proxy stops being used (auto-heal, same
property as the seed).

## Integrity / supply-chain (this is finance-critical + aws-samples-bound)

A third-party price feed on a billing path needs guardrails, so:

- **Pinned + checksummed, not floating `main`.** The refresher fetches a
  **pinned commit SHA / release tag** of the LiteLLM file (configurable env),
  not the moving branch — a surprise upstream edit can't silently change
  dollars. Config records the pinned ref.
- **Sanity-bounded.** Any provider rate is rejected if it's ≤ 0 or implausibly
  large (> $1/token); rejects are logged + metric'd, never written.
- **Clearly labeled, never "AWS".** The console shows a distinct
  **"provider list (est.)"** badge with the upstream ref; it reconciles as an
  estimate, never as authoritative AWS pricing (the accuracy ladder already
  separates estimate vs invoice-settled).
- **Off by default is not required** (it only fills gaps, never overrides AWS),
  but the source URL+ref is a stack env var, so an operator who wants a
  different feed (or none) sets it without code changes.
- **Third-party attribution** added to `THIRD-PARTY-LICENSES.md` (LiteLLM is MIT).

Net effect: the frontier models that were $0 → then hand-estimated → now carry
**real provider list prices**, refreshed daily, auto-superseded by AWS when it
catches up, and fully traceable — with the operator override still the top of
the chain for negotiated rates.
