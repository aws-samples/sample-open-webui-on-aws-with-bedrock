# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AWS Price List offer-file parsing into the token rate grid.

Walks an offer file's products and OnDemand price dimensions, keeps only
token-denominated units, and emits one ParsedRate per (identity, direction,
tier, routing, context) with the rate normalized to USD per 1M tokens
(Requirements 1.2, 1.3, 1.4).

Usage-type grammars observed across the three Bedrock offer files
(measured against live us-east-1 files, 2026-07):

  AmazonBedrock ("mantle" shapes, unit `1K tokens`)
      USE1-openai.gpt-oss-120b-mantle-input-tokens-standard
      USE1-openai.gpt-5.6-luna-mantle-cache-write-tokens-5m-long-ctx-flex
  AmazonBedrock ("legacy" shapes, unit `1K tokens`)
      USE1-Claude3Haiku-input-tokens
      USE1-Llama3-1-405B-output-tokens-batch
      USE1-NovaPro-input-tokens-latency-optimized
  AmazonBedrockFoundationModels (marketplace, unit `1M tokens`, identity via
  `servicename` minus " (Amazon Bedrock Edition)")
      USE1-MP:USE1_input_tokens_global_standard-Units          (snake)
      USE1-MP:USE1_CacheWrite1hInputTokenCount_Global-Units    (camel)
      USE1-MP:USE1_MillionBatchInputTokens-Units               (camel)
  AmazonBedrockService (unit `1K tokens`)
      USE1-Claude4Sonnet-input-tokens-cross-region-global-batch
      USE1-Claude4Sonnet-output-tokens-long-context-cross-region-global

A token usage type naming no tier is the `standard` on-demand tier
(Requirement 1.4). Cross-region markers set the routing mode: `global`
today; a `geo` marker is classified for the future without a schema change
(Requirement 7.5). Unknown grammar returns None — a rate is skipped rather
than misclassified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .identity import MODEL_ID_RE

MARKETPLACE_SUFFIX = " (Amazon Bedrock Edition)"

# unit → multiplier to USD per 1M tokens (Requirement 1.3)
_TOKEN_UNITS = {"1k tokens": Decimal(1000), "1m tokens": Decimal(1)}

_TIERS = frozenset({"standard", "batch", "flex", "priority"})
_REGION_PREFIX_RE = re.compile(r"^[A-Z0-9]+-")
_MP_PREFIX_RE = re.compile(r"^MP:[A-Z0-9]+_")
_DURATION_RE = re.compile(r"^\d+[mh]$")
_CAMEL_HEAD_RE = re.compile(
    r"^(?:Million)?(?P<batch>Batch)?"
    r"(?:Cache(?P<cache>Read|Write)(?P<dur>\d+[mh])?)?"
    r"(?P<dir>Input|Output)?Token(?:Count|s)$"
)
_LEGACY_DIR_RE = re.compile(
    r"-(input|output|cache-read|cache-write)(?:-(image|audio|video))?-(?:input-)?token(?:s|-count)"
)
_TRAINING_RE = re.compile(r"-Customization-Training$")


@dataclass(frozen=True)
class Classified:
    direction: str  # input | output | cache_read | cache_write[_5m|_1h|_30m]
    tier: str       # standard | batch | flex | priority | latency_optimized
    routing: str    # in_region | global | geo
    context: str    # default | long


@dataclass(frozen=True)
class ParsedRate:
    identity_kind: str  # "id" (usage type embeds a Bedrock model id) | "name"
    identity: str       # the model id, or the Price List name
    display_name: str
    provider: str
    direction: str
    tier: str
    routing: str
    context: str
    usd_per_1m: Decimal
    effective_date: str
    usagetype: str


def _cache_direction(kind: str, duration: str | None) -> str:
    d = f"cache_{kind}"
    if kind == "write" and duration:
        d += f"_{duration}"
    return d


def _classify_snake(segs: list[str]) -> Classified | None:
    """MP snake family: input_tokens_global_standard, cache_write_tokens_1h_…"""
    direction = None
    if segs[:1] == ["cache"] and len(segs) > 1 and segs[1] in ("read", "write"):
        kind, segs = segs[1], segs[2:]
    elif segs[:1] == ["input"] or segs[:1] == ["output"]:
        kind, segs = None, segs
    else:
        return None
    if kind is None:
        direction, segs = segs[0], segs[1:]
    if segs[:1] != ["tokens"]:
        return None
    segs = segs[1:]
    duration = None
    if segs and _DURATION_RE.match(segs[0]):
        duration, segs = segs[0], segs[1:]
    if kind is not None:
        direction = _cache_direction(kind, duration)
    routing, context, tier = "in_region", "default", "standard"
    for s in segs:
        if s == "global":
            routing = "global"
        elif s == "geo":
            routing = "geo"
        elif s in ("long", "context", "ctx"):
            context = "long"
        elif s in _TIERS:
            tier = s
        else:
            return None
    return Classified(direction, tier, routing, context)


def _classify_camel(head: str, quals: list[str]) -> Classified | None:
    """MP camel family: InputTokenCount, CacheWrite1hInputTokenCount_Global_Batch."""
    m = _CAMEL_HEAD_RE.match(head)
    if not m:
        return None
    if m.group("cache"):
        direction = _cache_direction(m.group("cache").lower(), m.group("dur"))
    elif m.group("dir"):
        direction = m.group("dir").lower()
    else:
        return None
    tier = "batch" if m.group("batch") else "standard"
    routing, context = "in_region", "default"
    for q in quals:
        if q == "Global":
            routing = "global"
        elif q == "Geo":
            routing = "geo"
        elif q == "Batch":
            tier = "batch"
        elif q == "LatencyOptimized":
            tier = "latency_optimized"
        elif q == "LongContext":
            context = "long"
        else:
            return None
    return Classified(direction, tier, routing, context)


def _classify_suffix_tokens(segs: list[str]) -> Classified | None:
    """Shared suffix grammar for mantle and legacy dash shapes."""
    routing, context, tier = "in_region", "default", None
    i = 0
    while i < len(segs):
        s = segs[i]
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        if s == "cross" and nxt == "region":
            i += 2
            if i < len(segs) and segs[i] in ("global", "geo"):
                routing = segs[i]
                i += 1
                continue
            return None
        if s == "long" and nxt in ("context", "ctx"):
            context, i = "long", i + 2
            continue
        if s == "latency" and nxt == "optimized":
            tier, i = "latency_optimized", i + 2
            continue
        if s == "custom" and nxt == "model":
            # custom-model serving prices the CUSTOMIZED model, not the base
            # foundation model — keeping it would corrupt the base grid.
            return None
        if s in _TIERS:
            tier, i = s, i + 1
            continue
        return None
    return Classified("", tier or "standard", routing, context)


def _classify_mantle(rest: str) -> Classified | None:
    """After '-mantle-': input-tokens-standard, cache-write-tokens-5m-long-ctx-flex."""
    segs = rest.split("-")
    kind = None
    if segs[:1] == ["cache"] and len(segs) > 1 and segs[1] in ("read", "write"):
        kind, segs = segs[1], segs[2:]
    elif segs[:1] in (["input"], ["output"]):
        pass
    else:
        return None
    if kind is None:
        direction, segs = segs[0], segs[1:]
    if segs[:1] != ["tokens"]:
        return None
    segs = segs[1:]
    duration = None
    if segs and _DURATION_RE.match(segs[0]):
        duration, segs = segs[0], segs[1:]
    if kind is not None:
        direction = _cache_direction(kind, duration)
    tail = _classify_suffix_tokens(segs)
    if tail is None:
        return None
    return Classified(direction, tail.tier, tail.routing, tail.context)


def classify_usagetype(usagetype: str) -> tuple[str | None, Classified | None]:
    """Classify one usage type → (embedded model id | None, Classified | None)."""
    ut = _REGION_PREFIX_RE.sub("", usagetype or "")
    if ut.startswith("MP:"):
        body = _MP_PREFIX_RE.sub("", ut)
        body = body.removesuffix("-Units")
        segs = body.split("_")
        if segs and segs[0][:1].isupper():
            return None, _classify_camel(segs[0], segs[1:])
        return None, _classify_snake(segs)
    if "-mantle-" in ut:
        model_id, _, rest = ut.partition("-mantle-")
        cls = _classify_mantle(rest)
        if cls and MODEL_ID_RE.match(model_id):
            return model_id, cls
        return None, None
    if _TRAINING_RE.search(ut):
        return None, Classified("training", "standard", "in_region", "default")
    m = _LEGACY_DIR_RE.search(ut)
    if m:
        tail = _classify_suffix_tokens([s for s in ut[m.end():].split("-") if s])
        if tail is None:
            return None, None
        direction = m.group(1).replace("-", "_")
        if m.group(2):
            direction = f"{direction}_{m.group(2)}"  # multimodal: input_image, …
        return None, Classified(direction, tail.tier, tail.routing, tail.context)
    return None, None


def parse_offer(offer: dict, region: str, service_code: str) -> tuple[list[ParsedRate], str]:
    """Parse one offer file → ([ParsedRate], offer version).

    Only token-denominated OnDemand price dimensions are kept — commitment
    (TPM-hour), image, request and training dimensions are excluded. Model
    identity comes from the usage type when it embeds a model id, else from
    the `model` attribute, else from the marketplace `servicename` minus the
    " (Amazon Bedrock Edition)" suffix (Requirements 1.2, 2.2).
    """
    version = str(offer.get("version") or "unknown")
    ondemand = offer.get("terms", {}).get("OnDemand", {})
    out: list[ParsedRate] = []
    for sku, product in offer.get("products", {}).items():
        attrs = product.get("attributes", {})
        if attrs.get("regionCode") and attrs.get("regionCode") != region:
            continue
        usagetype = attrs.get("usagetype", "")
        embedded_id, cls = classify_usagetype(usagetype)
        if cls is None:
            continue
        if embedded_id:
            kind, identity = "id", embedded_id
            display = attrs.get("model") or embedded_id
        elif attrs.get("model"):
            kind, identity = "name", attrs["model"]
            display = attrs["model"]
        elif attrs.get("servicename"):
            name = attrs["servicename"]
            name = name.removesuffix(MARKETPLACE_SUFFIX)
            kind, identity, display = "name", name, name
        else:
            continue
        for term in ondemand.get(sku, {}).values():
            for dim in term.get("priceDimensions", {}).values():
                multiplier = _TOKEN_UNITS.get((dim.get("unit") or "").strip().lower())
                if multiplier is None:
                    continue
                usd = dim.get("pricePerUnit", {}).get("USD")
                if usd is None:
                    continue
                try:
                    per_1m = Decimal(str(usd)) * multiplier
                except ArithmeticError:
                    continue
                out.append(ParsedRate(
                    identity_kind=kind,
                    identity=identity,
                    display_name=display,
                    provider=attrs.get("provider", ""),
                    direction=cls.direction,
                    tier=cls.tier,
                    routing=cls.routing,
                    context=cls.context,
                    usd_per_1m=per_1m,
                    effective_date=str(term.get("effectiveDate") or ""),
                    usagetype=usagetype,
                ))
    return out, version


def decimal_str(d: Decimal) -> str:
    """Exact, non-scientific string for storage (Decimal('27.5000') → '27.5')."""
    return format(d.normalize(), "f")
