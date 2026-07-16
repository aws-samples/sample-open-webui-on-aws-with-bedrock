# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the admin API's pure auth/validation/format logic (no AWS).

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load():
    os.environ.update({"TABLE": "t", "USER_POOL_ID": "us-east-1_test", "ALERTS_TOPIC_ARN": ""})
    spec = importlib.util.spec_from_file_location("admin_api", HERE.parent / "admin-api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_api"] = mod
    spec.loader.exec_module(mod)
    return mod


admin = _load()


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


def test_cursor_roundtrip():
    key = {"w": {"S": "2026-07"}, "used_usd": {"N": "1.5"}, "pk": {"S": "USE#x#2026-07"}, "sk": {"S": "COUNTER"}}
    assert admin._cursor_in(admin._cursor_out(key)) == key
    assert admin._cursor_out(None) is None
    assert admin._cursor_in(None) is None
    # a garbage cursor decodes to None instead of raising
    assert admin._cursor_in("!!!not-base64!!!") is None


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
