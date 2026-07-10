# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Probe every model on the Bedrock Mantle catalog against each OpenAI-compatible
API (Chat Completions, Responses, Anthropic Messages) and write a capability
matrix to config/model-capabilities.json.

The gateway's models-filter interceptor reads that matrix so Open WebUI only
ever surfaces models that actually work on a given API — nothing that would
400 in the chat window.

Auth: SigV4 with the caller's AWS credentials (service name "bedrock").
Usage: python scripts/probe-model-capabilities.py [--region us-east-1]
Requires: boto3, requests  (pip install boto3 requests)
"""

import argparse
import concurrent.futures
import json
import os
import sys

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"


def signer(region):
    creds = boto3.Session().get_credentials().get_frozen_credentials()

    def signed(method, path, payload=None, timeout=45):
        base = f"https://bedrock-mantle.{region}.api.aws"
        body = json.dumps(payload).encode() if payload is not None else None
        req = AWSRequest(method=method, url=f"{base}{path}", data=body,
                         headers={"Content-Type": "application/json"})
        SigV4Auth(creds, "bedrock", region).add_auth(req)
        try:
            r = requests.request(method, f"{base}{path}", data=body,
                                 headers=dict(req.headers), timeout=timeout)
            try:
                err = r.json().get("error", {}).get("message", "")
            except Exception:
                err = ""
            return r.status_code, err
        except requests.exceptions.Timeout:
            # A rejection returns instantly; a timeout means the model accepted
            # the request and started generating — treat as supported.
            return "timeout(accepted)", ""

    return signed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "config", "model-capabilities.json"))
    args = ap.parse_args()

    signed = signer(args.region)
    status, _ = signed("GET", "/v1/models")
    if status != 200:
        # /v1/models has no body payload; retry to fetch it
        pass
    resp = requests.get  # noqa: F841  (kept for readability)

    # Fetch the catalog (signed GET).
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    url = f"https://bedrock-mantle.{args.region}.api.aws/v1/models"
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(creds, "bedrock", args.region).add_auth(req)
    r = requests.get(url, headers=dict(req.headers), timeout=30)
    r.raise_for_status()
    models = [m["id"] for m in r.json().get("data", []) if m.get("status", "available") == "available"]
    print(f"probing {len(models)} models x 3 APIs against bedrock-mantle.{args.region}...", file=sys.stderr)

    def ok(v):
        return v == 200 or v == "timeout(accepted)"

    def probe(mid):
        cc, cce = signed("POST", "/v1/chat/completions",
                         {"model": mid, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16})
        rs, rse = signed("POST", "/v1/responses",
                         {"model": mid, "input": [{"role": "user", "content": "hi"}], "max_output_tokens": 16})
        ms, mse = signed("POST", "/anthropic/v1/messages",
                         {"model": mid, "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
                          "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})
        gated = "not enabled for this account" in (cce + rse + mse)
        return mid, {"cc": ok(cc), "rs": ok(rs), "ms": ok(ms), "gated": gated}

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for mid, r in ex.map(probe, models):
            results[mid] = r

    caps = {
        "_comment": ("Mantle model capability matrix by OpenAI-compatible API. The gateway "
                     "models-filter interceptor filters /v1/models to these lists so Open WebUI "
                     "only surfaces models that actually work on each connection's API. "
                     "Regenerate with scripts/probe-model-capabilities.py. Account-gated models "
                     "are excluded."),
        "region": args.region,
        "chat_completions": sorted(m for m, r in results.items() if r["cc"] and not r["gated"]),
        "responses": sorted(m for m, r in results.items() if r["rs"] and not r["gated"]),
        "messages": sorted(m for m, r in results.items() if r["ms"] and not r["gated"]),
    }
    out = os.path.abspath(args.out)
    json.dump(caps, open(out, "w"), indent=2)
    print(f"wrote {out}", file=sys.stderr)
    print(f"  chat_completions: {len(caps['chat_completions'])}", file=sys.stderr)
    print(f"  responses:        {len(caps['responses'])}", file=sys.stderr)
    print(f"  messages:         {len(caps['messages'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
