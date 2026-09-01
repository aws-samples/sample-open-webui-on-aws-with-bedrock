#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Advisory doc-drift guard. If a diff touches the pricing runtime code but not
// its operator docs, print a reminder. Advisory by default (exit 0); pass
// --strict to exit 1 so CI can gate on it. Node stdlib only, no dependencies.
//
// Usage:
//   node scripts/docs-check.mjs [<base-ref>] [--strict]
// An explicit base compares <base-ref>...HEAD (CI/branch mode). Without one,
// the check also includes staged and unstaged files so local pre-commit use is
// not vacuous. The default base is origin/main, then HEAD~1 when unavailable.

import { execFileSync } from 'node:child_process';

const args = process.argv.slice(2);
const strict = args.includes('--strict');
const explicitBase = args.find((arg) => !arg.startsWith('--'));
const base = explicitBase || 'origin/main';

const CODE = [/^metering\/pricing\//, /^metering\/pricing-refresher\//, /^gateway\/metering-interceptor\//];
const DOCS = ['docs/METERING.md'];

function changedFiles(args_) {
  try {
    const out = execFileSync('git', ['diff', '--name-only', ...args_], { encoding: 'utf8' });
    return out.split('\n').map((value) => value.trim()).filter(Boolean);
  } catch {
    return null;
  }
}

function resolveRange() {
  try {
    execFileSync('git', ['rev-parse', '--verify', base], { stdio: 'ignore' });
    return `${base}...HEAD`;
  } catch {
    if (explicitBase) {
      const message = `docs-check: explicit base '${base}' does not resolve.`;
      if (strict) {
        console.error(message);
        process.exit(1);
      }
      console.warn(`${message} Skipping advisory check.`);
      process.exit(0);
    }
    return 'HEAD~1...HEAD';
  }
}

const range = resolveRange();
const branchFiles = changedFiles([range]);
if (branchFiles === null) {
  const message = `docs-check: could not diff ${range} (not a git range?).`;
  if (strict) {
    console.error(message);
    process.exit(1);
  }
  console.warn(`${message} Skipping advisory check.`);
  process.exit(0);
}

const files = new Set(branchFiles);
let scope = range;
if (!explicitBase) {
  for (const extra of [changedFiles([]), changedFiles(['--cached'])]) {
    for (const file of extra ?? []) files.add(file);
  }
  scope += ' + staged/unstaged';
}

const codeTouched = [...files].filter((file) => CODE.some((pattern) => pattern.test(file)));
const docsTouched = [...files].some((file) => DOCS.includes(file));

if (codeTouched.length && !docsTouched) {
  console.warn(`docs-check: pricing runtime changed but ${DOCS.join(' / ')} did not, in ${scope}:`);
  for (const file of codeTouched) console.warn(`  - ${file}`);
  console.warn('  If operator-facing behavior changed (alarms, rates, coverage, precedence),');
  console.warn('  update the docs above. Advisory only; pass --strict to fail CI.');
  process.exit(strict ? 1 : 0);
}

console.log(`docs-check: OK (${scope}).`);
process.exit(0);
