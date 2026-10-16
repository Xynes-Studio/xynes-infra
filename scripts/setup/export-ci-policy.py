#!/usr/bin/env python3
"""Export the canonical standalone policy to independent repositories; no Git writes."""
import argparse
import json
from pathlib import Path
import sys
import subprocess

infra = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--workspace-root', type=Path, default=infra.parent.parent)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
manifest = json.loads((infra / 'scripts/setup/ci-action-pins.json').read_text())
pins = sorted(pin['uses'] for pin in manifest)
source = (infra / 'scripts/lib/ci-policy.ts').read_text()
coverage = (infra / 'scripts/lib/ci-coverage.ts').read_text()
release = (infra / 'scripts/lib/ci-release-gates.ts').read_text()
prefix = '// Generated from xynes-infra scripts/lib/ci-policy.ts; run scripts/setup/export-ci-policy.py.\n'
paths = ['xynes/xynes-infra', 'xynes/xynes-gateway', 'xynes/xynes-accounts-service',
         'xynes/xynes-authz-service', 'xynes/xynes-cms-core', 'xynes/xynes-doc-service',
         'xynes/xynes-telemetry-service', 'xynes/xynes-storage-service',
         'xynes/xynes-platform-config', 'xynes/xynes-platform-contracts',
         'xynes-front-end/infra', 'xynes-front-end/lumia-ds', 'xynes-front-end/xynes-auth-sdk',
         'xynes-front-end/xynes-i18n', 'xynes-front-end/xynes-auth-app',
         'xynes-front-end/xynes-cms-console-web', 'xynes/api-docs']
errors = []
for relative in paths:
    repo = args.workspace_root / relative
    if not repo.is_dir() or not (repo / '.git').exists():
        errors.append(f'{relative}: checkout missing')
        continue
    slug = 'xynes-frontend-infra' if relative == 'xynes-front-end/infra' else 'xynes-api-docs' if relative == 'xynes/api-docs' else relative.split('/')[-1]
    policy_args = ['bash', str(infra / 'scripts/setup/configure-branch-protection.sh'), '--print-policy', '--repo', slug, '--branch', 'main']
    if relative == 'xynes/api-docs':
        policy_args.extend(['--repos-file', str(infra / 'scripts/setup/ci-api-docs-repos.json')])
    payload = json.loads(subprocess.check_output(policy_args, text=True))
    checks = payload['required_status_checks']['contexts']
    exports = [('ci-policy.ts', prefix + source), ('release-gates.ts', '// Generated from xynes-infra scripts/lib/ci-release-gates.ts.\n' + release), ('action-pins.json', json.dumps(pins, indent=2) + '\n'), ('required-checks.json', json.dumps(checks, indent=2) + '\n')]
    if relative.startswith('xynes/') and relative not in ['xynes/xynes-telemetry-service', 'xynes/xynes-platform-contracts', 'xynes/api-docs']:
        exports.append(('coverage-gate.ts', '// Generated from xynes-infra scripts/lib/ci-coverage.ts.\n' + coverage))
    for name, expected in exports:
        target = repo / '.github/ci' / name
        if args.check:
            if not target.is_file() or target.read_text() != expected:
                errors.append(f'{relative}: {name} differs from canonical source')
        else:
            if target.is_symlink() or target.parent.is_symlink() or target.parent.parent.is_symlink():
                raise RuntimeError('Refusing symlink output')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected)
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(paths)} repository policy exports ' + ('match' if args.check else 'written'))
