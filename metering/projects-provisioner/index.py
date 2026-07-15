# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CloudFormation custom resource: Bedrock (mantle) Projects per cost-center group.

Bedrock Projects are the mantle-native attribution primitive (design M2): the
gateway interceptor injects `OpenAI-Project` / `anthropic-workspace-id` per
request, and the projects' cost-allocation tags flow to Cost Explorer / CUR.
This provisioner creates one project per group in config/metering-groups.json
(passed as the Groups property) plus a catch-all, and returns the
group→project-id map the interceptor uses.

Projects cannot be deleted, only archived — on Delete we archive the projects
we created (archived projects reject new inference and carry no documented
cost). CDK custom-resources Provider framework contract: return a dict.
"""

import json
import logging
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

log = logging.getLogger()
log.setLevel(logging.INFO)


def _call(method: str, path: str, region: str, payload: dict | None = None) -> tuple[int, dict]:
    url = f"https://bedrock-mantle.{region}.api.aws{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(creds, "bedrock", region).add_auth(req)
    http_req = urllib.request.Request(url, data=data, headers=dict(req.headers), method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(http_req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, json.loads(e.read() or b"{}")


def _existing_projects(region: str) -> dict:
    status, body = _call("GET", "/v1/organization/projects", region)
    if status != 200:
        raise RuntimeError(f"list projects failed: {status} {json.dumps(body)[:200]}")
    return {p["name"]: p for p in body.get("data", [])}


def _ensure_projects(groups: list[str], prefix: str, region: str, tags: dict) -> dict:
    existing = _existing_projects(region)
    mapping: dict[str, str] = {}
    for group in [*groups, "*"]:
        name = f"{prefix}{'unassigned' if group == '*' else group}"
        proj = existing.get(name)
        if proj and proj.get("status") == "active":
            mapping[group] = proj["id"]
            continue
        status, body = _call(
            "POST", "/v1/organization/projects", region,
            {"name": name, "tags": {**tags, "CostCenter": "unassigned" if group == "*" else group}},
        )
        if status != 200:
            raise RuntimeError(f"create project {name} failed: {status} {json.dumps(body)[:200]}")
        mapping[group] = body["id"]
        log.info(f"created project {name} -> {body['id']}")
    return mapping


def _archive_projects(mapping: dict, region: str):
    for group, pid in mapping.items():
        status, body = _call("POST", f"/v1/organization/projects/{pid}/archive", region)
        log.info(f"archive {pid} ({group}) -> {status}")


def handler(event, context):
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {}) or {}
    groups = json.loads(props.get("Groups", "[]"))
    prefix = props.get("NamePrefix", "owui-metering-")
    region = props.get("Region")
    tags = {"App": props.get("AppTag", "open-webui-sample")}

    if request_type in ("Create", "Update"):
        mapping = _ensure_projects(groups, prefix, region, tags)
        return {
            "PhysicalResourceId": f"metering-projects-{prefix}",
            "Data": {"ProjectMapJson": json.dumps(mapping)},
        }

    if request_type == "Delete":
        old = event.get("PhysicalResourceId", "")
        try:
            existing = _existing_projects(region)
            mapping = {
                name.removeprefix(prefix): p["id"]
                for name, p in existing.items()
                if name.startswith(prefix) and p.get("status") == "active"
            }
            _archive_projects(mapping, region)
        except Exception as e:  # noqa: BLE001 — best-effort archive on teardown
            log.warning(f"archive-on-delete best effort failed: {e}")
        return {"PhysicalResourceId": old or f"metering-projects-{prefix}"}

    raise ValueError(f"unexpected RequestType {request_type}")
