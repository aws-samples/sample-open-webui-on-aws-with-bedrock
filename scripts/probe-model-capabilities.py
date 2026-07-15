# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Probe every model on the Bedrock Mantle catalog against each OpenAI-compatible
API (Chat Completions, Responses, Anthropic Messages) and write a capability
matrix to config/model-capabilities.json.

The gateway's models-filter interceptor reads that matrix so Open WebUI only
ever surfaces models that actually work on a given API — nothing that would
400 in the chat window.

Classification (multi-path, account-gate aware) lives in
gateway/refresher/probe_core.py so the CLI and the scheduled refresher Lambda
stay in lockstep. See that module for the two hard-won facts it encodes
(the /openai/v1 path split and the non-uniform account gate).

Auth: SigV4 with the caller's AWS credentials (service name "bedrock").
Usage: python scripts/probe-model-capabilities.py [--region us-east-1]
Requires: boto3, requests  (pip install boto3 requests)
"""

import argparse
import json
import os
import sys

# Import the shared probe logic from the refresher package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway", "refresher"))
from probe_core import CAPS_COMMENT, probe_matrix  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..",
                                                  "config", "model-capabilities.json"))
    args = ap.parse_args()

    caps = probe_matrix(args.region, log=lambda m: print(m, file=sys.stderr))
    caps = {"_comment": CAPS_COMMENT, **caps}

    out = os.path.abspath(args.out)
    with open(out, "w") as f:
        json.dump(caps, f, indent=2)
    print(f"wrote {out}", file=sys.stderr)
    for lane in ("chat_completions", "responses", "messages"):
        print(f"  {lane}: {len(caps[lane])}", file=sys.stderr)


if __name__ == "__main__":
    main()
