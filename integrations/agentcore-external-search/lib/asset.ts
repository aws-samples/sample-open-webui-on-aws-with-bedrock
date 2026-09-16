import { execFileSync } from 'child_process';
import { createHash } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import * as lambda from 'aws-cdk-lib/aws-lambda';

export const MODULE_ROOT = fs.existsSync(path.join(__dirname, '..', 'cdk.json'))
  ? path.resolve(__dirname, '..') : path.resolve(__dirname, '..', '..');

function pythonFiles(directory: string, prefix = ''): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const relative = path.join(prefix, entry.name);
    if (entry.isDirectory() && entry.name !== '__pycache__') {
      return pythonFiles(path.join(directory, entry.name), relative);
    }
    return entry.isFile() && entry.name.endsWith('.py') ? [relative] : [];
  }).sort();
}

export function searchAsset(assetPath: string): lambda.Code {
  if (typeof assetPath !== 'string' || !path.isAbsolute(assetPath)
    || !fs.existsSync(assetPath) || !fs.statSync(assetPath).isDirectory()) {
    throw new Error('Build the Python bundle first and pass its absolute directory as -c assetPath=...');
  }
  const resolved = fs.realpathSync(assetPath);
  const requireFile = (relative: string): string => {
    const filename = path.join(resolved, relative);
    if (!fs.existsSync(filename) || !fs.statSync(filename).isFile()) {
      throw new Error(`Search asset is missing ${relative}; rebuild the bundle`);
    }
    return filename;
  };
  const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: MODULE_ROOT, encoding: 'utf8' }).trim();
  if (fs.readFileSync(requireFile('source-commit.txt'), 'utf8') !== `${head}\n`) {
    throw new Error('Search asset source-commit.txt must match current HEAD; rebuild the bundle');
  }
  for (const required of ['runtime/handler.py', 'provisioner/index.py', 'requirements.txt']) {
    requireFile(required);
  }
  const manifestFiles = ['requirements.txt', 'source-commit.txt'];
  for (const directory of ['runtime', 'provisioner']) {
    const sourceFiles = pythonFiles(path.join(MODULE_ROOT, directory));
    const bundledFiles = pythonFiles(path.join(resolved, directory));
    if (JSON.stringify(sourceFiles) !== JSON.stringify(bundledFiles)) {
      throw new Error(`Search asset ${directory} source files differ; rebuild the bundle`);
    }
    for (const filename of sourceFiles) {
      const relative = path.join(directory, filename);
      manifestFiles.push(relative);
      if (!fs.readFileSync(requireFile(relative)).equals(fs.readFileSync(path.join(MODULE_ROOT, relative)))) {
        throw new Error(`Search asset source is stale: ${relative}; rebuild the bundle`);
      }
    }
  }
  if (!fs.readFileSync(requireFile('requirements.txt')).equals(fs.readFileSync(path.join(MODULE_ROOT, 'requirements.txt')))) {
    throw new Error('Search asset requirements.txt is stale; rebuild the bundle');
  }
  const expectedManifest = manifestFiles.sort().map(relative => {
    const digest = createHash('sha256').update(fs.readFileSync(requireFile(relative))).digest('hex');
    return `${digest}  ${relative}\n`;
  }).join('');
  if (fs.readFileSync(requireFile('source-sha256.txt'), 'utf8') !== expectedManifest) {
    throw new Error('Search asset source-sha256.txt must match the source manifest; rebuild the bundle');
  }
  for (const packageName of ['boto3', 'botocore']) {
    const version = fs.readFileSync(path.join(MODULE_ROOT, 'requirements.txt'), 'utf8')
      .match(new RegExp(`^${packageName}==([0-9]+\\.[0-9]+\\.[0-9]+)(?:\\s|$)`, 'm'))?.[1];
    if (!version) throw new Error(`requirements.txt must pin ${packageName} to an exact version`);
    const metadata = fs.readFileSync(requireFile(`${packageName}-${version}.dist-info/METADATA`), 'utf8');
    const module = fs.readFileSync(requireFile(`${packageName}/__init__.py`), 'utf8');
    if (!metadata.split(/\r?\n/).includes(`Version: ${version}`)
      || module.match(/__version__\s*=\s*['"]([^'"]+)['"]/)?.[1] !== version) {
      throw new Error(`Search asset requires ${packageName}==${version}`);
    }
  }
  for (const dependency of ['s3transfer', 'jmespath', 'dateutil', 'urllib3', 'httpx', 'jsonschema']) {
    requireFile(`${dependency}/__init__.py`);
  }
  requireFile('six.py');
  const modelDirectory = path.join(resolved, 'botocore/data/bedrock-agentcore-control');
  if (!fs.existsSync(modelDirectory) || !fs.statSync(modelDirectory).isDirectory()) {
    throw new Error('Search asset requires the AgentCore control service model');
  }
  return lambda.Code.fromAsset(resolved, { exclude: ['**/__pycache__', '**/*.pyc'] });
}
