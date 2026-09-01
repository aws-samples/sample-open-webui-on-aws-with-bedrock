<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Diagram sources

These Mermaid files are the maintainable sources for the rendered SVGs in
[`../images/`](../images/). The SVGs are generated artifacts; do not edit them
by hand.

| Source | Purpose | Rendered files |
|---|---|---|
| [`architecture.mmd`](architecture.mmd) | Canonical system architecture shared by the repository front door and architecture/deployment guides | `architecture-light.svg`, `architecture-dark.svg` |
| [`metering-flow.mmd`](metering-flow.mmd) | Mechanism sequence for the optional metering lifecycle, not a second system topology | `metering-flow-light.svg`, `metering-flow-dark.svg` |

## Regenerate

Run from the repository root:

```bash
node scripts/render-doc-diagrams.mjs
node scripts/docs-integrity.mjs
```

The renderer invokes the pinned `@mermaid-js/mermaid-cli@11.16.0`, generates
neutral/light and dark SVGs at width 1800, and stamps each output with both the
source SHA-256 and the rendered-body SHA-256. The integrity check catches stale
source, body edits, missing/untracked assets, and size/type problems. Mermaid
flowchart layout is not byte-stable across every render, so CI validates the
committed stamps instead of performing a flaky byte-for-byte re-render. `npx`
downloads the pinned CLI into the local npm cache; no diagram package is added
to this repository.

The Markdown embeds use GitHub's `#gh-light-mode-only` and
`#gh-dark-mode-only` URL fragments, so the selected asset follows the GitHub
page theme rather than the operating-system preference. Suggested alt text is
maintained with each embed, not inside the generated SVG:

- Architecture: “Architecture flow from a user and Cognito through CloudFront,
  private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and
  optional metering services.”
- Metering: “Optional metering lifecycle showing real-time admission,
  asynchronous settlement, pricing coverage, estimate recovery, operator
  controls, and assurance signals.”

The diagrams intentionally use generic shapes and factual service names rather
than AWS or Open WebUI logos. Re-check the current AWS Architecture Icons, AWS
Trademark Guidelines, and Open WebUI brand terms before adding branded assets.
