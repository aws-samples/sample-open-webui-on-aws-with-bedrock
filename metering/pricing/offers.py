# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AWS Price List offer-file parsing into the token rate grid.

Walks an offer file's products and OnDemand price dimensions, keeps only
token-denominated units, and emits one ParsedRate per (identity, direction,
tier, routing, context) with the rate normalized to USD per 1M tokens
(Requirements 1.2, 1.3, 1.4).

Design 06-GATEWAY-PRICING-COVERAGE D5: the four usage-type grammars observed
across the three Bedrock offer files collapse into ONE canonical tokenizer
and ONE qualifier vocabulary, spelled once below. There is no longer a silent
``None`` drop — every dimension lands in the grid, on the named exclusion
list (``excluded``), or in the loud unknown bucket (``unclassified``).

Usage-type grammars the tokenizer subsumes (measured live us-east-1, 2026-07):

  AmazonBedrock ("mantle", unit `1K tokens`)
      USE1-openai.gpt-oss-120b-mantle-input-tokens-standard
      USE1-openai.gpt-5.6-luna-mantle-cache-write-tokens-5m-long-ctx-flex
  AmazonBedrock ("legacy", unit `1K tokens`)
      USE1-Claude3Haiku-input-tokens
      USE1-NovaPro-input-tokens-latency-optimized
  AmazonBedrockFoundationModels (marketplace, unit `1M tokens`)
      USE1-MP:USE1_input_tokens_global_standard-Units          (snake)
      USE1-MP:USE1_CacheWrite1hInputTokenCount_Global-Units    (camel)
  AmazonBedrockService (unit `1K tokens`)
      USE1-Claude4Sonnet-output-tokens-long-context-cross-region-global

A token usage type naming no tier is the `standard` on-demand tier
(Requirement 1.4). Cross-region markers set the routing mode. Deliberate
exclusions (custom-model SKUs, APO ``optimizePrompt``) match a named list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .identity import MODEL_ID_RE

MARKETPLACE_SUFFIX = " (Amazon Bedrock Edition)"

# unit -> multiplier to USD per 1M tokens (Requirement 1.3)
_TOKEN_UNITS = {"1k tokens": Decimal(1000), "1m tokens": Decimal(1)}

_REGION_PREFIX_RE = re.compile(r"^[a-z0-9]+-")           # USE1- (lowercased first)
_MP_PREFIX_RE = re.compile(r"^mp:[a-z0-9]+_")            # MP:USE1_ (lowercased)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")  # split camelCase joins
_CACHE_DUR_GLUE_RE = re.compile(r"\b(read|write)(\d+[mh])\b", re.IGNORECASE)  # Write1h
_DURATION_RE = re.compile(r"^\d+[mh]$")                  # 5m, 1h, 30m

# ── D5: ONE qualifier vocabulary, spelled ONCE ──────────────────────────────
#
# Each token (already lowercased, camel-split, delimiter-split) maps to a
# (axis, value) the classifier applies. Multi-token markers ("cross region",
# "long context", "latency optimized") are collapsed to a single token by the
# tokenizer before this table is consulted, so the vocabulary stays flat.
_TIERS = frozenset({"standard", "batch", "flex", "priority"})
_QUALIFIERS: dict[str, tuple[str, str]] = {
    # routing axis
    "global": ("routing", "global"),
    "geo": ("routing", "geo"),
    # context axis
    "longcontext": ("context", "long"),
    # tier axis (explicit tiers + the multiword latency-optimized marker)
    "latencyoptimized": ("tier", "latency_optimized"),
    **{t: ("tier", t) for t in _TIERS},
}
# tokens that carry no axis meaning and are skipped silently once the
# direction stem has been consumed (structural noise, never "unknown").
# "text" is here because TEXT tokens are the modality this meter counts —
# a text-input-tokens SKU is just the input rate.
_NOISE = frozenset({"tokens", "token", "tokencount", "count",
                    "cache", "million", "units", "flex", "text"} - _TIERS)
# multiword markers the tokenizer folds into one vocabulary token
_MULTIWORD = (
    (("cross", "region"), None),          # consumed; the following routing token wins
    (("long", "context"), "longcontext"),
    (("long", "ctx"), "longcontext"),
    (("latency", "optimized"), "latencyoptimized"),
)

# ── Named, deliberate exclusions (D5) — matched by name, recorded as excluded ─
_EXCLUSIONS = (
    # (predicate over the raw usage type, rule label per contract §1)
    (lambda ut: "custom-model" in ut or "custommodel" in ut, "custom-model"),
    (lambda ut: "optimizeprompt" in ut, "apo-optimize-prompt"),
)

# Non-text token modalities, excluded when ADJACENT to the direction anchor
# (USE1-NovaSonic-speech-input-tokens, Nova2.0Omni-input-audio-token-count,
# InputVideoSecond): real billed dimensions, but for a modality this meter does
# not count. Adjacency — not a raw substring — so a model NAME containing a
# modality word cannot poison its own text-token SKUs. Without this rule the
# tokenizer's anchor scan discards a leading modality token and the speech rate
# collides onto the text leaf: the nova-sonic $0.06→$3.40 regression the
# rate-diff gate caught live on 2026-08-21. "text" is deliberately NOT here —
# text tokens are the metered modality (it is qualifier noise instead).
_MODALITIES = frozenset({"speech", "audio", "video", "image", "images"})

_TRAINING_RE = re.compile(r"-customization-training$")


@dataclass(frozen=True)
class Classified:
    direction: str  # input | output | cache_read | cache_write[_5m|_1h|_30m] | training
    tier: str       # standard | batch | flex | priority | latency_optimized
    routing: str    # in_region | global | geo
    context: str    # default | long


# Classification verdicts. Exactly one is produced per usage type — never a
# bare None (contract §1: "No classification path may return a bare None").
GRID = "grid"
EXCLUDED = "excluded"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Verdict:
    kind: str                        # GRID | EXCLUDED | UNCLASSIFIED
    model_id: str | None = None      # embedded Bedrock id when the usage type carries one
    classified: Classified | None = None  # set iff kind == GRID
    rule: str | None = None          # exclusion label when kind == EXCLUDED


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


@dataclass
class ParseAccounting:
    """Aggregate classification + merge accounting (contract §1).

    Populated across one or many ``parse_offer`` calls (pass the SAME instance
    to accumulate a refresh's worth of dimensions). ``rate_conflicts`` is
    filled by ``merge_rate`` at grid-assembly time, not by the parser.
    """

    excluded: list[dict] = field(default_factory=list)        # {usage_type, rule}
    unclassified: list[dict] = field(default_factory=list)    # {usage_type, service_code}
    rate_conflicts: list[dict] = field(default_factory=list)  # {model_id, leaf, kept, dropped, usage_type}


# ── ONE canonical tokenizer (D5) ────────────────────────────────────────────

def _tokenize(usagetype: str) -> tuple[str | None, list[str]]:
    """Canonicalize a usage type into (embedded model id | None, tokens).

    Steps (spelled once): lowercase, strip the region prefix, strip the
    marketplace ``MP:REGION_`` prefix and ``-units`` suffix, lift an embedded
    ``…-mantle-…`` model id, split on ``-``/``_`` and camelCase boundaries,
    then fold known multiword markers into single vocabulary tokens.
    """
    ut = (usagetype or "").strip()
    # camel boundaries FIRST (before lowercasing) so InputTokenCount splits
    ut = _CAMEL_BOUNDARY_RE.sub("-", ut)
    # unglue a camel cache duration (CacheWrite1h… -> cache write 1h)
    ut = _CACHE_DUR_GLUE_RE.sub(r"\1-\2", ut)
    ut = ut.lower()
    ut = _REGION_PREFIX_RE.sub("", ut, count=1)

    model_id = None
    if ut.startswith("mp:"):
        ut = _MP_PREFIX_RE.sub("", ut, count=1)
        ut = ut.removesuffix("-units")
    elif "-mantle-" in ut:
        head, _, rest = ut.partition("-mantle-")
        # the head is a real Bedrock model id (already lowercased); validate it
        if MODEL_ID_RE.match(head):
            model_id = head
        ut = rest

    raw = [t for t in re.split(r"[-_]", ut) if t]

    # fold multiword markers
    tokens: list[str] = []
    i = 0
    while i < len(raw):
        matched = False
        for words, folded in _MULTIWORD:
            n = len(words)
            if tuple(raw[i:i + n]) == words:
                if folded is not None:
                    tokens.append(folded)
                i += n
                matched = True
                break
        if not matched:
            tokens.append(raw[i])
            i += 1
    return model_id, tokens


def _direction_from(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Consume the direction stem; return (direction | None, qualifier rest).

    The direction anchor (``input`` / ``output`` / ``cache``) may not sit at
    position 0: the legacy dash grammar prefixes the model-name tokens
    (``claude3-haiku-input-tokens``), and identity is resolved separately from
    the ``model`` attribute — so any tokens BEFORE the anchor are the name
    prefix and are discarded here. The tokens AFTER the stem are qualifiers.
    """
    if not tokens:
        return None, tokens
    # training dimension (kept for completeness; priced at standard elsewhere)
    if "training" in tokens:
        return "training", [t for t in tokens if t != "training"]
    # find the first direction anchor
    anchor = next((i for i, t in enumerate(tokens)
                   if t in ("cache", "input", "output")), None)
    if anchor is None:
        return None, tokens
    tok = tokens[anchor]
    tail = tokens[anchor:]
    # cache read/write [duration]
    if tok == "cache" and len(tail) >= 2 and tail[1] in ("read", "write"):
        kind = tail[1]
        rest = tail[2:]
        duration = None
        # duration may sit right after the kind or after the "tokens" noise word
        for j in (0, 1):
            if len(rest) > j and _DURATION_RE.match(rest[j]):
                duration = rest[j]
                rest = rest[:j] + rest[j + 1:]
                break
        # the camel family spells the redundant stem CacheReadINPUTTokenCount —
        # a cache direction already implies the input side, so absorb it
        rest = [t for t in rest if t not in ("input", "output")]
        direction = f"cache_{kind}"
        if kind == "write" and duration:
            direction += f"_{duration}"
        return direction, rest
    if tok in ("input", "output"):
        return tok, tail[1:]
    return None, tokens


def classify_usagetype(usagetype: str, service_code: str = "") -> Verdict:
    """Classify ONE usage type into exactly one verdict (D5, contract §1).

    Order: named exclusions -> training -> canonical grid classification ->
    unclassified. Never returns a bare None.
    """
    low = (usagetype or "").lower()
    for predicate, rule in _EXCLUSIONS:
        if predicate(low):
            return Verdict(EXCLUDED, rule=rule)

    model_id, tokens = _tokenize(usagetype)

    if _TRAINING_RE.search(low):
        return Verdict(GRID, model_id=model_id,
                       classified=Classified("training", "standard", "in_region", "default"))

    # non-text modality adjacent to the direction anchor (speech-input,
    # input-audio, InputVideoSecond) -> named exclusion, never a text leaf
    for i, t in enumerate(tokens):
        if t in ("input", "output", "cache"):
            if (i > 0 and tokens[i - 1] in _MODALITIES) or \
               (i + 1 < len(tokens) and tokens[i + 1] in _MODALITIES):
                return Verdict(EXCLUDED, rule="non-text-modality")
            break

    direction, rest = _direction_from(tokens)
    if direction is None:
        return Verdict(UNCLASSIFIED)

    routing, context, tier = "in_region", "default", "standard"
    for tok in rest:
        if tok in _NOISE:
            continue
        axis_value = _QUALIFIERS.get(tok)
        if axis_value is None:
            # an unknown qualifier is a LOUD unknown, never a silent drop
            return Verdict(UNCLASSIFIED)
        axis, value = axis_value
        if axis == "routing":
            routing = value
        elif axis == "context":
            context = value
        else:  # tier
            tier = value
    return Verdict(GRID, model_id=model_id,
                   classified=Classified(direction, tier, routing, context))


# ── D7: deterministic merge (identical -> silent; conflict -> keep MAX + signal)

def merge_rate(grid: dict, r: ParsedRate, acc: ParseAccounting | None = None,
               model_id: str = "") -> None:
    """Merge one ParsedRate into a routing->tier->context->direction grid.

    Replaces first-wins ``cell.setdefault`` (design D7). Identical duplicate
    values merge silently; a conflicting value keeps the MAXIMUM (conservative
    for quota admission) and records a ``rate_conflicts`` entry.
    """
    cell = (grid.setdefault(r.routing, {})
            .setdefault(r.tier, {}).setdefault(r.context, {}))
    new = float(r.usd_per_1m)
    if r.direction not in cell:
        cell[r.direction] = new
        return
    cur = cell[r.direction]
    if cur == new:
        return  # identical duplicate -> silent
    kept, dropped = max(cur, new), min(cur, new)
    cell[r.direction] = kept
    if acc is not None:
        acc.rate_conflicts.append({
            "model_id": model_id or r.identity,
            "leaf": f"{r.routing}/{r.tier}/{r.context}/{r.direction}",
            "kept": kept,
            "dropped": dropped,
            "usage_type": r.usagetype,
        })


def parse_offer(offer: dict, region: str, service_code: str,
                acc: ParseAccounting | None = None) -> tuple[list[ParsedRate], str]:
    """Parse one offer file -> ([ParsedRate], offer version).

    Only token-denominated OnDemand price dimensions are kept. Model identity
    comes from the usage type when it embeds a model id, else from the
    ``model`` attribute, else from the marketplace ``servicename`` minus the
    " (Amazon Bedrock Edition)" suffix (Requirements 1.2, 2.2).

    When ``acc`` is supplied, every usage type that is not gridded is recorded
    on it as ``excluded`` (named exclusion) or ``unclassified`` (loud unknown),
    so no dimension is dropped silently (contract §1 / D5).
    """
    version = str(offer.get("version") or "unknown")
    ondemand = offer.get("terms", {}).get("OnDemand", {})
    out: list[ParsedRate] = []
    for sku, product in offer.get("products", {}).items():
        attrs = product.get("attributes", {})
        if attrs.get("regionCode") and attrs.get("regionCode") != region:
            continue
        usagetype = attrs.get("usagetype", "")
        # only token-unit dimensions are in scope for the loud unknown bucket:
        # a non-token product (images, minutes, TPM commitments) with an
        # unknown shape is not a silently-dropped token rate (the live catalog
        # carries hundreds of these; counting them made the unclassified alarm
        # pure noise — 318 on 2026-08-21).
        has_token_dim = any(
            _TOKEN_UNITS.get((dim.get("unit") or "").strip().lower()) is not None
            for term in ondemand.get(sku, {}).values()
            for dim in term.get("priceDimensions", {}).values()
        )
        verdict = classify_usagetype(usagetype, service_code)
        if verdict.kind == EXCLUDED:
            if acc is not None and has_token_dim:
                acc.excluded.append({"usage_type": usagetype, "rule": verdict.rule})
            continue
        if verdict.kind == UNCLASSIFIED:
            if acc is not None and has_token_dim:
                acc.unclassified.append({"usage_type": usagetype,
                                         "service_code": service_code})
            continue
        cls = verdict.classified
        embedded_id = verdict.model_id
        if embedded_id:
            kind, identity = "id", embedded_id
            display = attrs.get("model") or embedded_id
        elif attrs.get("model"):
            kind, identity = "name", attrs["model"]
            display = attrs["model"]
        elif attrs.get("servicename"):
            name = attrs["servicename"].removesuffix(MARKETPLACE_SUFFIX)
            kind, identity, display = "name", name, name
        else:
            # a gridded dimension with no identity anchor is itself unknown
            if acc is not None and has_token_dim:
                acc.unclassified.append({"usage_type": usagetype,
                                         "service_code": service_code})
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
    """Exact, non-scientific string for storage (Decimal('27.5000') -> '27.5')."""
    return format(d.normalize(), "f")
