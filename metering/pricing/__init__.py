# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pricing package — model identity, rate resolution, offer parsing.

One automated price source (the AWS Price List) plus operator overrides,
resolved identically by every runtime consumer:

    metering/debit             settle-time pricing
    gateway/metering-interceptor  admission-time estimates
    metering/pricing-refresher    catalog refresh (also uses .offers)

Pure Python, stdlib only, no AWS calls — unit-testable offline. The CDK
stacks stage this directory into each consumer Lambda asset at synth time
(design D6), so `from pricing import identity, resolver` works both in the
Lambda task root and in the repo tree.
"""

from . import identity, resolver  # noqa: F401

__all__ = ["identity", "resolver"]
