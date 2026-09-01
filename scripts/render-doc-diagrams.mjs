#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Render the documentation diagrams with one pinned Mermaid CLI, then stamp
// each SVG with its source SHA-256 so docs-integrity.mjs can detect drift.

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const cli = '@mermaid-js/mermaid-cli@11.16.0';
const renders = [
  ['docs/diagrams/architecture.mmd', 'docs/images/architecture-light.svg', 'neutral', 'open-webui-architecture-light'],
  ['docs/diagrams/architecture.mmd', 'docs/images/architecture-dark.svg', 'dark', 'open-webui-architecture-dark'],
  ['docs/diagrams/metering-flow.mmd', 'docs/images/metering-flow-light.svg', 'neutral', 'open-webui-metering-flow-light'],
  ['docs/diagrams/metering-flow.mmd', 'docs/images/metering-flow-dark.svg', 'dark', 'open-webui-metering-flow-dark'],
];

for (const [source, output, theme, svgId] of renders) {
  const result = spawnSync(
    'npx',
    [
      '--yes', cli,
      '--input', source,
      '--output', output,
      '--theme', theme,
      '--backgroundColor', 'transparent',
      '--width', '1800',
      '--svgId', svgId,
    ],
    { cwd: root, encoding: 'utf8', stdio: 'inherit' },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);

  const sourceHash = createHash('sha256').update(readFileSync(resolve(root, source))).digest('hex');
  const outputPath = resolve(root, output);
  const svg = readFileSync(outputPath, 'utf8')
    .replace(/^<!-- source-sha256:[a-f0-9]{64} -->\n/, '')
    .replace(/^<!-- render-sha256:[a-f0-9]{64} -->\n/, '');
  const renderHash = createHash('sha256').update(svg).digest('hex');
  writeFileSync(
    outputPath,
    `<!-- source-sha256:${sourceHash} -->\n<!-- render-sha256:${renderHash} -->\n${svg}`,
  );
  console.log(`render-doc-diagrams: ${output} <- ${source} (${theme})`);
}
