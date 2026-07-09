# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CloudFormation custom resource: AgentCore Gateway inference *target*.

CloudFormation supports AWS::BedrockAgentCore::Gateway natively, but there is
no CFN resource type for inference targets yet. This Lambda creates/deletes the
bedrock-mantle inference connector target on the gateway via the
bedrock-agentcore-control API, so the whole gateway lifecycle stays in CDK.

Properties:
  GatewayIdentifier  - the gateway id (from the CFN Gateway resource)
  TargetName         - inference target name (default "bedrock")
  ConnectorId        - built-in connector id (default "bedrock-mantle")

The gateway's own execution role provides outbound auth (GATEWAY_IAM_ROLE), so
no credentials are configured here.
"""

import json
import urllib.request

import boto3

control = boto3.client("bedrock-agentcore-control")


def _send(event, context, status, physical_id, data=None, reason=""):
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch log stream {context.log_stream_name}",
        "PhysicalResourceId": physical_id or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": data or {},
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                 headers={"content-type": "", "content-length": str(len(body))})
    urllib.request.urlopen(req)


def _wait_target(gateway_id, target_id, want_ready=True):
    import time
    for _ in range(60):
        try:
            s = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"]
        except control.exceptions.ResourceNotFoundException:
            if not want_ready:
                return "DELETED"
            raise
        if want_ready and s in ("READY", "FAILED"):
            return s
        time.sleep(5)
    return "TIMEOUT"


def handler(event, context):
    rtype = event["RequestType"]
    props = event.get("ResourceProperties", {})
    gateway_id = props["GatewayIdentifier"]
    target_name = props.get("TargetName", "bedrock")
    connector_id = props.get("ConnectorId", "bedrock-mantle")
    physical_id = event.get("PhysicalResourceId", "")

    try:
        if rtype in ("Create", "Update"):
            # On Update, delete any existing target of this name first (id may change).
            if rtype == "Update" and physical_id and "/" in physical_id:
                old_target = physical_id.split("/", 1)[1]
                try:
                    control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=old_target)
                    _wait_target(gateway_id, old_target, want_ready=False)
                except control.exceptions.ResourceNotFoundException:
                    pass

            r = control.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=target_name,
                targetConfiguration={"inference": {"connector": {"source": {"connectorId": connector_id}}}},
                credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            )
            target_id = r["targetId"]
            status = _wait_target(gateway_id, target_id, want_ready=True)
            if status != "READY":
                _send(event, context, "FAILED", f"{gateway_id}/{target_id}",
                      reason=f"target status {status}")
                return
            _send(event, context, "SUCCESS", f"{gateway_id}/{target_id}",
                  data={"TargetId": target_id, "TargetName": target_name})

        elif rtype == "Delete":
            if physical_id and "/" in physical_id:
                target_id = physical_id.split("/", 1)[1]
                try:
                    control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
                    _wait_target(gateway_id, target_id, want_ready=False)
                except control.exceptions.ResourceNotFoundException:
                    pass
            _send(event, context, "SUCCESS", physical_id)

    except Exception as e:  # noqa: BLE001 — surface any failure to CFN
        _send(event, context, "FAILED", physical_id, reason=str(e)[:900])
