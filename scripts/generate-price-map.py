#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Generate config/model-prices.json from the AWS Price List Bulk API.

The metering debit Lambda prices usage from this map. Keyed
(model, direction, tier) with per-TOKEN USD rates (the offer files publish
per-1K-token rates; we divide by 1000 so debits multiply tokens directly).

WHY THIS READS MULTIPLE OFFER FILES AND MULTIPLE USAGE-TYPE SHAPES
------------------------------------------------------------------
Bedrock token pricing is spread across several Price List "services" and the
usage types take several shapes (see docs/plans/metering-admin-console/
02-PRICING-INVESTIGATION.md, grounded in live API responses):

  * AmazonBedrock         — both the newer "mantle" serverless tier
      (USE1-{model}-mantle-input-tokens-standard, the shape our bedrock-mantle
      gateway bills under) AND the classic on-demand shape
      (USE1-Claude3Sonnet-input-tokens) used by Claude 2/3, Llama, Gemma,
      DeepSeek-R1, Kimi, GPT-OSS-Safeguard, …
  * AmazonBedrockService  — newer Claude 4 / 4.5 on-demand token pricing
      (USE1-Claude4Sonnet-input-tokens-cross-region-global)

An earlier version read only AmazonBedrock and matched only the `-mantle-`
shape, so ~75 models (every Claude, Llama, Gemma, …) fell through as
"unpriced" / $0. That was our bug, not an AWS gap — every published model has
retrievable pricing here.

STILL-UNPRICED MODELS: a few frontier ids (e.g. anthropic.claude-sonnet-5,
openai.gpt-5.6-*) are genuinely absent from every offer file (pre-GA). Those
stay "unpriced" until AWS publishes OR an operator adds a rate under
"overrides" (preserved across regenerations; also editable in the admin
console's Pricing Catalog).

Usage:
  uv run --no-project --with requests python scripts/generate-price-map.py [--region us-east-1]
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import requests

INDEX_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json"
OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{svc}/current/{region}/index.json"
OUT = Path(__file__).resolve().parent.parent / "config" / "model-prices.json"

# Bedrock services that publish per-token inference pricing. (FoundationModels
# is marketplace-shape MP: units / provisioned throughput, not per-model token
# rates; AgentCore is runtime pricing — neither carries on-demand token SKUs we
# meter, so they're intentionally excluded.)
SERVICES = ["AmazonBedrock", "AmazonBedrockService"]

# Direction + tier extraction from the many usage-type shapes. We normalize all
# of them to (direction ∈ input|output|cache-read, tier ∈ standard|batch|flex|priority).
# mantle:   USE1-{model}-mantle-input-tokens-standard
# classic:  USE1-{model}-input-tokens[-batch]
# service:  USE1-{model}-input-tokens-cross-region-global[-batch]
_DIR = r"(?P<direction>input|output|cache-read|cache-write)"
SHAPES = [
    re.compile(rf"^[A-Z0-9]+-(?P<model>.+)-mantle-{_DIR}-tokens-(?P<tier>standard|batch|flex|priority)$"),
    re.compile(rf"^[A-Z0-9]+-(?P<model>.+)-{_DIR}-tokens(?:-(?P<tier>batch|flex|priority))?$"),
    re.compile(rf"^[A-Z0-9]+-(?P<model>.+)-{_DIR}-tokens-cross-region-(?:global|geo)(?:-(?P<tier>batch|flex|priority))?$"),
]


def _classify(usagetype: str):
    for shape in SHAPES:
        m = shape.match(usagetype)
        if m:
            gd = m.groupdict()
            return gd["model"], gd["direction"], (gd.get("tier") or "standard")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    # models[usage_key] = {tier: {direction: per_token_usd, _effective, _model_name,
    #                             _provider, _service, _shape}}
    models: dict = {}
    versions: dict = {}
    pub_dates: dict = {}

    for svc in SERVICES:
        url = OFFER_URL.format(svc=svc, region=args.region)
        print(f"fetching {url} …", file=sys.stderr)
        try:
            offer = requests.get(url, timeout=180).json()
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: {svc} unavailable in {args.region}: {e}", file=sys.stderr)
            continue
        versions[svc] = offer.get("version", "unknown")
        pub_dates[svc] = offer.get("publicationDate", "")
        matched = 0
        for product in offer.get("products", {}).values():
            attrs = product.get("attributes", {})
            usagetype = attrs.get("usagetype", "")
            cls = _classify(usagetype)
            if not cls:
                continue
            model_key, direction, tier = cls
            sku = product["sku"]
            terms = offer.get("terms", {}).get("OnDemand", {}).get(sku, {})
            for term in terms.values():
                for dim in term.get("priceDimensions", {}).values():
                    unit = (dim.get("unit") or "").lower()
                    # only per-1K-token dimensions (skip request/image/etc.)
                    if "token" not in unit:
                        continue
                    per_1k = float(dim.get("pricePerUnit", {}).get("USD", "0"))
                    entry = models.setdefault(model_key, {}).setdefault(tier, {})
                    entry[direction] = per_1k / 1000.0
                    entry.setdefault("_effective", term.get("effectiveDate", ""))
                    meta = models[model_key].setdefault("_meta", {})
                    meta.setdefault("display_name", attrs.get("model", model_key))
                    meta.setdefault("provider", attrs.get("provider", ""))
                    meta.setdefault("service", svc)
                    matched += 1
        print(f"  {svc}: matched {matched} token price dimensions", file=sys.stderr)

    existing_overrides = {}
    if OUT.exists():
        try:
            existing_overrides = json.loads(OUT.read_text()).get("overrides", {})
        except (ValueError, OSError):
            pass

    out = {
        "_comment": (
            "Per-TOKEN USD rates for Bedrock inference, generated from the AWS Price List "
            "Bulk API (all Bedrock offer files, all on-demand token usage-type shapes) — "
            "regenerate with scripts/generate-price-map.py. Models in the live catalog but "
            "absent here are UNPRICED (debit records tokens, prices at $0, raises the "
            "UnpricedModel alarm); add operator rates under 'overrides' (also editable in "
            "the admin console Pricing Catalog). Repricing can be backdated: compare "
            "'version' + per-entry '_effective' before trusting historical dollars."
        ),
        "region": args.region,
        "version": max(versions.values(), default="unknown"),
        "source_versions": versions,
        "publication_dates": pub_dates,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": dict(sorted(models.items())),
        "overrides": existing_overrides,
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(
        f"wrote {OUT} — {len(models)} priced models, {len(existing_overrides)} overrides kept",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
