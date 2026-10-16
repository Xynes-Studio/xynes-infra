import { describe, expect, test } from 'bun:test';
import { coverageFailure, runCoverage } from '../lib/ci-coverage';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
describe('native coverage gate', () => {
  test.each(['', 'All files | NaN | 95 |', 'All files | -1 | 95 |', 'All files | 101 | 100 |'])('rejects missing/invalid evidence %s', (text) => expect(coverageFailure(text)).toBeDefined());
  test.each(['All files | 79.99 | 100 |', 'All files | 100 | 79.99 |'])('rejects below-floor metrics %s', (text) => expect(coverageFailure(text)).toContain('below 80%'));
  test('accepts exact boundary and normal Bun formatting', () => {
    expect(coverageFailure('All files | 80 | 80 |')).toBeUndefined();
    expect(coverageFailure('header\n All files | 95.90 | 96.22 |\n')).toBeUndefined();
  });
  test('rejects arbitrary script selection', () => expect(runCoverage('deploy')).toBe(2));
  test('gate itself evaluates successful, failed and missing subprocess evidence', () => {
    const root = mkdtempSync(join(tmpdir(), 'xynes-direct-coverage-'));
    const previous = process.cwd();
    try {
      process.chdir(root);
      for (const [output, code, expected] of [['All files | 90 | 90 |', 0, 0], ['All files | 79 | 90 |', 0, 1], ['missing', 0, 1], ['All files | 90 | 90 |', 7, 7]]) {
        writeFileSync(join(root, 'child.ts'), `console.log(${JSON.stringify(output)});process.exit(${code});`);
        writeFileSync(join(root, 'package.json'), JSON.stringify({ scripts: { 'test:coverage': 'bun child.ts' } }));
        expect(runCoverage('test:coverage')).toBe(expected);
      }
    } finally { process.chdir(previous); rmSync(root, { recursive: true, force: true }); }
  });
  test('CLI preserves child failures and checks actual subprocess output', () => {
    const root = mkdtempSync(join(tmpdir(), 'xynes-coverage-gate-'));
    try {
      const cli = new URL('../lib/ci-coverage.ts', import.meta.url).pathname;
      for (const [output, code, expected] of [['All files | 95 | 95 |', 0, 0], ['All files | 79 | 95 |', 0, 1], ['no coverage', 0, 1], ['All files | 95 | 95 |', 7, 7]]) {
        writeFileSync(join(root, 'child.ts'), `console.log(${JSON.stringify(output)});process.exit(${code});`);
        writeFileSync(join(root, 'package.json'), JSON.stringify({ scripts: { coverage: 'bun child.ts' } }));
        expect(Bun.spawnSync([process.execPath, cli, 'coverage'], { cwd: root }).exitCode).toBe(expected);
      }
      writeFileSync(join(root, 'tiny.test.ts'), "import {test,expect} from 'bun:test';test('inert',()=>expect(1).toBe(1));");
      expect(Bun.spawnSync([process.execPath, cli, '--native'], { cwd: root }).exitCode).toBe(1); // No instrumented production code.
      expect(Bun.spawnSync([process.execPath, cli], { cwd: root }).exitCode).toBe(2);
    } finally { rmSync(root, { recursive: true, force: true }); }
  });
});
