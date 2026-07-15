#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Generate config/model-prices.json from the AWS Price List offer file.

The metering debit Lambda prices usage from this map. Keyed
(model, direction, tier) with per-TOKEN USD rates (the offer file publishes
per-1K-token rates; we divide by 1000 so debits multiply tokens directly).

Two operator-facing realities this script encodes (docs/plans/metering-enforcement):
  * Repricing can be BACKDATED — offer terms carry an effectiveDate days
    before publication. The map stores both dates; ledger rows stamp the map
    version so affected windows can be re-rated.
  * Some catalog models have NO mantle SKUs in the offer file (observed:
    anthropic.claude-*, openai.gpt-5.*). Those are "unpriced" — the debit
    Lambda records tokens, prices at 0, and alarms — until either the SKU
    appears or the operator adds a manual entry under "overrides" (which this
    script preserves across regenerations).

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

OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/{region}/index.json"
USAGE_RE = re.compile(r"^[A-Z0-9]+-(?P<model>.+)-mantle-(?P<direction>input|output|cache-read)-tokens-(?P<tier>[a-z]+)$")
OUT = Path(__file__).resolve().parent.parent / "config" / "model-prices.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    url = OFFER_URL.format(region=args.region)
    print(f"fetching {url} …", file=sys.stderr)
    offer = requests.get(url, timeout=120).json()

    models: dict = {}
    for product in offer.get("products", {}).values():
        attrs = product.get("attributes", {})
        m = USAGE_RE.match(attrs.get("usagetype", ""))
        if not m:
            continue
        sku = product["sku"]
        terms = offer.get("terms", {}).get("OnDemand", {}).get(sku, {})
        for term in terms.values():
            for dim in term.get("priceDimensions", {}).values():
                per_1k = float(dim.get("pricePerUnit", {}).get("USD", "0"))
                entry = models.setdefault(m["model"], {}).setdefault(m["tier"], {})
                entry[m["direction"]] = per_1k / 1000.0
                entry.setdefault("_effective", term.get("effectiveDate", ""))

    existing_overrides = {}
    if OUT.exists():
        try:
            existing_overrides = json.loads(OUT.read_text()).get("overrides", {})
        except (ValueError, OSError):
            pass

    out = {
        "_comment": (
            "Per-TOKEN USD rates for bedrock-mantle usage types, generated from the AWS "
            "Price List offer file — regenerate with scripts/generate-price-map.py. Models "
            "in the live catalog but absent here are UNPRICED (the debit Lambda records "
            "tokens, prices at $0, and raises the UnpricedModel alarm); add them under "
            "'overrides' with operator-entered rates. Repricing can be backdated: compare "
            "'version' + per-entry '_effective' before trusting historical dollars."
        ),
        "region": args.region,
        "version": offer.get("version", "unknown"),
        "publication_date": offer.get("publicationDate", ""),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": dict(sorted(models.items())),
        "overrides": existing_overrides,
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {OUT} — {len(models)} priced models, {len(existing_overrides)} overrides kept", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
