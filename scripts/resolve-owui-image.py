#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Resolve the Open WebUI container image to an immutable digest reference.

Called by deploy.sh before `cdk deploy` (and usable standalone). Turns the
operator's OPEN_WEBUI_IMAGE selection into the exact image ECS should run,
printing the final reference to stdout and progress/warnings to stderr.

Selection contract (mirrors .env.example):

  no argument / empty   Discover the latest official Open WebUI RELEASE tag
                        (GitHub releases API), then resolve it to its multi-arch
                        index digest on ghcr. Fails hard if either step fails —
                        silently deploying something other than the latest
                        release would defeat the point of the default.
  tag reference         e.g. ghcr.io/open-webui/open-webui:v0.11.0 — resolve the
                        tag to its digest. If ghcr can't be reached, warn and
                        pass the tag through unresolved (the deploy machine may
                        have restricted egress while the ECS tasks do not).
  digest reference      e.g. ghcr.io/open-webui/open-webui@sha256:… — used
                        verbatim; no network calls.
  other registries      (e.g. a private ECR mirror) — passed through verbatim
                        with a note; pin a digest yourself for reproducibility.

Why resolve at all: a task definition that carries a floating tag re-resolves
it on every task launch (autoscaling, crash replacement, --force-new-deployment),
so two tasks in one service can silently run different upstream versions — and
upstream runs its own DB migrations on container start. Carrying a digest in
the task definition makes every task launch byte-identical until the operator
deliberately deploys a new version. Note that ghcr's :latest tag is upstream's
main-branch build, not the newest release — another reason the default targets
the release feed.

Only stdlib is used (urllib); no docker, jq, or curl required.
"""

import json
import re
import sys
import urllib.error
import urllib.request

OFFICIAL_REPO = 'ghcr.io/open-webui/open-webui'
RELEASES_URL = 'https://api.github.com/repos/open-webui/open-webui/releases/latest'
TOKEN_URL = ('https://ghcr.io/token'
             '?scope=repository:open-webui/open-webui:pull&service=ghcr.io')
MANIFEST_URL = 'https://ghcr.io/v2/open-webui/open-webui/manifests/{tag}'
ACCEPT = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])
TIMEOUT = 15  # seconds per request


def _info(msg: str) -> None:
    print(msg, file=sys.stderr)


def _http(url: str, headers=None, method: str = 'GET'):
    req = urllib.request.Request(url, headers={'User-Agent': 'owui-sample-deploy', **(headers or {})}, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT)  # nosec B310 — fixed https URLs


def latest_release_tag() -> str:
    """The newest official release tag (never a draft or prerelease)."""
    with _http(RELEASES_URL) as resp:
        tag = json.load(resp).get('tag_name')
    if not tag or not re.match(r'^v\d', tag):
        raise RuntimeError(f'unexpected release tag from GitHub API: {tag!r}')
    return tag


def resolve_tag_digest(tag: str) -> str:
    """The sha256 index digest a ghcr tag currently points at (anonymous pull token flow)."""
    with _http(TOKEN_URL) as resp:
        token = json.load(resp)['token']
    with _http(MANIFEST_URL.format(tag=tag),
               headers={'Authorization': f'Bearer {token}', 'Accept': ACCEPT},
               method='HEAD') as resp:
        digest = resp.headers.get('Docker-Content-Digest')
    if not digest or not digest.startswith('sha256:'):
        raise RuntimeError(f'ghcr returned no content digest for tag {tag!r}')
    return digest


def main() -> int:
    ref = sys.argv[1].strip() if len(sys.argv) > 1 else ''

    # Explicit digest: immutable already, nothing to do (works offline).
    if '@sha256:' in ref:
        _info(f'[resolve-owui-image] digest supplied, using verbatim: {ref}')
        print(ref)
        return 0

    # A registry other than the official ghcr repo (e.g. a private mirror):
    # pass through untouched — we can't resolve foreign registries anonymously.
    if ref and not ref.startswith(OFFICIAL_REPO):
        _info(f'[resolve-owui-image] custom registry, passing through verbatim: {ref}')
        _info('[resolve-owui-image] note: pin an @sha256 digest for reproducible deploys.')
        print(ref)
        return 0

    if ref:
        # Official repo with a tag (or bare, which means :latest in registry terms).
        tag = ref[len(OFFICIAL_REPO):].lstrip(':') or 'latest'
        if tag == 'latest':
            _info('[resolve-owui-image] note: ghcr\'s :latest is upstream\'s main-branch '
                  'build, not the newest release. Prefer a vX.Y.Z release tag.')
        try:
            digest = resolve_tag_digest(tag)
        except Exception as exc:  # noqa: BLE001 — any failure degrades to tag mode
            _info(f'[resolve-owui-image] WARNING: could not resolve tag {tag!r} to a digest ({exc}).')
            _info('[resolve-owui-image] Proceeding with the tag as-is: the deploy still works, but the')
            _info('[resolve-owui-image] task definition will float — every task launch re-resolves the')
            _info('[resolve-owui-image] tag, so tasks can drift across upstream releases (and upstream')
            _info('[resolve-owui-image] auto-runs DB migrations on start). Pin an @sha256 digest to avoid this.')
            print(ref)
            return 0
        _info(f'[resolve-owui-image] resolved {tag} -> {digest}')
        print(f'{OFFICIAL_REPO}@{digest}')
        return 0

    # Default: latest official release, resolved to its digest.
    try:
        tag = latest_release_tag()
        _info(f'[resolve-owui-image] latest official release: {tag}')
        digest = resolve_tag_digest(tag)
        _info(f'[resolve-owui-image] resolved {tag} -> {digest}')
    except Exception as exc:  # noqa: BLE001 — fail hard: never guess the default
        _info(f'[resolve-owui-image] ERROR: could not determine the latest Open WebUI release ({exc}).')
        _info('[resolve-owui-image] The default requires reaching api.github.com and ghcr.io.')
        _info('[resolve-owui-image] Either retry, or set OPEN_WEBUI_IMAGE in .env to an explicit')
        _info('[resolve-owui-image] release tag or @sha256 digest and re-run ./deploy.sh.')
        return 1
    print(f'{OFFICIAL_REPO}@{digest}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
