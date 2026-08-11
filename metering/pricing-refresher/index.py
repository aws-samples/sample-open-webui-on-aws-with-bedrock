# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Metering pricing refresher — the single automated price source.

Reads the three Bedrock Price List offer files for the deployment region
(public HTTPS, no credentials — Requirement 11.2), joins every token-priced
product to a Bedrock model id, and writes the pricing catalog the settle and
admission paths resolve from:

    PRICING#<model_id>  sk=PUBLISHED    rate grid, AWS-sourced (this Lambda)
    PRICING#<model_id>  sk=OVERRIDE     operator-owned — NEVER written here
    PRICING#_ALIAS      sk=<pl_name>    operator binding, read here
    PRICING#_UNMATCHED  sk=<pl_name>    review queue for unresolved rates
    PRICING#_CATALOG    sk=META         refresh marker

Identity resolution per parsed product (design §4.2), first hit wins:
  1. operator alias  (PRICING#_ALIAS — operator intent outranks inference)
  2. direct model id (the usage type embeds one, e.g. mantle shapes)
  3. control-plane name join (bedrock:ListFoundationModels, normalized
     name-to-name comparison — never applied to a model id)
Zero or multiple candidates → PRICING#_UNMATCHED, which never prices a
request (Requirements 2.7, 3.1, 3.2).

Catalog keys are materialized on the CATALOG side: each resolved model is
written under every unambiguous alias key of its id(s) (`identity.id_aliases`),
so the settle path does one exact GetItem on the invoked key and never
rewrites the id it is pricing (Requirements 2.5, 2.9). A key claimed by two
different models is not written as an alias for either (Requirement 2.7).

Garbage collection (Requirements 10.1, 10.2, 4.6):
  - PUBLISHED rows whose key is not a model id (legacy display-token keys the
    settle path can never read) — deleted unconditionally.
  - Legacy PROVIDER / DEFAULT rows (removed source tiers) — deleted
    unconditionally.
  - Model-id-shaped PUBLISHED rows absent from this run — deleted only when
    every offer file fetched successfully.
  - OVERRIDE and _ALIAS rows — never touched.

Env: TABLE, REGION (deployment region, default us-east-1).
"""

import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request

import boto3

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..")):  # Lambda task root / repo tree
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pricing import identity, offers  # noqa: E402
from pricing.resolver import UNIT_PER_1M  # noqa: E402

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
REGION = os.environ.get("REGION", "us-east-1")
# Precedence order for first-wins leaf merges: the marketplace file publishes
# per-1M natively and carries the modern Anthropic grid; the mantle/legacy and
# Service files fill the remainder.
SERVICES = ["AmazonBedrockFoundationModels", "AmazonBedrock", "AmazonBedrockService"]
OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{svc}/current/{region}/index.json"

REASON_NO_MATCH = "no-control-plane-match"
REASON_AMBIGUOUS = "ambiguous-match"

ddb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")
# The bedrock client resolves auth at construction (bearer-token support), so
# build it lazily to keep module import free of credential lookups (tests).
_bedrock = None


def _bedrock_client():
    global _bedrock  # noqa: PLW0603 — per-container client cache
    if _bedrock is None:
        _bedrock = boto3.client("bedrock", region_name=REGION)
    return _bedrock


def _metric(name: str, value: float = 1, unit: str = "Count"):
    try:
        cw.put_metric_data(Namespace="Metering", MetricData=[{"MetricName": name, "Value": value, "Unit": unit}])
    except Exception as e:  # noqa: BLE001 — metrics must never break the refresh
        log.warning(f"metric {name} failed: {e}")


def _urlopen_https(target, timeout: float):
    """urlopen restricted to https (Bandit B310 / Ruff S310).

    urlopen honors every scheme its installed openers support — `file:`, `ftp:`,
    and custom schemes included — so a URL that is ever influenced by
    configuration or another service could be turned into a local-file read.
    REGION below comes from the environment and is interpolated into the offer
    URL, so the scheme is checked rather than assumed.
    """
    url = target.full_url if isinstance(target, urllib.request.Request) else str(target)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"refusing URL that is not https://<host>: {url[:80]}")
    return urllib.request.urlopen(target, timeout=timeout)  # nosec B310 — scheme allowlisted above


def _fetch_offer(svc: str) -> dict:
    url = OFFER_URL.format(svc=svc, region=REGION)
    with _urlopen_https(url, timeout=180) as r:
        return json.loads(r.read())


def _list_cp_models() -> list:
    """(model_id, model_name, provider) triples from the Bedrock control plane."""
    resp = _bedrock_client().list_foundation_models()
    return [
        (m.get("modelId", ""), m.get("modelName", ""), m.get("providerName", ""))
        for m in resp.get("modelSummaries", [])
        if m.get("modelId")
    ]


def _query_pk(pk: str) -> list:
    items, lek = [], None
    while True:
        kwargs = {
            "TableName": TABLE,
            "KeyConditionExpression": "pk = :p",
            "ExpressionAttributeValues": {":p": {"S": pk}},
        }
        if lek:
            kwargs["ExclusiveStartKey"] = lek
        page = ddb.query(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
    return items


def _load_aliases() -> dict:
    """Operator bindings: Price List name → model id (one Query, Req 2.4)."""
    out = {}
    for it in _query_pk("PRICING#_ALIAS"):
        name = it.get("sk", {}).get("S", "")
        mid = it.get("model_id", {}).get("S", "")
        if name and mid and identity.MODEL_ID_RE.match(mid):
            out[name] = mid
    return out


# ── resolution and grid accumulation ─────────────────────────────────────────

def _new_entry(display: str, provider: str, via: str, service_code: str, pl_name: str | None):
    return {
        "grid": {},
        "display_name": display,
        "provider": provider,
        "via": via,
        "service_code": service_code,
        "price_list_name": pl_name,
        "effective_date": "",
        "extra_ids": set(),
    }


def _merge_rate(entry: dict, r) -> None:
    cell = (entry["grid"].setdefault(r.routing, {})
            .setdefault(r.tier, {}).setdefault(r.context, {}))
    cell.setdefault(r.direction, r.usd_per_1m)  # first-wins: file order is precedence
    if not entry["effective_date"] and r.effective_date:
        entry["effective_date"] = r.effective_date
    if not entry["provider"] and r.provider:
        entry["provider"] = r.provider


def _resolve(parsed_by_service: dict, aliases: dict, cp_models: list) -> tuple[dict, dict]:
    """Join parsed rates to model ids → (resolved by canonical id, unmatched by name)."""
    cp_index = identity.build_index(mid for mid, _, _ in cp_models)
    name_index = identity.build_name_index((mid, name) for mid, name, _ in cp_models)
    cp_provider = {mid: prov for mid, _, prov in cp_models}
    cp_by_id = {mid: name for mid, name, _ in cp_models}

    resolved: dict = {}
    unmatched: dict = {}
    for svc in SERVICES:  # deterministic file precedence
        for r in parsed_by_service.get(svc, []):
            if r.identity_kind == "id":
                canonical, via, pl_name, group_ids = r.identity, "direct-id", None, [r.identity]
                linked = cp_index.get(r.identity)
                if linked:
                    group_ids.append(linked)
            else:
                pl_name = r.identity
                bound = aliases.get(pl_name)
                if bound:
                    canonical, via, group_ids = bound, "alias", [bound]
                else:
                    hit = name_index.get(identity.normalize_name(pl_name))
                    if hit and hit[0]:
                        canonical, group_ids = hit[0], list(hit[1])
                        via = "control-plane-name"
                    else:
                        u = unmatched.setdefault(pl_name, {
                            "price_list_name": pl_name,
                            "provider": r.provider,
                            "service_code": svc,
                            "reason": REASON_AMBIGUOUS if hit else REASON_NO_MATCH,
                            "grid": {},
                        })
                        _merge_rate({"grid": u["grid"], "effective_date": "", "provider": ""}, r)
                        continue
            entry = resolved.get(canonical)
            if entry is None:
                display = cp_by_id.get(canonical) or r.display_name
                provider = cp_provider.get(canonical) or r.provider
                entry = resolved[canonical] = _new_entry(display, provider, via, svc, pl_name)
            if pl_name and not entry["price_list_name"]:
                entry["price_list_name"] = pl_name
            entry["extra_ids"].update(group_ids)
            _merge_rate(entry, r)
    return resolved, unmatched


_VIA_ORDER = {"direct-id": 0, "alias": 1, "control-plane-name": 2}


def _merge_canonicals(resolved: dict) -> dict:
    """Collapse entries that describe the same model under different ids.

    A mantle-priced id and its control-plane twin (e.g. `openai.gpt-oss-120b`
    priced directly, `openai.gpt-oss-120b-1:0` from the name join) share member
    ids after alias linking — they are one model and must be one catalog entry.
    Strongest identity wins the canonical slot (direct-id > alias > name join);
    grids merge first-wins in that same order.
    """
    owner: dict = {}
    for canonical in sorted(
        resolved, key=lambda c: (_VIA_ORDER.get(resolved[c]["via"], 9), len(c), c)
    ):
        entry = resolved[canonical]
        members = {canonical, *entry["extra_ids"]}
        target = next((owner[m] for m in members if m in owner), None)
        if target is None:
            for m in members:
                owner[m] = canonical
            continue
        tgt = resolved[target]
        for routing, tiers in entry["grid"].items():
            for tier, ctxs in tiers.items():
                for ctx, dirs in ctxs.items():
                    cell = (tgt["grid"].setdefault(routing, {})
                            .setdefault(tier, {}).setdefault(ctx, {}))
                    for d, v in dirs.items():
                        cell.setdefault(d, v)
        tgt["extra_ids"].update(members)
        if not tgt["price_list_name"] and entry["price_list_name"]:
            tgt["price_list_name"] = entry["price_list_name"]
        if not tgt["effective_date"] and entry["effective_date"]:
            tgt["effective_date"] = entry["effective_date"]
        for m in members:
            owner[m] = target
        del resolved[canonical]
    return resolved


def _materialize_keys(resolved: dict) -> dict:
    """canonical → set of catalog keys (canonical + unambiguous alias keys).

    Catalog-side expansion (Requirement 2.5): every id in the model's group is
    expanded through `id_aliases`. A key claimed by two different models keeps
    only its own canonical row — the alias claimants are dropped so an exact
    settle-time lookup can never land on the wrong model (Requirement 2.7).
    """
    claims: dict = {}
    for canonical, entry in resolved.items():
        keys = set()
        for mid in {canonical, *entry["extra_ids"]}:
            keys.update(identity.id_aliases(mid))
        for k in keys:
            claims.setdefault(k, set()).add(canonical)
    out = {c: set() for c in resolved}
    for key, claimants in claims.items():
        if key in resolved:
            out[key].add(key)  # a canonical always keeps its own key
        elif len(claimants) == 1:
            out[next(iter(claimants))].add(key)
        else:
            log.info(f"alias key {key} is ambiguous between {sorted(claimants)}; not materialized")
    return out


# ── DynamoDB writes ──────────────────────────────────────────────────────────

def _grid_to_attr(grid: dict) -> dict:
    return {"M": {
        routing: {"M": {
            tier: {"M": {
                ctx: {"M": {d: {"N": offers.decimal_str(v)} for d, v in dirs.items()}}
                for ctx, dirs in ctxs.items()
            }}
            for tier, ctxs in tiers.items()
        }}
        for routing, tiers in grid.items()
    }}


def _write_published(resolved: dict, keys_by_canonical: dict, versions: dict,
                     generation: int, now: int) -> set:
    written = set()
    for canonical, entry in resolved.items():
        if not entry["grid"]:
            continue
        rates_attr = _grid_to_attr(entry["grid"])
        for key in sorted(keys_by_canonical.get(canonical, {canonical})):
            item = {
                "pk": {"S": f"PRICING#{key}"},
                "sk": {"S": "PUBLISHED"},
                "model_id": {"S": key},
                "canonical_id": {"S": canonical},
                "display_name": {"S": str(entry["display_name"])[:128]},
                "provider": {"S": str(entry["provider"])[:64]},
                "source": {"S": "aws-published"},
                "_UNIT": {"S": UNIT_PER_1M},
                "resolved_via": {"S": entry["via"]},
                "service_code": {"S": entry["service_code"]},
                "region": {"S": REGION},
                "offer_version": {"S": str(versions.get(entry["service_code"], "unknown"))},
                "refresh_generation": {"N": str(generation)},
                "updated_at": {"N": str(now)},
                "rates": rates_attr,
            }
            if entry["effective_date"]:
                item["effective_date"] = {"S": str(entry["effective_date"])[:64]}
            if entry["price_list_name"]:
                item["price_list_name"] = {"S": str(entry["price_list_name"])[:128]}
            if key != canonical:
                item["alias_of"] = {"S": canonical}
            ddb.put_item(TableName=TABLE, Item=item)
            written.add(key)
    return written


def _write_unmatched(unmatched: dict, generation: int, now: int) -> None:
    for name, u in unmatched.items():
        ddb.put_item(TableName=TABLE, Item={
            "pk": {"S": "PRICING#_UNMATCHED"},
            "sk": {"S": str(name)[:512]},
            "price_list_name": {"S": str(name)[:128]},
            "provider": {"S": str(u.get("provider", ""))[:64]},
            "service_code": {"S": str(u.get("service_code", ""))[:64]},
            "reason": {"S": u.get("reason", REASON_NO_MATCH)},
            "candidate_rates": _grid_to_attr(u.get("grid", {})),
            "refresh_generation": {"N": str(generation)},
            "updated_at": {"N": str(now)},
        })


def _gc(written_keys: set, current_unmatched: set, full_success: bool) -> dict:
    """Delete rows the new pricing path can never read (design §6 step 8)."""
    stats = {"legacy_key": 0, "provider_default": 0, "stale": 0, "stale_unmatched": 0}
    if not written_keys:  # defensive: an empty run must never trigger deletion
        log.warning("no keys written this run; skipping garbage collection")
        return stats
    items, lek = [], None
    while True:
        kwargs = {
            "TableName": TABLE,
            "FilterExpression": "begins_with(pk, :p)",
            "ExpressionAttributeValues": {":p": {"S": "PRICING#"}},
            "ProjectionExpression": "pk, sk",
        }
        if lek:
            kwargs["ExclusiveStartKey"] = lek
        page = ddb.scan(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
    for it in items:
        pk = it.get("pk", {}).get("S", "")
        sk = it.get("sk", {}).get("S", "")
        key = pk.removeprefix("PRICING#")
        reason = None
        if sk == "OVERRIDE" or key in ("_ALIAS", "_CATALOG"):
            continue  # operator-owned / marker rows — never touched (Req 10.2)
        if key == "_UNMATCHED":
            if full_success and sk not in current_unmatched:
                reason = "stale_unmatched"
        elif sk in ("PROVIDER", "DEFAULT"):
            reason = "provider_default"  # removed source tiers (Req 9.4)
        elif sk == "PUBLISHED":
            if not identity.MODEL_ID_RE.match(key):
                reason = "legacy_key"  # unreadable by the settle path (Req 10.1)
            elif full_success and key not in written_keys:
                reason = "stale"  # absent from a fully-successful refresh
        if reason:
            ddb.delete_item(TableName=TABLE, Key={"pk": {"S": pk}, "sk": {"S": sk}})
            stats[reason] += 1
    return stats


def _read_generation() -> int:
    try:
        item = ddb.get_item(
            TableName=TABLE,
            Key={"pk": {"S": "PRICING#_CATALOG"}, "sk": {"S": "META"}},
        ).get("Item") or {}
        return int(item.get("refresh_generation", {}).get("N", "0"))
    except Exception:  # noqa: BLE001
        return 0


def handler(event, context):
    started = time.time()
    try:
        generation = _read_generation() + 1
        now = int(started)
        aliases = _load_aliases()

        parsed_by_service, versions, failed = {}, {}, []
        for svc in SERVICES:
            try:
                offer = _fetch_offer(svc)
                parsed_by_service[svc], versions[svc] = offers.parse_offer(offer, REGION, svc)
            except Exception as e:  # noqa: BLE001 — one file must not sink the rest (Req 4.6)
                log.warning(f"{svc} offer file unavailable: {e}")
                failed.append(svc)
        if len(failed) == len(SERVICES):
            raise RuntimeError("all offer files unavailable")

        cp_models = _list_cp_models()
        resolved, unmatched = _resolve(parsed_by_service, aliases, cp_models)
        resolved = _merge_canonicals(resolved)
        keys_by_canonical = _materialize_keys(resolved)

        written_keys = _write_published(resolved, keys_by_canonical, versions, generation, now)
        _write_unmatched(unmatched, generation, now)
        gc_stats = _gc(written_keys, set(unmatched.keys()), full_success=not failed)

        partial = bool(failed)
        duration_ms = int((time.time() - started) * 1000)
        ddb.put_item(TableName=TABLE, Item={
            "pk": {"S": "PRICING#_CATALOG"},
            "sk": {"S": "META"},
            "offer_versions": {"M": {s: {"S": str(v)} for s, v in versions.items()}},
            "region": {"S": REGION},
            "refresh_generation": {"N": str(generation)},
            "model_count": {"N": str(len(resolved))},
            "row_count": {"N": str(len(written_keys))},
            "unmatched_count": {"N": str(len(unmatched))},
            "alias_count": {"N": str(len(aliases))},
            "refreshed_at": {"N": str(now)},
            "duration_ms": {"N": str(duration_ms)},
            "partial": {"BOOL": partial},
            "failed_services": {"L": [{"S": s} for s in failed]},
        })

        _metric("PricingRefreshModels", len(resolved))
        _metric("PricingUnmatched", len(unmatched))
        if partial:
            # rates stay intact (GC skipped stale deletion), but the operator
            # must see that a source went dark (Req 4.4/4.6)
            _metric("PricingRefreshFailure")
        summary = {
            "ok": True, "partial": partial, "generation": generation,
            "models": len(resolved), "rows": len(written_keys),
            "unmatched": len(unmatched), "aliases": len(aliases),
            "versions": versions, "failed_services": failed,
            "gc": gc_stats, "duration_ms": duration_ms,
        }
        log.info(json.dumps(summary))
        return summary
    except Exception:
        _metric("PricingRefreshFailure")
        log.exception("pricing refresh failed")
        raise
