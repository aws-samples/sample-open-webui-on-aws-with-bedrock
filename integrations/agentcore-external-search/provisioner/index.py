"""Pin the CFN-owned search target; creation and deletion remain native CFN operations."""

import copy
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


TARGET_NAME = "web-search-tool"
CONNECTOR_ID = "web-search"
CONNECTOR_VERSION = "1.2.0"
POLL_ATTEMPTS = 20
POLL_DELAY = 3
WAIT_SECONDS = 60
UPDATE_ATTEMPTS = 3
control = None


def _client():
    global control
    if control is None:
        control = boto3.client(
            "bedrock-agentcore-control",
            config=Config(connect_timeout=3, read_timeout=5,
                          retries={"mode": "standard", "total_max_attempts": 2}),
        )
    return control


def _get(gateway_id, target_id):
    try:
        target = _client().get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise
    source = target.get("targetConfiguration", {}).get("mcp", {}).get("connector", {}).get("source", {})
    if (target.get("targetId") != target_id or target.get("name") != TARGET_NAME
            or source.get("connectorId") != CONNECTOR_ID):
        raise RuntimeError("Native search target identity does not match")
    return target


def _wait(gateway_id, target_id, expected=None):
    deadline = time.monotonic() + WAIT_SECONDS
    for attempt in range(POLL_ATTEMPTS):
        target = _get(gateway_id, target_id)
        if target is not None:
            status = target["status"]
            if expected is None and status in ("READY", "UPDATE_UNSUCCESSFUL"):
                return target
            if expected is not None and status == "READY" and all(target.get(key) == value for key, value in expected.items()):
                return target
            if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL", "DELETING",
                          "CREATE_PENDING_AUTH", "UPDATE_PENDING_AUTH", "SYNCHRONIZE_PENDING_AUTH"):
                raise RuntimeError("Native search target failed readiness")
        if time.monotonic() >= deadline or attempt == POLL_ATTEMPTS - 1:
            break
        time.sleep(POLL_DELAY)
    raise RuntimeError("Native search target readiness timed out")


def _pin(gateway_id, target_id):
    current = _wait(gateway_id, target_id)
    source = current["targetConfiguration"]["mcp"]["connector"]["source"]
    if current["status"] == "READY" and source.get("version") == CONNECTOR_VERSION:
        return
    expected = {key: copy.deepcopy(current[key]) for key in (
        "name", "description", "targetConfiguration", "credentialProviderConfigurations",
        "metadataConfiguration", "privateEndpoint",
    ) if key in current}
    expected["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = CONNECTOR_VERSION
    for attempt in range(UPDATE_ATTEMPTS):
        try:
            _client().update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, **expected)
            break
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConflictException" or attempt == UPDATE_ATTEMPTS - 1:
                raise
            time.sleep(POLL_DELAY)
    _wait(gateway_id, target_id, expected=expected)


def _handle(event):
    request_type = event["RequestType"]
    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "uncreated-web-search-version")}
    if request_type not in ("Create", "Update"):
        raise ValueError("Unsupported resource operation")
    props = event["ResourceProperties"]
    for key, value in (("TargetName", TARGET_NAME), ("ConnectorId", CONNECTOR_ID),
                       ("ConnectorVersion", CONNECTOR_VERSION)):
        if props.get(key) != value:
            raise ValueError("Unsupported search version-pin configuration")
    gateway_id = props["GatewayIdentifier"]
    target_id = props["TargetId"]
    if not all(isinstance(identifier, str) and identifier and "/" not in identifier
               for identifier in (gateway_id, target_id)):
        raise ValueError("Explicit native gateway and target IDs are required")
    physical_id = f"web-search-version/{gateway_id}/{target_id}"
    if request_type == "Update":
        old = event["OldResourceProperties"]
        if (any(props[key] != old.get(key) for key in ("GatewayIdentifier", "TargetId", "TargetName", "ConnectorId"))
                or event.get("PhysicalResourceId") != physical_id):
            raise ValueError("Gateway and target identity changes require a separately approved replacement")
    _pin(gateway_id, target_id)
    return {"PhysicalResourceId": physical_id,
            "Data": {"TargetId": target_id, "ToolName": f"{TARGET_NAME}___WebSearch",
                     "ConnectorVersion": CONNECTOR_VERSION}}


def handler(event, context):
    """Return the CDK Provider contract; never expose raw SDK failures."""
    try:
        return _handle(event)
    except Exception:
        raise RuntimeError("Search version pin failed; inspect the native target status with authorized tooling") from None
