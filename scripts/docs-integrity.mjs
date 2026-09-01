#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Dependency-free repository documentation integrity check:
// - relative Markdown/HTML links and Markdown anchors
// - local image existence, alt text, ignore status, type, and size
// - quoted repository paths in inline code
// - canonical diagram ownership and source-hash stamps
// - accidental deployment identifiers in current public guidance/assets

import { createHash } from 'node:crypto';
import {
  existsSync,
  lstatSync,
  readdirSync,
  readFileSync,
  statSync,
} from 'node:fs';
import { dirname, extname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const errors = [];
const checkedLinks = new Set();
const checkedImages = new Set();

const currentDocs = [
  'README.md',
  'docs/README.md',
  'docs/AWS_DEPLOYMENT_GUIDE.md',
  'docs/COSTS.md',
  'docs/COST_ANALYSIS_20K_USERS.md',
  'docs/GATEWAY_INTEGRATION_GUIDE.md',
  'docs/METERING.md',
  'docs/UPGRADE_RUNBOOK.md',
  'docs/diagrams/README.md',
  'docs/images/SCREENSHOT-SPEC.md',
  'infra/README.md',
  'pipe/README.md',
  'console/README.md',
];

const diagramPairs = [
  ['docs/diagrams/architecture.mmd', 'docs/images/architecture-light.svg'],
  ['docs/diagrams/architecture.mmd', 'docs/images/architecture-dark.svg'],
  ['docs/diagrams/metering-flow.mmd', 'docs/images/metering-flow-light.svg'],
  ['docs/diagrams/metering-flow.mmd', 'docs/images/metering-flow-dark.svg'],
];

function repoPath(path) {
  return relative(root, path).split(sep).join('/');
}

function walk(path, predicate = () => true) {
  const out = [];
  for (const entry of readdirSync(path, { withFileTypes: true })) {
    if (['.git', 'node_modules', 'dist', 'build', 'cdk.out', 'local_only', '.pytest_cache'].includes(entry.name)) continue;
    const child = resolve(path, entry.name);
    if (entry.isDirectory()) out.push(...walk(child, predicate));
    else if (predicate(child)) out.push(child);
  }
  return out;
}

function markdownFiles() {
  const files = readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.md'))
    .map((entry) => resolve(root, entry.name));
  for (const subdir of ['docs', 'infra', 'pipe', 'console']) {
    files.push(...walk(resolve(root, subdir), (path) => path.endsWith('.md')));
  }
  return [...new Set(files)].sort();
}

function contentOutsideFences(text) {
  let fence = null;
  return text.split('\n').map((line) => {
    const match = line.match(/^\s*(```+|~~~+)/);
    if (match) {
      if (!fence) fence = match[1][0];
      else if (match[1][0] === fence) fence = null;
      return '';
    }
    return fence ? '' : line;
  }).join('\n');
}

function decodeEntities(value) {
  return value
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function headingSlug(value) {
  return decodeEntities(value)
    .replace(/<[^>]*>/g, '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~]/g, '')
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-');
}

const anchorCache = new Map();
function anchorsFor(path) {
  if (anchorCache.has(path)) return anchorCache.get(path);
  const text = contentOutsideFences(readFileSync(path, 'utf8'));
  const anchors = new Set();
  const counts = new Map();
  for (const line of text.split('\n')) {
    const heading = line.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const base = headingSlug(heading[1]);
      if (base) {
        const count = counts.get(base) ?? 0;
        anchors.add(count ? `${base}-${count}` : base);
        counts.set(base, count + 1);
      }
    }
    for (const explicit of line.matchAll(/<(?:a|span)\b[^>]*(?:id|name)=["']([^"']+)["'][^>]*>/gi)) {
      anchors.add(explicit[1]);
    }
  }
  anchorCache.set(path, anchors);
  return anchors;
}

function location(text, index) {
  return text.slice(0, index).split('\n').length;
}

function isExternal(target) {
  return /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(target);
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function checkTarget(fromFile, rawTarget, kind, line) {
  let target = rawTarget.trim().replace(/^<|>$/g, '');
  if (!target || isExternal(target)) return;
  if (target.startsWith('/')) {
    errors.push(`${repoPath(fromFile)}:${line}: repository-local ${kind} must be relative: ${target}`);
    return;
  }

  const hashIndex = target.indexOf('#');
  const rawPath = hashIndex >= 0 ? target.slice(0, hashIndex) : target;
  const fragment = hashIndex >= 0 ? safeDecode(target.slice(hashIndex + 1)) : '';
  const pathOnly = safeDecode(rawPath.split('?')[0]);
  const resolved = pathOnly ? resolve(dirname(fromFile), pathOnly) : fromFile;
  const key = `${repoPath(fromFile)}|${target}|${kind}`;
  if (checkedLinks.has(key)) return;
  checkedLinks.add(key);

  if (!resolved.startsWith(`${root}${sep}`) && resolved !== root) {
    errors.push(`${repoPath(fromFile)}:${line}: ${kind} escapes repository: ${target}`);
    return;
  }
  if (!existsSync(resolved)) {
    errors.push(`${repoPath(fromFile)}:${line}: missing ${kind} target: ${target}`);
    return;
  }
  if (fragment && statSync(resolved).isFile() && extname(resolved).toLowerCase() === '.md') {
    if (!anchorsFor(resolved).has(fragment)) {
      errors.push(`${repoPath(fromFile)}:${line}: missing anchor #${fragment} in ${repoPath(resolved)}`);
    }
  }
}

function checkAsset(fromFile, rawTarget, kind, line) {
  if (isExternal(rawTarget)) return;
  checkTarget(fromFile, rawTarget, kind, line);

  const pathOnly = safeDecode(rawTarget.split('#')[0].split('?')[0]);
  const resolved = resolve(dirname(fromFile), pathOnly);
  if (!existsSync(resolved) || !statSync(resolved).isFile()) return;
  const key = repoPath(resolved);
  if (checkedImages.has(key)) return;
  checkedImages.add(key);

  const ignored = spawnSync('git', ['check-ignore', '-q', '--', key], { cwd: root });
  if (ignored.status === 0) errors.push(`${key}: linked asset is gitignored`);
  const tracked = spawnSync('git', ['ls-files', '--error-unmatch', '--', key], { cwd: root });
  if (tracked.status !== 0) errors.push(`${key}: linked asset is not tracked by git`);

  const ext = extname(resolved).toLowerCase();
  const size = statSync(resolved).size;
  if (['.png', '.jpg', '.jpeg', '.webp', '.gif'].includes(ext) && size > 500 * 1024) {
    errors.push(`${key}: raster image exceeds 500 KiB (${size} bytes)`);
  }
  if (ext === '.svg') {
    if (size > 250 * 1024) errors.push(`${key}: SVG exceeds 250 KiB (${size} bytes)`);
    if (!readFileSync(resolved, 'utf8').includes('<svg')) errors.push(`${key}: SVG markup not found`);
  }
}

function checkImage(fromFile, rawTarget, alt, line) {
  if (!alt || alt.trim().length < 5 || /^image$/i.test(alt.trim())) {
    errors.push(`${repoPath(fromFile)}:${line}: image needs meaningful alt text: ${rawTarget}`);
  }
  checkAsset(fromFile, rawTarget, 'image', line);
}

const allowedMissingQuotedPaths = new Set([
  'infra/deploy.config.json',
  'infra/cdk.context.json',
  'console/public/config.json',
  'docs/images/metering-console-dashboard.png',
  'docs/images/metering-console-pricing.png',
]);

function maybeQuotedPath(fromFile, value, line) {
  let candidate = value.trim().replace(/[.,;]$/, '').split('#')[0];
  if (!candidate || /[\s<>*|$]/.test(candidate) || isExternal(candidate) || candidate.startsWith('/')) return;
  const roots = ['docs/', 'infra/', 'gateway/', 'metering/', 'pipe/', 'console/', 'config/', 'scripts/', '.github/', '.kiro/', '../', './'];
  if (!roots.some((prefix) => candidate.startsWith(prefix))) return;
  if (allowedMissingQuotedPaths.has(candidate)) return;

  const resolved = candidate === './deploy.sh'
    ? resolve(root, 'deploy.sh')
    : candidate.startsWith('../') || candidate.startsWith('./')
      ? resolve(dirname(fromFile), candidate)
      : resolve(root, candidate);
  if (!resolved.startsWith(`${root}${sep}`) || !existsSync(resolved)) {
    errors.push(`${repoPath(fromFile)}:${line}: quoted repository path does not resolve: ${value}`);
  }
}

for (const file of markdownFiles()) {
  const original = readFileSync(file, 'utf8');
  const text = contentOutsideFences(original);

  for (const match of text.matchAll(/(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^)]*)?\)/g)) {
    const line = location(text, match.index);
    if (match[1] === '!') checkImage(file, match[3], match[2], line);
    else checkTarget(file, match[3], 'link', line);
  }
  for (const match of text.matchAll(/^\s*\[[^\]]+\]:\s*(\S+)/gm)) {
    checkTarget(file, match[1], 'reference link', location(text, match.index));
  }
  for (const match of text.matchAll(/<a\b[^>]*href=["']([^"']+)["'][^>]*>/gi)) {
    checkTarget(file, match[1], 'HTML link', location(text, match.index));
  }
  for (const match of text.matchAll(/<img\b[^>]*>/gi)) {
    const src = match[0].match(/\bsrc=["']([^"']+)["']/i)?.[1];
    const alt = match[0].match(/\balt=["']([^"']*)["']/i)?.[1] ?? '';
    if (src) checkImage(file, src, alt, location(text, match.index));
  }
  for (const match of text.matchAll(/<source\b[^>]*\bsrcset=["']([^"']+)["'][^>]*>/gi)) {
    for (const item of match[1].split(',')) {
      const src = item.trim().split(/\s+/)[0];
      if (src) checkAsset(file, src, 'picture source', location(text, match.index));
    }
  }
  if (currentDocs.includes(repoPath(file))) {
    for (const match of text.matchAll(/`([^`\n]+)`/g)) {
      maybeQuotedPath(file, match[1], location(text, match.index));
    }
  }
}

for (const [source, output] of diagramPairs) {
  const sourcePath = resolve(root, source);
  const outputPath = resolve(root, output);
  if (!existsSync(sourcePath) || !existsSync(outputPath)) {
    errors.push(`diagram pair missing: ${source} -> ${output}`);
    continue;
  }
  const hash = createHash('sha256').update(readFileSync(sourcePath)).digest('hex');
  const contents = readFileSync(outputPath, 'utf8');
  const sourceStamp = contents.match(/^<!-- source-sha256:([a-f0-9]{64}) -->\n/)?.[1];
  const withoutSource = contents.replace(/^<!-- source-sha256:[a-f0-9]{64} -->\n/, '');
  const renderStamp = withoutSource.match(/^<!-- render-sha256:([a-f0-9]{64}) -->\n/)?.[1];
  const svg = withoutSource.replace(/^<!-- render-sha256:[a-f0-9]{64} -->\n/, '');
  const renderedHash = createHash('sha256').update(svg).digest('hex');
  if (sourceStamp !== hash) {
    errors.push(`${output}: stale source stamp; run node scripts/render-doc-diagrams.mjs`);
  }
  if (renderStamp !== renderedHash) {
    errors.push(`${output}: generated body changed without a matching render stamp`);
  }
}

const allowedArchitectureAssets = new Set([
  'docs/diagrams/architecture.mmd',
  'docs/images/architecture-light.svg',
  'docs/images/architecture-dark.svg',
]);
for (const path of walk(resolve(root, 'docs'), (value) => /architecture.*\.(?:mmd|svg|png|jpe?g|drawio)$/i.test(value))) {
  const rel = repoPath(path);
  if (!allowedArchitectureAssets.has(rel)) errors.push(`${rel}: competing architecture asset; update the canonical source instead`);
}

for (const path of currentDocs) {
  const absolute = resolve(root, path);
  if (!existsSync(absolute)) {
    errors.push(`${path}: current-document inventory entry is missing`);
    continue;
  }
  const text = readFileSync(absolute, 'utf8');
  if (/```\s*mermaid/i.test(text)) errors.push(`${path}: embeds Mermaid; reference the canonical generated asset instead`);
  if (/[┌┐└┘├┤┬┴┼│─]/u.test(text)) errors.push(`${path}: contains an ASCII/box-drawing diagram`);
}

for (const path of ['README.md', 'docs/GATEWAY_INTEGRATION_GUIDE.md', 'docs/AWS_DEPLOYMENT_GUIDE.md']) {
  const text = readFileSync(resolve(root, path), 'utf8');
  for (const asset of ['architecture-light.svg', 'architecture-dark.svg']) {
    if (!text.includes(asset)) errors.push(`${path}: does not reference canonical ${asset}`);
  }
}
for (const asset of ['metering-flow-light.svg', 'metering-flow-dark.svg']) {
  if (!readFileSync(resolve(root, 'docs/METERING.md'), 'utf8').includes(asset)) {
    errors.push(`docs/METERING.md: does not reference ${asset}`);
  }
}

const tracked = spawnSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' });
if (tracked.status !== 0) {
  errors.push('unable to enumerate tracked files for public-identifier scan');
}
const publicTextPaths = (tracked.stdout ?? '')
  .split('\0')
  .filter(Boolean)
  .filter((path) => path === '.env.example' || /(?:\.md|\.mmd|\.svg)$/i.test(path));
const publicTrackedText = publicTextPaths
  .map((path) => `${path}\n${readFileSync(resolve(root, path), 'utf8')}`)
  .join('\n');
const identifierPatterns = [
  ['AWS account ID in an ARN/account field', /(?:arn:aws[^:\s]*:[^:\s]*:[^:\s]*:|account(?:\s+id)?\s*[:=]?\s*)(?!000000000000)\d{12}\b/gi],
  ['CloudFront deployment hostname', /\b[a-z0-9]{10,}\.cloudfront\.net\b/gi],
  ['API Gateway deployment hostname', /\b[a-z0-9]{8,}\.execute-api\.[a-z0-9-]+\.amazonaws\.com\b/gi],
  ['Cognito user-pool ID', /\b(?:af|ap|ca|eu|il|me|mx|sa|us)-[a-z]+-\d_[A-Za-z0-9]+\b/g],
];
for (const [label, regex] of identifierPatterns) {
  const matches = [...publicTrackedText.matchAll(regex)]
    .map((match) => match[0])
    .filter((match) => !/(?:X{4,}|REDACTED|TEST_ACCOUNT_ID)/i.test(match));
  if (matches.length) errors.push(`tracked public docs/assets contain ${label}: ${[...new Set(matches)].join(', ')}`);
}

if (errors.length) {
  console.error(`docs-integrity: FAILED (${errors.length} issue${errors.length === 1 ? '' : 's'})`);
  for (const error of errors.sort()) console.error(`  - ${error}`);
  process.exit(1);
}

console.log(
  `docs-integrity: OK (${markdownFiles().length} Markdown files, ${checkedLinks.size} local references, ${checkedImages.size} local assets)`,
);
