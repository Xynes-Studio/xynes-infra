// Generated from xynes-infra scripts/lib/ci-release-gates.ts.
// Canonical SEC-008 release preflight; generated consumer copies are immutable mirrors.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';

type Mapping = Record<string, unknown>;
const mapping = (value: unknown): value is Mapping => !!value && typeof value === 'object' && !Array.isArray(value);
export function requiredNames(value: unknown): string[] {
  if (!Array.isArray(value) || !value.length || !value.every((name) => typeof name === 'string' && /^[\w ()/-]+$/.test(name)) || new Set(value).size !== value.length) throw new Error('Invalid required check manifest');
  return value;
}
export function assertProtection(value: unknown, required: readonly string[]): void {
  if (!mapping(value) || !mapping(value.required_status_checks) || value.required_status_checks.strict !== true || !Array.isArray(value.required_status_checks.checks)) throw new Error('Strict protected-branch checks are required');
  const checks = value.required_status_checks.checks;
  for (const name of required) if (!checks.some((check) => mapping(check) && check.context === name && check.app_id === 15368)) throw new Error(`Missing app-bound protected check: ${name}`);
  if (!mapping(value.enforce_admins) || value.enforce_admins.enabled !== true || !mapping(value.required_pull_request_reviews) || value.required_pull_request_reviews.dismiss_stale_reviews !== true || !Number.isSafeInteger(value.required_pull_request_reviews.required_approving_review_count) || Number(value.required_pull_request_reviews.required_approving_review_count) < 1 || !mapping(value.required_conversation_resolution) || value.required_conversation_resolution.enabled !== true) throw new Error('Protected review/admin policy is incomplete');
  for (const key of ['allow_force_pushes', 'allow_deletions']) if (!mapping(value[key]) || value[key].enabled !== false) throw new Error('Protected source permits history replacement');
}
export function assertChecks(value: unknown[], required: readonly string[], sha: string): void {
  for (const name of required) {
    const matching = value.filter((check): check is Mapping => mapping(check) && check.name === name);
    if (matching.some((check) => !Number.isSafeInteger(check.id) || Number(check.id) < 1)) throw new Error(`Malformed required check: ${name}`);
    matching.sort((a, b) => Number(b.id) - Number(a.id));
    const check = matching[0];
    if (!check || check.head_sha !== sha || !mapping(check.app) || check.app.id !== 15368 || check.status !== 'completed' || check.conclusion !== 'success') throw new Error(`Exact protected commit check is not successful: ${name}`);
  }
}
export function assertReleaseEnvironment(env: NodeJS.ProcessEnv): { repository: string; sha: string } {
  if (env.GITHUB_EVENT_NAME !== 'workflow_dispatch' || env.GITHUB_REF !== 'refs/heads/main' || env.GITHUB_REF_PROTECTED !== 'true' || !env.GITHUB_SHA || !/^[a-f0-9]{40}$/.test(env.GITHUB_SHA) || !env.GITHUB_REPOSITORY || !/^Xynes-Studio\/[\w-]+$/.test(env.GITHUB_REPOSITORY) || env.GITHUB_API_URL !== 'https://api.github.com') throw new Error('Release requires manual dispatch of protected main on GitHub');
  return { repository: env.GITHUB_REPOSITORY, sha: env.GITHUB_SHA };
}
export function assertPublicBuildEnvironment(env: NodeJS.ProcessEnv): void {
  for (const name of ['NEXT_PUBLIC_AUTH_APP_URL', 'NEXT_PUBLIC_APP_URL', 'NEXT_PUBLIC_API_URL', 'NEXT_PUBLIC_SUPABASE_URL']) {
    const value = env[name];
    if (!value) throw new Error(`Missing public release setting: ${name}`);
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) || url.search || url.hash) throw new Error(`Invalid public release URL: ${name}`);
  }
  const key = env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? '';
  if (/^sb_publishable_[A-Za-z0-9_-]{16,}$/.test(key)) return;
  let claims: unknown;
  try { claims = JSON.parse(Buffer.from(key.split('.')[1] ?? '', 'base64url').toString()); } catch { throw new Error('Missing public Supabase publishable/anon key'); }
  if (key.split('.').length !== 3 || !mapping(claims) || claims.role !== 'anon') throw new Error('Only a public Supabase publishable/anon key may enter a browser build');
}
export type ReadApi = (path: string) => Promise<unknown>;
export function githubReader(token: string, http: (url: string, options: RequestInit) => Promise<Response> = fetch): ReadApi {
  if (!token) throw new Error('Read-only GitHub token is unavailable');
  return async (path) => {
    const response = await http('https://api.github.com' + path, { headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' }, signal: AbortSignal.timeout(15000), redirect: 'error' });
    if (!response.ok) throw new Error(`GitHub release preflight refused (${response.status})`);
    return response.json();
  };
}
export async function validateRemote(read: ReadApi, repository: string, sha: string, required: readonly string[], policyRead: ReadApi = read): Promise<void> {
  assertProtection(await policyRead(`/repos/${repository}/branches/main/protection`), required);
  const checks: unknown[] = [];
  for (let page = 1; page <= 20; page++) {
    const result = await read(`/repos/${repository}/commits/${sha}/check-runs?filter=all&per_page=100&page=${page}`);
    if (!mapping(result) || !Array.isArray(result.check_runs) || result.check_runs.length > 100) throw new Error('Invalid check-run response');
    checks.push(...result.check_runs);
    if (result.check_runs.length < 100) { assertChecks(checks, required, sha); return; }
  }
  throw new Error('Check-run pagination limit exceeded');
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if (process.argv[2] === '--public-build') assertPublicBuildEnvironment(process.env);
    else {
      const { repository, sha } = assertReleaseEnvironment(process.env);
      if (execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim() !== sha) throw new Error('Checkout differs from the protected commit');
      if (process.argv[2] !== '--source') {
        const required = requiredNames(JSON.parse(readFileSync('.github/ci/required-checks.json', 'utf8')));
        // GITHUB_TOKEN cannot request administration:read. Use a separate,
        // repository-scoped read-only token only in the approved protected job.
        await validateRemote(githubReader(process.env.GH_TOKEN ?? ''), repository, sha, required, githubReader(process.env.GH_POLICY_READ_TOKEN ?? ''));
      }
    }
    console.log('PASS: release preflight');
  } catch (error) { console.error(error instanceof Error ? error.message : 'Release preflight failed'); process.exitCode = 1; }
}
