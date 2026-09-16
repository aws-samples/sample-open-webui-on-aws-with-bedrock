"""Offline tests for the native target's version-only lifecycle."""

import copy
import datetime
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber


spec = importlib.util.spec_from_file_location(
    "external_search_provisioner", Path(__file__).parents[1] / "provisioner" / "index.py")
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class VersionPinTests(unittest.TestCase):
    def setUp(self):
        self.props = {"GatewayIdentifier": "gateway-123", "TargetId": "target-123",
                      "TargetName": "web-search-tool", "ConnectorId": "web-search", "ConnectorVersion": "1.2.0"}
        self.physical_id = "web-search-version/gateway-123/target-123"
        self.event = {"RequestType": "Create", "ResourceProperties": self.props,
                      "OldResourceProperties": copy.deepcopy(self.props)}
        self.target = {
            "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gateway-123",
            "targetId": "target-123", "status": "READY", "name": "web-search-tool", "description": "Keep this",
            "createdAt": datetime.datetime(2026, 9, 16, tzinfo=datetime.timezone.utc),
            "updatedAt": datetime.datetime(2026, 9, 16, tzinfo=datetime.timezone.utc),
            "targetConfiguration": {"mcp": {"connector": {
                "source": {"connectorId": "web-search", "version": "1.1.0"}, "enabled": ["WebSearch"],
                "configurations": [{"name": "WebSearch", "parameterValues": {}}]}}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        }
        self.ready = copy.deepcopy(self.target)
        self.ready["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = "1.2.0"
        self.control = Mock()
        self.control.get_gateway_target.side_effect = [self.target, self.ready]
        client_patch = patch.object(provider, "control", self.control)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        sleep_patch = patch.object(provider.time, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def tearDown(self):
        self.control.create_gateway_target.assert_not_called()
        self.control.delete_gateway_target.assert_not_called()
        self.control.list_gateway_targets.assert_not_called()

    def call(self, operation="Create"):
        self.event["RequestType"] = operation
        if operation == "Update":
            self.event["PhysicalResourceId"] = self.physical_id
        return provider.handler(self.event, None)

    def error(self, code):
        return ClientError({"Error": {"Code": code, "Message": "private-service-payload"}}, "GetGatewayTarget")

    def test_sdk_wire_contract_pins_12_and_waits_for_ready(self):
        client = boto3.client("bedrock-agentcore-control", region_name="us-east-1",
                              aws_access_key_id="test", aws_secret_access_key="test")
        identity = {"gatewayIdentifier": "gateway-123", "targetId": "target-123"}
        expected = {**identity, **{key: self.ready[key] for key in (
            "name", "description", "targetConfiguration", "credentialProviderConfigurations")}}
        with Stubber(client) as stubber, patch.object(provider, "control", client):
            stubber.add_response("get_gateway_target", self.target, identity)
            stubber.add_response("update_gateway_target", self.ready, expected)
            stubber.add_response("get_gateway_target", self.ready, identity)
            self.assertEqual(self.call(), {"PhysicalResourceId": self.physical_id, "Data": {
                "TargetId": "target-123", "ToolName": "web-search-tool___WebSearch", "ConnectorVersion": "1.2.0"}})
            stubber.assert_no_pending_responses()

    def test_create_and_update_are_idempotent(self):
        self.control.get_gateway_target.side_effect = [self.ready, self.ready]
        self.assertEqual(self.call()["PhysicalResourceId"], self.physical_id)
        self.assertEqual(self.call("Update")["PhysicalResourceId"], self.physical_id)
        self.control.update_gateway_target.assert_not_called()

    def test_preserves_other_fields_without_mutating_or_sending_readonly_fields(self):
        configuration = self.target["targetConfiguration"]["mcp"]["connector"]["configurations"][0]
        configuration["parameterValues"] = {"domainFilter": {"exclude": ["example.com"]}}
        configuration["parameterOverrides"] = [{"path": "maxResults", "visible": False}]
        self.target["metadataConfiguration"] = {"allowedResponseHeaders": ["x-search-id"]}
        self.target["privateEndpoint"] = {"selfManagedLatticeResource": {
            "resourceConfigurationIdentifier": "arn:aws:vpc-lattice:us-east-1:123456789012:resourceconfiguration/rcfg-0123456789abcdef0"}}
        original = copy.deepcopy(self.target)
        pinned = copy.deepcopy(original)
        pinned["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = "1.2.0"
        self.control.get_gateway_target.side_effect = [self.target, pinned]
        self.call()
        expected = {key: pinned[key] for key in ("name", "description", "targetConfiguration",
                    "credentialProviderConfigurations", "metadataConfiguration", "privateEndpoint")}
        self.control.update_gateway_target.assert_called_once_with(
            gatewayIdentifier="gateway-123", targetId="target-123", **expected)
        self.assertEqual(self.target, original)

    def test_waits_for_creation_and_synchronized_pinned_configuration(self):
        self.control.get_gateway_target.side_effect = [
            self.error("ResourceNotFoundException"), {**self.target, "status": "CREATING"}, self.target,
            {**self.ready, "status": "UPDATING"}, {**self.ready, "status": "SYNCHRONIZING"}, self.target, self.ready]
        self.call()
        self.assertEqual(self.sleep.call_count, 5)

    def test_conflicts_retry_and_succeed_within_bound(self):
        self.control.update_gateway_target.side_effect = [self.error("ConflictException"), self.error("ConflictException"), self.ready]
        self.call()
        self.assertEqual(self.control.update_gateway_target.call_count, provider.UPDATE_ATTEMPTS)

    def test_exhausted_conflicts_fail_without_owning_cleanup(self):
        self.control.update_gateway_target.side_effect = self.error("ConflictException")
        with self.assertRaisesRegex(RuntimeError, "Search version pin failed"):
            self.call()
        self.assertEqual(self.control.update_gateway_target.call_count, 3)

    def test_delete_is_unconditional_noop(self):
        for physical_id in (self.physical_id, "failed-create", "old-gateway/old-target"):
            self.assertEqual(provider.handler({"RequestType": "Delete", "PhysicalResourceId": physical_id}, None),
                             {"PhysicalResourceId": physical_id})
        provider.handler({"RequestType": "Delete"}, None)
        self.assertEqual(self.control.mock_calls, [])

    def test_wrong_resource_or_configuration_is_rejected_before_sdk_calls(self):
        cases = [{**self.event, "ResourceProperties": {**self.props, key: value}} for key, value in (
            ("GatewayIdentifier", "wrong/id"), ("TargetId", ""), ("TargetName", "another"),
            ("ConnectorId", "another"), ("ConnectorVersion", "latest"))]
        cases.append({**self.event, "RequestType": "Unsupported"})
        for key in ("GatewayIdentifier", "TargetId"):
            cases.append({**self.event, "RequestType": "Update", "PhysicalResourceId": self.physical_id,
                          "OldResourceProperties": {**self.props, key: "replaced"}})
        cases.append({**self.event, "RequestType": "Update", "PhysicalResourceId": "wrong"})
        for event in cases:
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                provider.handler(event, None)
        self.assertEqual(self.control.mock_calls, [])

    def test_native_identity_mismatch_fails_closed(self):
        wrong_connector = copy.deepcopy(self.target)
        wrong_connector["targetConfiguration"]["mcp"]["connector"]["source"]["connectorId"] = "another"
        for target in ({**self.target, "name": "another"}, {**self.target, "targetId": "another"}, wrong_connector):
            self.control.get_gateway_target.side_effect = [target]
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                self.call()
        self.control.update_gateway_target.assert_not_called()

    def test_readiness_poll_count_and_deadline_are_bounded(self):
        self.control.get_gateway_target.side_effect = None
        self.control.get_gateway_target.return_value = {**self.target, "status": "CREATING"}
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(self.control.get_gateway_target.call_count, 20)
        self.control.reset_mock()
        with patch.object(provider.time, "monotonic", side_effect=[0, provider.WAIT_SECONDS + 1]):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                provider._wait("gateway-123", "target-123")
        self.assertEqual(self.control.get_gateway_target.call_count, 1)

    def test_wrong_pin_or_parameter_drift_never_reports_success(self):
        drifted = copy.deepcopy(self.ready)
        drifted["targetConfiguration"]["mcp"]["connector"]["configurations"][0]["parameterValues"] = {"unexpected": True}
        for target in (self.target, drifted):
            self.control.get_gateway_target.side_effect = [self.target] + [target] * provider.POLL_ATTEMPTS
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                self.call()

    def test_terminal_readiness_errors_fail_promptly(self):
        for status in ("FAILED", "SYNCHRONIZE_UNSUCCESSFUL", "DELETING", "UPDATE_PENDING_AUTH"):
            self.control.get_gateway_target.side_effect = [{**self.target, "status": status}]
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.call()
        self.sleep.assert_not_called()

    def test_failed_update_can_be_repaired_on_rollback(self):
        self.control.get_gateway_target.side_effect = [{**self.ready, "status": "UPDATE_UNSUCCESSFUL"}, self.ready]
        self.call("Update")
        self.control.update_gateway_target.assert_called_once()

    def test_access_denied_is_not_missing_and_errors_are_sanitized(self):
        self.control.get_gateway_target.side_effect = self.error("AccessDeniedException")
        with self.assertRaisesRegex(RuntimeError, "^Search version pin failed;") as caught:
            self.call()
        self.assertNotIn("private-service-payload", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.control.get_gateway_target.call_count, 1)


if __name__ == "__main__":
    unittest.main()
