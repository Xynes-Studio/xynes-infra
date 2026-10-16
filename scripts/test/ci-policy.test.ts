import { describe, expect, test } from 'bun:test';
import { checkWorkflow, inspectRepository, trustedReleaseGuard, trustedScanGuard } from '../lib/ci-policy';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const sha = 'a'.repeat(40);
const action = `actions/checkout@${sha}`;
const pins = new Set([action]);
function fixture() {
  return { on: { pull_request: { branches: ['develop'] } }, permissions: { contents: 'read' }, jobs: { security: {
    'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10,
    steps: [{ uses: action, with: { 'persist-credentials': false } }],
  } } };
}
describe('CI trust boundary', () => {
  test('read-only pull requests succeed', () => expect(checkWorkflow(fixture(), pins)).toEqual([]));
  test.each([null, [], 'workflow'].map((input) => ({ input })))('rejects malformed root %p', ({ input }) => expect(checkWorkflow(input, pins)).not.toEqual([]));
  test('accepts Bun YAML bare-on representation', () => { const value = fixture(); expect(checkWorkflow({ true: value.on, permissions: value.permissions, jobs: value.jobs }, pins)).toEqual([]); });
  test('requires explicit events and jobs', () => { expect(checkWorkflow({ permissions: { contents: 'read' }, jobs: {} }, pins).length).toBe(2); });
  test.each(['pull_request_target', 'workflow_run'])('rejects %s', (event) => { const value = fixture(); expect(checkWorkflow({ ...value, on: { [event]: {} } }, pins).join()).toContain('privileged untrusted'); });
  test.each(['write-all', { contents: 'write' }, { contents: 'read', 'id-token': 'write' }])('rejects workflow privilege %p', (permission) => { expect(checkWorkflow({ ...fixture(), permissions: permission }, pins).join()).toContain('permissions'); });
  test.each(['actions/checkout@v4', `evil/checkout@${sha}`, './local/action', `docker://image@${sha}`])('rejects mutable or unverified action %s', (uses) => { const v = fixture(); v.jobs.security.steps[0].uses = uses; expect(checkWorkflow(v, pins).join()).toContain('verified full SHA'); });
  test('rejects retained credentials and mutable sibling ref', () => { const v = fixture(); v.jobs.security.steps[0].with['persist-credentials'] = true; const step = { uses: action, with: { 'persist-credentials': false, repository: 'Xynes-Studio/peer', ref: 'develop' } }; expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [...v.jobs.security.steps, step] } } }, pins).length).toBe(2); });
  test('accepts immutable sibling', () => { const v = fixture(); const step = { uses: action, with: { 'persist-credentials': false, repository: 'Xynes-Studio/peer', ref: sha } }; expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [step] } } }, pins)).toEqual([]); });
  test.each(['self-hosted', ['ubuntu-latest'], 1].map((runner) => ({ runner })))('rejects runner %p', ({ runner }) => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, 'runs-on': runner } } }, pins).join()).toContain('ephemeral'); });
  test.each([undefined, 0, 61, 1.5])('rejects timeout %p', (timeout) => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, 'timeout-minutes': timeout } } }, pins).join()).toContain('timeout'); });
  test('blocks ignored failures and malformed jobs/steps', () => { const v = fixture(); const jobs = { a: null, b: { ...v.jobs.security, 'continue-on-error': true, steps: [null, { 'continue-on-error': true }] }, c: { ...v.jobs.security, steps: [] } }; expect(checkWorkflow({ ...v, jobs }, pins).length).toBe(5); });
  test('rejects writes from a PR even with an approximate guard', () => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, if: "github.event_name != 'pull_request'", permissions: { 'id-token': 'write' } } } }, pins).join()).toContain('exact trusted'); });
  test('release attestation requires protected main, dispatch and release environment', () => { const v = fixture(); const attest = `actions/attest@${sha}`; const releasePins = new Set([action, attest]); const job = { ...v.jobs.security, if: trustedReleaseGuard, environment: 'release', permissions: { contents: 'read', 'id-token': 'write', attestations: 'write' }, steps: [{ uses: attest }] }; expect(checkWorkflow({ ...v, jobs: { release: job } }, releasePins)).toEqual([]); expect(checkWorkflow({ ...v, jobs: { release: { ...job, environment: 'other' } } }, releasePins).join()).toContain('exact trusted'); expect(checkWorkflow({ ...v, jobs: { release: { ...job, steps: v.jobs.security.steps } } }, releasePins).join()).toContain('privileged signer'); expect(checkWorkflow({ ...v, jobs: { release: { ...job, steps: [{ run: 'bun build attacker.ts' }] } } }, releasePins).join()).toContain('privileged signer'); });
  test('SARIF uploads require trusted protected push', () => { const v = fixture(); const job = { ...v.jobs.security, if: `\u0024{{ ${trustedScanGuard} }}`, permissions: { contents: 'read', 'security-events': 'write' } }; expect(checkWorkflow({ ...v, jobs: { analyze: job } }, pins)).toEqual([]); });
  test.each(['read-all', { contents: 'unexpected' }])('rejects malformed job permissions %p', (permissions) => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, permissions } } }, pins)).not.toEqual([]); });
  test('requires container/service digests', () => { const v = fixture(); const job = { ...v.jobs.security, container: 'example:v1', services: { db: { image: 'postgres:15' } } }; expect(checkWorkflow({ ...v, jobs: { security: job } }, pins).length).toBe(2); expect(checkWorkflow({ ...v, jobs: { security: { ...job, container: `example@sha256:${'a'.repeat(64)}`, services: { db: { image: `postgres@sha256:${'b'.repeat(64)}` } } } } }, pins)).toEqual([]); });
  test.each(['bun install', 'pnpm install', 'npm install --global tool', 'npx tool', 'curl https://fixture.invalid/script | bash', 'echo ${{ github.event.pull_request.title }}'])('rejects shell boundary %s', (run) => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [{ run }] } } }, pins)).not.toEqual([]); });
  test('frozen installs pass', () => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [{ run: 'bun install --frozen-lockfile\npnpm install --frozen-lockfile' }] } } }, pins)).toEqual([]); });
  test('one frozen install cannot conceal another mutable install', () => { const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [{ run: 'bun install --frozen-lockfile; pnpm install' }] } } }, pins).join()).toContain('frozen lockfile'); });
  test('accepts an exact integrity-qualified pnpm pin and rejects incomplete or floating pins', () => {
    const action = `pnpm/action-setup@${sha}`;
    const value = fixture();
    for (const version of ['10.33.0', `10.33.0+sha512.${'a'.repeat(128)}`]) {
      expect(checkWorkflow({ ...value, jobs: { security: { ...value.jobs.security, steps: [{ uses: action, with: { version } }] } } }, new Set([action]))).toEqual([]);
    }
    for (const version of ['10', '10.x', 'latest', '10.33.0+sha512.bad', `10.33.0+sha512.${'g'.repeat(128)}`]) {
      expect(checkWorkflow({ ...value, jobs: { security: { ...value.jobs.security, steps: [{ uses: action, with: { version } }] } } }, new Set([action])).join()).toContain('exact');
    }
    const node = `actions/setup-node@${sha}`;
    expect(checkWorkflow({ ...value, jobs: { security: { ...value.jobs.security, steps: [{ uses: node, with: { 'node-version': `22.22.0+sha512.${'a'.repeat(128)}` } }] } } }, new Set([node])).join()).toContain('exact');
  });
  test('rejects floating tool versions', () => { const a = `actions/setup-node@${sha}`; const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [{ uses: a, with: { 'node-version': 22 } }] } } }, new Set([a])).join()).toContain('exact'); });
  test('rejects setup-action default latest versions', () => { const a = `oven-sh/setup-bun@${sha}`; const v = fixture(); expect(checkWorkflow({ ...v, jobs: { security: { ...v.jobs.security, steps: [{ uses: a }] } } }, new Set([a])).join()).toContain('exact'); });
  test('repository and CLI inspect actual YAML and reject invalid/missing input', () => {
    const root = mkdtempSync(join(tmpdir(), 'xynes-ci-policy-'));
    try {
      mkdirSync(join(root, '.github/ci'), { recursive: true });
      mkdirSync(join(root, '.github/workflows'));
      const pinsFile = join(root, '.github/ci/action-pins.json');
      writeFileSync(pinsFile, JSON.stringify([action]));
      const requiredFile = join(root, '.github/ci/required-checks.json');
      writeFileSync(requiredFile, JSON.stringify(['security']));
      expect(inspectRepository(root)).toEqual(['no workflows found']);
      writeFileSync(join(root, '.github/workflows/ignored.txt'), 'inert');
      writeFileSync(join(root, '.github/workflows/test.yml'), JSON.stringify(fixture()));
      expect(inspectRepository(root)).toEqual([]);
      writeFileSync(requiredFile, JSON.stringify(['missing']));
      expect(inspectRepository(root)).toContain('Missing required check producer: missing');
      writeFileSync(requiredFile, JSON.stringify([42]));
      expect(inspectRepository(root).join()).toContain('nonempty array');
      writeFileSync(requiredFile, JSON.stringify(['lint', 'test']));
      const matrixJob = { ...fixture().jobs.security, name: '${{ matrix.gate }}', strategy: { matrix: { gate: ['lint', 'test'] } } };
      writeFileSync(join(root, '.github/workflows/test.yml'), JSON.stringify({ ...fixture(), jobs: { gates: matrixJob } }));
      expect(inspectRepository(root)).toEqual([]);
      writeFileSync(requiredFile, JSON.stringify(['security']));
      writeFileSync(join(root, '.github/workflows/test.yml'), JSON.stringify(fixture()));
      const cli = new URL('../lib/ci-policy.ts', import.meta.url).pathname;
      const passed = Bun.spawnSync([process.execPath, cli, root]);
      expect(passed.exitCode).toBe(0);
      expect(passed.stdout.toString()).toContain('PASS: immutable');
      writeFileSync(join(root, '.github/workflows/test.yml'), 'permissions: write-all\non: {pull_request: {}}\njobs: {}\n');
      expect(inspectRepository(root).join()).toContain('test.yml:');
      expect(Bun.spawnSync([process.execPath, cli, root]).exitCode).toBe(1);
      writeFileSync(pinsFile, JSON.stringify([42]));
      expect(inspectRepository(root)).toEqual(['action pins must be an array of strings']);
      writeFileSync(pinsFile, '{invalid');
      const failed = Bun.spawnSync([process.execPath, cli, root]);
      expect(failed.exitCode).toBe(1);
      expect(failed.stderr.toString()).toContain('input is invalid');
    } finally { rmSync(root, { recursive: true, force: true }); }
  });
});
