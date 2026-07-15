# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Shared Bedrock Mantle capability-probe logic.

Used by both the CLI (scripts/probe-model-capabilities.py) and the scheduled
refresher Lambda (gateway/refresher/index.py) so the classification rules live
in exactly one place.

What it does: for every model in the Mantle catalog, decide which OpenAI-
compatible "lane" it belongs to by *actually invoking* it — model listings on
Mantle carry no capability metadata, and a listed model is not necessarily
invocable (see the two hard-won facts below).

Two facts this encodes, both learned the hard way against live Mantle:

  1. A lane is served on MORE THAN ONE PATH. OpenAI-branded families (gpt-5.x)
     answer on `/openai/v1/*`; open-weight models (gpt-oss, etc.) answer on the
     generic `/v1/*`. A model is "responses-capable" if EITHER responses path
     returns 200. Probing only `/v1/responses` yields a literally-true but
     misleading "does not support the '/v1/responses' API" 400 for every gpt-5.x
     model. The gateway's bedrock-mantle connector path-rewrites to whichever
     path a model needs, so "works on either path here" == "works through the
     gateway lane".

  2. The account gate ("Berm is not enabled for this account", HTTP 401) is NOT
     enforced uniformly across paths on the public endpoint — a gated model can
     return 401 on `/v1/*` yet 200 on `/openai/v1/*`. The gateway DOES enforce
     it, so if a model reports the gate on ANY path it is not usable through the
     gateway and must be excluded from every lane. We therefore probe all
     candidate paths (never short-circuit on first success) so the gate is
     always observed.

Auth: SigV4 with the caller's AWS credentials, signing service "bedrock"
(bedrock-mantle's HTTP API accepts SigV4; API keys are only needed for the
OpenAI SDK).
"""

import concurrent.futures
import json

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"

# Candidate paths per lane (probed in order; a lane is supported if ANY 200s).
CHAT_PATHS = ["/v1/chat/completions", "/openai/v1/chat/completions"]
RESP_PATHS = ["/v1/responses", "/openai/v1/responses"]
MSG_PATHS = ["/anthropic/v1/messages"]

# Big enough that reasoning models (gpt-5.x reject a tiny budget as "below
# minimum" — which is NOT an unsupported-API signal) accept the probe.
PROBE_MAX_TOKENS = 1024

# The account-gate marker. A model reporting this on any path is excluded.
GATE_MARKER = "not enabled for this account"


def _signer(region, creds):
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


def _ok(status):
    return status == 200 or status == "timeout(accepted)"


def fetch_catalog(region, creds):
    """Return the list of 'available' model ids from Mantle's /v1/models."""
    url = f"https://bedrock-mantle.{region}.api.aws/v1/models"
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(creds, "bedrock", region).add_auth(req)
    r = requests.get(url, headers=dict(req.headers), timeout=30)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])
            if m.get("status", "available") == "available"]


def probe_matrix(region, creds=None, max_workers=4, log=None):
    """Probe the whole catalog and return the capability matrix dict:
        {"region", "chat_completions": [...], "responses": [...], "messages": [...]}

    creds: frozen AWS credentials; defaults to the ambient boto3 session (the
    Lambda execution role / the CLI caller). max_workers kept modest because
    each model now issues up to 5 probes and the account throttles bursts.
    """
    creds = creds or boto3.Session().get_credentials().get_frozen_credentials()
    signed = _signer(region, creds)
    models = fetch_catalog(region, creds)
    if log:
        log(f"probing {len(models)} models across "
            f"{len(CHAT_PATHS)}+{len(RESP_PATHS)}+{len(MSG_PATHS)} paths on bedrock-mantle.{region}")

    def any_ok(method, paths, body_fn):
        """(supported, combined_error_text). Probe EVERY candidate path even
        after a success so the caller still sees an account-gate a model reports
        on one path while returning 200 on another (fact #2 above)."""
        supported = False
        errs = []
        for p in paths:
            status, err = signed(method, p, body_fn(p))
            if _ok(status):
                supported = True
            else:
                errs.append(err or "")
        return supported, " ".join(errs)

    def probe(mid):
        cc, cce = any_ok("POST", CHAT_PATHS,
                         lambda p: {"model": mid, "messages": [{"role": "user", "content": "hi"}],
                                    "max_tokens": PROBE_MAX_TOKENS})
        rs, rse = any_ok("POST", RESP_PATHS,
                         lambda p: {"model": mid, "input": [{"role": "user", "content": "hi"}],
                                    "max_output_tokens": PROBE_MAX_TOKENS})
        ms, mse = any_ok("POST", MSG_PATHS,
                         lambda p: {"model": mid, "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
                                    "max_tokens": PROBE_MAX_TOKENS,
                                    "messages": [{"role": "user", "content": "hi"}]})
        gated = GATE_MARKER in (cce + rse + mse)
        return mid, {"cc": cc, "rs": rs, "ms": ms, "gated": gated}

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for mid, res in ex.map(probe, models):
            results[mid] = res

    return {
        "region": region,
        "chat_completions": sorted(m for m, r in results.items() if r["cc"] and not r["gated"]),
        "responses": sorted(m for m, r in results.items() if r["rs"] and not r["gated"]),
        "messages": sorted(m for m, r in results.items() if r["ms"] and not r["gated"]),
    }


CAPS_COMMENT = (
    "Mantle model capability matrix by OpenAI-compatible API. The gateway "
    "models-filter interceptor filters /v1/models to these lists so Open WebUI "
    "only surfaces models that actually work on each connection's API. "
    "Regenerate with scripts/probe-model-capabilities.py (or the opt-in "
    "scheduled refresher). Account-gated models are excluded. Probed by "
    "invocation across /v1/* and /openai/v1/* paths — listing is not "
    "capability-aware."
)
