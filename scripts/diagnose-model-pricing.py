# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Answer "why is this model (un)priced?" in one command.

Runs the PRODUCTION pricing pipeline — the same `metering/pricing` parser,
identity join, and resolver the refresher and settle path use — against the
current AWS Bedrock offer files, and prints, per model:

  * SKUs (token price dimensions) found per service code,
  * each dimension's classification (direction/tier/routing/context) or the
    named reason it was excluded / left unclassified,
  * the identity join outcome (direct id, control-plane name, operator alias,
    or unmatched — and why),
  * the final effective standard in-region grid the resolver returns, and
  * if unpriced, the named reason (no-pricing-row / null-rates).

Read-only. Downloads the three public Bedrock offer files over HTTPS (no auth),
or reads them from a local directory with --offer-dir. No DynamoDB, no writes.

Usage:
  scripts/diagnose-model-pricing.py --model openai.gpt-5.6-luna
  scripts/diagnose-model-pricing.py --all [--region us-east-1]
  scripts/diagnose-model-pricing.py --all --offer-dir /tmp/pricing-offers

Requires: Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

# Import the production pricing package from the repo tree (metering/pricing).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "metering"))
from pricing import identity, offers  # noqa: E402
from pricing.resolver import resolve_rate  # noqa: E402

# Same three offer files, in the same precedence order, as the refresher.
SERVICES = ["AmazonBedrockFoundationModels", "AmazonBedrock", "AmazonBedrockService"]
OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{svc}/current/{region}/index.json"


def _urlopen_https(url: str, timeout: float):
    """urlopen restricted to https:// (mirror pricing-refresher._urlopen_https)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"refusing URL that is not https://<host>: {url[:80]}")
    return urllib.request.urlopen(url, timeout=timeout)  # nosec B310 — scheme allowlisted


def load_offer(svc: str, region: str, offer_dir: str | None) -> dict | None:
    """One offer file, from a local dir (svc*.json) or the public HTTPS endpoint."""
    if offer_dir:
        # Accept a few reasonable local file names for the same service.
        candidates = [
            os.path.join(offer_dir, f"{svc}.json"),
            os.path.join(offer_dir, f"{svc}-{region}.json"),
            os.path.join(offer_dir, f"{svc}_{region}.json"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                with open(path) as f:
                    return json.load(f)
        return None
    url = OFFER_URL.format(svc=svc, region=region)
    with _urlopen_https(url, timeout=180) as r:
        return json.loads(r.read())


def parse_all(region: str, offer_dir: str | None):
    """Return (parsed_by_service, raw_by_service, versions, missing).

    parsed_by_service: svc -> [ParsedRate]  (token dims that classified)
    raw_by_service:    svc -> [(usagetype, service_code)]  (all token dims seen,
                       for the per-SKU / classification report; excluded and
                       unclassified are derived by re-running classify here)
    """
    parsed_by_service: dict[str, list] = {}
    versions: dict[str, str] = {}
    missing: list[str] = []
    for svc in SERVICES:
        offer = load_offer(svc, region, offer_dir)
        if offer is None:
            missing.append(svc)
            parsed_by_service[svc] = []
            continue
        rates, version = offers.parse_offer(offer, region, svc)
        parsed_by_service[svc] = rates
        versions[svc] = version
    return parsed_by_service, versions, missing


def _merge_rate(grid: dict, r) -> None:
    cell = grid.setdefault(r.routing, {}).setdefault(r.tier, {}).setdefault(r.context, {})
    cell.setdefault(r.direction, r.usd_per_1m)  # first-wins: file order = precedence


def build_join(parsed_by_service: dict, cp_models: list, aliases: dict):
    """Mirror pricing-refresher._resolve: join parsed rates to model ids.

    Returns (resolved, unmatched):
      resolved:  canonical_id -> {"grid", "display_name", "provider", "via",
                                  "members": set(ids), "service_codes": set()}
      unmatched: price_list_name -> {"reason", "service_code", "grid"}
    """
    cp_index = identity.build_index(mid for mid, _, _ in cp_models)
    name_index = identity.build_name_index((mid, name) for mid, name, _ in cp_models)
    cp_by_id = {mid: name for mid, name, _ in cp_models}
    cp_provider = {mid: prov for mid, _, prov in cp_models}

    resolved: dict = {}
    unmatched: dict = {}
    for svc in SERVICES:
        for r in parsed_by_service.get(svc, []):
            if r.identity_kind == "id":
                canonical, via, members = r.identity, "direct-id", {r.identity}
                linked = cp_index.get(r.identity)
                if linked:
                    members.add(linked)
            else:
                pl_name = r.identity
                bound = aliases.get(pl_name)
                if bound:
                    canonical, via, members = bound, "alias", {bound}
                else:
                    hit = name_index.get(identity.normalize_name(pl_name))
                    if hit and hit[0]:
                        canonical, members = hit[0], set(hit[1])
                        via = "control-plane-name"
                    else:
                        u = unmatched.setdefault(pl_name, {
                            "reason": "ambiguous-match" if hit else "no-control-plane-match",
                            "class": "ambiguous" if hit else "no-match",
                            "service_code": svc,
                            "grid": {},
                        })
                        _merge_rate(u["grid"], r)
                        continue
            e = resolved.get(canonical)
            if e is None:
                e = resolved[canonical] = {
                    "grid": {},
                    "display_name": cp_by_id.get(canonical) or r.display_name,
                    "provider": cp_provider.get(canonical) or r.provider,
                    "via": via,
                    "members": set(),
                    "service_codes": set(),
                }
            e["members"].update(members)
            e["service_codes"].add(svc)
            _merge_rate(e["grid"], r)
    return resolved, unmatched


def effective_grid(pub_row: dict | None):
    """Resolver-computed standard grid: routing -> direction -> per-1M (or None)."""
    entry = {"override": None, "published": pub_row}
    out: dict = {}
    for routing in ("in_region", "global", "geo"):
        for direction in ("input", "output"):
            res = resolve_rate(entry, direction, routing=routing)
            if res.usd_per_1m is not None:
                out.setdefault(routing, {})[direction] = res.usd_per_1m
    return out


def _published_row(entry: dict) -> dict:
    """A PUBLISHED-shaped row the resolver reads (rates grid + _UNIT)."""
    # Decimal grid -> float for display; resolver reads either.
    grid = {
        routing: {
            tier: {
                ctx: {d: float(v) for d, v in dirs.items()}
                for ctx, dirs in ctxs.items()
            }
            for tier, ctxs in tiers.items()
        }
        for routing, tiers in entry["grid"].items()
    }
    return {"rates": grid, "_UNIT": "USD/1M-tokens", "offer_version": ""}


def per_service_skus(parsed_by_service: dict, model_id: str) -> dict:
    """svc -> [(usagetype, classification-tuple)] for SKUs whose embedded id or
    control-plane-joined identity is this model. Direct-id only for the SKU view
    (the join view below covers name-matched entries)."""
    out: dict[str, list] = {}
    for svc, rates in parsed_by_service.items():
        for r in rates:
            if r.identity_kind == "id" and r.identity == model_id:
                out.setdefault(svc, []).append(
                    (r.usagetype, f"{r.direction}/{r.tier}/{r.routing}/{r.context}")
                )
    return out


def diagnose_one(model_id: str, resolved: dict, parsed_by_service: dict) -> None:
    print(f"\n=== {model_id} ===")
    # 1. Which canonical entry (if any) owns this id?
    owner = None
    for canonical, e in resolved.items():
        if model_id == canonical or model_id in e["members"]:
            owner = (canonical, e)
            break

    # 2. SKUs found per service code (direct-id embedding).
    skus = per_service_skus(parsed_by_service, model_id)
    if skus:
        print("  SKUs found (direct-id embedded), per service code:")
        for svc, dims in skus.items():
            print(f"    {svc}: {len(dims)} token dimension(s)")
            for ut, cls in dims[:12]:
                print(f"      - {ut}  ->  {cls}")
            if len(dims) > 12:
                print(f"      ... (+{len(dims) - 12} more)")
    else:
        print("  SKUs found (direct-id embedded): NONE")

    # 3. Join outcome.
    if owner:
        canonical, e = owner
        print(f"  Join: matched -> canonical '{canonical}' via {e['via']} "
              f"(members: {sorted(e['members'])}; from files: {sorted(e['service_codes'])})")
        pub = _published_row(e)
    else:
        print("  Join: NO catalog entry — this id resolved to no priced grid.")
        pub = None

    # 4. Effective grid + priced verdict.
    eff = effective_grid(pub)
    if eff:
        print("  Effective standard grid (resolver, USD per 1M tokens):")
        for routing, dirs in eff.items():
            parts = ", ".join(f"{d}={v}" for d, v in dirs.items())
            print(f"    {routing}: {parts}")
    else:
        print("  Effective standard grid: EMPTY")

    res_in = resolve_rate({"override": None, "published": pub}, "input")
    res_out = resolve_rate({"override": None, "published": pub}, "output")
    priced = res_in.usd_per_1m is not None and res_out.usd_per_1m is not None
    if priced:
        print(f"  VERDICT: PRICED (source={res_in.source})")
    else:
        # Named reason (contract §2 vocabulary).
        if pub is None or not (pub.get("rates")):
            reason = "no-pricing-row"
        else:
            reason = "null-rates"
        print(f"  VERDICT: UNPRICED — reason={reason} "
              f"(input={res_in.usd_per_1m}, output={res_out.usd_per_1m})")


def report_dimension_health(parsed_by_service: dict) -> None:
    """Contract §1: excluded / unclassified are the parser's honesty signals.

    parse_offer today drops unclassified dims silently (pre-refactor package);
    degrade gracefully — re-run classify_usagetype over the raw offer to count
    what parse kept vs. dropped, so this script works on both package versions.
    """
    kept = sum(len(v) for v in parsed_by_service.values())
    print(f"\n=== dimension health ===\n  classified token dimensions: {kept}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose why a Bedrock model is (un)priced.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--model", help="a single Bedrock model id, e.g. openai.gpt-5.6-luna")
    g.add_argument("--all", action="store_true", help="diagnose every model with a priced grid")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--offer-dir", help="read offer files from a local dir instead of downloading")
    ap.add_argument("--control-plane", help="optional JSON file of [[id,name,provider],...] "
                    "to join name-matched Price List entries (default: none — id join only)")
    args = ap.parse_args()

    parsed_by_service, versions, missing = parse_all(args.region, args.offer_dir)
    if missing:
        print(f"WARNING: offer file(s) unavailable: {', '.join(missing)}", file=sys.stderr)
    if versions:
        print("offer versions: " + ", ".join(f"{k}={v}" for k, v in versions.items()), file=sys.stderr)

    cp_models: list = []
    if args.control_plane and os.path.isfile(args.control_plane):
        with open(args.control_plane) as f:
            cp_models = [tuple(x) for x in json.load(f)]

    resolved, unmatched = build_join(parsed_by_service, cp_models, aliases={})
    report_dimension_health(parsed_by_service)

    if args.model:
        diagnose_one(args.model, resolved, parsed_by_service)
    else:
        print(f"\n{len(resolved)} model(s) with a priced grid:")
        for canonical in sorted(resolved):
            diagnose_one(canonical, resolved, parsed_by_service)
        if unmatched:
            print(f"\n=== unmatched Price List names ({len(unmatched)}) ===")
            for name in sorted(unmatched):
                u = unmatched[name]
                print(f"  {name}: class={u.get('class')} reason={u['reason']} "
                      f"(from {u['service_code']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
