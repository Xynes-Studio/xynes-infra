// Read-only source checks. Successful checks never authorize a release or deployment.
import { resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { assertChecks, githubReader, requiredNames, type ReadApi } from '../lib/ci-release-gates';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SOURCE_KEYS = ['xynes-gateway', 'xynes-accounts-service', 'xynes-authz-service',
  'xynes-cms-core', 'xynes-doc-service', 'xynes-telemetry-service', 'xynes-storage-service',
  'xynes-auth-app', 'xynes-cms-console-web', 'xynes-auth-sdk', 'xynes-i18n', 'lumia-ds'] as const;
type Mapping = Record<string, unknown>;
export function sourceReader(token: string, http: (url: string, options: RequestInit) => Promise<Response> = fetch): ReadApi {
  return githubReader(token, async (url, options) => {
    for (let attempt = 0; ; attempt++) {
      const response = await http(url, options);
      if (attempt === 2 || ![502, 503, 504].includes(response.status)) return response;
      await response.body?.cancel();
      // Reuse the original 15-second abort budget across all three GET attempts.
      // Auth/policy failures and malformed successful data never enter this path.
      await new Promise(resolve => setTimeout(resolve, 200 * (attempt + 1)));
      options.signal?.throwIfAborted();
    }
  });
}
export type Source = { repository: string; commit: string };
export type RequiredCatalog = Record<string, Record<string, string>>;
const mapping = (value: unknown): value is Mapping => value !== null && typeof value === 'object' && !Array.isArray(value);
function require(condition: unknown, message: string): asserts condition { if (!condition) throw new Error(message); }
const positiveId = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;

export function validateSources(value: unknown): Record<string, Source> {
  require(mapping(value) && Object.keys(value).length === SOURCE_KEYS.length && SOURCE_KEYS.every((key) => Object.hasOwn(value, key)), 'Invalid closed source inventory');
  const result: Record<string, Source> = {};
  for (const key of SOURCE_KEYS) {
    const source = value[key];
    require(mapping(source) && Object.keys(source).length === 2 && source.repository === `Xynes-Studio/${key}` && typeof source.commit === 'string' && /^[a-f0-9]{40}$/.test(source.commit), 'Invalid immutable source');
    result[key] = { repository: source.repository, commit: source.commit };
  }
  return result;
}

const specialPaths: RequiredCatalog = {
  'xynes-storage-service': Object.fromEntries(['release-image-security', 'immutable-source-security', 'integration-processors', 'archive-safety'].map((name) => [name, '.github/workflows/ci.yml'])),
  'lumia-ds': { 'test': '.github/workflows/test.yml', 'build': '.github/workflows/ci.yml',
                'Visual Regression': '.github/workflows/visual-regression.yml' },
  'xynes-i18n': { 'CodeQL analyze': '.github/workflows/codeql.yml' },
  'xynes-cms-console-web': { 'Code and browser gates': '.github/workflows/quality.yml' },
};

export function loadRequiredCatalog(): RequiredCatalog {
  const catalog: RequiredCatalog = {};
  for (const key of SOURCE_KEYS) {
    const policy: unknown = JSON.parse(execFileSync('bash', [resolve(ROOT, 'scripts/setup/configure-branch-protection.sh'), '--print-policy', '--repo', key, '--branch', 'main'], { encoding: 'utf8', timeout: 15000, maxBuffer: 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] }));
    require(mapping(policy) && mapping(policy.required_status_checks), 'Canonical required catalog is unavailable');
    const names = requiredNames(policy.required_status_checks.contexts);
    const checks = policy.required_status_checks.checks;
    require(Array.isArray(checks) && checks.length === names.length && names.every((name) => checks.some((check) => mapping(check) && check.context === name && check.app_id === 15368)), 'Canonical catalog is not app-bound');
    catalog[key] = {};
    for (const name of names) {
      const path = specialPaths[key]?.[name] ?? (name === 'security' ? '.github/workflows/security.yml' : ['lint', 'test', 'coverage', 'build', 'typecheck'].includes(name) ? '.github/workflows/quality-gates.yml' : undefined);
      require(path, 'Required check has no reviewed producer catalog');
      catalog[key][name] = path;
    }
  }
  return catalog;
}

function sourceWorkflow(value: unknown, path: string): string {
  require(mapping(value) && value.path === path && value.type === 'file'
    && value.encoding === 'base64' && typeof value.content === 'string'
    && Number.isSafeInteger(value.size) && Number(value.size) > 0 && Number(value.size) <= 256 * 1024
    && typeof value.sha === 'string' && /^[a-f0-9]{40}$/.test(value.sha), 'Invalid exact-source workflow');
  const encoded = value.content.replace(/\n/g, '');
  require(/^[A-Za-z0-9+/]*={0,2}$/.test(encoded), 'Invalid workflow encoding');
  const bytes = Buffer.from(encoded, 'base64');
  require(bytes.length === value.size && bytes.toString('base64') === encoded, 'Invalid workflow bytes');
  require(createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex') === value.sha,
    'Workflow Git blob binding differs');
  const runtime = globalThis as unknown as { Bun?: { YAML: { parse: (text: string) => unknown } } };
  require(runtime.Bun, 'Bun YAML parser is required');
  const parsed: unknown = runtime.Bun.YAML.parse(bytes.toString('utf8'));
  require(mapping(parsed) && mapping(parsed.jobs) && Object.keys(parsed.jobs).length > 0,
    'Required producer workflow has no jobs');
  return value.sha;
}

export async function verifySourceChecks(value: unknown, read: ReadApi, catalog: RequiredCatalog = loadRequiredCatalog()) {
  const sources = validateSources(value);
  require(Object.keys(catalog).length === SOURCE_KEYS.length && SOURCE_KEYS.every(key => Object.hasOwn(catalog, key)),
    'Invalid required catalog inventory');
  const receipts = [];
  for (const key of SOURCE_KEYS) {
    const source = sources[key];
    const required = requiredNames(Object.keys(catalog[key]));
    require(Object.values(catalog[key]).every(path => /^\.github\/workflows\/[a-z0-9-]+\.yml$/.test(path)),
      'Invalid producer catalog path');
    const prefix = `/repos/${source.repository}`;
    const checks: Mapping[] = [];
    const ids = new Set<number>();
    let complete = false;
    for (let page = 1; page <= 20; page++) {
      const response = await read(`${prefix}/commits/${source.commit}/check-runs?filter=all&per_page=100&page=${page}`);
      require(mapping(response) && Array.isArray(response.check_runs) && response.check_runs.length <= 100,
        'Invalid check-run response');
      for (const check of response.check_runs) {
        require(mapping(check) && positiveId(check.id) && !ids.has(check.id), 'Invalid duplicate check ID');
        ids.add(check.id); checks.push(check);
      }
      if (response.check_runs.length < 100) { complete = true; break; }
    }
    require(complete, 'Check-run pagination limit exceeded');
    assertChecks(checks, required, source.commit);
    const entries = [];
    const workflowBlobs = new Map<string, string>();
    const runs = new Map<number, Mapping>();
    const workflows = new Map<number, Mapping>();
    for (const name of required) {
      const chosen = checks.filter(check => check.name === name).sort((a, b) => Number(b.id) - Number(a.id))[0];
      require(typeof chosen.details_url === 'string', 'Check producer link unavailable');
      const match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/actions\/runs\/([1-9][0-9]*)\/job\/([1-9][0-9]*)$/.exec(chosen.details_url);
      require(match && match[1] === source.repository, 'Check producer link is not repository-bound');
      const runId = Number(match[2]), jobId = Number(match[3]);
      require(positiveId(runId) && positiveId(jobId) && mapping(chosen.check_suite) && positiveId(chosen.check_suite.id),
        'Invalid check producer identity');
      const job = await read(`${prefix}/actions/jobs/${jobId}`);
      require(mapping(job) && job.id === jobId && job.name === name && job.head_sha === source.commit
        && job.run_id === runId && positiveId(job.run_attempt) && job.status === 'completed'
        && job.conclusion === 'success' && job.check_run_url === `https://api.github.com${prefix}/check-runs/${chosen.id}`,
        'Check does not match its successful Actions job');
      if (!runs.has(runId)) {
        const run = await read(`${prefix}/actions/runs/${runId}`);
        require(mapping(run), 'Invalid Actions run'); runs.set(runId, run);
      }
      const run = runs.get(runId)!;
      const path = catalog[key][name];
      require(run.id === runId && run.head_sha === source.commit && mapping(run.repository)
        && run.repository.full_name === source.repository && mapping(run.head_repository)
        && run.head_repository.full_name === source.repository && run.check_suite_id === chosen.check_suite.id
        && positiveId(run.workflow_id) && run.path === path && run.run_attempt === job.run_attempt
        && run.status === 'completed' && run.conclusion === 'success', 'Check Actions run binding differs');
      if (!workflows.has(run.workflow_id)) {
        const workflow = await read(`${prefix}/actions/workflows/${run.workflow_id}`);
        require(mapping(workflow), 'Invalid workflow metadata'); workflows.set(run.workflow_id, workflow);
      }
      const workflow = workflows.get(run.workflow_id)!;
      require(workflow.id === run.workflow_id && workflow.path === path, 'Unexpected check workflow producer');
      if (!workflowBlobs.has(path)) {
        workflowBlobs.set(path, sourceWorkflow(await read(`${prefix}/contents/${path}?ref=${source.commit}`), path));
      }
      entries.push({ name, check_id: chosen.id, job_id: jobId, run_id: runId, run_attempt: job.run_attempt,
        workflow_id: run.workflow_id, workflow_path: path, workflow_blob: workflowBlobs.get(path)! });
    }
    receipts.push({ repository: source.repository, commit: source.commit, checks: entries });
  }
  return { checks_verified: true, approved: false, deployable: false, sources: receipts };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    require(process.argv.length === 4 && process.argv[2] === '--manifest', 'Source manifest argument required');
    // Reuse the complete closed manifest parser; never echo its arbitrary input.
    const parsed = execFileSync('python3', ['-c',
      'import sys,json;sys.path.insert(0,sys.argv[1]);import release_manifest,source_context;v=release_manifest.read_json(__import__("pathlib").Path(sys.argv[2]));source_context.validate_manifest(v,"planning");print(json.dumps(v["sources"]))',
      resolve(ROOT, 'scripts/release'), resolve(process.argv[3])],
    { encoding: 'utf8', timeout: 15000, maxBuffer: 256 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });
    console.log(JSON.stringify(await verifySourceChecks(JSON.parse(parsed), sourceReader(process.env.GH_TOKEN ?? ''))));
  } catch {
    console.error('Source check verification refused: unavailable or mismatched source/producer evidence.');
    process.exitCode = 1;
  }
}
