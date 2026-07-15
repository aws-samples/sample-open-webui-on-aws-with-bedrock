# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Scheduled model-capability refresher (opt-in).

Bedrock Mantle adds and moves models over time. This Lambda keeps the gateway's
model surface current WITHOUT a human in the loop, and does it safely:

  1. Probe the live Mantle catalog by invocation (gateway/refresher/probe_core),
     producing a fresh capability matrix (chat_completions / responses /
     messages), correctly path-aware and account-gate aware.

  2. COLLAPSE GUARD — refuse to apply a matrix that catastrophically shrinks a
     currently-populated lane (a transient Mantle blip can 400 everything). A
     bad probe must never empty every user's model dropdown; instead we alert
     and leave the last-good list in place.

  3. Apply the matrix to the interceptor Lambda's MODEL_CAPS environment
     variable (that's where the capability list lives — no gateway config
     change needed to refresh the listing).

  4. RE-SNAPSHOT THE CONNECTOR — the bedrock-mantle inference connector caches
     its model/path map at target-creation time and does NOT self-refresh, so a
     brand-new model (or one that moved paths) will list but fail to *route*
     until the target is refreshed. A no-op update_gateway_target (re-PUT of the
     identical config) forces a zero-downtime re-snapshot. Without this, new
     models appear in the dropdown but 400 on selection.

  5. Notify via SNS with a human-readable diff (+added / -removed per lane), or
     an alert if the guard tripped or the run failed.

Idempotent: if the fresh matrix equals what's already live, it does nothing
(beyond the connector re-snapshot, which is itself idempotent).

Environment:
  INTERCEPTOR_FUNCTION_NAME  interceptor Lambda whose MODEL_CAPS we update (required)
  GATEWAY_ID                 gateway id, for the connector re-snapshot (required)
  CONNECTOR_TARGET_NAME      connector target name to re-snapshot (default "bedrock")
  CONNECTOR_ID               connector id (default "bedrock-mantle")
  MANTLE_REGION              region to probe (default: the Lambda's region)
  SNS_TOPIC_ARN              topic for diff/alert notifications (optional)
  COLLAPSE_MIN_RATIO         guard: reject if a non-empty lane drops below this
                             fraction of its previous size (default 0.5)

Requires a vendored boto3 >= 1.43 (update_gateway_target with the "inference"
target shape) and requests — installed into this asset at deploy time
(see requirements.txt; gitignored).
"""

import json
import os

import boto3

from probe_core import CAPS_COMMENT, probe_matrix

REGION = os.environ.get("MANTLE_REGION") or os.environ.get("AWS_REGION", "us-east-1")
INTERCEPTOR = os.environ["INTERCEPTOR_FUNCTION_NAME"]
GATEWAY_ID = os.environ["GATEWAY_ID"]
CONNECTOR_TARGET_NAME = os.environ.get("CONNECTOR_TARGET_NAME", "bedrock")
CONNECTOR_ID = os.environ.get("CONNECTOR_ID", "bedrock-mantle")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
COLLAPSE_MIN_RATIO = float(os.environ.get("COLLAPSE_MIN_RATIO", "0.5"))

LANES = ("chat_completions", "responses", "messages")

_lambda = boto3.client("lambda")
_agc = boto3.client("bedrock-agentcore-control")
_sns = boto3.client("sns") if SNS_TOPIC_ARN else None


def _log(msg):
    print(f"model-refresher: {msg}")


def _notify(subject, message):
    _log(f"{subject} :: {message}")
    if _sns:
        try:
            _sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
        except Exception as e:  # noqa: BLE001 — notification must never crash the run
            _log(f"SNS publish failed: {e}")


def _current_caps():
    cfg = _lambda.get_function_configuration(FunctionName=INTERCEPTOR)
    raw = cfg["Environment"]["Variables"].get("MODEL_CAPS", "{}")
    caps = json.loads(raw)
    return cfg["Environment"]["Variables"], caps


def _diff(old, new):
    lines = []
    for lane in LANES:
        o, n = set(old.get(lane, [])), set(new.get(lane, []))
        added, removed = sorted(n - o), sorted(o - n)
        if added or removed:
            parts = []
            if added:
                parts.append("+" + ", +".join(added))
            if removed:
                parts.append("-" + ", -".join(removed))
            lines.append(f"[{lane}] {len(o)}->{len(n)}: " + "; ".join(parts))
    return "\n".join(lines)


def _collapse_tripped(old, new):
    """True if any currently-populated lane would drop below COLLAPSE_MIN_RATIO
    of its size — the signature of a transient probe failure, not a real change."""
    for lane in LANES:
        o, n = len(old.get(lane, [])), len(new.get(lane, []))
        if o > 0 and n < o * COLLAPSE_MIN_RATIO:
            return True, f"[{lane}] {o}->{n} (< {COLLAPSE_MIN_RATIO:.0%} of previous)"
    return False, ""


def _resnapshot_connector():
    """Force the bedrock-mantle connector to re-read its model/path map by
    re-PUTting its identical config. Zero-downtime; the target stays live."""
    targets = _agc.list_gateway_targets(gatewayIdentifier=GATEWAY_ID, maxResults=50).get("items", [])
    match = next((t for t in targets if t.get("name") == CONNECTOR_TARGET_NAME), None)
    if not match:
        _log(f"connector target {CONNECTOR_TARGET_NAME!r} not found; skipping re-snapshot")
        return "connector-target-not-found"
    _agc.update_gateway_target(
        gatewayIdentifier=GATEWAY_ID,
        targetId=match["targetId"],
        name=CONNECTOR_TARGET_NAME,
        targetConfiguration={"inference": {"connector": {"source": {"connectorId": CONNECTOR_ID}}}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    _log(f"connector target {CONNECTOR_TARGET_NAME!r} re-snapshotted")
    return "connector-resnapshotted"


def handler(event, context):
    # 1. Probe the live catalog.
    try:
        fresh = probe_matrix(REGION, log=_log)
    except Exception as e:  # noqa: BLE001
        _notify("model-refresher: PROBE FAILED", f"Region {REGION}: {e}\nNo changes applied.")
        raise

    # 2. Compare to what's live.
    env_vars, current = _current_caps()
    fresh_caps = {lane: fresh[lane] for lane in LANES}
    diff = _diff(current, fresh_caps)

    if not diff:
        # No listing change. Still re-snapshot the connector so any model that
        # changed paths under the same id routes correctly.
        try:
            _resnapshot_connector()
        except Exception as e:  # noqa: BLE001
            _log(f"connector re-snapshot failed (non-fatal): {e}")
        _log("no capability change; MODEL_CAPS unchanged")
        return {"changed": False, "region": REGION, "caps": {k: len(v) for k, v in fresh_caps.items()}}

    # 3. Collapse guard.
    tripped, why = _collapse_tripped(current, fresh_caps)
    if tripped:
        _notify(
            "model-refresher: COLLAPSE GUARD TRIPPED — no changes applied",
            f"Region {REGION}. A lane shrank suspiciously (likely a transient Mantle "
            f"error), so the live list was left untouched.\n\nGuard hit: {why}\n\n"
            f"Proposed diff (NOT applied):\n{diff}",
        )
        return {"changed": False, "guard_tripped": True, "reason": why}

    # 4. Apply the new MODEL_CAPS to the interceptor.
    new_env = dict(env_vars)
    new_env["MODEL_CAPS"] = json.dumps(fresh_caps)
    _lambda.update_function_configuration(
        FunctionName=INTERCEPTOR, Environment={"Variables": new_env}
    )
    _log("MODEL_CAPS updated on interceptor")

    # 5. Re-snapshot the connector so newly-listed models actually route.
    try:
        _resnapshot_connector()
    except Exception as e:  # noqa: BLE001
        _notify(
            "model-refresher: applied list, but connector re-snapshot FAILED",
            f"Region {REGION}. MODEL_CAPS was updated but the connector re-snapshot "
            f"failed — new models may list yet 400 on selection until the connector "
            f"target is refreshed.\n\nError: {e}\n\nDiff applied:\n{diff}",
        )
        return {"changed": True, "resnapshot_failed": True, "diff": diff}

    _notify("model-refresher: model list updated", f"Region {REGION}.\n\n{diff}")
    return {"changed": True, "region": REGION, "diff": diff,
            "caps": {k: len(v) for k, v in fresh_caps.items()}}


# For the CLI-style comment when a fresh matrix is written elsewhere.
__all__ = ["handler", "CAPS_COMMENT"]
