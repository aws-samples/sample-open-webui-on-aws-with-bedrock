#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Build a comprehensive Amazon Bedrock model pricing catalog CSV from the AWS
Price List Bulk API offer files.

NOTE: this is an operator/finance AUDIT EXPORT, not a runtime dependency.
The deployed pricing path never reads its output: the metering
pricing-refresher Lambda parses the same offer files directly into DynamoDB
(.kiro/specs/metering-pricing-single-source/design.md). Use this script to
eyeball or hand to finance what AWS publishes, including dimensions the
runtime deliberately ignores (commitments, images, AgentCore).

Why four service codes
----------------------
Bedrock pricing is NOT all under the `AmazonBedrock` service code. It is split:

  AmazonBedrock                  First-party (Nova/Titan) plus open-weight models
                                 (Llama, Mistral, Qwen, DeepSeek, gpt-oss,
                                 openai.gpt-5.x, xai.grok-*) and platform features.
                                 Model name lives in the `model` attribute.
                                 Token rates are published per 1K tokens.

  AmazonBedrockFoundationModels  Third-party models sold through AWS Marketplace
                                 (Anthropic Claude, Cohere, AI21, Stability,
                                 TwelveLabs, Writer). There is NO `model`
                                 attribute - the model name is carried in
                                 `servicename` as "<Model> (Amazon Bedrock Edition)".
                                 Token rates are published per 1M tokens.

  AmazonBedrockService           Reserved throughput commitments and cross-region
                                 (global/geo) inference rates.

  AmazonBedrockAgentCore         AgentCore platform runtime, not per-model token
                                 pricing. Excluded unless --include-agentcore.

Missing any of the first three yields an incomplete catalog. In particular, all
modern Anthropic Claude pricing is in AmazonBedrockFoundationModels, and it is
keyed by `servicename`, not `model`.

Website parity
--------------
https://aws.amazon.com/bedrock/pricing/ renders from
  https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/<slug>/USD/current/<slug>.json
Those manifests contain only {opaque hash -> rateCode, price}; the human-readable
labels are baked into the page HTML. Every rateCode resolves back to a Price List
offer file rate. `--verify-website` proves that parity (100% match, 0 price deltas
when this script was written), so the Price List Bulk API is the complete source.

Usage:
  python scripts/fetch-bedrock-pricing.py
  python scripts/fetch-bedrock-pricing.py --tokens-only
  python scripts/fetch-bedrock-pricing.py --verify-website
  python scripts/fetch-bedrock-pricing.py --out config/bedrock-pricing-catalog.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime
import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

BULK_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"
WEB_BASE = "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps"

MODEL_SERVICE_CODES = [
    "AmazonBedrock",
    "AmazonBedrockFoundationModels",
    "AmazonBedrockService",
]
AGENTCORE_SERVICE_CODE = "AmazonBedrockAgentCore"

WEB_SLUGS = {
    "AmazonBedrock": "bedrock",
    "AmazonBedrockFoundationModels": "bedrockfoundationmodels",
}

MARKETPLACE_SUFFIX = " (Amazon Bedrock Edition)"

# Provider inference for AmazonBedrockFoundationModels, which has no `provider`
# attribute. Ordered: first matching prefix wins.
PROVIDER_BY_PREFIX = [
    ("Claude", "Anthropic"),
    ("Cohere", "Cohere"),
    ("Jamba", "AI21 Labs"),
    ("Jurassic", "AI21 Labs"),
    ("Meta Llama", "Meta"),
    ("Llama", "Meta"),
    ("Mistral", "Mistral AI"),
    ("Palmyra", "Writer"),
    ("Stable", "Stability AI"),
    ("Stability", "Stability AI"),
    ("TwelveLabs", "TwelveLabs"),
    ("Titan", "Amazon"),
    ("Nova", "Amazon"),
    ("Marengo", "TwelveLabs"),
    ("Pegasus", "TwelveLabs"),
    ("DeepSeek", "DeepSeek"),
    ("Qwen", "Qwen"),
    ("GPT", "OpenAI"),
    ("gpt", "OpenAI"),
    ("Gemma", "Google"),
    ("Grok", "xAI"),
    ("Luma", "Luma AI"),
    ("Ray", "Luma AI"),
]

# Units that represent token metering, mapped to a per-1M-token multiplier.
TOKEN_UNIT_TO_1M = {
    "1k tokens": 1000.0,
    "1m tokens": 1.0,
    "1000 tokens": 1000.0,
    "millionbatchinputtokens": 1.0,
    "millionbatchoutputtokens": 1.0,
    "training tokens": None,  # token-classed but not an inference rate
}

THROUGHPUT_UNITS = {"1m tpm hour", "1k tpm hour"}
IMAGE_UNITS = {"image", "images processed", "input images", "created_image"}
MEDIA_UNITS = {"seconds", "second", "minutes processed", "video"}
REQUEST_UNITS = {"requests", "api calls", "per 1000 requests", "text requests", "textunit"}
HOSTING_UNITS = {"hour", "hours", "model/month", "custom model unit per min"}

# Usage-type segments that begin a metering dimension. The text before the
# earliest marker is the model name, for rows with no `model` attribute.
DIMENSION_MARKERS = (
    "input",
    "output",
    "cache",
    "customization",
    "provisionedthroughput",
    "custommodelimport",
    "reserved",
    "modelstorage",
    "created_image",
    "search_units",
    "millionbatch",
    "tokencount",
    "videomedium",
)

# First usage-type segments that are Bedrock platform features, not models.
PLATFORM_PREFIXES = {
    "guardrail",
    "guardrailchecks",
    "dataautomation",
    "bedrockflows",
    "generatesql",
    "prompt",
    "apo",
    "amazonrerank",
    "knowledgebase",
    "agentcore",
    "modelevaluation",
    "us",
}


def fetch_json(url: str, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "bedrock-pricing-catalog/1.0", "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace").strip()
    if not text.startswith("{"):  # tolerate JSONP wrappers
        text = text[text.index("(") + 1 : text.rindex(")")]
    return json.loads(text)


def strip_usagetype(usagetype: str) -> str:
    """Reduce a usagetype to its dimension core.

    USE1-MP:USE1_cache_read_tokens_global_standard-Units -> cache_read_tokens_global_standard
    USE1-openai.gpt-5.6-luna-mantle-input-tokens-standard -> openai.gpt-5.6-luna-mantle-input-tokens-standard
    """
    core = re.sub(r"^[A-Z0-9]{3,6}-MP:[A-Za-z0-9]+_", "", usagetype)
    core = re.sub(r"-Units$", "", core)
    if core == usagetype:  # non-marketplace form: drop only the region prefix
        core = re.sub(r"^[A-Z0-9]{3,6}-", "", usagetype)
    return core


def _has(hay: str, *needles: str) -> bool:
    return any(n in hay for n in needles)


def classify_direction(core: str, token_type: str) -> str:
    h = re.sub(r"[^a-z0-9]", "", (core + " " + token_type).lower())
    if _has(h, "cachewrite1h", "cachewritetokens1h", "cachewrite1hinput"):
        return "cache_write_1h"
    if _has(h, "cachewritetokens30m", "cachewrite30m"):
        return "cache_write_30m"
    if _has(h, "cachewrite"):
        return "cache_write"
    if _has(h, "cacheread"):
        return "cache_read"
    if _has(h, "customizationtoken", "trainingtoken", "customizationtraining"):
        return "training"
    if _has(h, "inputtpm"):
        return "input"
    if _has(h, "outputtpm"):
        return "output"
    if _has(h, "inputtoken", "inputimage", "inputvideo", "inputaudio", "inputtext", "batchinputtoken"):
        return "input"
    if _has(h, "outputtoken", "outputimage", "batchoutputtoken"):
        return "output"
    return ""


def classify_tier(core: str, batch_attr: str, inference_type: str) -> str:
    """Explicit tier token, or "" when the usage type names none.

    Note the legacy marketplace shapes (`InputTokenCount`, `OutputTokenCount_Global`)
    and the Nova sub-modality shapes carry no tier token at all; those are the
    plain on-demand rate. build_rows() defaults them to "standard" for token
    dimensions so a per-tier lookup can't miss the on-demand price.
    """
    h = core.lower()
    a = (batch_attr or "").lower()
    it = (inference_type or "").lower()
    if _has(h, "latencyoptimized", "latency-optimized", "latency_optimized") or "optimi" in it:
        return "latency_optimized"
    if _has(h, "_batch", "-batch", "batchinput", "batchoutput") or a in {"true", "yes"} or "batch" in it:
        return "batch"
    if _has(h, "priority"):
        return "priority"
    if _has(h, "flex"):
        return "flex"
    if _has(h, "standard"):
        return "standard"
    return ""


def classify_routing(core: str, cross_region: str) -> str:
    h = core.lower()
    if _has(h, "_geo", "-geo", "geo_", "cross-region-geo"):
        return "geo"
    if _has(h, "global"):
        return "global"
    if cross_region and cross_region.lower() not in {"", "false", "no"}:
        return "cross_region"
    if _has(h, "cross-region", "crossregion"):
        return "cross_region"
    return "regional"


def classify_context(core: str) -> str:
    h = core.lower()
    if _has(h, "long-ctx", "longctx", "long_context", "long-context"):
        return "long"
    return ""


def classify_commitment(core: str) -> str:
    h = core.lower()
    if "reserved_1month" in h or "1monthcommit" in h:
        return "1_month"
    if "reserved_3month" in h or "3monthscommit" in h or "3monthcommit" in h:
        return "3_month"
    if "reserved_6month" in h or "6monthscommit" in h or "6monthcommit" in h:
        return "6_month"
    if "nocommit" in h:
        return "no_commit"
    return ""


def classify_dimension(unit: str, core: str) -> str:
    u = (unit or "").lower()
    h = core.lower()
    if "provisionedthroughput" in h or u in THROUGHPUT_UNITS:
        return "throughput_commitment"
    if u in TOKEN_UNIT_TO_1M:
        return "token"
    if u in IMAGE_UNITS:
        return "image"
    if u in MEDIA_UNITS:
        return "media"
    if u in REQUEST_UNITS:
        return "request"
    if u == "embeddings":
        return "embedding"
    if u == "search units":
        return "search"
    if u in HOSTING_UNITS:
        return "hosting"
    return "other"


def infer_provider(model_name: str, attr_provider: str) -> str:
    if attr_provider:
        return "Mistral AI" if attr_provider == "Mistral" else attr_provider
    for prefix, provider in PROVIDER_BY_PREFIX:
        if model_name.startswith(prefix):
            return provider
    if "." in model_name:
        vendor = model_name.split(".", 1)[0]
        return {
            "openai": "OpenAI",
            "anthropic": "Anthropic",
            "google": "Google",
            "meta": "Meta",
            "mistral": "Mistral AI",
            "amazon": "Amazon",
            "deepseek": "DeepSeek",
            "qwen": "Qwen",
            "xai": "xAI",
            "cohere": "Cohere",
            "ai21": "AI21 Labs",
            "writer": "Writer",
            "stability": "Stability AI",
            "twelvelabs": "TwelveLabs",
            "luma": "Luma AI",
        }.get(vendor.lower(), "")
    return ""


def model_from_usagetype(core: str) -> str:
    """Derive a model name by cutting the usage type at its first dimension marker.

    TitanText-Premier-output-tokens          -> TitanText-Premier
    Llama3-1-70B-Customization-Training      -> Llama3-1-70B
    TitanImageGeneratorV2-ProvisionedThrough -> TitanImageGeneratorV2
    """
    if core.split("-")[0].lower() in PLATFORM_PREFIXES:
        return ""
    low = core.lower()
    cut = len(core)
    for marker in DIMENSION_MARKERS:
        idx = low.find(marker)
        if 0 < idx < cut:
            cut = idx
    return core[:cut].strip("-_") if cut < len(core) else ""


def resolve_model(service_code: str, attrs: dict, core: str) -> tuple[str, str]:
    """Return (model_name, model_id_hint)."""
    servicename = attrs.get("servicename", "") or ""
    if service_code == "AmazonBedrockFoundationModels":
        if servicename.endswith(MARKETPLACE_SUFFIX):
            return servicename[: -len(MARKETPLACE_SUFFIX)], ""
        return servicename, ""
    model = attrs.get("model") or attrs.get("titanModel") or ""
    if model:
        return model, model
    # Custom Model Import prices by source architecture rather than a named model.
    arch = attrs.get("architectureName") or ""
    if arch:
        return arch, attrs.get("architecture", "") or ""
    m = re.match(r"^([A-Za-z0-9.\-]+?)-mantle-", core)
    if m:
        return m.group(1), m.group(1)
    # Platform features (Flows, Guardrails, Data Automation) legitimately have none.
    derived = model_from_usagetype(core)
    return derived, derived


def resolve_feature(attrs: dict) -> str:
    parts = [
        attrs.get("feature") or "",
        attrs.get("featureType") or attrs.get("featuretype") or "",
        attrs.get("policyType") or "",
    ]
    return " / ".join(p for p in parts if p)


def build_rows(service_code: str, offer: dict) -> list[dict]:
    version = offer.get("version", "")
    pub = offer.get("publicationDate", "")
    products = offer.get("products", {})
    rows: list[dict] = []

    for sku, terms in offer.get("terms", {}).get("OnDemand", {}).items():
        product = products.get(sku)
        if not product:
            continue
        attrs = product.get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        core = strip_usagetype(usagetype)
        model_name, model_id = resolve_model(service_code, attrs, core)

        for term in terms.values():
            effective = term.get("effectiveDate", "")
            for rate_code, dim in term.get("priceDimensions", {}).items():
                unit = dim.get("unit", "")
                try:
                    price = float(dim.get("pricePerUnit", {}).get("USD", ""))
                except (TypeError, ValueError):
                    continue

                dim_class = classify_dimension(unit, core)
                mult = TOKEN_UNIT_TO_1M.get(unit.lower())
                per_1m = ""
                if dim_class == "token" and mult:
                    per_1m = f"{price * mult:.10f}".rstrip("0").rstrip(".")

                tier = classify_tier(core, attrs.get("batch", ""), attrs.get("inferenceType", ""))
                commitment = classify_commitment(core)
                # Token usage types with no explicit tier token are the plain
                # on-demand rate (legacy `InputTokenCount`, Nova sub-modalities).
                if not tier and dim_class == "token" and not commitment:
                    tier = "standard"

                rows.append(
                    {
                        "service_code": service_code,
                        "provider": infer_provider(model_name, attrs.get("provider", "")),
                        "model_name": model_name,
                        "model_id_hint": model_id,
                        "feature": resolve_feature(attrs),
                        "region_code": attrs.get("regionCode", ""),
                        "location": attrs.get("location", ""),
                        "dimension_class": dim_class,
                        "token_direction": classify_direction(core, attrs.get("tokenType", "")),
                        "inference_tier": tier,
                        "routing": classify_routing(core, attrs.get("crossRegion", "")),
                        "context_mode": classify_context(core),
                        "commitment": commitment,
                        "unit": unit,
                        "price_per_unit_usd": f"{price:.10f}".rstrip("0").rstrip(".") if price else "0",
                        "price_per_1m_tokens_usd": per_1m,
                        "effective_date": effective,
                        "description": dim.get("description", ""),
                        "usage_type": usagetype,
                        "sku": sku,
                        "rate_code": rate_code,
                        "offer_version": version,
                        "publication_date": pub,
                    }
                )
    return rows


def verify_website(offers: dict[str, dict]) -> int:
    """Cross-check the public pricing page manifests against the Price List data."""
    bulk: dict[str, tuple[str, float]] = {}
    for svc, offer in offers.items():
        for sku, terms in offer.get("terms", {}).get("OnDemand", {}).items():
            for term in terms.values():
                for rc, d in term.get("priceDimensions", {}).items():
                    try:
                        bulk[rc] = (svc, float(d["pricePerUnit"]["USD"]))
                    except (KeyError, TypeError, ValueError):
                        continue

    total = matched = mismatched = 0
    for svc, slug in WEB_SLUGS.items():
        url = f"{WEB_BASE}/{slug}/USD/current/{slug}.json"
        print(f"verifying {url}", file=sys.stderr)
        data = fetch_json(url)
        stamp = data.get("manifest", {}).get("esIndex", "")
        print(
            f"  manifest esIndex={stamp}  offer_version={offers.get(svc, {}).get('version', 'n/a')}",
            file=sys.stderr,
        )
        for entries in data.get("regions", {}).values():
            for v in entries.values():
                total += 1
                rc = v.get("rateCode", "")
                if rc in bulk:
                    matched += 1
                    try:
                        if abs(float(v.get("price", "nan")) - bulk[rc][1]) > 1e-9:
                            mismatched += 1
                    except ValueError:
                        mismatched += 1

    pct = (100.0 * matched / total) if total else 0.0
    print(
        f"\nwebsite parity: {matched}/{total} rate codes resolved ({pct:.2f}%), "
        f"{mismatched} price mismatches",
        file=sys.stderr,
    )
    return 0 if (total and matched == total and mismatched == 0) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="config/bedrock-pricing-catalog.csv", help="output CSV path")
    ap.add_argument("--tokens-only", action="store_true", help="emit only per-token inference rates")
    ap.add_argument("--models-only", action="store_true", help="drop rows with no resolvable model name")
    ap.add_argument("--region", action="append", help="limit to region code(s); repeatable")
    ap.add_argument("--include-agentcore", action="store_true", help="also include AmazonBedrockAgentCore")
    ap.add_argument("--verify-website", action="store_true", help="cross-check aws.amazon.com pricing manifests")
    ap.add_argument("--cache-dir", default=".tmp-pricing", help="directory for downloaded offer files")
    args = ap.parse_args()

    codes = list(MODEL_SERVICE_CODES)
    if args.include_agentcore:
        codes.append(AGENTCORE_SERVICE_CODE)

    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    offers: dict[str, dict] = {}
    for code in codes:
        path = cache / f"{code}.json"
        if path.exists():
            print(f"using cached {path}", file=sys.stderr)
            offers[code] = json.loads(path.read_text(encoding="utf-8"))
        else:
            url = f"{BULK_BASE}/{code}/current/index.json"
            print(f"fetching {url}", file=sys.stderr)
            offers[code] = fetch_json(url)
            path.write_text(json.dumps(offers[code]), encoding="utf-8")

    if args.verify_website:
        return verify_website({k: v for k, v in offers.items() if k in WEB_SLUGS})

    rows: list[dict] = []
    for code, offer in offers.items():
        got = build_rows(code, offer)
        print(f"  {code}: {len(got)} rate rows (offer version {offer.get('version')})", file=sys.stderr)
        rows.extend(got)

    if args.region:
        wanted = set(args.region)
        rows = [r for r in rows if r["region_code"] in wanted]
    if args.tokens_only:
        rows = [r for r in rows if r["dimension_class"] == "token"]
    if args.models_only:
        rows = [r for r in rows if r["model_name"]]

    rows.sort(
        key=lambda r: (
            r["provider"],
            r["model_name"].lower(),
            r["region_code"],
            r["dimension_class"],
            r["token_direction"],
            r["inference_tier"],
            r["routing"],
            r["context_mode"],
            r["usage_type"],
        )
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    models = {(r["provider"], r["model_name"]) for r in rows if r["model_name"]}
    regions = {r["region_code"] for r in rows}
    print(
        f"\nwrote {out} — {len(rows)} rows, {len(models)} distinct models, "
        f"{len(regions)} regions, generated {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
