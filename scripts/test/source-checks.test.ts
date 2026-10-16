import { describe, expect, test } from 'bun:test';
import { loadRequiredCatalog, validateSources, verifySourceChecks } from '../release/source_checks';
import * as SourceChecks from '../release/source_checks';
const sourceReader = SourceChecks.sourceReader;
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const manifest = JSON.parse(readFileSync(new URL('../../infra/release/source-manifest.json', import.meta.url), 'utf8'));

describe('bounded transient source reads', () => {
  test('retries transient read failures and returns only successful native data', async () => {
    expect(typeof sourceReader).toBe('function');
    let calls = 0;
    const statuses = [502, 503, 200];
    const read = sourceReader('inert-fixture', async (url, options) => {
      expect(url).toBe('https://api.github.com/repos/Xynes-Studio/xynes-gateway');
      expect(options.redirect).toBe('error');
      expect(options.signal).toBeInstanceOf(AbortSignal);
      return new Response(JSON.stringify({native_fixture: true}), {status: statuses[calls++]});
    });
    expect(await read('/repos/Xynes-Studio/xynes-gateway')).toEqual({native_fixture: true});
    expect(calls).toBe(3);
  });
  test('three failed transient attempts refuse; auth and malformed data never retry', async () => {
    expect(typeof sourceReader).toBe('function');
    for (const [status, body, expected] of [[504, '{}', 3], [401, '{}', 1], [200, 'not-json', 1]] as const) {
      let calls = 0;
      const read = sourceReader('inert-fixture', async () => {calls++; return new Response(body, {status});});
      await expect(read('/repos/Xynes-Studio/xynes-gateway')).rejects.toThrow();
      expect(calls).toBe(expected);
    }
  });
});

describe('bounded exact-source checks without deployment authorization', () => {
  test('accepts only the closed twelve immutable source repositories', () => {
    expect(validateSources(manifest.sources)).toEqual(manifest.sources);
    for (const value of [{}, { ...manifest.sources, hostile: manifest.sources['xynes-gateway'] },
      { ...manifest.sources, 'xynes-gateway': { repository: 'other/xynes-gateway', commit: 'a'.repeat(40) } },
      { ...manifest.sources, 'xynes-gateway': { repository: 'Xynes-Studio/xynes-gateway', commit: 'develop' } },
      { ...manifest.sources, 'xynes-gateway': { ...manifest.sources['xynes-gateway'], token: 'do-not-expose' } }])
      expect(() => validateSources(value)).toThrow();
  });
  test('catalog retains canonical repository-specific security/browser checks', () => {
    const catalog = loadRequiredCatalog();
    expect(JSON.parse(readFileSync(new URL('../../infra/release/source-check-catalog.json',import.meta.url),'utf8'))).toEqual(catalog);
    expect(Object.keys(catalog)).toHaveLength(12);
    expect(catalog['xynes-storage-service']['archive-safety']).toBe('.github/workflows/ci.yml');
    expect(catalog['xynes-storage-service']['immutable-source-security']).toBe('.github/workflows/ci.yml');
    expect(catalog['lumia-ds']['Visual Regression']).toBe('.github/workflows/visual-regression.yml');
    expect(catalog['lumia-ds']['test']).toBe('.github/workflows/test.yml');
    expect(catalog['lumia-ds']['build']).toBe('.github/workflows/ci.yml');
    expect(catalog['xynes-i18n']['CodeQL analyze']).toBe('.github/workflows/codeql.yml');
    expect(catalog['xynes-cms-console-web']['Code and browser gates']).toBe('.github/workflows/quality.yml');
    expect(catalog['xynes-gateway']['security']).toBe('.github/workflows/security.yml');
  });
});

import { createHash } from 'node:crypto';
const catalog = loadRequiredCatalog();
const fixture = () => {
  const replies = new Map<string, unknown>();
  let sequence = 1;
  for (const [key, source] of Object.entries(manifest.sources) as [string, { repository: string; commit: string }][]) {
    const repo = source.repository;
    const checks: unknown[] = [];
    for (const path of new Set(Object.values(catalog[key]))) {
      const workflowId = sequence++;
      const runId = sequence++;
      const suiteId = sequence++;
      const workflowChecks = Object.entries(catalog[key]).filter(([, expected]) => expected === path).map(([name]) => name);
      const jobs: Record<string, unknown> = {};
      workflowChecks.forEach((name, index) => jobs['job' + index] = { name, steps: [{ run: 'true' }] });
      const bytes = Buffer.from(JSON.stringify({ jobs }));
      replies.set(`/repos/${repo}/contents/${path}?ref=${source.commit}`, { path, type: 'file', encoding: 'base64', content: bytes.toString('base64'), size: bytes.length, sha: createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex') });
      replies.set(`/repos/${repo}/actions/workflows/${workflowId}`, { id: workflowId, path });
      replies.set(`/repos/${repo}/actions/runs/${runId}`, { id: runId, head_sha: source.commit, repository: { full_name: repo }, head_repository: { full_name: repo }, workflow_id: workflowId, path, check_suite_id: suiteId, status: 'completed', conclusion: 'success', run_attempt: 1 });
      for (const name of workflowChecks) {
        const checkId = sequence++;
        const jobId = sequence++;
        replies.set(`/repos/${repo}/actions/jobs/${jobId}`, { id: jobId, name, head_sha: source.commit, run_id: runId, run_attempt: 1, check_run_url: `https://api.github.com/repos/${repo}/check-runs/${checkId}`, status: 'completed', conclusion: 'success' });
        checks.push({ id: checkId, name, head_sha: source.commit, app: { id: 15368 }, status: 'completed', conclusion: 'success', check_suite: { id: suiteId }, details_url: `https://github.com/${repo}/actions/runs/${runId}/job/${jobId}` });
      }
    }
    replies.set(`/repos/${repo}/commits/${source.commit}/check-runs?filter=all&per_page=100&page=1`, { check_runs: checks });
  }
  const calls: string[] = [];
  return { replies, calls, read: async (path: string) => { calls.push(path); if (!replies.has(path)) throw new Error('Unexpected fixture request'); return structuredClone(replies.get(path)); } };
};
const firstRepository = 'Xynes-Studio/xynes-gateway';
const mutate = (setup: ReturnType<typeof fixture>, match: (path: string) => boolean, change: (value: any) => unknown) => {
  for (const [path, value] of setup.replies) if (path.startsWith('/repos/' + firstRepository + '/') && match(path)) setup.replies.set(path, change(value));
};

describe('check to producer trust chain', () => {
  test('verifies actual jobs/runs/workflows for all sources, without implying approval', async () => {
    const setup = fixture();
    const result: any = await verifySourceChecks(manifest.sources, setup.read, catalog);
    expect(result.checks_verified).toBe(true);
    expect(result.approved).toBe(false);
    expect(result.deployable).toBe(false);
    expect(result.sources).toHaveLength(12);
    expect(setup.calls.every((path) => !path.includes('/protection') && !path.includes('dispatch'))).toBe(true);
    expect(result.sources[0].checks[0].check_id).toBeGreaterThan(0);
  });
  test('rejects foreign app, wrong commit, incomplete and failed latest check', async () => {
    for (const change of [
      (check: any) => ({ ...check, app: { id: 7 } }),
      (check: any) => ({ ...check, head_sha: 'a'.repeat(40) }),
      (check: any) => ({ ...check, status: 'in_progress' }),
      (check: any) => ({ ...check, conclusion: 'failure' }),
      (check: any) => ({ ...check, id: 0 }),
    ]) {
      const setup = fixture();
      mutate(setup, (path) => path.includes('/check-runs?'), (value) => ({ check_runs: [change(value.check_runs[0]), ...value.check_runs.slice(1)] }));
      await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow();
    }
  });
  test('checks every pagination page and fails on a later higher-ID rerun', async () => {
    const setup = fixture();
    const prefix = `/repos/${firstRepository}/commits/${manifest.sources['xynes-gateway'].commit}/check-runs?filter=all&per_page=100&page=`;
    const checks = (setup.replies.get(prefix + '1') as any).check_runs;
    setup.replies.set(prefix + '1', { check_runs: [...checks, ...Array.from({ length: 100 - checks.length }, (_, i) => ({ id: 1000 + i, name: 'irrelevant' }))] });
    setup.replies.set(prefix + '2', { check_runs: [{ ...checks[0], id: 999999, conclusion: 'failure' }] });
    await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow('not successful');
    expect(setup.calls.some((path) => path === prefix + '2')).toBe(true);
  });
  test('rejects spoofed external/wrong-repository check details links', async () => {
    for (const details_url of ['https://attacker.invalid/Xynes-Studio/xynes-gateway/actions/runs/2/job/4', 'https://github.com/other/repo/actions/runs/2/job/4', 'https://github.com/Xynes-Studio/xynes-gateway/actions/runs/2/job/4?token=secret', 'https://github.com/Xynes-Studio/xynes-gateway/actions/runs/0/job/4']) {
      const setup = fixture();
      mutate(setup, (path) => path.includes('/check-runs?'), (value) => ({ check_runs: [{ ...value.check_runs[0], details_url }, ...value.check_runs.slice(1)] }));
      await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow();
    }
  });
  test('binds job ID/check ID/name/run ID/attempt/commit and result', async () => {
    for (const change of [{ id: 999 }, { check_run_url: 'https://api.github.com/repos/other/repo/check-runs/1' }, { name: 'spoof' }, { run_id: 999 }, { run_attempt: 2 }, { head_sha: 'a'.repeat(40) }, { status: 'queued' }, { conclusion: 'neutral' }]) {
      const setup = fixture();
      mutate(setup, (path) => path.includes('/actions/jobs/'), (value) => ({ ...value, ...change }));
      await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow();
    }
  });
  test('binds run repository/head repository/commit/check suite/workflow/attempt/result', async () => {
    for (const change of [{ id: 999 }, { repository: { full_name: 'other/repo' } }, { head_repository: { full_name: 'fork/repo' } }, { head_sha: 'a'.repeat(40) }, { check_suite_id: 999 }, { path: '.github/workflows/spoof.yml' }, { run_attempt: 0 }, { status: 'queued' }, { conclusion: 'failure' }]) {
      const setup = fixture();
      mutate(setup, (path) => path.includes('/actions/runs/'), (value) => ({ ...value, ...change }));
      await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow();
    }
  });
  test('wrong workflow metadata/path or exact-source producer body fails closed', async () => {
    for (const change of [{ id: 999 }, { path: '.github/workflows/spoof.yml' }]) {
      const setup = fixture();
      mutate(setup, (path) => path.includes('/actions/workflows/'), (value) => ({ ...value, ...change }));
      await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow();
    }
    for (const change of [{ sha: 'a'.repeat(40) }, { encoding: 'utf8' }, { content: Buffer.from('{"jobs":{}}').toString('base64') }, { path: '.github/workflows/spoof.yml' }]) {
      const setup = fixture();
      mutate(setup, (path) => path.includes('/contents/'), (value) => ({ ...value, ...change }));
      await expect(verifySourceChecks(manifest.sources, setup.read, catalog)).rejects.toThrow();
    }
  });
  test('API failures, malformed pages, endless pagination and duplicate IDs fail closed', async () => {
    await expect(verifySourceChecks(manifest.sources, async () => { throw new Error('unavailable'); }, catalog)).rejects.toThrow();
    for (const check_runs of [null, {}, Array(101).fill({}), [{ id: 1, name: 'irrelevant' }, { id: 1, name: 'irrelevant' }]]) {
      await expect(verifySourceChecks(manifest.sources, async () => ({ check_runs }), catalog)).rejects.toThrow();
    }
    let page = 0;
    await expect(verifySourceChecks(manifest.sources, async () => ({ check_runs: Array.from({ length: 100 }, (_, i) => ({ id: (++page) * 100 + i, name: 'irrelevant' })) }), catalog)).rejects.toThrow('pagination limit');
  });
});


describe('non-authorizing sanitized CLI', () => {
  test('missing arguments and read credential fail without exposing manifest values', () => {
    const script = new URL('../release/source_checks.ts', import.meta.url).pathname;
    const dir = mkdtempSync(join(tmpdir(), 'apex-source-checks-cli-'));
    try {
      const file = join(dir, 'manifest.json');
      writeFileSync(file, JSON.stringify(manifest));
      for (const args of [[], ['--manifest', file]]) {
        const result = spawnSync(process.execPath, [script, ...args], {encoding:'utf8', env:{...process.env, GH_TOKEN:''}});
        expect(result.status).toBe(1);
        expect(result.stdout).toBe('');
        expect(result.stderr).toContain('Source check verification refused');
        expect(result.stderr).not.toContain(manifest.sources['xynes-gateway'].commit);
      }
      writeFileSync(file, '{"DO-NOT-EXPOSE":"secret","schema_version":1,"schema_version":1}');
      const result = spawnSync(process.execPath, [script, '--manifest', file], {encoding:'utf8', env:{...process.env, GH_TOKEN:''}});
      expect(result.status).toBe(1);expect(result.stdout).toBe('');expect(result.stderr).not.toContain('DO-NOT-EXPOSE');
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
  test('exact Git hash is insufficient when workflow body has no jobs', async () => {
    const setup = fixture();
    const bytes = Buffer.from('{"jobs":{}}');
    mutate(setup, path => path.includes('/contents/'), value => ({...value, content:bytes.toString('base64'), size:bytes.length, sha:createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex')}));
    await expect(verifySourceChecks(manifest.sources,setup.read,catalog)).rejects.toThrow('no jobs');
  });
});
