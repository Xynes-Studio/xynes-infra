// Generated from xynes-infra scripts/lib/ci-policy.ts; run scripts/setup/export-ci-policy.py.
// Canonical SEC-008 policy. Exported copies are generated, never edited by hand.
import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// The standalone runner uses the pinned Bun runtime; no package installation.
declare const Bun: { YAML: { parse(text: string): unknown } };
type ObjectValue = Record<string, unknown>;
const object = (v: unknown): v is ObjectValue => !!v && typeof v === 'object' && !Array.isArray(v);
export const trustedReleaseGuard = "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && github.ref_protected";
export const trustedScanGuard = "github.event_name == 'push' && github.ref_protected";

export function checkWorkflow(value: unknown, pins: ReadonlySet<string>): string[] {
  const errors: string[] = [];
  const reject = (message: string) => errors.push(message);
  if (!object(value)) return ['workflow must be a mapping'];
  // Bun's YAML 1.1 parser represents the bare `on` key as `true`.
  const events = value.on ?? value.true;
  if (!object(events)) reject('events must be an explicit mapping');
  else if (['pull_request_target', 'workflow_run'].some((key) => key in events)) reject('privileged untrusted event is forbidden');
  if (!object(value.permissions) || Object.keys(value.permissions).length !== 1 || value.permissions.contents !== 'read') reject('workflow permissions must be contents: read only');
  if (!object(value.jobs) || !Object.keys(value.jobs).length) return [...errors, 'jobs must be nonempty'];
  for (const [name, job] of Object.entries(value.jobs)) {
    const report = (message: string) => reject(`${name}: ${message}`);
    if (!object(job)) { report('job must be a mapping'); continue; }
    if (typeof job['runs-on'] !== 'string' || !/^ubuntu-(latest|[0-9]+\.[0-9]+)$/.test(job['runs-on'])) report('untrusted jobs must use an ephemeral hosted runner');
    if (!Number.isInteger(job['timeout-minutes']) || Number(job['timeout-minutes']) < 1 || Number(job['timeout-minutes']) > 60) report('bounded timeout required');
    if (job['continue-on-error']) report('job failure must block');
    if (job.permissions !== undefined) {
      if (!object(job.permissions)) report('permissions must be a mapping');
      else {
        const privileged = Object.entries(job.permissions).filter(([, level]) => level === 'write');
        if (Object.values(job.permissions).some((level) => !['read', 'write', 'none'].includes(String(level)))) report('invalid permission level');
        if (privileged.length) {
          const guard = typeof job.if === 'string' ? job.if.replace(/^\$\{\{\s*|\s*\}\}$/g, '').trim() : '';
          const keys = privileged.map(([key]) => key).sort().join(',');
          const release = guard === trustedReleaseGuard && keys === 'attestations,id-token' && job.environment === 'release';
          const scan = guard === trustedScanGuard && keys === 'security-events';
          if (!release && !scan) report('write permissions require an exact trusted event/protected-ref guard');
          if (release && (!Array.isArray(job.steps) || job.steps.some((step) => !object(step) || typeof step.uses !== 'string' || !step.uses.startsWith('actions/attest@') || 'run' in step))) report('privileged signer must only attest; no checkout or executable build code');
        }
      }
    }
    for (const item of [job.container, ...(object(job.services) ? Object.values(job.services) : [])]) {
      if (item === undefined) continue;
      const image = typeof item === 'string' ? item : object(item) ? item.image : undefined;
      if (typeof image !== 'string' || !/@sha256:[a-f0-9]{64}$/.test(image)) report('container image must have a digest');
    }
    if (!Array.isArray(job.steps) || !job.steps.length) { report('steps must be nonempty'); continue; }
    for (const step of job.steps) {
      if (!object(step)) { report('step must be a mapping'); continue; }
      if (step['continue-on-error']) report('step failure must block');
      if (step.uses !== undefined) {
        if (typeof step.uses !== 'string' || !/^[\w.-]+\/[\w./-]+@[a-f0-9]{40}$/.test(step.uses) || !pins.has(step.uses)) report('action must match an independently verified full SHA');
        if (typeof step.uses === 'string' && step.uses.startsWith('actions/checkout@')) {
          if (!object(step.with) || step.with['persist-credentials'] !== false) report('checkout must discard credentials');
          if (object(step.with) && step.with.repository && (typeof step.with.ref !== 'string' || !/^[a-f0-9]{40}$/.test(step.with.ref))) report('sibling checkout must pin a commit');
        }
        if (typeof step.uses === 'string' && /(?:setup-node|setup-bun|setup-go|pnpm\/action-setup)@/.test(step.uses)) {
          const key = step.uses.includes('setup-node@') ? 'node-version' : step.uses.includes('setup-bun@') ? 'bun-version' : step.uses.includes('setup-go@') ? 'go-version' : 'version';
          const exactVersion = step.uses.startsWith('pnpm/action-setup@')
            ? /^\d+\.\d+\.\d+(?:\+sha512\.[a-f0-9]{128})?$/
            : /^\d+\.\d+\.\d+$/;
          if (!object(step.with) || !exactVersion.test(String(step.with[key]))) report('runtime/package-manager version must be exact');
        }
      }
      if (typeof step.run === 'string') {
        if (/\$\{\{\s*github\.event\./.test(step.run)) report('event input must not be interpolated into shell code');
        for (const match of step.run.matchAll(/\b(?:bun|pnpm) install\b([^\n;&|]*)/g)) {
          if (!/^ --frozen-lockfile\b/.test(match[1])) report('dependency installation must use the frozen lockfile');
        }
        if (/\b(?:npm install|npm ci|npx)\b|\bcurl\b[^\n]*\|\s*(?:sh|bash)/.test(step.run)) report('unpinned package/script execution is forbidden');
      }
    }
  }
  return errors;
}

export function inspectRepository(root: string): string[] {
  const pinsValue: unknown = JSON.parse(readFileSync(resolve(root, '.github/ci/action-pins.json'), 'utf8'));
  if (!Array.isArray(pinsValue) || !pinsValue.every((pin) => typeof pin === 'string')) return ['action pins must be an array of strings'];
  const pins = new Set<string>(pinsValue);
  const directory = resolve(root, '.github/workflows');
  const files = readdirSync(directory).filter((file) => /\.ya?ml$/.test(file));
  if (!files.length) return ['no workflows found'];
  const names = new Set<string>();
  const failures = files.flatMap((file) => {
    const parsed = Bun.YAML.parse(readFileSync(resolve(directory, file), 'utf8'));
    if (object(parsed) && object(parsed.jobs)) {
      for (const [id, job] of Object.entries(parsed.jobs)) {
        if (!object(job)) continue;
        if (job.name === '${{ matrix.gate }}' && object(job.strategy) && object(job.strategy.matrix) && Array.isArray(job.strategy.matrix.gate)) {
          for (const name of job.strategy.matrix.gate) if (typeof name === 'string') names.add(name);
        } else if (job.name === undefined || typeof job.name === 'string' && !job.name.includes('${{')) names.add(typeof job.name === 'string' ? job.name : id);
      }
    }
    return checkWorkflow(parsed, pins).map((error) => `${file}: ${error}`);
  });
  const required: unknown = JSON.parse(readFileSync(resolve(root, '.github/ci/required-checks.json'), 'utf8'));
  if (!Array.isArray(required) || !required.length || !required.every((name) => typeof name === 'string' && name.length)) return [...failures, 'required check names must be a nonempty array of strings'];
  for (const name of required) if (!names.has(name)) failures.push(`Missing required check producer: ${name}`);
  return failures;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const failures = inspectRepository(resolve(process.argv[2] ?? '.'));
    if (failures.length) { console.error(failures.join('\n')); process.exitCode = 1; }
    else console.log('PASS: immutable actions, isolated PR jobs and least-privilege policy');
  } catch { console.error('CI policy input is invalid or unavailable'); process.exitCode = 1; }
}
