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

# Provider-list price source (docs/plans/metering-admin-console/04-PROVIDER-PRICE-SOURCE.md):
# LiteLLM's community price JSON — the de-facto machine-readable list of provider
# public rates, keyed by Bedrock-namespaced model ids. Fills the gap for models
# AWS hasn't published a SKU for yet (frontier Claude/GPT versions). PINNED to a
# commit SHA (not floating main) so an upstream edit can't silently move dollars;
# override via env to re-pin or disable (set PROVIDER_PRICE_URL="").
PROVIDER_PRICE_REF = os.environ.get("PROVIDER_PRICE_REF", "ba70189e328a5376700e9535d0629118857395e7")
PROVIDER_PRICE_URL = os.environ.get(
    "PROVIDER_PRICE_URL",
    f"https://raw.githubusercontent.com/BerriAI/litellm/{PROVIDER_PRICE_REF}/model_prices_and_context_window.json",
)
# reject implausible rates before they touch a billing path (per-token USD)
_MAX_SANE_RATE = 1.0


def _load_seed_overrides() -> dict:
    """Curated default overrides for frontier model ids AWS hasn't published a
    SKU for yet (config/model-price-overrides.json, bundled into this asset).
    Seeded as PRICING#<model>/DEFAULT so a real AWS-published rate or an operator
    override both outrank them — auto-healing (03-PRICING-RECONCILIATION.md)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "model-price-overrides.json")) as f:
            return json.load(f).get("overrides", {})
    except (OSError, ValueError):
        return {}

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


def _seed_defaults(published_models: set) -> int:
    """Write PRICING#<model>/DEFAULT rows for curated frontier models AWS hasn't
    published a SKU for. Skip any model AWS DID publish (aws-published wins and
    the seed would be dead weight). Operator OVERRIDE rows are a different sk and
    always win, so seeding never clobbers an operator's rate."""
    seeds = _load_seed_overrides()
    now = int(time.time())
    written = 0
    for model_key, spec in seeds.items():
        if model_key in published_models:
            continue  # AWS now prices it — don't seed a stale estimate
        item = {
            "pk": {"S": f"PRICING#{model_key}"},
            "sk": {"S": "DEFAULT"},
            "model": {"S": model_key},
            "source": {"S": "default-override"},
            "updated_at": {"N": str(now)},
        }
        if spec.get("input") is not None:
            item["input"] = {"N": str(float(spec["input"]))}
        if spec.get("output") is not None:
            item["output"] = {"N": str(float(spec["output"]))}
        if spec.get("note"):
            item["note"] = {"S": str(spec["note"])[:500]}
        if spec.get("source_ref"):
            item["source_ref"] = {"S": str(spec["source_ref"])[:256]}
        ddb.put_item(TableName=TABLE, Item=item)
        written += 1
    return written


_VER_SUFFIX = re.compile(r"(-\d{8})?(-v\d+:\d+)?(@\d{8})?$")


def _normalize_provider_keys(k: str) -> list:
    """Map a LiteLLM key to the model id(s) our debit/interceptor settle under.
    They use ids like 'anthropic.claude-sonnet-5' / 'openai.gpt-5.6-sol' (gateway
    'bedrock/' or 'bedrock_mantle/' prefix and region qualifiers stripped).
    LiteLLM keys take many shapes: 'anthropic.claude-sonnet-5',
    'bedrock_mantle/openai.gpt-5.6-sol', 'us.anthropic.claude-…',
    'anthropic.claude-haiku-4-5-20251001-v1:0'. Returns the canonical id AND a
    version-suffix-trimmed alias (so a dated/versioned SKU also matches our bare
    id); empty list if it isn't a Bedrock-family model id."""
    k = k.strip()
    for pfx in ("bedrock_mantle/", "bedrock_converse/", "bedrock/"):
        if k.startswith(pfx):
            k = k[len(pfx):]
            break
    # drop leading region scopes: us. eu. apac. global. <region>.
    parts = k.split(".")
    if len(parts) > 2 and parts[0] in ("us", "eu", "apac", "global", "ap", "ca", "sa"):
        k = ".".join(parts[1:])
    if "." not in k:  # not a provider.model id (e.g. bare 'claude-…' or 'azure/…')
        return []
    ids = {k}
    trimmed = _VER_SUFFIX.sub("", k)
    if trimmed and trimmed != k:
        ids.add(trimmed)
    return list(ids)


def _fetch_provider_rates() -> dict:
    """Return {model_id: {'input':x,'output':y,'cache-read':z,'_key':litellm_key}}
    for Bedrock-namespaced models with sane token costs. Never raises."""
    if not PROVIDER_PRICE_URL:
        return {}
    out: dict = {}
    try:
        with urllib.request.urlopen(PROVIDER_PRICE_URL, timeout=60) as r:  # noqa: S310 — pinned https URL
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        log.warning(f"provider price list unavailable: {e}")
        _metric("ProviderPriceFetchFailure")
        return {}
    def sane(v):
        return isinstance(v, (int, float)) and 0 < float(v) <= _MAX_SANE_RATE

    for key, spec in data.items():
        if not isinstance(spec, dict):
            continue
        prov = str(spec.get("litellm_provider", ""))
        if "bedrock" not in prov:  # only Bedrock-served rows apply to our bill
            continue
        model_ids = _normalize_provider_keys(key)
        if not model_ids:
            continue
        entry = {}
        if sane(spec.get("input_cost_per_token")):
            entry["input"] = float(spec["input_cost_per_token"])
        if sane(spec.get("output_cost_per_token")):
            entry["output"] = float(spec["output_cost_per_token"])
        if sane(spec.get("cache_read_input_token_cost")):
            entry["cache-read"] = float(spec["cache_read_input_token_cost"])
        if not entry:
            continue
        for model_id in model_ids:
            e = dict(entry)
            e["_key"] = key
            # an EXACT key match beats a version-trimmed alias; among same-kind,
            # the shortest (most canonical, region-unqualified) key wins.
            e["_exact"] = model_id == key
            prev = out.get(model_id)
            if (not prev
                    or (e["_exact"] and not prev.get("_exact"))
                    or (e["_exact"] == prev.get("_exact") and len(key) < len(prev.get("_key", "")))):
                out[model_id] = e
    return out


def _write_provider(provider_rates: dict, published_models: set) -> int:
    """Write PRICING#<model>/PROVIDER rows ONLY for models AWS did not publish —
    AWS is authoritative for the actual invoice where it has a SKU."""
    now = int(time.time())
    written = 0
    for model_id, entry in provider_rates.items():
        if model_id in published_models:
            continue  # AWS prices it — provider list is a proxy only for the gap
        item = {
            "pk": {"S": f"PRICING#{model_id}"},
            "sk": {"S": "PROVIDER"},
            "model": {"S": model_id},
            "source": {"S": "provider-list"},
            "source_ref": {"S": f"litellm@{PROVIDER_PRICE_REF[:12]}:{entry.get('_key','')}"[:256]},
            "updated_at": {"N": str(now)},
        }
        tiers = {"standard": {k: v for k, v in entry.items() if not k.startswith("_")}}
        item["tiers"] = {"S": json.dumps(tiers)}
        if "input" in entry:
            item["input"] = {"N": str(entry["input"])}
        if "output" in entry:
            item["output"] = {"N": str(entry["output"])}
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
        provider_written = _write_provider(_fetch_provider_rates(), set(models.keys()))
        seeded = _seed_defaults(set(models.keys()))
        # marker row so the admin API / console can show last-refresh time + version
        ddb.put_item(
            TableName=TABLE,
            Item={
                "pk": {"S": "PRICING#_CATALOG"},
                "sk": {"S": "META"},
                "version": {"S": str(version)},
                "model_count": {"N": str(written)},
                "provider_count": {"N": str(provider_written)},
                "seeded_defaults": {"N": str(seeded)},
                "provider_ref": {"S": PROVIDER_PRICE_REF[:40]},
                "region": {"S": REGION},
                "refreshed_at": {"N": str(int(started))},
                "duration_ms": {"N": str(int((time.time() - started) * 1000))},
            },
        )
        _metric("PricingRefreshModels", written)
        log.info(json.dumps({"ok": True, "models": written, "provider": provider_written, "seeded": seeded, "version": version}))
        return {"ok": True, "models": written, "provider": provider_written, "seeded": seeded, "version": version}
    except Exception as e:  # noqa: BLE001
        _metric("PricingRefreshFailure")
        log.exception("pricing refresh failed")
        raise
