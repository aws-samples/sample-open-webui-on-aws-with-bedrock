#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# CI helper: verify every patch in this repo applies cleanly against a pristine
# checkout of upstream Open WebUI at the pinned tag.
set -euo pipefail

OPEN_WEBUI_VERSION="${OPEN_WEBUI_VERSION:-v0.10.2}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Cloning open-webui/open-webui @ ${OPEN_WEBUI_VERSION} ..."
git clone --quiet --depth 1 --branch "${OPEN_WEBUI_VERSION}" \
  https://github.com/open-webui/open-webui "$WORKDIR/upstream"

cd "$WORKDIR/upstream"
rc=0
for p in "$REPO_ROOT"/patches/backend/*.patch "$REPO_ROOT"/patches/frontend/*.patch; do
  if git apply --check "$p" 2>/dev/null; then
    echo "OK      $(basename "$p")"
  else
    echo "FAILED  $(basename "$p")" >&2
    rc=1
  fi
done
exit $rc
