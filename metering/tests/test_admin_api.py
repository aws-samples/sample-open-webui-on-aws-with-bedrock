# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the admin API's pure auth/validation/format logic (no AWS).

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import os
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ → import pricing


def _load():
    os.environ.update({"TABLE": "t", "USER_POOL_ID": "us-east-1_test", "ALERTS_TOPIC_ARN": ""})
    spec = importlib.util.spec_from_file_location("admin_api", HERE.parent / "admin-api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_api"] = mod
    spec.loader.exec_module(mod)
    return mod


admin = _load()


class FakeDdb:
    """get/put/delete double for the override + alias mutation paths."""

    def __init__(self):
        self.items: dict = {}

    def get_item(self, TableName=None, Key=None, **kw):
        item = self.items.get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": item} if item else {}

    def put_item(self, TableName=None, Item=None, **kw):
        self.items[(Item["pk"]["S"], Item["sk"]["S"])] = Item
        return {}

    def delete_item(self, TableName=None, Key=None, **kw):
        self.items.pop((Key["pk"]["S"], Key["sk"]["S"]), None)
        return {}


def _claims(groups):
    return {"sub": "u1", "cognito:groups": groups}


def test_group_claim_parsing_shapes():
    # HTTP API renders list claims as "[a b]"; also accept real lists + csv
    assert admin._groups(_claims("[admin user]")) == ["admin", "user"]
    assert admin._groups(_claims(["admin", "user"])) == ["admin", "user"]
    assert admin._groups(_claims("admin,user")) == ["admin", "user"]
    assert admin._groups(_claims("")) == []


def test_is_admin_only_for_admin_groups():
    assert admin._is_admin(_claims("[admin]")) is True
    assert admin._is_admin(_claims("[webui-admins]")) is True
    assert admin._is_admin(_claims("[admins]")) is True
    # a lookalike / non-admin group must NOT confer admin
    assert admin._is_admin(_claims("[administrator]")) is False
    assert admin._is_admin(_claims("[power-users user]")) is False
    assert admin._is_admin(_claims("")) is False


def test_config_hides_admin_groups_from_non_admins():
    # security review L2: don't disclose the admin group names to non-admins
    event_admin = {"routeKey": "GET /config", "requestContext": {"authorizer": {"jwt": {"claims": _claims("[admin]")}}}}
    event_user = {"routeKey": "GET /config", "requestContext": {"authorizer": {"jwt": {"claims": _claims("[user]")}}}}
    import json

    admin_cfg = json.loads(admin.handler(event_admin, None)["body"])
    user_cfg = json.loads(admin.handler(event_user, None)["body"])
    assert admin_cfg["is_admin"] is True and "admin_groups" in admin_cfg
    assert user_cfg["is_admin"] is False and "admin_groups" not in user_cfg


def test_window_validation():
    assert admin._safe_window({"window": "2026-07"}) == "2026-07"
    # malformed windows fall back to the current window, never used raw as a key
    assert admin._safe_window({"window": "../../etc"}) == admin._window_now()
    assert admin._safe_window({"window": "2026-7"}) == admin._window_now()
    assert admin._safe_window({}) == admin._window_now()
    # trailing newline must NOT slip through ($ would allow it; \Z does not)
    assert admin._safe_window({"window": "2026-07\n"}) == admin._window_now()
    assert admin._safe_window({"window": "2026-07\nZZZ"}) == admin._window_now()


def test_int_param_bounds_and_fallback():
    assert admin._int_param({"limit": "50"}, "limit", 25, 1, 100) == 50
    assert admin._int_param({"limit": "9999"}, "limit", 25, 1, 100) == 100  # clamped to hi
    assert admin._int_param({"limit": "-5"}, "limit", 25, 1, 100) == 1      # clamped to lo
    assert admin._int_param({}, "limit", 25, 1, 100) == 25                  # default
    # non-numeric must fall back to the default, never raise (would 500)
    assert admin._int_param({"limit": "abc"}, "limit", 25, 1, 100) == 25
    assert admin._int_param({"limit": ""}, "limit", 25, 1, 100) == 25


def test_cursor_roundtrip():
    key = {"w": {"S": "2026-07"}, "used_usd": {"N": "1.5"}, "pk": {"S": "USE#x#2026-07"}, "sk": {"S": "COUNTER"}}
    assert admin._cursor_in(admin._cursor_out(key)) == key
    assert admin._cursor_out(None) is None
    assert admin._cursor_in(None) is None
    # a garbage cursor decodes to None instead of raising
    assert admin._cursor_in("!!!not-base64!!!") is None


def test_price_override_per_1m_validation_bounds(monkeypatch):
    """Req 5.4: rates are USD per 1M tokens, 0 ≤ v ≤ 1e6, model id must be
    settle-reachable. Valid writes carry rates map + scope + _UNIT."""
    fake = FakeDdb()
    monkeypatch.setattr(admin, "ddb", fake)
    with pytest.raises(ValueError):
        admin._put_price_override("Claude3Haiku", {"input": 5.5}, "a1")  # not a model id
    with pytest.raises(ValueError):
        admin._put_price_override("anthropic.claude-opus-5", {}, "a1")  # no direction
    with pytest.raises(ValueError):
        admin._put_price_override("anthropic.claude-opus-5", {"input": -1}, "a1")
    with pytest.raises(ValueError):
        admin._put_price_override("anthropic.claude-opus-5", {"output": 2e6}, "a1")
    out = admin._put_price_override("anthropic.claude-opus-5", {"input": 4.0, "output": 20.0, "note": "EDP"}, "a1")
    row = fake.items[("PRICING#anthropic.claude-opus-5", "OVERRIDE")]
    assert row["_UNIT"]["S"] == "USD/1M-tokens" and row["scope"]["S"] == "ALL"
    assert row["rates"]["M"]["input"]["N"] == "4.0"
    assert out  # audited after-image returned
    # audit row written
    assert any(pk.startswith("AUDIT#") for (pk, _sk) in fake.items)


def test_alias_binding_validates_and_audits(monkeypatch):
    fake = FakeDdb()
    monkeypatch.setattr(admin, "ddb", fake)
    with pytest.raises(ValueError):
        admin._put_alias({"price_list_name": "Ministral 8B 3.0", "model_id": "NotAnId"}, "a1")
    with pytest.raises(ValueError):
        admin._put_alias({"price_list_name": "", "model_id": "mistral.ministral-3-8b-instruct"}, "a1")
    out = admin._put_alias(
        {"price_list_name": "Ministral 8B 3.0", "model_id": "mistral.ministral-3-8b-instruct"}, "a1")
    assert out["model_id"] == "mistral.ministral-3-8b-instruct"
    assert ("PRICING#_ALIAS", "Ministral 8B 3.0") in fake.items
    assert admin._delete_alias("Ministral 8B 3.0", "a1")["deleted"] is True
    assert ("PRICING#_ALIAS", "Ministral 8B 3.0") not in fake.items


def test_alias_routes_require_admin_group():
    """Req 11.3: pricing alias mutation is admin-gated like every mutation."""
    for route, params in [("POST /pricing/alias", {}),
                          ("DELETE /pricing/alias/{name}", {"name": "X"})]:
        event = {"routeKey": route, "pathParameters": params,
                 "requestContext": {"authorizer": {"jwt": {"claims": _claims("[user]")}}},
                 "body": "{}"}
        r = admin.handler(event, None)
        assert r["statusCode"] == 403, route


def test_counter_row_derives_pct_and_total():
    item = {
        "pk": {"S": "USE#abc#2026-07"}, "sk": {"S": "COUNTER"}, "w": {"S": "2026-07"},
        "used_usd": {"N": "4.5"}, "est_usd": {"N": "0.5"}, "hard_limit_usd": {"N": "5"},
    }
    row = admin._counter_row(item, "user")
    assert row["sub"] == "abc"
    assert row["total_usd"] == 5.0  # used + max(0, est)
    assert row["pct_of_limit"] == 100.0
    assert "pk" not in row and "sk" not in row  # internal keys stripped from API output
