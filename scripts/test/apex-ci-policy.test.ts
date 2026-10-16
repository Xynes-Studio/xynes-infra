import { describe, expect, test } from 'bun:test';
import { existsSync, readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { checkWorkflow, trustedReleaseGuard } from '../lib/ci-policy';
const path = new URL('../lib/apex-ci-policy.ts', import.meta.url);
const checkout = 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683';
const attest = 'actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6';
const pins = new Set<string>(JSON.parse(readFileSync(new URL('../../.github/ci/action-pins.json', import.meta.url), 'utf8')));
const guard = "github.repository == 'Xynes-Studio/xynes-infra' && " + trustedReleaseGuard;
const context = () => ({ repository: 'Xynes-Studio/xynes-infra', registryPrefix: 'ghcr.io/xynes-studio/apex',
  roles: { 'publish-images': { kind: 'package-publisher' } } });
function fixture(): any {
  return { name: 'Inert publisher policy fixture', on: { workflow_dispatch: {} }, permissions: { contents: 'read' }, jobs: {
    admission: { 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10, steps: [{ run: 'true' }] },
    'build-images': { 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 45, needs: ['admission'], steps: [{ run: 'true' }] },
    'publish-images': { name: 'Publish qualified OCI archives', 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 30,
      needs: ['admission', 'build-images'], if: guard, environment: 'release',
      permissions: { contents: 'read', actions: 'read', packages: 'write' }, steps: [
        { uses: checkout, with: { ref: '${{ github.sha }}', 'persist-credentials': false,
          'sparse-checkout': '/scripts/release/\n/scripts/lib/\n/infra/release/', 'sparse-checkout-cone-mode': false } },
        { run: 'python3 scripts/release/publish_images.py', env: {
          GH_TOKEN: '${{ github.token }}', GH_POLICY_READ_TOKEN: '${{ secrets.RELEASE_POLICY_READ_TOKEN }}',
          APEX_REGISTRY_PREFIX: 'ghcr.io/xynes-studio/apex',
          APEX_ADMISSION_RECEIPT: '${{ needs.admission.outputs.receipt }}',
          APEX_BUILD_RECEIPT: '${{ needs.build-images.outputs.receipt }}' } },
      ] },
  } };
}
async function check(value: unknown, policy: unknown = context(), approvedPins = pins): Promise<string[]> {
  expect(existsSync(path)).toBe(true); // RED is missing role checker, not an import/collection error.
  const { checkApexWorkflow } = await import('../lib/apex-ci-policy');
  return checkApexWorkflow(value, approvedPins, policy);
}
function publisher(value: any) { return value.jobs['publish-images']; }

describe('isolated infra package publisher configuration, never approval', () => {
  test('only a fixed pinned publication-receipt upload and exact output bindings may extend the publisher', async () => {
    const v=fixture();const job=publisher(v);
    job.outputs={subjects:'${{ steps.publish.outputs.subjects }}',receipt:'{"id":${{ steps.publication.outputs.artifact-id }},"digest":"sha256:${{ steps.publication.outputs.artifact-digest }}"}'};
    job.steps[1].id='publish';
    job.steps.push({id:'publication',uses:'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',with:{name:'apex-publication-${{ github.run_id }}-${{ github.run_attempt }}',path:'${{ runner.temp }}/apex-publications/published-receipt.json','if-no-files-found':'error','retention-days':14,'compression-level':0}});
    expect(await check(v)).toEqual([]);
    for(const change of [{path:'${{ github.workspace }}/**'},{name:'${{ inputs.name }}'},{'if-no-files-found':'warn'},{'retention-days':90}]){
      const bad=structuredClone(v);Object.assign(publisher(bad).steps[2].with,change);expect(await check(bad)).not.toEqual([]);
    }
    const bad=structuredClone(v);publisher(bad).outputs.subjects='${{ needs.build-images.outputs.subjects }}';expect(await check(bad)).not.toEqual([]);
  });
  test('adds only the exact checked publisher and leaves original inputs unchanged', async () => {
    const v = fixture(); const before = structuredClone(v);
    expect(checkWorkflow(v, pins).join()).toContain('write permissions');
    expect(await check(v)).toEqual([]);
    expect(v).toEqual(before);
    const { publisherPermissions, publisherGuard, publisherRegistry, publisherCommand } = await import('../lib/apex-ci-policy');
    expect(publisherPermissions).toEqual({ contents: 'read', actions: 'read', packages: 'write' });
    expect(publisherGuard).toBe(guard);expect(publisherRegistry).toBe('ghcr.io/xynes-studio/apex');
    expect(publisherCommand).toBe('python3 scripts/release/publish_images.py');
  });
  test('normal read-only PR remains subject to unchanged canonical policy', async () => {
    const v = fixture();delete v.jobs['publish-images'];v.on = { pull_request: { branches: ['develop'] } };
    expect(await check(v, { ...context(), roles: {} })).toEqual(checkWorkflow(v, pins));
    v.jobs.admission.permissions = { packages: 'write' };
    expect(await check(v, { ...context(), roles: {} })).not.toEqual([]);
  });
  test('rejects unknown role, repository, registry and missing declaration', async () => {
    for (const policy of [null, {}, { ...context(), repository: 'Xynes-Studio/xynes-gateway' },
      { ...context(), registryPrefix: 'ghcr.io/attacker/apex' }, { ...context(), registryPrefix: 'ghcr.io/xynes-studio' },
      { ...context(), roles: {} }, { ...context(), roles: { admission: { kind: 'package-publisher' } } },
      { ...context(), roles: { 'publish-images': { kind: 'deployer' } } },
      { ...context(), roles: { 'publish-images': { kind: 'package-publisher', approved: true } } },
      { ...context(), approved: true }, { ...context(), roles: { 'publish-images': { kind: 'package-publisher' }, other: { kind: 'package-publisher' } } }])
      expect(await check(fixture(), policy)).not.toEqual([]);
  });
  test.each(['pull_request', 'pull_request_target', 'workflow_run', 'push', 'schedule'])('rejects publisher event %s', async event => {
    const v = fixture();v.on = { [event]: {} };expect(await check(v)).not.toEqual([]);
  });
  test('manual event cannot hide a PR event or user-supplied admission boolean', async () => {
    for (const events of [{ workflow_dispatch: {}, pull_request: {} },
      { workflow_dispatch: { inputs: { approved: { type: 'boolean' } } } }]) {
      const v = fixture();v.on = events;expect(await check(v)).not.toEqual([]);
    }
  });
  test.each([undefined, "github.event_name != 'pull_request'", trustedReleaseGuard,
    guard.replace('main', 'develop'), guard.replace('github.ref_protected', 'true'),
    guard.replace('xynes-infra', 'xynes-gateway'), 'always() && ' + guard])('rejects approximate publisher guard %p', async condition => {
    const v = fixture();publisher(v).if = condition;expect(await check(v)).not.toEqual([]);
  });
  test('accepts expression delimiters and bare-on parser representation without other rewrites', async () => {
    const v = fixture();publisher(v).if = '${{ ' + guard + ' }}';
    v.true = v.on;delete v.on;expect(await check(v)).toEqual([]);
    v.on = { pull_request: {} };expect(await check(v)).not.toEqual([]);
  });
  test.each(['prod', '', { name: 'release' }, '${{ inputs.environment }}', undefined])('rejects environment %p', async environment => {
    const v = fixture();publisher(v).environment = environment;expect(await check(v)).not.toEqual([]);
  });
  test('requires successful dependencies by fixed IDs without always or outputs-as-approval', async () => {
    for (const needs of [undefined, [], ['build-images'], ['admission'], ['admission', 'other'],
      ['admission', 'build-images', 'other'], 'admission', ['build-images', 'admission']]) {
      const v = fixture();publisher(v).needs = needs;expect(await check(v)).not.toEqual([]);
    }
    const v = fixture();delete v.jobs.admission;expect(await check(v)).not.toEqual([]);
  });
  test('exact publisher permissions cannot become other writer capabilities', async () => {
    for (const permissions of ['write-all', { contents: 'read', packages: 'write' },
      { contents: 'write', actions: 'read', packages: 'write' }, { contents: 'read', actions: 'write', packages: 'write' },
      { contents: 'read', actions: 'read', packages: 'read' }, { contents: 'read', actions: 'read', packages: 'write', 'id-token': 'write' },
      { contents: 'read', actions: 'read', packages: 'write', attestations: 'write' }]) {
      const v = fixture();publisher(v).permissions = permissions;expect(await check(v)).not.toEqual([]);
    }
    const v = fixture();v.permissions = { contents: 'read', packages: 'write' };expect(await check(v)).not.toEqual([]);
  });
  test('other jobs never inherit a publisher exemption', async () => {
    const v = fixture();v.jobs.hostile = { ...v.jobs.admission, if: guard, environment: 'release', permissions: { packages: 'write' } };
    expect(await check(v)).not.toEqual([]);
  });
  test('canonical signer is preserved and cannot check out or build with signing credentials', async () => {
    const v = fixture();v.jobs.attest = { 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10,
      if: trustedReleaseGuard, environment: 'release', permissions: { contents: 'read', 'id-token': 'write', attestations: 'write' },
      steps: [{ uses: attest }] };
    expect(await check(v)).toEqual([]);
    v.jobs.attest.steps = publisher(v).steps;expect(await check(v)).not.toEqual([]);
    v.jobs.attest.steps = [{ run: 'bun build hostile.ts' }];expect(await check(v)).not.toEqual([]);
  });
  test('requires the independently approved immutable checkout action', async () => {
    for (const uses of ['actions/checkout@v4', 'evil/checkout@' + 'a'.repeat(40), './local/action']) {
      const v = fixture();publisher(v).steps[0].uses = uses;expect(await check(v)).not.toEqual([]);
    }
    expect(await check(fixture(), context(), new Set())).not.toEqual([]);
  });
  test('checkout only reads current exact controller SHA without credential persistence', async () => {
    for (const change of [{ ref: 'main' }, { ref: 'a'.repeat(40) }, { ref: '${{ inputs.controller_sha }}' },
      { repository: 'Xynes-Studio/xynes-infra' }, { repository: 'Xynes-Studio/xynes-auth-app' },
      { 'persist-credentials': true }, { token: '${{ github.token }}' }, { path: 'apps' },
      { submodules: true }, { lfs: true }]) {
      const v = fixture();Object.assign(publisher(v).steps[0].with, change);expect(await check(v)).not.toEqual([]);
    }
  });
  test('sparse checkout refuses application sources, escapes and unrelated build dependencies', async () => {
    for (const pattern of ['', '.', '/scripts/release/\n/scripts/lib/\n/infra/release/\n/apps/',
      '../apps', '**', '/scripts/release/\n/scripts/lib/\n/infra/release/\n/package.json']) {
      const v = fixture();publisher(v).steps[0].with['sparse-checkout'] = pattern;expect(await check(v)).not.toEqual([]);
    }
    const v = fixture();publisher(v).steps[0].with['sparse-checkout-cone-mode'] = true;expect(await check(v)).not.toEqual([]);
  });
  test('actual sparse Git materialization excludes root package files, app code and build recipes', async () => {
    const v = fixture();expect(await check(v)).toEqual([]);
    const base = mkdtempSync(join(tmpdir(), 'apex-publisher-sparse-'));
    try {
      const paths = ['package.json', 'apps/hostile.ts', 'scripts/build.ts', 'infra/images/Dockerfile',
        'scripts/release/publish_images.py', 'scripts/lib/apex-ci-policy.ts', 'infra/release/registry-policy.json'];
      const git = (...args: string[]) => execFileSync('git', ['-C', base, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
      git('init', '-q');
      for (const path of paths) { mkdirSync(dirname(join(base, path)), { recursive: true });writeFileSync(join(base, path), 'inert fixture'); }
      git('add', '.');const tree = git('write-tree'); // No fixture or real repository commit.
      git('config', 'core.sparseCheckout', 'true');git('config', 'core.sparseCheckoutCone', 'false');
      writeFileSync(join(base, '.git/info/sparse-checkout'), publisher(v).steps[0].with['sparse-checkout'] + '\n');
      git('read-tree', '-m', '-u', tree);
      for (const path of paths.slice(0, 4)) expect(existsSync(join(base, path))).toBe(false);
      for (const path of paths.slice(4)) expect(existsSync(join(base, path))).toBe(true);
    } finally { rmSync(base, { recursive: true, force: true }); }
  });
  test.each(['docker build .', 'bun install --frozen-lockfile', 'python3 scripts/release/publish_images.py --approved',
    'echo "$GH_TOKEN"', 'python3 scripts/release/publish_images.py\necho "$GH_TOKEN"',
    'bash -c "python3 scripts/release/publish_images.py"', 'python3 scripts/release/publish_images.py || true'])('rejects publisher source execution or shell leakage %s', async run => {
    const v = fixture();publisher(v).steps[1].run = run;expect(await check(v)).not.toEqual([]);
  });
  test('publisher cannot add login/build/download or attacker-controlled setup actions', async () => {
    const v = fixture();publisher(v).steps.push({ run: 'docker login ghcr.io' });expect(await check(v)).not.toEqual([]);
    publisher(v).steps.pop();publisher(v).steps.push({ uses: 'actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020', with: { 'node-version': '22.23.1' } });
    expect(await check(v)).not.toEqual([]);
  });
  test('auth and opaque receipt inputs are exact bindings, never a passed boolean or untrusted expression', async () => {
    for (const change of [{ GH_TOKEN: '${{ secrets.REGISTRY_TOKEN }}' }, { GH_POLICY_READ_TOKEN: '${{ github.token }}' },
      { APEX_REGISTRY_PREFIX: 'ghcr.io/foreign/apex' }, { APEX_ADMISSION_RECEIPT: '${{ inputs.approved }}' },
      { APEX_BUILD_RECEIPT: '${{ inputs.archive }}' }, { APPROVED: 'true' }, { NODE_OPTIONS: '--require hostile.js' },
      { BASH_ENV: 'hostile.sh' }, { APEX_ADMISSION_RECEIPT: true }]) {
      const v = fixture();Object.assign(publisher(v).steps[1].env, change);expect(await check(v)).not.toEqual([]);
    }
    const v = fixture();delete publisher(v).steps[1].env.GH_POLICY_READ_TOKEN;expect(await check(v)).not.toEqual([]);
  });
  test('root/job/step inheritance cannot override isolated execution or leak credentials', async () => {
    for (const rootChange of [{ env: { GH_TOKEN: '${{ github.token }}' } }, { defaults: { run: { shell: 'bash -x' } } }]) {
      const v = fixture();Object.assign(v, rootChange);expect(await check(v)).not.toEqual([]);
    }
    for (const change of [{ env: { BASH_ENV: 'hostile.sh' } }, { strategy: { matrix: { env: ['prod'] } } },
      { container: 'example@sha256:' + 'a'.repeat(64) }, { services: {} }, { 'continue-on-error': true },
      { uses: './reusable.yml' }, { secrets: 'inherit' }, { defaults: { run: { shell: 'bash -x' } } }]) {
      const v = fixture();Object.assign(publisher(v), change);expect(await check(v)).not.toEqual([]);
    }
    for (const change of [{ shell: 'bash -x' }, { 'working-directory': '../apps' }, { 'continue-on-error': true }, { if: 'false' }]) {
      const v = fixture();Object.assign(publisher(v).steps[1], change);expect(await check(v)).not.toEqual([]);
    }
  });
  test.each([['runs-on', 'self-hosted'], ['timeout-minutes', 61], ['timeout-minutes', 0]])('retains canonical bound %s=%p', async (key, value) => {
    const v = fixture();publisher(v)[key] = value;expect(await check(v)).not.toEqual([]);
  });
  test('malformed, mixed root events and absent publishing job fail closed', async () => {
    for (const v of [null, [], {}, { ...fixture(), true: { pull_request: {} } }]) expect(await check(v)).not.toEqual([]);
    const v = fixture();delete v.jobs['publish-images'];expect(await check(v)).not.toEqual([]);
  });
});

describe('infra workflow policy CLI', () => {
  function repository(run: (root: string) => void) {
    const root = mkdtempSync(join(tmpdir(), 'apex-policy-cli-'));
    try {
      mkdirSync(join(root, '.github/ci'), { recursive: true });
      mkdirSync(join(root, '.github/workflows'));
      writeFileSync(join(root, '.github/ci/action-pins.json'), JSON.stringify([...pins]));
      writeFileSync(join(root, '.github/ci/required-checks.json'), JSON.stringify(['security']));
      const value = fixture();value.jobs.security = value.jobs.admission;
      writeFileSync(join(root, '.github/workflows/apex-build.yml'), JSON.stringify(value));
      run(root);
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
  const cli = (root: string) => Bun.spawnSync(['bun', new URL('../lib/apex-ci-policy.ts', import.meta.url).pathname, root]);
  test('executes the checked publisher role through the real CLI', () => repository(root => {
    const result = cli(root);
    expect(result.exitCode).toBe(0);
    expect(result.stdout.toString()).toContain('PASS:');
    // Generic sibling policy continues refusing package writes.
    const generic = Bun.spawnSync(['bun', new URL('../lib/ci-policy.ts', import.meta.url).pathname, root]);
    expect(generic.exitCode).toBe(1);
  }));
  test('refuses publisher privileges transplanted to another workflow filename', () => repository(root => {
    writeFileSync(join(root, '.github/workflows/other.yml'), readFileSync(join(root, '.github/workflows/apex-build.yml')));
    const result = cli(root);
    expect(result.exitCode).toBe(1);expect(result.stderr.toString()).toContain('other.yml:');
  }));
  test('retains required producer and ordinary PR validation', () => repository(root => {
    writeFileSync(join(root, '.github/ci/required-checks.json'), JSON.stringify(['missing']));
    let result = cli(root);expect(result.exitCode).toBe(1);
    expect(result.stderr.toString()).toContain('Missing required check producer: missing');
    writeFileSync(join(root, '.github/ci/required-checks.json'), JSON.stringify(['security']));
    const value = fixture();delete value.jobs['publish-images'];value.on = { pull_request: {} };
    value.jobs.security = value.jobs.admission;
    writeFileSync(join(root, '.github/workflows/quality.yml'), JSON.stringify(value));
    result = cli(root);expect(result.exitCode).toBe(0);
    value.jobs.security.permissions = { packages: 'write' };
    writeFileSync(join(root, '.github/workflows/quality.yml'), JSON.stringify(value));
    result = cli(root);expect(result.exitCode).toBe(1);
    expect(result.stderr.toString()).toContain('quality.yml:');
  }));
  test('invalid policy input fails without printing its contents', () => repository(root => {
    const sentinel = 'private-fixture-do-not-print';
    writeFileSync(join(root, '.github/workflows/apex-build.yml'), '\t' + sentinel + ': [');
    const result = cli(root);expect(result.exitCode).toBe(1);
    expect(result.stderr.toString()).toContain('CI policy input is invalid or unavailable');
    expect(result.stderr.toString()).not.toContain(sentinel);
  }));
});
