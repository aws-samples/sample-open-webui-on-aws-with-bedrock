#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Seed clearly-labeled DEMO data into the metering table so the admin console
can be evaluated without real traffic (console decision D8).

Writes synthetic users (counters + ledger rows + a few policies/audit rows)
for the CURRENT window, records every key into a local manifest, and removes
exactly those keys with --cleanup (never deletes by prefix or scan).

  uv run --no-project --with boto3 python scripts/seed-demo-metering-data.py \
      --table open-webui-metering [--profile test] [--region us-east-1]
  ... --cleanup   # delete exactly what the manifest lists

Demo identities use reserved example.com addresses and a demo- sub prefix so
they can never collide with real Cognito subs (which are UUIDs).
"""

import argparse
import json
import os
import random
import time

import boto3

WINDOW = time.strftime("%Y-%m", time.gmtime())
TODAY = time.strftime("%Y-%m-%d", time.gmtime())
NOW = int(time.time())

# (sub-suffix, email, group, spent_usd, hard_limit, calls) — a spread that
# exercises every console state: healthy, near-limit, over-limit, idle.
DEMO_USERS = [
    ("demo-aria", "aria.demo@example.com", "power-users", 4.62, 5.0, 214),
    ("demo-bo", "bo.demo@example.com", "power-users", 5.31, 5.0, 302),
    ("demo-caleb", "caleb.demo@example.com", "user", 3.18, 5.0, 121),
    ("demo-dev", "devon.demo@example.com", "user", 1.42, 5.0, 63),
    ("demo-em", "emery.demo@example.com", "basic-users", 0.87, 2.0, 41),
    ("demo-finn", "finn.demo@example.com", "basic-users", 1.96, 2.0, 88),
    ("demo-gray", "gray.demo@example.com", "user", 0.04, 5.0, 3),
    ("demo-hollis", "hollis.demo@example.com", "power-users", 12.40, 25.0, 517),
]
MODELS = [
    ("qwen.qwen3-32b", 1.5e-07, 6e-07),
    ("openai.gpt-oss-120b", 1.5e-07, 6e-07),
    ("deepseek.v3-v1", 5.8e-07, 1.68e-06),
    ("meta.llama4-maverick-17b-instruct-v1", 3.5e-07, 1.4e-06),
]
LANES = ["chat/completions", "responses", "messages"]


def seed(ddb, table: str, manifest_path: str):
    keys = []

    def put(item):
        ddb.put_item(TableName=table, Item=item)
        keys.append({"pk": item["pk"]["S"], "sk": item["sk"]["S"]})

    rng = random.Random(42)  # deterministic demo data
    for suffix, email, group, spent, hard, calls in DEMO_USERS:
        sub = f"00000000-demo-4000-8000-{suffix.replace('demo-', ''):>012s}".replace(" ", "0")
        tin = int(spent / 2.0e-07 * 0.7)
        tout = int(spent / 8.0e-07 * 0.3)
        put({
            "pk": {"S": f"USE#{sub}#{WINDOW}"},
            "sk": {"S": "COUNTER"},
            "w": {"S": WINDOW},
            "used_usd": {"N": str(round(spent, 6))},
            "est_usd": {"N": "0"},
            "used_in": {"N": str(tin)},
            "used_out": {"N": str(tout)},
            "req_count": {"N": str(calls)},
            "hard_limit_usd": {"N": str(hard)},
            "soft_limit_usd": {"N": str(hard * 0.8)},
            "updated_at": {"N": str(NOW - rng.randint(60, 7200))},
            "alerted": {"S": "100" if spent >= hard else ("80" if spent >= 0.8 * hard else "")},
            "demo": {"S": "1"},
        })
        # a few ledger rows per user, spread over today
        for i in range(min(6, max(2, calls // 60))):
            model, rin, rout = rng.choice(MODELS)
            ci, co = rng.randint(400, 9000), rng.randint(150, 2500)
            ts = NOW - rng.randint(120, 43200)
            put({
                "pk": {"S": f"LEDGER#{TODAY}"},
                "sk": {"S": f"{ts}#resp#demo-{suffix}-{i}"},
                "idem": {"S": f"resp#demo-{suffix}-{i}"},
                "sub": {"S": sub},
                "billing_group": {"S": group},
                "groups": {"SS": [group]},
                "model": {"S": model},
                "lane": {"S": rng.choice(LANES)},
                "tier": {"S": "standard"},
                "tokens_in": {"N": str(ci)},
                "tokens_out": {"N": str(co)},
                "tokens_cached": {"N": "0"},
                "rate_in": {"N": str(rin)},
                "rate_out": {"N": str(rout)},
                "price_map_version": {"S": "demo"},
                "usd": {"N": str(round(ci * rin + co * rout, 8))},
                "state": {"S": "SETTLED"},
                "source": {"S": "DEMO"},
                "ttl": {"N": str(NOW + 7 * 24 * 3600)},
                "demo": {"S": "1"},
            })

    # group rollups consistent with the users above
    for group in {u[2] for u in DEMO_USERS}:
        total = sum(u[3] for u in DEMO_USERS if u[2] == group)
        n = sum(u[5] for u in DEMO_USERS if u[2] == group)
        put({
            "pk": {"S": f"GROUP#{group}#{WINDOW}"},
            "sk": {"S": "COUNTER"},
            "w": {"S": WINDOW},
            "used_usd": {"N": str(round(total, 6))},
            "used_in": {"N": str(int(total / 2.0e-07 * 0.7))},
            "used_out": {"N": str(int(total / 8.0e-07 * 0.3))},
            "req_count": {"N": str(n)},
            "updated_at": {"N": str(NOW - 300)},
            "demo": {"S": "1"},
        })

    # a demo group ceiling + an expired demo override + audit rows for them
    put({
        "pk": {"S": "POLICY#GROUP#power-users"},
        "sk": {"S": "POLICY"},
        "hard_limit_usd": {"N": "50"},
        "soft_limit_usd": {"N": "40"},
        "rpm_limit": {"N": "60"},
        "note": {"S": "DEMO: team ceiling for the power-users cost center"},
        "state": {"S": "POLICY"},
        "created_at": {"N": str(NOW - 86400 * 6)},
        "updated_at": {"N": str(NOW - 86400 * 6)},
        "updated_by": {"S": "demo-admin"},
        "demo": {"S": "1"},
    })
    hollis_sub = "00000000-demo-4000-8000-000000hollis"
    put({
        "pk": {"S": f"POLICY#USER#{hollis_sub}"},
        "sk": {"S": "POLICY"},
        "hard_limit_usd": {"N": "25"},
        "soft_limit_usd": {"N": "20"},
        "rpm_limit": {"N": "60"},
        "note": {"S": "DEMO: Q3 analytics project bump — expires mid-month"},
        "override_until": {"N": str(NOW + 86400 * 9)},
        "state": {"S": "POLICY"},
        "created_at": {"N": str(NOW - 86400 * 2)},
        "updated_at": {"N": str(NOW - 86400 * 2)},
        "updated_by": {"S": "demo-admin"},
        "demo": {"S": "1"},
    })
    for i, (action, target, before, after) in enumerate([
        ("PUT_POLICY", f"USER#{hollis_sub}", {}, {"hard_limit_usd": 25}),
        ("PUT_POLICY", "GROUP#power-users", {}, {"hard_limit_usd": 50}),
        ("COUNTER_RESET", f"{hollis_sub}#{WINDOW}", {"used_usd": 25.1}, {"used_usd": 0}),
    ]):
        ts = NOW - 86400 * (2 - i) - 3600
        put({
            "pk": {"S": f"AUDIT#{time.strftime('%Y-%m-%d', time.gmtime(ts))}"},
            "sk": {"S": f"{ts}#demo-admin#{action}"},
            "actor": {"S": "demo-admin"},
            "action": {"S": action},
            "target": {"S": target},
            "before": {"S": json.dumps(before)},
            "after": {"S": json.dumps(after)},
            "ttl": {"N": str(NOW + 7 * 24 * 3600)},
            "demo": {"S": "1"},
        })

    with open(manifest_path, "w") as f:
        json.dump({"table": table, "window": WINDOW, "keys": keys}, f, indent=1)
    print(f"seeded {len(keys)} demo items; manifest → {manifest_path}")


def cleanup(ddb, manifest_path: str):
    with open(manifest_path) as f:
        manifest = json.load(f)
    n = 0
    for key in manifest["keys"]:
        ddb.delete_item(
            TableName=manifest["table"],
            Key={"pk": {"S": key["pk"]}, "sk": {"S": key["sk"]}},
        )
        n += 1
    print(f"deleted {n} demo items listed in {manifest_path}")
    os.remove(manifest_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--region")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--manifest", default=".demo-metering-manifest.json")
    args = ap.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ddb = session.client("dynamodb")
    if args.cleanup:
        cleanup(ddb, args.manifest)
    else:
        seed(ddb, args.table, args.manifest)


if __name__ == "__main__":
    main()
