<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Product

This repository is an AWS deployment sample for running the **unmodified official Open WebUI image** on ECS Fargate with Amazon Bedrock. Open WebUI itself is not forked, patched, or built here; the integration is implemented through AWS infrastructure, runtime configuration, and small Python components.

## Core behavior

- CloudFront fronts an internal ALB and private ECS service.
- Amazon Cognito provides sign-in and per-user OAuth identity.
- An Amazon Bedrock AgentCore inference gateway forwards each user's identity to Bedrock.
- Three model lanes support Chat Completions, Responses, and Anthropic Messages APIs.
- `config/model-capabilities.json` controls which models appear in each lane so users only see compatible models.
- Aurora PostgreSQL/pgvector, ElastiCache Redis, S3, and Secrets Manager provide application data and supporting services.
- An optional metering stack reserves quota, captures Open WebUI usage, settles ledger and counter state, refreshes pricing, and exposes an administrative and self-service console.

## Product constraints

- Preserve the upstream, digest-pinned Open WebUI image model; do not add an application fork or image build without an explicit architecture change.
- Keep model compatibility data separate from routing code and regenerate the capability matrix through `scripts/probe-model-capabilities.py`.
- Preserve end-to-end per-user identity and private-network/security boundaries when changing authentication, gateway, or compute behavior.
- Keep metering optional and preserve its explicit availability-first posture unless an architecture decision changes the enforcement contract.
- Model and AgentCore availability is region-dependent; `us-east-1` is the documented default for the full three-lane experience.
