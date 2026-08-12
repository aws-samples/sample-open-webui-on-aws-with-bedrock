// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Image-selection contract tests. These run the REAL composition root
// (bin/app.ts) through `cdk synth` as a child process and assert on the
// emitted CloudFormation, so a broken context wire-up anywhere along
// deploy.sh -> -c openWebuiImage -> app.ts getConfig() -> ComputeStack prop
// fails loudly instead of silently deploying the fallback image.

import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const INFRA_DIR = path.join(__dirname, '..');

/** The fallback in lib/compute-stack.ts — keep in sync with DEFAULT_IMAGE. */
const EXPECTED_DEFAULT_IMAGE = 'ghcr.io/open-webui/open-webui:v0.11.0';

interface SynthResult {
  image: string;
  appImageUri: string;
}

function synthComputeTemplate(extraArgs: string[]): SynthResult {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'owui-synth-test-'));
  try {
    execFileSync(
      'npx',
      ['cdk', 'synth', 'OpenWebUI-Compute', '--quiet', '--output', outDir, ...extraArgs],
      { cwd: INFRA_DIR, stdio: ['ignore', 'pipe', 'pipe'], timeout: 180_000 },
    );
    const template = JSON.parse(
      fs.readFileSync(path.join(outDir, 'OpenWebUI-Compute.template.json'), 'utf-8'),
    );
    const taskDefs = Object.values(template.Resources as Record<string, any>).filter(
      (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
    );
    expect(taskDefs).toHaveLength(1);
    const outputs = template.Outputs as Record<string, { Value: string }>;
    const appImageUriKey = Object.keys(outputs).find((k) => k.startsWith('AppImageUri'));
    expect(appImageUriKey).toBeDefined();
    return {
      image: (taskDefs[0] as any).Properties.ContainerDefinitions[0].Image,
      appImageUri: outputs[appImageUriKey!].Value,
    };
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
}

describe('Open WebUI image selection', () => {
  test(
    'an explicit -c openWebuiImage lands verbatim in the task definition and AppImageUri output',
    () => {
      const pinned =
        'ghcr.io/open-webui/open-webui@sha256:9fcea9c6e32ab60b0498f3986c6cdf651ddbe61db48d2213a3d28048ddd673d4';
      const { image, appImageUri } = synthComputeTemplate(['-c', `openWebuiImage=${pinned}`]);
      expect(image).toBe(pinned);
      expect(appImageUri).toBe(pinned);
    },
    240_000,
  );

  test(
    'without the context the stack falls back to the pinned release-tag default',
    () => {
      const { image, appImageUri } = synthComputeTemplate([]);
      expect(image).toBe(EXPECTED_DEFAULT_IMAGE);
      expect(appImageUri).toBe(EXPECTED_DEFAULT_IMAGE);
      // Guard the design decision: the fallback must never be a floating or
      // main-branch reference (ghcr's :latest is upstream's main build).
      expect(image).not.toContain(':latest');
      expect(image).not.toContain(':main');
    },
    240_000,
  );
});
