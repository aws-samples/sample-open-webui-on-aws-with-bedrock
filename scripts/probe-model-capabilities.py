# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Probe every model on the Bedrock Mantle catalog against Chat Completions,
Responses, and Anthropic Messages, then write a capability snapshot.

The probe performs real inference attempts and can incur model charges. Each
catalog model is tested on up to five candidate paths with an output limit of
1,024 tokens per attempt. Manual runs require --yes.

Classification lives in gateway/refresher/probe_core.py so the CLI and optional
scheduled refresher remain aligned.

Usage:
  python3 scripts/probe-model-capabilities.py \
    --profile PROFILE --region us-east-1 --yes

Requires the dependencies in gateway/refresher/requirements.txt.
"""

import argparse
import json
import os
import sys

import boto3

# Import the shared probe logic from the refresher package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway", "refresher"))
from probe_core import CAPS_COMMENT, PROBE_MAX_TOKENS, probe_matrix  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", help="AWS shared-config profile to use explicitly")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(__file__), "..", "config", "model-capabilities.json"),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="acknowledge that the probe makes real, potentially billable inference calls",
    )
    args = parser.parse_args()

    if not args.yes:
        parser.error(
            "--yes is required: this command probes every catalog model on up to five paths "
            f"with max output {PROBE_MAX_TOKENS} tokens per attempt and can incur charges"
        )

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    credentials = session.get_credentials()
    if credentials is None:
        parser.error("no AWS credentials resolved for the selected profile/session")
    identity = session.client("sts", region_name=args.region).get_caller_identity()
    profile_label = args.profile or "ambient credential chain"
    print(
        f"probe identity: account={identity['Account']} arn={identity['Arn']} "
        f"profile={profile_label} region={args.region}",
        file=sys.stderr,
    )
    print(
        "cost notice: performing real inference attempts across the live Mantle catalog; "
        f"up to five paths/model, max output {PROBE_MAX_TOKENS} tokens/attempt",
        file=sys.stderr,
    )

    caps = probe_matrix(
        args.region,
        creds=credentials.get_frozen_credentials(),
        log=lambda message: print(message, file=sys.stderr),
    )
    caps = {"_comment": CAPS_COMMENT, **caps}

    output = os.path.abspath(args.out)
    with open(output, "w") as handle:
        json.dump(caps, handle, indent=2)
    print(f"wrote {output}", file=sys.stderr)
    for lane in ("chat_completions", "responses", "messages"):
        print(f"  {lane}: {len(caps[lane])}", file=sys.stderr)


if __name__ == "__main__":
    main()
