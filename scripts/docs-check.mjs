#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Advisory doc-drift guard. If a diff range touches the pricing runtime code but
// not its operator docs, print a reminder. Advisory by default (exit 0); pass
// --strict to exit 1 so CI can gate on it. Node stdlib only, no dependencies.
//
// Usage:
//   node scripts/docs-check.mjs [<base-ref>] [--strict]
// <base-ref> defaults to origin/main (falls back to HEAD~1). Compares the merge
// base of <base-ref>..HEAD so it reflects the branch's own changes.

import { execFileSync } from 'node:child_process';

const args = process.argv.slice(2);
const strict = args.includes('--strict');
const base = args.find((a) => !a.startsWith('--')) || 'origin/main';

// Code that, when changed, should be reflected in the operator docs.
const CODE = [/^metering\/pricing\//, /^metering\/pricing-refresher\//, /^gateway\/metering-interceptor\//];
// The docs that describe that code's operator-facing behavior.
const DOCS = ['docs/METERING.md', 'docs/plans/metering-admin-console/06-GATEWAY-PRICING-COVERAGE.md'];

function changedFiles(range) {
  try {
    const out = execFileSync('git', ['diff', '--name-only', range], { encoding: 'utf8' });
    return out.split('\n').map((s) => s.trim()).filter(Boolean);
  } catch {
    return null;
  }
}

function resolveRange() {
  // Prefer <base>...HEAD (merge-base); fall back to HEAD~1..HEAD if base is unknown.
  try {
    execFileSync('git', ['rev-parse', '--verify', base], { stdio: 'ignore' });
    return `${base}...HEAD`;
  } catch {
    return 'HEAD~1...HEAD';
  }
}

const range = resolveRange();
const files = changedFiles(range);
if (files === null) {
  console.log(`docs-check: could not diff ${range} (not a git range?) — skipping.`);
  process.exit(0);
}

const codeTouched = files.filter((f) => CODE.some((re) => re.test(f)));
const docsTouched = files.some((f) => DOCS.includes(f));

if (codeTouched.length && !docsTouched) {
  console.warn(`docs-check: pricing runtime changed but ${DOCS.join(' / ')} did not, in ${range}:`);
  for (const f of codeTouched) console.warn(`  - ${f}`);
  console.warn('  If operator-facing behavior changed (alarms, rates, coverage, precedence),');
  console.warn('  update the docs above. Advisory only; pass --strict to fail CI.');
  process.exit(strict ? 1 : 0);
}

console.log(`docs-check: OK (${range}).`);
process.exit(0);
