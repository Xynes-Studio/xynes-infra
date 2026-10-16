// Generated from xynes-infra scripts/lib/ci-coverage.ts.
// Canonical SEC-008 native Bun coverage gate; no environment threshold overrides.
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

export function coverageFailure(output: string): string | undefined {
  const row = output.match(/(?:^|\n)\s*All files\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|/);
  if (!row) return 'Missing native Bun coverage evidence';
  const functions = Number(row[1]);
  const lines = Number(row[2]);
  if (![functions, lines].every((n) => Number.isFinite(n) && n >= 0 && n <= 100)) return 'Invalid coverage metrics';
  if (functions < 80 || lines < 80) return `ADR-001 coverage below 80% (functions=${functions}, lines=${lines})`;
}

export function runCoverage(script: string): number {
  const args = script === '--policy' ? ['test', 'scripts/test/ci-policy.test.ts', 'scripts/test/ci-coverage.test.ts', 'scripts/test/ci-release-gates.test.ts', '--coverage'] : script === '--native' ? ['test', '--coverage'] : script === 'coverage' || script === 'test:coverage' ? ['run', script] : undefined;
  if (!args) { console.error('Expected coverage, test:coverage, --native or --policy'); return 2; }
  const result = spawnSync(process.execPath, args, { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 600000 });
  process.stdout.write(result.stdout ?? '');
  process.stderr.write(result.stderr ?? '');
  if (result.error || result.status === null) { console.error('Coverage command failed or timed out'); return 1; }
  if (result.status !== 0) return result.status;
  const failure = coverageFailure((result.stdout ?? '') + '\n' + (result.stderr ?? ''));
  if (failure) { console.error(failure); return 1; }
  console.log('PASS: ADR-001 native Bun coverage floor');
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) process.exitCode = runCoverage(process.argv[2] ?? '');
