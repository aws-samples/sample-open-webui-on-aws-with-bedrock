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
  INTERCEPTOR_ALIAS          if set (e.g. "live"), the gateway invokes the
                             interceptor through this ALIAS pinned to a published
                             VERSION (the metering module does this, behind a
                             CodeDeploy canary). A published version's env is
                             FROZEN, so updating $LATEST alone is invisible to
                             traffic. When set, we publish a fresh version after
                             updating $LATEST and repoint the alias to it.
                             Absent (the base sample) ⇒ the gateway invokes
                             $LATEST directly and updating it is sufficient.
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
INTERCEPTOR_ALIAS = os.environ.get("INTERCEPTOR_ALIAS", "").strip()
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
    """Return (latest_env_vars, currently_SERVED_caps).

    The env we mutate lives on $LATEST, but what SERVES traffic is the aliased
    published version when an alias is configured. We diff against what serves,
    not against $LATEST — otherwise, if a prior run wrote $LATEST but failed to
    promote the alias, the diff would read 'no change' and the alias would stay
    stuck on the stale version forever."""
    latest = _lambda.get_function_configuration(FunctionName=INTERCEPTOR)
    served_qualifier = INTERCEPTOR_ALIAS or None
    if served_qualifier:
        served = _lambda.get_function_configuration(FunctionName=INTERCEPTOR, Qualifier=served_qualifier)
    else:
        served = latest
    served_caps = json.loads(served["Environment"]["Variables"].get("MODEL_CAPS", "{}"))
    return latest["Environment"]["Variables"], served_caps


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


def _promote_to_alias():
    """When the gateway invokes the interceptor through an alias pinned to a
    published version (the metering module's CodeDeploy canary), updating
    $LATEST's env is invisible — a published version's environment is frozen.
    So: wait for the $LATEST config update to settle, publish a NEW version
    (which snapshots current $LATEST code+env), then repoint the alias to it.

    Repointing a CodeDeploy-managed alias outside a deployment window is the
    supported way to ship a config-only change; CodeDeploy only owns the alias
    during an active deployment (an interceptor CODE roll)."""
    waiter = _lambda.get_waiter("function_updated_v2")
    waiter.wait(FunctionName=INTERCEPTOR)
    version = _lambda.publish_version(FunctionName=INTERCEPTOR)["Version"]
    _lambda.update_alias(FunctionName=INTERCEPTOR, Name=INTERCEPTOR_ALIAS, FunctionVersion=version)
    _log(f"published version {version} and repointed alias {INTERCEPTOR_ALIAS!r} to it")
    return version


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

    # 4. Apply the new MODEL_CAPS to the interceptor ($LATEST).
    new_env = dict(env_vars)
    new_env["MODEL_CAPS"] = json.dumps(fresh_caps)
    _lambda.update_function_configuration(
        FunctionName=INTERCEPTOR, Environment={"Variables": new_env}
    )
    _log("MODEL_CAPS updated on interceptor ($LATEST)")

    # 5. Re-snapshot the connector so newly-listed models actually route.
    # This runs BEFORE the alias promote and never early-returns: the two steps
    # heal independent layers (connector routing vs. served listing), and a
    # promote failure must not leave a new model listed-but-404ing. (Observed
    # live: six consecutive runs died at the promote on a stale IAM role and
    # skipped this step, so anthropic.claude-opus-5 listed but 404'd.)
    resnapshot_failed = False
    try:
        _resnapshot_connector()
    except Exception as e:  # noqa: BLE001
        resnapshot_failed = True
        _notify(
            "model-refresher: connector re-snapshot FAILED",
            f"Region {REGION}. The connector re-snapshot failed — new models may "
            f"list yet 404 on selection until the connector target is refreshed."
            f"\n\nError: {e}\n\nDiff:\n{diff}",
        )

    # 6. If the gateway invokes via an alias→published-version (metering module),
    # $LATEST is not what serves traffic — publish a new version and move the
    # alias, or the refresh silently never takes effect.
    if INTERCEPTOR_ALIAS:
        try:
            _promote_to_alias()
        except Exception as e:  # noqa: BLE001
            _notify(
                "model-refresher: updated $LATEST but ALIAS PROMOTE FAILED — refresh not live",
                f"Region {REGION}. MODEL_CAPS was written to $LATEST but the gateway "
                f"invokes the interceptor via alias {INTERCEPTOR_ALIAS!r}, and publishing "
                f"a new version / repointing the alias failed — so the new list is NOT "
                f"serving traffic yet. Re-run once resolved.\n\nError: {e}\n\nDiff:\n{diff}",
            )
            return {"changed": True, "alias_promote_failed": True,
                    "resnapshot_failed": resnapshot_failed, "diff": diff}

    if resnapshot_failed:
        return {"changed": True, "resnapshot_failed": True, "diff": diff}
    _notify("model-refresher: model list updated", f"Region {REGION}.\n\n{diff}")
    return {"changed": True, "region": REGION, "diff": diff,
            "caps": {k: len(v) for k, v in fresh_caps.items()}}


# For the CLI-style comment when a fresh matrix is written elsewhere.
__all__ = ["handler", "CAPS_COMMENT"]
