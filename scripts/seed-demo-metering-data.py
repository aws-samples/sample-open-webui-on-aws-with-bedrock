#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Seed isolated, clearly labeled DEMO data into a metering table so the admin
console can be evaluated without real traffic.

Safety properties:
- every run gets a unique id embedded in user, group, policy, ledger, and audit
  keys;
- every PutItem is conditional and cannot overwrite an existing key;
- the cleanup manifest is created and proven writable before the first write,
  then updated after every successful write;
- cleanup deletes only rows whose demo_run marker matches the manifest; and
- an existing manifest blocks a new seed so one run cannot hide another.

  python scripts/seed-demo-metering-data.py \
      --table open-webui-metering [--profile test] [--region us-east-1]
  ... --cleanup   # delete exactly the matching rows in the manifest

Use only in a verified non-production account. Demo identities use example.com
addresses and a demo-<run-id> prefix; no Cognito users are created.
"""

import argparse
import json
import os
import random
import time
import uuid

import boto3
from botocore.exceptions import ClientError

WINDOW = time.strftime("%Y-%m", time.gmtime())
TODAY = time.strftime("%Y-%m-%d", time.gmtime())
NOW = int(time.time())

# (name, email, group, spent_usd, hard_limit, calls) — synthetic presentation
# values spanning healthy, near-limit, over-limit, and low-activity states.
DEMO_USERS = [
    ("aria", "aria.demo@example.com", "power-users", 4.62, 5.0, 214),
    ("bo", "bo.demo@example.com", "power-users", 5.31, 5.0, 302),
    ("caleb", "caleb.demo@example.com", "user", 3.18, 5.0, 121),
    ("dev", "devon.demo@example.com", "user", 1.42, 5.0, 63),
    ("em", "emery.demo@example.com", "basic-users", 0.87, 2.0, 41),
    ("finn", "finn.demo@example.com", "basic-users", 1.96, 2.0, 88),
    ("gray", "gray.demo@example.com", "user", 0.04, 5.0, 3),
    ("hollis", "hollis.demo@example.com", "power-users", 12.40, 25.0, 517),
]
# Synthetic display rates only. They are marked price_map_version=demo and do
# not seed or modify the production pricing catalog.
MODELS = [
    ("qwen.qwen3-32b", 1.5e-07, 6e-07),
    ("openai.gpt-oss-120b", 1.5e-07, 6e-07),
    ("deepseek.v3.1", 5.8e-07, 1.68e-06),
    ("mistral.mistral-large-3-675b-instruct", 3.5e-07, 1.4e-06),
]
LANES = ["chat/completions", "responses", "messages"]


def _write_manifest(path: str, manifest: dict) -> None:
    """Atomically persist the cleanup manifest with owner-only permissions."""
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(manifest, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _prepare_manifest(path: str, table: str, table_arn: str, run_id: str) -> dict:
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(absolute):
        raise RuntimeError(
            f"manifest already exists: {path}; clean up or inspect that run before seeding again"
        )
    manifest = {
        "version": 3,
        "table": table,
        "table_arn": table_arn,
        "window": WINDOW,
        "run_id": run_id,
        "keys": [],
    }
    # Exclusive creation proves the destination is writable before DynamoDB is
    # touched. _write_manifest takes over with atomic replacements afterwards.
    fd = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    return manifest


def seed(ddb, table: str, manifest_path: str):
    table_arn = ddb.describe_table(TableName=table)["Table"]["TableArn"]
    table_arn = ddb.describe_table(TableName=table)["Table"]["TableArn"]
    run_id = uuid.uuid4().hex[:10]
    manifest = _prepare_manifest(manifest_path, table, table_arn, run_id)
    keys = manifest["keys"]

    def put(item):
        item = dict(item)
        item["demo"] = {"S": "1"}
        item["demo_run"] = {"S": run_id}
        try:
            ddb.put_item(
                TableName=table,
                Item=item,
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise RuntimeError(
                    f"refusing to overwrite existing metering row {item['pk']['S']} / {item['sk']['S']}"
                ) from error
            raise
        keys.append({"pk": item["pk"]["S"], "sk": item["sk"]["S"]})
        _write_manifest(manifest_path, manifest)

    rng = random.Random(42)
    subs = {}
    try:
        for name, _email, base_group, spent, hard, calls in DEMO_USERS:
            sub = f"demo-{run_id}-{name}"
            group = f"demo-{run_id}-{base_group}"
            subs[name] = sub
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
            })
            for i in range(min(6, max(2, calls // 60))):
                model, rin, rout = rng.choice(MODELS)
                ci, co = rng.randint(400, 9000), rng.randint(150, 2500)
                ts = NOW - rng.randint(120, 43200)
                put({
                    "pk": {"S": f"LEDGER#{TODAY}"},
                    "sk": {"S": f"{ts}#resp#demo-{run_id}-{name}-{i}"},
                    "idem": {"S": f"resp#demo-{run_id}-{name}-{i}"},
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
                })

        for base_group in {user[2] for user in DEMO_USERS}:
            group = f"demo-{run_id}-{base_group}"
            total = sum(user[3] for user in DEMO_USERS if user[2] == base_group)
            calls = sum(user[5] for user in DEMO_USERS if user[2] == base_group)
            put({
                "pk": {"S": f"GROUP#{group}#{WINDOW}"},
                "sk": {"S": "COUNTER"},
                "w": {"S": WINDOW},
                "used_usd": {"N": str(round(total, 6))},
                "used_in": {"N": str(int(total / 2.0e-07 * 0.7))},
                "used_out": {"N": str(int(total / 8.0e-07 * 0.3))},
                "req_count": {"N": str(calls)},
                "updated_at": {"N": str(NOW - 300)},
            })

        demo_power_group = f"demo-{run_id}-power-users"
        put({
            "pk": {"S": f"POLICY#GROUP#{demo_power_group}"},
            "sk": {"S": "POLICY"},
            "hard_limit_usd": {"N": "50"},
            "soft_limit_usd": {"N": "40"},
            "rpm_limit": {"N": "60"},
            "note": {"S": "DEMO: advisory ceiling for a synthetic cost center"},
            "state": {"S": "POLICY"},
            "created_at": {"N": str(NOW - 86400 * 6)},
            "updated_at": {"N": str(NOW - 86400 * 6)},
            "updated_by": {"S": f"demo-admin-{run_id}"},
        })
        hollis_sub = subs["hollis"]
        put({
            "pk": {"S": f"POLICY#USER#{hollis_sub}"},
            "sk": {"S": "POLICY"},
            "hard_limit_usd": {"N": "25"},
            "soft_limit_usd": {"N": "20"},
            "rpm_limit": {"N": "60"},
            "note": {"S": "DEMO: synthetic project bump with a cleanup marker"},
            "override_until": {"N": str(NOW + 86400 * 9)},
            "state": {"S": "POLICY"},
            "created_at": {"N": str(NOW - 86400 * 2)},
            "updated_at": {"N": str(NOW - 86400 * 2)},
            "updated_by": {"S": f"demo-admin-{run_id}"},
        })
        for i, (action, target, before, after) in enumerate([
            ("PUT_POLICY", f"USER#{hollis_sub}", {}, {"hard_limit_usd": 25}),
            ("PUT_POLICY", f"GROUP#{demo_power_group}", {}, {"hard_limit_usd": 50}),
            ("COUNTER_RESET", f"{hollis_sub}#{WINDOW}", {"used_usd": 25.1}, {"used_usd": 0}),
        ]):
            ts = NOW - 86400 * (2 - i) - 3600
            put({
                "pk": {"S": f"AUDIT#{time.strftime('%Y-%m-%d', time.gmtime(ts))}"},
                "sk": {"S": f"{ts}#demo-admin-{run_id}#{action}"},
                "actor": {"S": f"demo-admin-{run_id}"},
                "action": {"S": action},
                "target": {"S": target},
                "before": {"S": json.dumps(before)},
                "after": {"S": json.dumps(after)},
                "ttl": {"N": str(NOW + 7 * 24 * 3600)},
            })
    except Exception:
        print(
            f"seed failed after {len(keys)} writes; manifest retained at {manifest_path}; "
            "run the same command with --cleanup",
        )
        raise

    print(f"seeded {len(keys)} isolated demo items (run {run_id}); manifest -> {manifest_path}")


def cleanup(ddb, table: str, manifest_path: str):
    with open(manifest_path) as handle:
        manifest = json.load(handle)
    if manifest.get("version") != 3 or not manifest.get("run_id") or not manifest.get("table_arn"):
        raise RuntimeError(
            "refusing unsafe cleanup: manifest predates account/region-bound demo runs; inspect it manually"
        )
    if manifest.get("table") != table:
        raise RuntimeError(
            f"manifest table {manifest.get('table')!r} does not match --table {table!r}"
        )
    current_table_arn = ddb.describe_table(TableName=table)["Table"]["TableArn"]
    if manifest["table_arn"] != current_table_arn:
        raise RuntimeError(
            "refusing cleanup because the selected account/region table ARN does not match the seed manifest: "
            f"{current_table_arn!r} != {manifest['table_arn']!r}"
        )

    run_id = manifest["run_id"]
    failures = []
    for key in manifest["keys"]:
        try:
            ddb.delete_item(
                TableName=table,
                Key={"pk": {"S": key["pk"]}, "sk": {"S": key["sk"]}},
                ConditionExpression="attribute_not_exists(pk) OR demo_run = :run",
                ExpressionAttributeValues={":run": {"S": run_id}},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                failures.append(key)
                continue
            raise
    if failures:
        raise RuntimeError(
            f"refused to delete {len(failures)} rows whose demo_run marker changed; "
            f"manifest retained at {manifest_path}"
        )
    print(f"deleted {len(manifest['keys'])} isolated demo items from run {run_id}")
    os.remove(manifest_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--manifest", default=".demo-metering-manifest.json")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ddb = session.client("dynamodb")
    if args.cleanup:
        cleanup(ddb, args.table, args.manifest)
    else:
        seed(ddb, args.table, args.manifest)


if __name__ == "__main__":
    main()
