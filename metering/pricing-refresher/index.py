# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering pricing refresher — keeps the model pricing catalog current from the
authoritative AWS Price List Bulk API (docs/plans/metering-admin-console/
02-PRICING-INVESTIGATION.md).

Runs on a schedule (default daily). For every Bedrock inference token SKU it
finds, it writes a catalog row:

    pk=PRICING#<model_key>  sk=PUBLISHED
      { input, output, cache_read, tier maps, display_name, provider, service,
        effective_date, price_map_version, source=aws-published, updated_at }

Operator overrides live in SEPARATE rows the admin API owns:

    pk=PRICING#<model_key>  sk=OVERRIDE
      { input, output, note, updated_by, updated_at, source=override }

The debit Lambda resolves a rate as:  OVERRIDE → PUBLISHED → bundled file → unpriced.
So published prices refresh WITHOUT a redeploy, operator overrides are never
clobbered by a refresh (different sk), and a genuinely-unpublished frontier
model (e.g. claude-sonnet-5) stays unpriced until an operator adds an override
or AWS publishes — never a silent $0, never a guess.

Why multiple offer files + shapes: Bedrock token pricing is spread across the
AmazonBedrock (mantle + classic) and AmazonBedrockService (Claude 4/4.5) offer
files under several usage-type shapes — an earlier single-file/single-shape
parser missed ~75 models. See the investigation doc.

Env: TABLE, REGION (offer-file region, default us-east-1).
"""

import datetime
import json
import logging
import os
import re
import time
import urllib.request

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
REGION = os.environ.get("REGION", "us-east-1")
SERVICES = ["AmazonBedrock", "AmazonBedrockService"]
OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{svc}/current/{region}/index.json"

_DIR = r"(?P<direction>input|output|cache-read|cache-write)"
SHAPES = [
    re.compile(rf"^[A-Z0-9]+-(?P<model>.+)-mantle-{_DIR}-tokens-(?P<tier>standard|batch|flex|priority)$"),
    re.compile(rf"^[A-Z0-9]+-(?P<model>.+)-{_DIR}-tokens(?:-(?P<tier>batch|flex|priority))?$"),
    re.compile(rf"^[A-Z0-9]+-(?P<model>.+)-{_DIR}-tokens-cross-region-(?:global|geo)(?:-(?P<tier>batch|flex|priority))?$"),
]

ddb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")


def _metric(name: str, value: float = 1, unit: str = "Count"):
    try:
        cw.put_metric_data(Namespace="Metering", MetricData=[{"MetricName": name, "Value": value, "Unit": unit}])
    except Exception as e:  # noqa: BLE001
        log.warning(f"metric {name} failed: {e}")


def _classify(usagetype: str):
    for shape in SHAPES:
        m = shape.match(usagetype)
        if m:
            gd = m.groupdict()
            return gd["model"], gd["direction"], (gd.get("tier") or "standard")
    return None


def _fetch(svc: str) -> dict:
    url = OFFER_URL.format(svc=svc, region=REGION)
    with urllib.request.urlopen(url, timeout=180) as r:  # noqa: S310 — fixed https AWS URL
        return json.loads(r.read())


def _parse() -> tuple[dict, str]:
    """Return {model_key: {tier: {dir: per_token_usd}, _meta:{...}}}, version."""
    models: dict = {}
    version = "unknown"
    for svc in SERVICES:
        try:
            offer = _fetch(svc)
        except Exception as e:  # noqa: BLE001
            log.warning(f"{svc} offer file unavailable: {e}")
            continue
        version = offer.get("version", version) or version
        for product in offer.get("products", {}).values():
            attrs = product.get("attributes", {})
            cls = _classify(attrs.get("usagetype", ""))
            if not cls:
                continue
            model_key, direction, tier = cls
            terms = offer.get("terms", {}).get("OnDemand", {}).get(product["sku"], {})
            for term in terms.values():
                for dim in term.get("priceDimensions", {}).values():
                    if "token" not in (dim.get("unit") or "").lower():
                        continue
                    per_1k = float(dim.get("pricePerUnit", {}).get("USD", "0"))
                    entry = models.setdefault(model_key, {}).setdefault(tier, {})
                    entry[direction] = per_1k / 1000.0
                    meta = models[model_key].setdefault("_meta", {})
                    meta.setdefault("display_name", attrs.get("model", model_key))
                    meta.setdefault("provider", attrs.get("provider", ""))
                    meta.setdefault("service", svc)
                    meta.setdefault("effective_date", term.get("effectiveDate", ""))
    return models, version


def _write_published(models: dict, version: str) -> int:
    now = int(time.time())
    written = 0
    for model_key, tiers in models.items():
        meta = tiers.get("_meta", {})
        std = tiers.get("standard", {})
        item = {
            "pk": {"S": f"PRICING#{model_key}"},
            "sk": {"S": "PUBLISHED"},
            "model": {"S": model_key},
            "source": {"S": "aws-published"},
            "display_name": {"S": str(meta.get("display_name", model_key))[:128]},
            "provider": {"S": str(meta.get("provider", ""))[:64]},
            "service": {"S": str(meta.get("service", ""))[:64]},
            "effective_date": {"S": str(meta.get("effective_date", ""))},
            "price_map_version": {"S": str(version)},
            "updated_at": {"N": str(now)},
            # store the full tier map as JSON (rates are tiny floats; one attr keeps it simple)
            "tiers": {"S": json.dumps({t: v for t, v in tiers.items() if t != "_meta"})},
            # convenience top-level standard rates for cheap reads
            "input": {"N": str(std.get("input", 0) or 0)},
            "output": {"N": str(std.get("output", 0) or 0)},
        }
        ddb.put_item(TableName=TABLE, Item=item)
        written += 1
    return written


def handler(event, context):
    started = time.time()
    try:
        models, version = _parse()
        if not models:
            _metric("PricingRefreshFailure")
            log.error("no models parsed from any offer file")
            return {"ok": False, "models": 0}
        written = _write_published(models, version)
        # marker row so the admin API / console can show last-refresh time + version
        ddb.put_item(
            TableName=TABLE,
            Item={
                "pk": {"S": "PRICING#_CATALOG"},
                "sk": {"S": "META"},
                "version": {"S": str(version)},
                "model_count": {"N": str(written)},
                "region": {"S": REGION},
                "refreshed_at": {"N": str(int(started))},
                "duration_ms": {"N": str(int((time.time() - started) * 1000))},
            },
        )
        _metric("PricingRefreshModels", written)
        log.info(json.dumps({"ok": True, "models": written, "version": version}))
        return {"ok": True, "models": written, "version": version}
    except Exception as e:  # noqa: BLE001
        _metric("PricingRefreshFailure")
        log.exception("pricing refresh failed")
        raise
