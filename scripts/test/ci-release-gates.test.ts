import { describe, expect, test } from 'bun:test';
import { assertChecks, assertProtection, assertPublicBuildEnvironment, assertReleaseEnvironment, githubReader, requiredNames, validateRemote } from '../lib/ci-release-gates';
const sha = 'a'.repeat(40);
const good = { id: 1, name: 'security', head_sha: sha, app: { id: 15368 }, status: 'completed', conclusion: 'success' };
const protection = () => ({ required_status_checks: { strict: true, checks: [{ context: 'security', app_id: 15368 }] }, enforce_admins: { enabled: true }, required_pull_request_reviews: { dismiss_stale_reviews: true, required_approving_review_count: 1 }, required_conversation_resolution: { enabled: true }, allow_force_pushes: { enabled: false }, allow_deletions: { enabled: false } });
const environment = () => ({ GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_REF: 'refs/heads/main', GITHUB_REF_PROTECTED: 'true', GITHUB_SHA: sha, GITHUB_REPOSITORY: 'Xynes-Studio/fixture', GITHUB_API_URL: 'https://api.github.com' });
const publicEnv = () => ({ NEXT_PUBLIC_AUTH_APP_URL: 'https://auth.fixture.invalid', NEXT_PUBLIC_APP_URL: 'https://app.fixture.invalid', NEXT_PUBLIC_API_URL: 'https://api.fixture.invalid', NEXT_PUBLIC_SUPABASE_URL: 'https://db.fixture.invalid', NEXT_PUBLIC_SUPABASE_ANON_KEY: 'sb_publishable_abcdefghijklmnop' });
describe('release source and quality trust boundary', () => {
  test('requires unique, nonempty named gates', () => { expect(requiredNames(['security'])).toEqual(['security']); for (const value of [null, [], [''], [1], ['security', 'security'], ['bad\nname']]) expect(() => requiredNames(value)).toThrow(); });
  test('accepts complete app-bound protection', () => expect(() => assertProtection(protection(), ['security'])).not.toThrow());
  test('rejects weaker or malformed protection independently', () => {
    for (const value of [null, {}, { ...protection(), required_status_checks: { strict: false, checks: [] } }, { ...protection(), required_status_checks: { strict: true, checks: [{ context: 'security', app_id: -1 }] } }, { ...protection(), enforce_admins: { enabled: false } }, { ...protection(), required_pull_request_reviews: { dismiss_stale_reviews: false, required_approving_review_count: 1 } }, { ...protection(), required_pull_request_reviews: { dismiss_stale_reviews: true, required_approving_review_count: 0 } }, { ...protection(), required_pull_request_reviews: { dismiss_stale_reviews: true } }, { ...protection(), required_conversation_resolution: { enabled: false } }, { ...protection(), allow_force_pushes: { enabled: true } }, { ...protection(), allow_deletions: { enabled: true } }]) expect(() => assertProtection(value, ['security'])).toThrow();
  });
  test('requires every latest exact-commit result from GitHub Actions', () => {
    expect(() => assertChecks([{ ...good, id: 2 }, { ...good, id: 1, conclusion: 'failure' }], ['security'], sha)).not.toThrow();
    for (const value of [[], [null], [{ ...good, id: '1' }], [{ ...good, id: 0 }], [{ ...good, id: -1 }], [{ ...good, head_sha: 'b'.repeat(40) }], [{ ...good, app: null }], [{ ...good, app: { id: 1 } }], [{ ...good, status: 'in_progress' }], [{ ...good, conclusion: 'skipped' }], [{ ...good, conclusion: 'neutral' }], [good, { ...good, id: 2, conclusion: 'failure' }], [good, { ...good, id: 2, app: { id: 7 } }]]) expect(() => assertChecks(value, ['security'], sha)).toThrow();
  });
  test('dispatch requires protected main and fixed API origin', () => {
    expect(assertReleaseEnvironment(environment())).toEqual({ repository: 'Xynes-Studio/fixture', sha });
    for (const key of Object.keys(environment())) expect(() => assertReleaseEnvironment({ ...environment(), [key]: 'attacker-fixture' })).toThrow();
  });
  test('browser release builds accept only public URLs and public keys', () => {
    expect(() => assertPublicBuildEnvironment(publicEnv())).not.toThrow();
    const anon = 'e30.' + Buffer.from(JSON.stringify({ role: 'anon' })).toString('base64url') + '.fixture';
    expect(() => assertPublicBuildEnvironment({ ...publicEnv(), NEXT_PUBLIC_SUPABASE_ANON_KEY: anon })).not.toThrow();
    for (const key of ['fixture-public-key', 'sb_secret_abcdefghijklmnop', 'e30.' + Buffer.from(JSON.stringify({ role: 'service_role' })).toString('base64url') + '.fixture', 'e30.' + Buffer.from('null').toString('base64url') + '.fixture']) expect(() => assertPublicBuildEnvironment({ ...publicEnv(), NEXT_PUBLIC_SUPABASE_ANON_KEY: key })).toThrow();
    for (const url of ['', 'not-a-url', 'http://app.fixture.invalid', 'https://localhost', 'https://127.0.0.1', 'https://[::1]', 'https://user:password@app.fixture.invalid', 'https://app.fixture.invalid?token=fixture', 'https://app.fixture.invalid#fragment']) expect(() => assertPublicBuildEnvironment({ ...publicEnv(), NEXT_PUBLIC_APP_URL: url })).toThrow();
  });
  test('pagination cannot conceal a later failure', async () => {
    const requests: string[] = [];
    await validateRemote(async (path) => { requests.push(path); if (path.endsWith('/protection')) return protection(); return { check_runs: [good] }; }, 'Xynes-Studio/fixture', sha, ['security']);
    expect(requests).toHaveLength(2);
    await expect(validateRemote(async (path) => path.endsWith('/protection') ? protection() : path.endsWith('page=1') ? { check_runs: Array.from({ length: 100 }, (_, i) => ({ ...good, id: i + 1 })) } : { check_runs: [{ ...good, id: 101, conclusion: 'failure' }] }, 'Xynes-Studio/fixture', sha, ['security'])).rejects.toThrow('not successful');
  });
  test('malformed, unavailable and endless APIs fail closed', async () => {
    await expect(validateRemote(async () => { throw new Error('unavailable'); }, 'Xynes-Studio/fixture', sha, ['security'])).rejects.toThrow('unavailable');
    await expect(validateRemote(async (path) => path.endsWith('/protection') ? protection() : {}, 'Xynes-Studio/fixture', sha, ['security'])).rejects.toThrow('Invalid check-run');
    await expect(validateRemote(async (path) => path.endsWith('/protection') ? protection() : { check_runs: Array(100).fill(good) }, 'Xynes-Studio/fixture', sha, ['security'])).rejects.toThrow('pagination limit');
  });
  test('administration metadata uses a separate reader from commit checks', async () => {
    const calls: string[] = [];
    await validateRemote(async (path) => { expect(path).toContain('/check-runs?'); calls.push('checks'); return { check_runs: [good] }; }, 'Xynes-Studio/fixture', sha, ['security'], async (path) => { expect(path).toEndWith('/branches/main/protection'); calls.push('policy'); return protection(); });
    expect(calls).toEqual(['policy', 'checks']);
  });
  test('actual CLI refuses non-GitHub execution and missing browser settings', () => {
    const script = new URL('../lib/ci-release-gates.ts', import.meta.url).pathname;
    for (const args of [[], ['--public-build']]) {
      const result = Bun.spawnSync([process.execPath, script, ...args], { env: { PATH: process.env.PATH ?? '' } });
      expect(result.exitCode).toBe(1);
      expect(result.stdout.toString()).not.toContain('PASS');
    }
  });
  test('HTTP transport is fixed-origin, bounded and rejects redirects/non-success', async () => {
    expect(() => githubReader('')).toThrow('unavailable');
    const read = githubReader('inert-fixture', async (url, options) => {
      expect(url).toBe('https://api.github.com/repos/Xynes-Studio/fixture');
      expect(options.redirect).toBe('error');
      expect(options.signal).toBeInstanceOf(AbortSignal);
      expect(new Headers(options.headers).get('Authorization')).toBe('Bearer inert-fixture');
      return Response.json({ inert: true });
    });
    expect(await read('/repos/Xynes-Studio/fixture')).toEqual({ inert: true });
    await expect(githubReader('fixture', async () => new Response('do not expose remote errors', { status: 403 }))('/fixture')).rejects.toThrow('refused (403)');
    await expect(githubReader('fixture', async () => { throw new Error('network unavailable'); })('/fixture')).rejects.toThrow('network unavailable');
  });
});
