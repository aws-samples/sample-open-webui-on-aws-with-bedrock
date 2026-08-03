# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model identity — parsing invoked ids, safe alias expansion, name joins.

Three concerns, all pure functions (no AWS calls):

1. `parse_model_ref` — turn whatever id the gateway/pipe invoked
   ("bedrock/global.anthropic.claude-opus-5") into the catalog key
   ("anthropic.claude-opus-5") plus the routing mode the request actually
   used (`in_region` | `geo` | `global`). Requirements 2.8, 2.9, 7.1-7.4.

2. `id_aliases` — expand a Bedrock model id into the alias keys it may be
   invoked or published under, stripping ONLY suffixes that cannot carry
   version meaning (`:N[:tag]`, `-vN`, `-YYYYMMDD`, and a trailing `-N`
   only when preceded by a letter). The letter guard is load-bearing:
   without it `anthropic.claude-opus-4-7` collapses to `…opus-4` and
   collides with `…opus-4-6` — the silent mis-pricing Requirement 2.6
   forbids. Expansion is applied to the CATALOG side only; the id being
   priced is always matched exactly (Requirement 2.5).

3. `normalize_name` / `build_name_index` — join Price List display names
   ("Claude Opus 5") to control-plane model names from
   `bedrock:ListFoundationModels`. Names are normalized on BOTH sides of a
   name-to-name comparison; a model id is never normalized. Zero or
   multiple candidates mean unresolved, never a guess (Requirement 2.7).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# A Bedrock model id: vendor.model, lowercase, optionally versioned/qualified
# (e.g. anthropic.claude-3-haiku-20240307-v1:0:48k). Used both to validate
# ids extracted from usage types and to garbage-collect catalog rows whose
# key is not a model id (Requirement 10.1).
MODEL_ID_RE = re.compile(r"^[a-z0-9]+\.[a-z0-9][a-z0-9.:\-]*$")

ROUTING_IN_REGION = "in_region"
ROUTING_GEO = "geo"
ROUTING_GLOBAL = "global"
_ROUTING_MODES = (ROUTING_IN_REGION, ROUTING_GEO, ROUTING_GLOBAL)

# Geographic cross-region inference-profile scopes AWS uses today. A geo
# prefix means the request may be served from another region in the
# geography; AWS publishes no on-demand token rate for it (Requirement 7.4).
_GEO_SCOPES = frozenset({"us", "eu", "apac", "ap", "ca", "sa", "jp", "au"})

# Pipe-level prefixes the Open WebUI integration prepends to model ids.
_PIPE_PREFIXES = frozenset({"gateway_anthropic", "metering"})


@dataclass(frozen=True)
class ModelRef:
    """An invoked model id resolved to (catalog key, routing mode)."""

    key: str
    routing: str


def parse_model_ref(raw: str, default_routing: str | None = None) -> ModelRef:
    """Parse an invoked model id into its catalog key and routing mode.

    Steps, in order (design §4.1):
      1. Strip a gateway path prefix ("bedrock/…" — keep the last segment).
      2. Strip a pipe prefix ("gateway_anthropic.", "metering.") when what
         follows is still a dotted id.
      3. Peel exactly ONE routing scope: "global." → global; a geographic
         scope ("us.", "eu.", "apac.", …) → geo; peel only when the
         remainder still looks like vendor.model (Requirement 2.8).

    Two ids differing only by scope therefore yield the same key
    (Requirement 2.9). When no prefix is present, `default_routing` (or the
    ROUTING_DEFAULT env var) applies — id-derived routing always wins over
    the default (Requirement 7.11).
    """
    s = str(raw or "").strip()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    head, dot, rest = s.partition(".")
    if dot and head in _PIPE_PREFIXES and "." in rest:
        s = rest
    routing = None
    head, dot, rest = s.partition(".")
    if dot and rest and MODEL_ID_RE.match(rest):
        if head == "global":
            s, routing = rest, ROUTING_GLOBAL
        elif head in _GEO_SCOPES:
            s, routing = rest, ROUTING_GEO
    if routing is None:
        routing = default_routing or os.environ.get("ROUTING_DEFAULT", ROUTING_IN_REGION)
        if routing not in _ROUTING_MODES:
            routing = ROUTING_IN_REGION
    return ModelRef(key=s, routing=routing)


# Alias-expansion suffix rules (design §4.3). Order matters: qualifiers are
# peeled outside-in (":N[:tag]" → "-vN" → "-YYYYMMDD" → guarded "-N").
#
# The trailing "-N" guard is tighter than "preceded by a letter": it strips
# only when the letter terminates a digit+letter size token ("…120b-1" — a
# release counter), never after a word ("…sonnet-5" — a version). The wider
# letter-only guard from the design table would reduce claude-sonnet-5 to
# claude-sonnet; stripping less can only reduce wrong-match risk, and the
# design's pinned positive case (gpt-oss-120b-1:0 → gpt-oss-120b) still holds.
_SUFFIX_RULES = (
    re.compile(r":\d+(?::[a-z0-9]+)?$"),   # -v1:0, -v1:0:48k
    re.compile(r"-v\d+$"),                  # -v1
    re.compile(r"-\d{8}$"),                 # -20251001
    re.compile(r"(?<=\d[a-z])-\d+$"),       # -1 only after a size token (120b-1)
)


def id_aliases(model_id: str) -> list[str]:
    """Expand a model id into its alias keys, most-specific first.

    The original id is always first; each successive entry strips one
    version-only suffix. A trailing `-N` is stripped only when preceded by
    a letter, so `claude-opus-4-7` never collapses to `claude-opus-4`
    (Requirement 2.5, 2.6).
    """
    out = [model_id]
    cur = model_id
    while True:
        nxt = None
        for rule in _SUFFIX_RULES:
            m = rule.search(cur)
            if m:
                candidate = cur[: m.start()]
                if candidate and "." in candidate:
                    nxt = candidate
                break
        if not nxt or nxt == cur:
            break
        out.append(nxt)
        cur = nxt
    return out


def build_index(model_ids) -> dict[str, str | None]:
    """Alias-expanded index over CATALOG-side ids: alias key → model id.

    A key claimed by two different ids maps to None (ambiguous) — lookup
    returns no match rather than a guess (Requirement 2.7). Query with the
    exact id being priced; never rewrite the queried id (Requirement 2.5).
    """
    index: dict[str, str | None] = {}
    for mid in model_ids:
        for alias in id_aliases(mid):
            if alias in index and index[alias] != mid:
                index[alias] = None
            else:
                index[alias] = mid
    return index


# Name normalization: lowercase, keep parenthesised content as ordinary
# tokens (Price List names put versions outside parens — "Pixtral Large
# 25.02" — where the control plane parenthesises them — "Pixtral Large
# (25.02)"), collapse ".0" version tails ("Nova 2.0 Lite" ≡ "Nova 2 Lite"),
# drop training/format noise tokens, strip non-alphanumerics. These are
# exact, deterministic equivalences — not similarity matching (Req 2.6).
# Applied to BOTH sides of a name-to-name comparison — never to a model id.
_DOT_ZERO_RE = re.compile(r"(\d+)\.0\b")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_NOISE_TOKENS = frozenset({"instruct", "it", "pt", "bf16", "vl", "dense"})


def normalize_name(name: str) -> str:
    s = _DOT_ZERO_RE.sub(r"\1", str(name or "").lower())
    return "".join(t for t in _SPLIT_RE.split(s) if t and t not in _NOISE_TOKENS)


def build_name_index(id_name_pairs) -> dict[str, tuple[str | None, tuple[str, ...]]]:
    """Index control-plane models by normalized name.

    Input: iterable of (model_id, model_name) from ListFoundationModels.
    Output: normalized name → (canonical id | None, all ids sharing the name).

    Multiple ids may legitimately share a name (context-window variants like
    `…-v1:0`, `…-v1:0:48k`, `…-v1:0:200k`). When every id in the group
    reduces to the same alias base they are one model: the canonical id is
    the shortest (ties broken lexically). When the ids reduce differently
    the name is ambiguous and maps to (None, ids) — unresolved, never a
    guess (Requirement 2.7).
    """
    groups: dict[str, set[str]] = {}
    for mid, name in id_name_pairs:
        n = normalize_name(name)
        if n:
            groups.setdefault(n, set()).add(mid)
    out: dict[str, tuple[str | None, tuple[str, ...]]] = {}
    for n, ids in groups.items():
        bases = {id_aliases(i)[-1] for i in ids}
        canonical = min(ids, key=lambda i: (len(i), i)) if len(bases) == 1 else None
        out[n] = (canonical, tuple(sorted(ids)))
    return out
