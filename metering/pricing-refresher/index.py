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
from decimal import Decimal

import boto3
from botocore.auth import SigV4Auth  # noqa: E402
from botocore.awsrequest import AWSRequest  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..")):  # Lambda task root / repo tree
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pricing import identity, offers  # noqa: E402
from pricing.resolver import UNIT_PER_1M, resolve_rate, unwrap_item  # noqa: E402

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
REGION = os.environ.get("REGION", "us-east-1")
# Region whose bedrock-mantle catalog the gateway fronts (may differ from the
# offer-file/table region). Defaults to REGION when unset.
MANTLE_REGION = os.environ.get("MANTLE_REGION") or REGION
# Interceptor Lambda whose MODEL_CAPS env var carries the SERVED lane lists
# (wired by CDK). Absent ⇒ metering deployed without the model refresher; the
# coverage join then falls back to the packaged capability matrix (§8).
INTERCEPTOR_FUNCTION_NAME = os.environ.get("INTERCEPTOR_FUNCTION_NAME", "").strip()
# Lanes, mirroring gateway/refresher/index.py::LANES and the interceptor's CAPS.
LANES = ("chat_completions", "responses", "messages")
# Precedence order for first-wins leaf merges: the marketplace file publishes
# per-1M natively and carries the modern Anthropic grid; the mantle/legacy and
# Service files fill the remainder.
SERVICES = ["AmazonBedrockFoundationModels", "AmazonBedrock", "AmazonBedrockService"]
OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{svc}/current/{region}/index.json"

REASON_NO_MATCH = "no-control-plane-match"
REASON_AMBIGUOUS = "ambiguous-match"
# D8 unmatched-row classification (contract §4): no-match = zero control-plane
# candidates (historical); ambiguous = >1 candidates, refused to guess (actionable).
CLASS_NO_MATCH = "no-match"
CLASS_AMBIGUOUS = "ambiguous"

ddb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")
_lambda = boto3.client("lambda")
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


def _apply_meta(entry: dict, r) -> None:
    """Carry effective_date/provider from a rate onto the resolved entry.

    (The grid merge itself is delegated to the package's offers.merge_rate,
    which owns the D7 identical→silent / conflict→MAX rule — the refresher no
    longer reimplements cell aggregation.)"""
    if not entry["effective_date"] and r.effective_date:
        entry["effective_date"] = r.effective_date
    if not entry["provider"] and r.provider:
        entry["provider"] = r.provider


def _resolve(parsed_by_service: dict, aliases: dict, cp_models: list,
             acc: "offers.ParseAccounting | None" = None) -> tuple[dict, dict]:
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
                            # D8 (contract §4): actionable if the join found >1
                            # control-plane candidate and refused to guess.
                            "class": CLASS_AMBIGUOUS if hit else CLASS_NO_MATCH,
                            "grid": {},
                        })
                        # Candidate rates for the review queue — no accounting
                        # sink (conflicts on an unresolved name aren't actionable).
                        offers.merge_rate(u["grid"], r)
                        continue
            entry = resolved.get(canonical)
            if entry is None:
                display = cp_by_id.get(canonical) or r.display_name
                provider = cp_provider.get(canonical) or r.provider
                entry = resolved[canonical] = _new_entry(display, provider, via, svc, pl_name)
            if pl_name and not entry["price_list_name"]:
                entry["price_list_name"] = pl_name
            entry["extra_ids"].update(group_ids)
            # Package-owned D7 merge (identical→silent, conflict→MAX + record).
            offers.merge_rate(entry["grid"], r, acc, model_id=canonical)
            _apply_meta(entry, r)
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
                        # D7: keep the maximum on a same-leaf conflict (matches
                        # offers.merge_rate); a new leaf is taken as-is.
                        cell[d] = v if d not in cell else max(cell[d], v)
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
    # offers.merge_rate stores float leaves; decimal_str needs a Decimal, so
    # coerce via str() to preserve the published magnitude exactly.
    def _n(v):
        return {"N": offers.decimal_str(v if isinstance(v, Decimal) else Decimal(str(v)))}
    return {"M": {
        routing: {"M": {
            tier: {"M": {
                ctx: {"M": {d: _n(v) for d, v in dirs.items()}}
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
            # D8: no-match (historical) vs ambiguous (actionable). Defaults to
            # no-match so a legacy row missing the field reads as historical.
            "class": {"S": u.get("class", CLASS_NO_MATCH)},
            "candidate_rates": _grid_to_attr(u.get("grid", {})),
            "refresh_generation": {"N": str(generation)},
            "updated_at": {"N": str(now)},
        })


def _gc(written_keys: set, current_unmatched: set, full_success: bool,
        prior_keys: list | None, prior_unmatched: list | None) -> dict:
    """Delete rows the new pricing path can never read (design §6 step 8).

    D9: when the previous catalog meta carries its written model-key list
    (`prior_keys`), garbage collection is a targeted diff of that list against
    this run's `written_keys` — no full table Scan. The prior meta's model-key
    list only ever held valid model-id PUBLISHED keys, so legacy display-token
    keys and removed PROVIDER/DEFAULT tiers cannot appear there; the diff path
    therefore skips them (they were already collected by an earlier Scan run,
    and a fresh install with meta present has none). The legacy full Scan runs
    only when the prior meta is absent (first refresh after upgrade).
    """
    stats = {"legacy_key": 0, "provider_default": 0, "stale": 0, "stale_unmatched": 0}
    if not written_keys:  # defensive: an empty run must never trigger deletion
        log.warning("no keys written this run; skipping garbage collection")
        return stats

    if prior_keys is not None:
        # Targeted diff (D9): only prior model-id keys absent from this run.
        if full_success:
            for key in prior_keys:
                if key not in written_keys and identity.MODEL_ID_RE.match(key):
                    ddb.delete_item(TableName=TABLE,
                                    Key={"pk": {"S": f"PRICING#{key}"}, "sk": {"S": "PUBLISHED"}})
                    stats["stale"] += 1
            for name in (prior_unmatched or []):
                if name not in current_unmatched:
                    ddb.delete_item(TableName=TABLE,
                                    Key={"pk": {"S": "PRICING#_UNMATCHED"}, "sk": {"S": str(name)}})
                    stats["stale_unmatched"] += 1
        return stats

    # Legacy full Scan (prior meta absent — first refresh after upgrade).
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


def _read_prior_meta() -> tuple[int, list | None, list | None]:
    """Return (generation, prior model-key list | None, prior unmatched list | None).

    The model-key / unmatched lists power the D9 targeted GC. `None` means the
    prior meta predates D9 (no `model_keys`) — GC falls back to a full Scan.
    """
    try:
        item = ddb.get_item(
            TableName=TABLE,
            Key={"pk": {"S": "PRICING#_CATALOG"}, "sk": {"S": "META"}},
        ).get("Item") or {}
    except Exception:  # noqa: BLE001
        return 0, None, None
    gen = int(item.get("refresh_generation", {}).get("N", "0"))
    keys = item.get("model_keys")
    prior_keys = [k.get("S", "") for k in keys["L"]] if keys and "L" in keys else None
    un = item.get("unmatched_names")
    prior_unmatched = [k.get("S", "") for k in un["L"]] if un and "L" in un else None
    return gen, prior_keys, prior_unmatched


# ── D1/D2 gateway↔pricing coverage join (contract §2, §8) ─────────────────────

def _mantle_catalog() -> list:
    """Available model ids from the gateway's bedrock-mantle catalog.

    Mirrors gateway/refresher/probe_core.fetch_catalog(): a SigV4 GET (signing
    service "bedrock") against the mantle /v1/models endpoint, keeping only
    status=="available". Not imported from gateway/ — that tree is not staged
    into the metering Lambda — so the minimal equivalent is inlined here with
    credit to probe_core as the pattern source. Signs via botocore (already a
    boto3 dependency) and sends over urllib, so no `requests` vendoring is
    needed in this Lambda's asset.
    """
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    url = f"https://bedrock-mantle.{MANTLE_REGION}.api.aws/v1/models"
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(creds, "bedrock", MANTLE_REGION).add_auth(req)
    request = urllib.request.Request(url, headers=dict(req.headers), method="GET")
    with _urlopen_https(request, timeout=30) as r:
        data = json.loads(r.read())
    return [m["id"] for m in data.get("data", [])
            if m.get("status", "available") == "available"]


def _bundled_caps() -> dict:
    """Capability matrix packaged beside the handler (config/model-capabilities.json).

    This is the SAME source of truth the interceptor falls back to when its
    MODEL_CAPS env var is unset (gateway/metering-interceptor reads
    os.environ["MODEL_CAPS"] else _bundled("model-capabilities.json")). Staged
    into this Lambda's asset by CDK; resolvable in the task root or repo tree.
    """
    for cand in (
        os.path.join(_HERE, "model-capabilities.json"),
        os.path.join(_HERE, "..", "..", "config", "model-capabilities.json"),
    ):
        try:
            with open(cand) as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def _served_caps() -> dict:
    """The SERVED lane→[model ids] map (contract §8).

    Primary: read the interceptor Lambda's MODEL_CAPS env var via
    lambda:GetFunctionConfiguration (same parse as
    gateway/refresher/index.py::_current_caps). Fallback (env var unset ⇒
    metering deployed without the model refresher): the packaged capability
    matrix the interceptor itself falls back to. Coverage must work in BOTH
    enableModelRefresh states, so any failure degrades to the bundled file.
    """
    if INTERCEPTOR_FUNCTION_NAME:
        try:
            cfg = _lambda.get_function_configuration(FunctionName=INTERCEPTOR_FUNCTION_NAME)
            raw = (cfg.get("Environment", {}).get("Variables", {}) or {}).get("MODEL_CAPS")
            if raw:
                return json.loads(raw)
            log.info("interceptor MODEL_CAPS env unset; using packaged capability matrix")
        except Exception as e:  # noqa: BLE001 — coverage must not fail the refresh
            log.warning(f"served MODEL_CAPS read failed ({e}); using packaged matrix")
    return _bundled_caps()


def _lanes_by_model(caps: dict) -> dict:
    """Invert {lane: [ids]} → {model_id: sorted[lanes]} over the known lanes."""
    out: dict = {}
    for lane in LANES:
        for mid in caps.get(lane, []) or []:
            out.setdefault(mid, set()).add(lane)
    return {mid: sorted(lanes) for mid, lanes in out.items()}


def _override_rows(universe_keys: set) -> dict:
    """OVERRIDE rows for the coverage universe, keyed by catalog key.

    One BatchGetItem round (universe is ~60 models; chunked at the 100-key
    API limit). Operator overrides are owned by the admin API — the coverage
    join only READS them, so a publishing-gap model priced by override counts
    as priced (live 2026-08-21 finding: coverage reported the 7 overridden
    openai.* models unpriced because this read was missing).
    """
    keys = [{"pk": {"S": f"PRICING#{k}"}, "sk": {"S": "OVERRIDE"}}
            for k in sorted(universe_keys)]
    out: dict = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            resp = ddb.batch_get_item(RequestItems={TABLE: {"Keys": chunk}})
        except Exception as e:  # noqa: BLE001 — coverage degrades, never raises
            log.warning(f"override batch read failed: {e}")
            continue
        for it in resp.get("Responses", {}).get(TABLE, []):
            pk = it.get("pk", {}).get("S", "")
            out[pk.removeprefix("PRICING#")] = it
        # single retry for unprocessed keys; a miss degrades to unpriced=alarm
        unproc = resp.get("UnprocessedKeys", {}).get(TABLE, {}).get("Keys")
        if unproc:
            try:
                resp2 = ddb.batch_get_item(RequestItems={TABLE: {"Keys": unproc}})
                for it in resp2.get("Responses", {}).get(TABLE, []):
                    pk = it.get("pk", {}).get("S", "")
                    out[pk.removeprefix("PRICING#")] = it
            except Exception:  # noqa: BLE001
                pass
    return out


def _priced(resolved: dict, keys_by_canonical: dict, model_key: str,
            override_row: dict | None = None) -> tuple[bool, str | None]:
    """(priced, source) for a catalog key via the production resolver.

    Priced ⇔ the resolver returns a non-null input AND output rate from the
    SAME entry shape the settle path uses: operator OVERRIDE outranks the
    published grid. `source` is the winning label
    (override|aws-published|None).
    """
    canonical = None
    for c, keys in keys_by_canonical.items():
        if model_key == c or model_key in keys:
            canonical = c
            break
    entry_row = resolved.get(canonical) if canonical else None
    published = ({"rates": entry_row["grid"], "_UNIT": UNIT_PER_1M}
                 if entry_row and entry_row.get("grid") else None)
    override = unwrap_item(override_row) if override_row else None
    if not published and not override:
        return False, None
    entry = {"override": override, "published": published}
    rin = resolve_rate(entry, "input")
    rout = resolve_rate(entry, "output")
    if rin.usd_per_1m is not None and rout.usd_per_1m is not None:
        # the resolver names the winning side (override outranks published)
        return True, ("override" if "override" in (rin.source, rout.source)
                      else "aws-published")
    if rin.usd_per_1m is not None or rout.usd_per_1m is not None:
        partial = rin if rin.usd_per_1m is not None else rout
        return False, partial.source if partial.source != "unpriced" else None
    return False, None


def _build_coverage(resolved: dict, keys_by_canonical: dict, caps: dict,
                    catalog_ids: set, catalog_error: str | None) -> dict:
    """Coverage item (contract §2): per-model listed/available/priced + counts.

    Universe = union(served MODEL_CAPS models, catalog-available models). The
    resolver prices each via its exact catalog key (parse_model_ref strips any
    routing scope). reason: ok | no-pricing-row | null-rates | stale-caps.
    """
    lanes_by_model = _lanes_by_model(caps)
    listed_ids = set(lanes_by_model)
    universe = sorted(listed_ids | catalog_ids)
    overrides = _override_rows({identity.parse_model_ref(m).key for m in universe})

    models = []
    counts = {"invokable": 0, "invokable_priced": 0, "invokable_unpriced": 0,
              "listed_not_available": 0}
    for mid in universe:
        key = identity.parse_model_ref(mid).key
        listed = mid in listed_ids
        available = mid in catalog_ids
        priced, source = _priced(resolved, keys_by_canonical, key,
                                 override_row=overrides.get(key))
        if source == "override":
            source = "OVERRIDE"  # contract §2 spelling
        if listed and not available:
            reason = "stale-caps"
        elif priced:
            reason = "ok"
        elif source == "aws-published":
            reason = "null-rates"
        else:
            reason = "no-pricing-row"
        models.append({
            "id": mid, "lanes": lanes_by_model.get(mid, []),
            "listed": listed, "catalog_available": available,
            "priced": priced, "source": source, "reason": reason,
        })
        if available:
            counts["invokable"] += 1
            counts["invokable_priced" if priced else "invokable_unpriced"] += 1
        elif listed:
            counts["listed_not_available"] += 1
    return {"counts": counts, "models": models, "catalog_error": catalog_error}


def _coverage_attr(models: list) -> dict:
    return {"L": [{"M": {
        "id": {"S": str(m["id"])[:256]},
        "lanes": {"L": [{"S": ln} for ln in m["lanes"]]},
        "listed": {"BOOL": m["listed"]},
        "catalog_available": {"BOOL": m["catalog_available"]},
        "priced": {"BOOL": m["priced"]},
        "source": ({"S": m["source"]} if m["source"] else {"NULL": True}),
        "reason": {"S": m["reason"]},
    }} for m in models]}


def _write_coverage(coverage: dict, generation: int, now: int, region: str) -> None:
    counts = coverage["counts"]
    item = {
        "pk": {"S": "PRICING#_COVERAGE"},
        "sk": {"S": "META"},
        "computed_at": {"N": str(now)},
        "refresh_generation": {"N": str(generation)},
        "region": {"S": region},
        "counts": {"M": {k: {"N": str(v)} for k, v in counts.items()}},
        "models": _coverage_attr(coverage["models"]),
    }
    if coverage.get("catalog_error"):
        item["error"] = {"S": str(coverage["catalog_error"])[:256]}
    ddb.put_item(TableName=TABLE, Item=item)


def _emit_coverage_metrics(coverage: dict, unmatched: dict, rate_conflicts: int,
                           unclassified: int) -> None:
    """Contract §3 gauges, emitted every run (so alarms clear when clean)."""
    _metric("UnpricedGatewayModels", coverage["counts"]["invokable_unpriced"])
    actionable = sum(1 for u in unmatched.values() if u.get("class") == CLASS_AMBIGUOUS)
    _metric("PricingUnmatchedActionable", actionable)
    _metric("PricingDimensionUnclassified", unclassified)
    _metric("PricingRateConflict", rate_conflicts)
    # Heartbeat for PricingCoverageStaleAlarm: the value gauges above use
    # NOT_BREACHING (value alarms), so a refresher that silently stops running
    # would otherwise read healthy forever. This datapoint's ABSENCE is the
    # staleness signal (adversarial review MAJOR-1).
    _metric("PricingCoverageComputed", 1)


def handler(event, context):
    started = time.time()
    try:
        prior_gen, prior_keys, prior_unmatched = _read_prior_meta()
        generation = prior_gen + 1
        now = int(started)
        aliases = _load_aliases()

        parsed_by_service, versions, failed = {}, {}, []
        # Package-owned classification + merge accounting (contract §1 / D5, D7):
        # excluded / unclassified across parse, rate_conflicts across merge.
        acc = offers.ParseAccounting()
        for svc in SERVICES:
            try:
                offer = _fetch_offer(svc)
                parsed_by_service[svc], versions[svc] = offers.parse_offer(offer, REGION, svc, acc)
            except Exception as e:  # noqa: BLE001 — one file must not sink the rest (Req 4.6)
                log.warning(f"{svc} offer file unavailable: {e}")
                failed.append(svc)
        if len(failed) == len(SERVICES):
            raise RuntimeError("all offer files unavailable")

        cp_models = _list_cp_models()
        resolved, unmatched = _resolve(parsed_by_service, aliases, cp_models, acc)
        resolved = _merge_canonicals(resolved)
        keys_by_canonical = _materialize_keys(resolved)

        written_keys = _write_published(resolved, keys_by_canonical, versions, generation, now)
        _write_unmatched(unmatched, generation, now)
        gc_stats = _gc(written_keys, set(unmatched.keys()), full_success=not failed,
                       prior_keys=prior_keys, prior_unmatched=prior_unmatched)

        # ── D1/D2 coverage join (must never fail the whole refresh) ──────────
        catalog_ids, catalog_error = set(), None
        try:
            catalog_ids = set(_mantle_catalog())
        except Exception as e:  # noqa: BLE001 — partial coverage from what is known (§8)
            catalog_error = f"{e.__class__.__name__}: {e}"
            log.warning(f"mantle catalog fetch failed; recording partial coverage: {catalog_error}")
        caps = _served_caps()
        coverage = _build_coverage(resolved, keys_by_canonical, caps, catalog_ids, catalog_error)
        _write_coverage(coverage, generation, now, REGION)

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
            # Classification/merge accounting counts (contract §1 / D5, D7).
            "excluded_count": {"N": str(len(acc.excluded))},
            "unclassified_count": {"N": str(len(acc.unclassified))},
            "rate_conflict_count": {"N": str(len(acc.rate_conflicts))},
            # D9: the written model-key + unmatched lists power the next run's
            # targeted GC (diff instead of a full table Scan).
            "model_keys": {"L": [{"S": k} for k in sorted(written_keys)]},
            "unmatched_names": {"L": [{"S": str(n)[:512]} for n in sorted(unmatched)]},
        })

        _metric("PricingRefreshModels", len(resolved))
        _metric("PricingUnmatched", len(unmatched))
        _emit_coverage_metrics(coverage, unmatched, len(acc.rate_conflicts), len(acc.unclassified))
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
            "coverage": coverage["counts"], "coverage_error": catalog_error,
            "excluded": len(acc.excluded), "unclassified": len(acc.unclassified),
            "rate_conflicts": len(acc.rate_conflicts),
        }
        log.info(json.dumps(summary))
        return summary
    except Exception:
        _metric("PricingRefreshFailure")
        log.exception("pricing refresh failed")
        raise
