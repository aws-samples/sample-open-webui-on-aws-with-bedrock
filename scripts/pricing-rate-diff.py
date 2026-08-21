# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Pre-deploy pricing gate: diff the EFFECTIVE per-model, per-leaf rates between
two snapshots and fail if anything moved beyond tolerance.

A gap count tells you how many models are priced; a rate diff tells you whether
the numbers a user is billed changed — which is what actually catches a matcher
that "improved coverage" by matching less exactly. Run it before a deploy with
the live table on one side and the about-to-ship compute on the other; a clean
run (or only the intended override additions) is the go signal.

Each side is one of:
  * a JSON dump file (--old / --new): either the admin-API `/pricing` catalog
    response ({"models": [...]}) or a raw list of DynamoDB-plain PRICING# rows.
  * the live DynamoDB table (--old-live TABLE / --new-live TABLE): scans the
    PRICING# key space via boto3 (read-only).

Rates are resolved through the PRODUCTION resolver (metering/pricing), so the
diff reflects what settle/estimate would actually bill — override precedence,
routing ladder and all — not the raw stored grid.

Leaves compared: routing in {in_region, global, geo} x direction in
{input, output} at the standard/default tier (the effective standard grid the
console shows). Exit 1 when any |old-new| > --tolerance (default 0: any change
is flagged), or when a model was added / removed.

Usage:
  scripts/pricing-rate-diff.py --old a.json --new b.json
  scripts/pricing-rate-diff.py --old-live open-webui-metering --new compute.json
  scripts/pricing-rate-diff.py --old-live tbl --new-live tbl --tolerance 0.01

Requires: Python 3 stdlib; boto3 only when a --*-live side is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "metering"))
from pricing.resolver import resolve_rate, unwrap_item  # noqa: E402

ROUTINGS = ("in_region", "global", "geo")
DIRECTIONS = ("input", "output")


def _rows_from_catalog(models: list) -> dict:
    """admin-API /pricing catalog `models` -> {model: {published, override}}.

    The catalog already carries `rates` (grid) and `override`; reconstruct the
    resolver `entry` shape from it.
    """
    out: dict = {}
    for m in models:
        model = m.get("model")
        if not model:
            continue
        pub = None
        if m.get("rates"):
            pub = {"rates": m["rates"], "_UNIT": "USD/1M-tokens",
                   "offer_version": m.get("offer_version") or ""}
        out[model] = {"published": pub, "override": m.get("override")}
    return out


def _rows_from_ddb_items(items: list) -> dict:
    """Raw PRICING# rows (DynamoDB-typed OR already-plain) -> resolver entries.

    Alias rows fold into their canonical model (alias_of), mirroring _catalog().
    """
    out: dict = {}
    for it in items:
        p = unwrap_item(it)
        pk = p.get("pk", "")
        sk = p.get("sk", "")
        key = pk[len("PRICING#"):] if pk.startswith("PRICING#") else pk
        if key in ("_CATALOG", "_UNMATCHED", "_ALIAS", "_COVERAGE"):
            continue
        if sk == "PUBLISHED":
            canonical = p.get("alias_of") or p.get("canonical_id") or key
            e = out.setdefault(canonical, {"published": None, "override": None})
            if key == canonical or e["published"] is None:
                e["published"] = p
        elif sk == "OVERRIDE":
            e = out.setdefault(key, {"published": None, "override": None})
            e["override"] = p
    return out


def load_side_file(path: str) -> dict:
    with open(path) as f:
        doc = json.load(f)
    if isinstance(doc, dict) and isinstance(doc.get("models"), list):
        return _rows_from_catalog(doc["models"])
    if isinstance(doc, list):
        return _rows_from_ddb_items(doc)
    if isinstance(doc, dict) and isinstance(doc.get("Items"), list):
        return _rows_from_ddb_items(doc["Items"])
    raise SystemExit(f"unrecognized snapshot shape in {path} "
                     "(expected /pricing catalog, a row list, or a Scan dump)")


def load_side_live(table: str) -> dict:
    try:
        import boto3  # noqa: PLC0415 — optional, only when --*-live is used
    except ImportError:
        raise SystemExit("boto3 is required for --*-live (pip install boto3)")
    ddb = boto3.client("dynamodb")
    items, lek = [], None
    while True:
        kwargs = {
            "TableName": table,
            "FilterExpression": "begins_with(pk, :p)",
            "ExpressionAttributeValues": {":p": {"S": "PRICING#"}},
        }
        if lek:
            kwargs["ExclusiveStartKey"] = lek
        page = ddb.scan(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
    return _rows_from_ddb_items(items)


def effective_leaves(entry: dict) -> dict:
    """(routing, direction) -> per-1M rate (only leaves that resolve non-null)."""
    out: dict = {}
    for routing in ROUTINGS:
        for direction in DIRECTIONS:
            res = resolve_rate(entry, direction, routing=routing)
            if res.usd_per_1m is not None:
                out[(routing, direction)] = res.usd_per_1m
    return out


def diff(old: dict, new: dict, tolerance: float) -> int:
    old_models, new_models = set(old), set(new)
    added = sorted(new_models - old_models)
    removed = sorted(old_models - new_models)
    common = sorted(old_models & new_models)

    changed = 0
    print("=== per-model, per-leaf effective-rate diff ===")
    for model in common:
        ol = effective_leaves(old[model])
        nl = effective_leaves(new[model])
        leaves = sorted(set(ol) | set(nl))
        rows = []
        for leaf in leaves:
            o = ol.get(leaf)
            n = nl.get(leaf)
            if o is None or n is None:
                delta = None  # leaf appeared/disappeared
                flagged = True
            else:
                delta = n - o
                flagged = abs(delta) > tolerance
            if flagged:
                rows.append((leaf, o, n, delta))
        if rows:
            changed += 1
            print(f"\n  {model}:")
            for (routing, direction), o, n, delta in rows:
                od = "—" if o is None else f"{o:g}"
                nd = "—" if n is None else f"{n:g}"
                dd = "leaf +/-" if delta is None else f"{delta:+g}"
                print(f"    {routing}/{direction}: {od} -> {nd}  ({dd})")

    if added:
        print(f"\n=== added models ({len(added)}) ===")
        for m in added:
            leaves = effective_leaves(new[m])
            print(f"  + {m}: {len(leaves)} priced leaf/leaves")
    if removed:
        print(f"\n=== removed models ({len(removed)}) ===")
        for m in removed:
            print(f"  - {m}")

    total_issues = changed + len(added) + len(removed)
    print(f"\n=== summary ===")
    print(f"  models: {len(common)} common, {len(added)} added, {len(removed)} removed")
    print(f"  models with a leaf delta beyond tolerance ({tolerance:g}): {changed}")
    if total_issues == 0:
        print("  RESULT: no changes — safe to deploy.")
        return 0
    print("  RESULT: changes detected — review before deploy (added override rows "
          "are the only expected delta for a pricing-fill deploy).")
    return 1


def resolve_side(kind_file: str | None, kind_live: str | None, label: str) -> dict:
    if kind_file and kind_live:
        raise SystemExit(f"{label}: give a file OR a live table, not both")
    if kind_file:
        return load_side_file(kind_file)
    if kind_live:
        return load_side_live(kind_live)
    raise SystemExit(f"{label}: one of the file / live options is required")


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff effective pricing between two snapshots (pre-deploy gate).")
    ap.add_argument("--old", help="OLD snapshot: JSON dump (catalog or row list)")
    ap.add_argument("--old-live", metavar="TABLE", help="OLD snapshot: live DynamoDB table name")
    ap.add_argument("--new", help="NEW snapshot: JSON dump (catalog or row list)")
    ap.add_argument("--new-live", metavar="TABLE", help="NEW snapshot: live DynamoDB table name")
    ap.add_argument("--tolerance", type=float, default=0.0,
                    help="max allowed |old-new| per leaf before flagging (default 0: any change)")
    args = ap.parse_args()

    old = resolve_side(args.old, args.old_live, "old")
    new = resolve_side(args.new, args.new_live, "new")
    return diff(old, new, args.tolerance)


if __name__ == "__main__":
    sys.exit(main())
