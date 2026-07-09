# syntax=docker/dockerfile:1
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Overlay Dockerfile: official Open WebUI release + native Amazon Bedrock provider.
#
# Targets:
#   backend (DEFAULT — last stage): official image + 2 overlay modules + 5 backend
#            patches. No frontend rebuild; ships the official UI unchanged.
#   full    (opt-in, `--target full`): backend content + a UI rebuilt from upstream
#            source at the pinned tag with the admin Connections Bedrock panel
#            (2 frontend patches + 1 overlay module).
#
# Pinned upstream release: v0.10.2 (includes upstream's 0.10.2 security and
# access-control fixes). Index digest recorded at pin time:
#   ghcr.io/open-webui/open-webui:v0.10.2
#   sha256:9fcea9c6e32ab60b0498f3986c6cdf651ddbe61db48d2213a3d28048ddd673d4
# To hard-pin, build with:
#   --build-arg OPEN_WEBUI_VERSION=@sha256:9fcea9c6e32ab60b0498f3986c6cdf651ddbe61db48d2213a3d28048ddd673d4
#   (or edit the FROM line to use the digest directly).
ARG OPEN_WEBUI_VERSION=v0.10.2
ARG OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui

########## upstream official image
FROM ${OPEN_WEBUI_IMAGE}:${OPEN_WEBUI_VERSION} AS upstream

########## backend patcher (git tooling stays out of the runtime image)
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS patcher
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY --from=upstream /app/backend /work/backend
COPY patches/backend/ /tmp/patches/backend/
WORKDIR /work
RUN for p in /tmp/patches/backend/*.patch; do git apply --verbose -p1 "$p" || exit 1; done

########## frontend build (full target only; upstream source exists ONLY in this ephemeral stage)
FROM public.ecr.aws/docker/library/node:22-alpine AS frontend
ARG OPEN_WEBUI_VERSION
RUN apk add --no-cache git
RUN git clone --depth 1 --branch ${OPEN_WEBUI_VERSION} https://github.com/open-webui/open-webui /build
WORKDIR /build
COPY patches/frontend/ /tmp/patches/frontend/
RUN git apply --verbose /tmp/patches/frontend/*.patch
COPY overlay/src/ /build/src/
# Open WebUI's Vite build exceeds Node's default heap:
ENV NODE_OPTIONS=--max-old-space-size=8192
RUN npm ci && npm run build

########## OPT-IN TARGET: full — patched backend + rebuilt UI with the Bedrock admin panel
# (Duplicates the two backend COPY lines rather than inheriting from the backend
#  stage: Docker stages can only reference earlier stages, and keeping `backend`
#  last makes it the default for a bare `docker build .`.)
FROM upstream AS full
COPY overlay/backend/open_webui/ /app/backend/open_webui/
COPY --from=patcher /work/backend/ /app/backend/
COPY --from=frontend /build/build /app/build

########## DEFAULT TARGET: backend — official image + Bedrock provider only
FROM upstream AS backend
COPY overlay/backend/open_webui/ /app/backend/open_webui/
COPY --from=patcher /work/backend/ /app/backend/
