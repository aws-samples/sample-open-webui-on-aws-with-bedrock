# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared test session setup: keep every Lambda module import offline.

The Lambda modules construct boto3 clients at import time. Static fake
credentials make botocore's env provider win before any profile-based
provider (SSO/login sessions on dev machines would otherwise be consulted,
and no test here ever calls AWS)."""

import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.pop("AWS_PROFILE", None)
