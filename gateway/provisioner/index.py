# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CloudFormation custom resource: AgentCore Gateway inference *target*.

CloudFormation supports AWS::BedrockAgentCore::Gateway natively, but there is
no CFN resource type for inference targets yet. This Lambda is the onEvent
handler for a CDK `custom-resources.Provider`, so it follows the *framework*
contract: it RETURNS a result object ({"PhysicalResourceId", "Data"}) rather
than POSTing to a response URL. The provider framework relays that to
CloudFormation and polls isComplete if configured.

It creates/deletes the bedrock-mantle inference connector target on the gateway
via the bedrock-agentcore-control API, so the whole gateway lifecycle stays in
CDK. The gateway's own execution role provides outbound auth (GATEWAY_IAM_ROLE),
so no credentials are configured on the target.

Properties:
  GatewayIdentifier  - the gateway id (from the CFN Gateway resource)
  TargetName         - inference target name (default "bedrock")
  ConnectorId        - built-in connector id (default "bedrock-mantle")
"""

import time

import boto3

control = boto3.client("bedrock-agentcore-control")


def _wait_target(gateway_id, target_id, want_ready=True, tries=48, delay=5):
    for _ in range(tries):
        try:
            s = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"]
        except control.exceptions.ResourceNotFoundException:
            if not want_ready:
                return "DELETED"
            raise
        if want_ready and s in ("READY", "FAILED"):
            return s
        time.sleep(delay)
    return "TIMEOUT"


def _create(gateway_id, target_name, connector_id):
    r = control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        targetConfiguration={"inference": {"connector": {"source": {"connectorId": connector_id}}}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    target_id = r["targetId"]
    status = _wait_target(gateway_id, target_id, want_ready=True)
    if status != "READY":
        raise RuntimeError(f"inference target {target_id} reached status {status}, not READY")
    return target_id


def _delete(gateway_id, target_id):
    if not target_id:
        return
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        _wait_target(gateway_id, target_id, want_ready=False)
    except control.exceptions.ResourceNotFoundException:
        pass


def handler(event, context):
    """CDK custom-resources.Provider onEvent contract: return a dict; raise to fail."""
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {}) or {}
    gateway_id = props["GatewayIdentifier"]
    target_name = props.get("TargetName", "bedrock")
    connector_id = props.get("ConnectorId", "bedrock-mantle")
    physical_id = event.get("PhysicalResourceId")

    if request_type == "Create":
        target_id = _create(gateway_id, target_name, connector_id)
        return {"PhysicalResourceId": f"{gateway_id}/{target_id}",
                "Data": {"TargetId": target_id, "TargetName": target_name}}

    if request_type == "Update":
        # Replace: delete the old target (from the physical id), create a new one.
        if physical_id and "/" in physical_id:
            _delete(gateway_id, physical_id.split("/", 1)[1])
        target_id = _create(gateway_id, target_name, connector_id)
        return {"PhysicalResourceId": f"{gateway_id}/{target_id}",
                "Data": {"TargetId": target_id, "TargetName": target_name}}

    if request_type == "Delete":
        if physical_id and "/" in physical_id:
            _delete(gateway_id, physical_id.split("/", 1)[1])
        return {"PhysicalResourceId": physical_id or f"{gateway_id}/none"}

    raise ValueError(f"unexpected RequestType {request_type}")
