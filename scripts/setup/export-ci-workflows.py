#!/usr/bin/env python3
"""Render the recorded quality profiles without applying GitHub configuration."""
import argparse
import json
from pathlib import Path
import re
import sys
import subprocess

infra = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--workspace-root', type=Path, default=infra.parent.parent)
parser.add_argument('--check', action='store_true')
parser.add_argument('--repository', help='Export only this canonical repository path')
parser.add_argument('--checkout', type=Path, help='Explicit checkout for selected xynes/xynes-infra export')
parser.add_argument('--workflow', choices=('quality-gates.yml', 'release-provenance.yml'), help='Export only this workflow')
args = parser.parse_args()
manifest = json.loads((infra / 'scripts/setup/ci-action-pins.json').read_text())
actions = {p['upstream_repository']: p['uses'] for p in manifest if p['upstream_repository'] != 'github/codeql-action'}
profiles = json.loads((infra / 'scripts/setup/ci-quality-profiles.json').read_text())
if args.repository:
    profiles = [profile for profile in profiles if profile['repository'] == args.repository]
    if len(profiles) != 1:
        parser.error('Unknown canonical repository selection')
if args.checkout:
    if args.repository != 'xynes/xynes-infra' or args.checkout.is_symlink():
        parser.error('Explicit checkout is supported only for the selected infra repository and a real directory')
    slug = 'xynes-infra'
    remote = subprocess.run(['git', '-C', str(args.checkout), 'remote', 'get-url', 'origin'], capture_output=True, text=True)
    expected = r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)Xynes-Studio/' + re.escape(slug) + r'(?:\.git)?'
    if remote.returncode or not re.fullmatch(expected, remote.stdout.strip()):
        parser.error('Selected checkout does not belong to the canonical repository')
peers = {'lumia-ds': '9add6012fd37ff4a26a0c192d80f05cd01fa8efa',
         'xynes-auth-sdk': 'a9e6265863ea31fa14eb991da95615494f2a6822',
         'xynes-i18n': '16c31d264e1065c443f57311fc96768022c1605b'}


def yaml(value, indent=0):
    """Serialize known JSON values using quoted YAML scalars; no YAML parser."""
    pad = ' ' * indent
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = key if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9-]*', key) and key not in ['on', 'off', 'true', 'false', 'yes', 'no'] else json.dumps(key)
            if isinstance(item, (dict, list)) and item:
                lines.append(pad + label + ':')
                lines.extend(yaml(item, indent + 2))
            elif isinstance(item, str) and '\n' in item:
                lines.append(pad + label + ': |')
                lines.extend(' ' * (indent + 2) + line for line in item.splitlines())
            else:
                lines.append(pad + label + ': ' + json.dumps(item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(pad + '-')
                lines.extend(yaml(item, indent + 2))
            else:
                lines.append(pad + '- ' + json.dumps(item))
    else:
        raise ValueError('Unexpected YAML value')
    return lines


def checkout(path=None, repository=None, ref=None):
    options = {'persist-credentials': False}
    if path: options['path'] = path
    if repository: options.update(repository=repository, ref=ref)
    return {'uses': actions['actions/checkout'], 'with': options}


def setup_node():
    return {'uses': actions['actions/setup-node'], 'with': {'node-version': '22.22.0'}}


def setup_pnpm(version='10.33.0'):
    return {'uses': actions['pnpm/action-setup'], 'with': {'version': version}}


def run(command, directory=None):
    step = {'run': command}
    if directory: step['working-directory'] = directory
    return step


def workflow(profile):
    kind = profile['kind']
    name = profile['repository'].split('/')[-1]
    steps = [checkout()]
    job_env = {'GATE_NAME': '${{ matrix.gate }}'}
    if kind.startswith('bun') or kind in ['infra', 'docs']:
        steps.append({'uses': actions['oven-sh/setup-bun'], 'with': {'bun-version': '1.4.2'}})
    if kind in ['pnpm', 'lumia', 'app', 'fe-infra', 'infra', 'docs']:
        steps.append(setup_node())
    if kind.startswith('bun'):
        if kind == 'bun':
            job_env.update(NODE_ENV='test', RUN_INTEGRATION_TESTS='false', XYNES_ENV_FILE='.github/ci/fixture.env')
            steps.append(run("printf '%s\\n' 'NODE_ENV=test' 'RUN_INTEGRATION_TESTS=false' 'DATABASE_URL=postgresql://fixture:fixture@127.0.0.1:1/security_fixture' 'ACCOUNTS_DATABASE_URL=postgresql://fixture:fixture@127.0.0.1:1/security_fixture' > .github/ci/fixture.env"))
        steps.append(run('bun install --frozen-lockfile'))
    elif kind in ['pnpm', 'lumia']:
        package = json.loads((args.workspace_root / profile['repository'] / 'package.json').read_text())
        version = package['packageManager'].removeprefix('pnpm@')
        expected = '10.23.0' if kind == 'lumia' else '10.33.0'
        if not re.fullmatch(re.escape(expected) + r'(?:\+sha512\.[a-f0-9]{128})?', version):
            raise ValueError('Unexpected package-manager pin')
        steps.append(setup_pnpm(version))
        steps.append(run('pnpm install --frozen-lockfile'))
        if kind == 'lumia': steps.append(run("pnpm --filter './packages/*' build"))
    elif kind == 'app':
        steps = [checkout(name)]
        steps.extend(checkout(peer, 'Xynes-Studio/' + peer, sha) for peer, sha in peers.items())
        steps.extend([setup_node(), setup_pnpm(), run('pnpm install --frozen-lockfile\npnpm --filter @lumia-ui/components... --filter @lumia-ui/forms... --filter @lumia-ui/editor... --filter @lumia-ui/icons... --filter @lumia-ui/layout... --filter @lumia-ui/marketing... build', 'lumia-ds')])
        for peer in ['xynes-auth-sdk', 'xynes-i18n']:
            steps.append(run('pnpm install --frozen-lockfile\npnpm build', peer))
        # Siblings have separate reviewed locks; the auth app's workspace list
        # must not re-install them using its unrelated cross-repo importers.
        steps.append(run('pnpm install --frozen-lockfile --ignore-workspace', name))
        job_env.update(NEXT_PUBLIC_AUTH_APP_URL='http://localhost:3100', NEXT_PUBLIC_APP_URL='http://localhost:3100' if name == 'xynes-auth-app' else 'http://localhost:3000', NEXT_PUBLIC_API_URL='http://127.0.0.1:4100', NEXT_PUBLIC_SUPABASE_URL='http://127.0.0.1:54321', NEXT_PUBLIC_SUPABASE_ANON_KEY='fixture-public-key', NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS='localhost:3000,localhost:3100,127.0.0.1:3200')
    elif kind == 'infra':
        step = setup_pnpm()
        step['if'] = "matrix.gate == 'typecheck'"
        steps.append(step)
        step = run('pnpm install --frozen-lockfile --ignore-scripts', '.github/type-tools')
        step['if'] = "matrix.gate == 'typecheck'"
        steps.append(step)
    elif kind == 'docs':
        step = checkout('ci-type-tools', 'Xynes-Studio/xynes-platform-contracts', '141e32a21deccfbb6711c2cbc770bffaf983b791')
        step['if'] = "matrix.gate == 'typecheck'"
        steps.append(step)
        step = setup_pnpm()
        step['if'] = "matrix.gate == 'typecheck'"
        steps.append(step)
        step = run('pnpm install --frozen-lockfile', 'ci-type-tools')
        step['if'] = "matrix.gate == 'typecheck'"
        steps.append(step)
    if kind == 'infra':
        step = run('set -euo pipefail\n'
                   'age_dir="${RUNNER_TEMP:?}/age-tools"\n'
                   'mkdir -p "$age_dir"\nchmod 700 "$age_dir"\n'
                   'curl --fail --silent --show-error --location https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz --output "$age_dir/age.tar.gz"\n'
                   'printf \'%s  %s\\n\' cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 "$age_dir/age.tar.gz" | sha256sum -c -\n'
                   'tar -xzf "$age_dir/age.tar.gz" --strip-components=1 -C "$age_dir" age/age age/age-keygen\n'
                   'printf \'XYNES_TEST_AGE_BIN=%s\\n\' "$age_dir" >> "${GITHUB_ENV:?}"')
        step['name'] = 'Install independently verified age fixture binaries'
        step['if'] = "matrix.gate == 'test'"
        steps.append(step)
        tools = profile.get('apex_test_tools')
        if (not isinstance(tools, dict) or set(tools) != {'go_version', 'go_linux_amd64_sha256', 'linux_fixture', 'postgres_fixture', 'postgres_auth_fixture', 'auth_fixture'}
                or tools['go_version'] != '1.27.2'
                or not isinstance(tools['go_linux_amd64_sha256'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', tools['go_linux_amd64_sha256'])
                or tools['postgres_fixture'] != 'postgres:17-alpine@sha256:979c4379dd698aba0b890599a6104e082035f98ef31d9b9291ec22f2b13059ca'
                or tools['postgres_auth_fixture'] != 'postgres:17-alpine@sha256:5a6fcbc5d93831991d2386fa634509b3c49a1ac5ffb70c13c2322840f821d7e7'
                or tools['auth_fixture'] != 'supabase/gotrue:v2.196.0@sha256:7e813221b93fbf54b515036438550e483bfaf057b9db52fe9bc1ce91c47e817e'
                or not isinstance(tools['linux_fixture'], str)
                or not re.fullmatch(r'node:22\.[0-9]+\.[0-9]+-alpine[0-9]+\.[0-9]+@sha256:[a-f0-9]{64}', tools['linux_fixture'])):
            raise ValueError('Invalid independently verified Apex tool inventory')
        prepare = f'''set -euo pipefail
apex_dir="${{RUNNER_TEMP:?}}/apex-go-tools"
mkdir "$apex_dir"
chmod 700 "$apex_dir"
curl --fail --silent --show-error --location https://go.dev/dl/go{tools['go_version']}.linux-amd64.tar.gz --output "$apex_dir/go.tar.gz"
printf '%s  %s\\n' {tools['go_linux_amd64_sha256']} "$apex_dir/go.tar.gz" | sha256sum -c -
tar -xzf "$apex_dir/go.tar.gz" -C "$apex_dir"
mkdir "$apex_dir/cache"
GOROOT="$apex_dir/go" GOTOOLCHAIN=local "$apex_dir/go/bin/go" version | grep -qx 'go version go{tools['go_version']} linux/amd64'
fixture='{tools['linux_fixture']}'
docker pull --platform linux/amd64 "$fixture"
[[ "$(docker image inspect --format '{{{{.Os}}}}/{{{{.Architecture}}}}' "$fixture")" == linux/amd64 ]]
postgres_fixture='{tools['postgres_fixture']}'
docker pull --platform linux/amd64 "$postgres_fixture"
[[ "$(docker image inspect --format '{{{{.Os}}}}/{{{{.Architecture}}}}' "$postgres_fixture")" == linux/amd64 ]]
postgres_auth_fixture='{tools['postgres_auth_fixture']}'
docker pull --platform linux/amd64 "$postgres_auth_fixture"
[[ "$(docker image inspect --format '{{{{.Os}}}}/{{{{.Architecture}}}}' "$postgres_auth_fixture")" == linux/amd64 ]]
auth_fixture='{tools['auth_fixture']}'
docker pull --platform linux/amd64 "$auth_fixture"
[[ "$(docker image inspect --format '{{{{.Os}}}}/{{{{.Architecture}}}}' "$auth_fixture")" == linux/amd64 ]]
printf 'GOROOT=%s\\nGOCACHE=%s\\nGOTOOLCHAIN=local\\nXYNES_GO_BIN=%s\\nXYNES_IDLE_FIXTURE_IMAGE=%s\\n' "$apex_dir/go" "$apex_dir/cache" "$apex_dir/go/bin/go" "$fixture" >> "${{GITHUB_ENV:?}}"
'''
        step = run(prepare)
        step['name'] = 'Prepare verified Apex Go and public Linux fixture'
        step['if'] = "matrix.gate == 'test'"
        steps.append(step)
    commands = ['case "$GATE_NAME" in']
    for gate, command in profile['gates'].items():
        commands.append('  ' + gate + ')')
        commands.extend('    ' + line for line in command.splitlines())
        commands.append('    ;;')
    commands.extend(['  *) exit 2 ;;', 'esac'])
    steps.append(run('\n'.join(commands), name if kind == 'app' else None))
    job = {'name': '${{ matrix.gate }}', 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 45, 'strategy': {'fail-fast': False, 'matrix': {'gate': list(profile['gates'])}}, 'env': job_env, 'steps': steps}
    branches = ['main'] if kind == 'docs' else ['main', 'develop', 'release/*']
    return {'name': 'Required quality gates', 'on': {'pull_request': {'branches': branches}, 'push': {'branches': branches}, 'workflow_dispatch': {}}, 'permissions': {'contents': 'read'}, 'concurrency': {'group': 'quality-${{ github.workflow }}-${{ github.ref }}', 'cancel-in-progress': True}, 'jobs': {'gates': job}}


def release_workflow(profile):
    """Build on read-only runners; attest only a same-run digest after approval."""
    kind = profile['kind']
    name = profile['repository'].split('/')[-1]
    directory = name if kind == 'app' else None
    preflight = {
        'name': 'Protected release preflight', 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10,
        'permissions': {'contents': 'read'},
        'steps': [checkout(), {'uses': actions['oven-sh/setup-bun'], 'with': {'bun-version': '1.4.2'}},
                  {'name': 'Refuse dispatch outside protected main before using release credentials',
                   'run': 'bun .github/ci/release-gates.ts --source'}],
    }
    guard = "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && github.ref_protected"
    policy = {'name': 'Verify protected release checks', 'needs': 'preflight', 'if': guard,
              'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10, 'environment': 'release',
              'permissions': {'contents': 'read', 'checks': 'read'},
              'steps': [checkout(), {'uses': actions['oven-sh/setup-bun'], 'with': {'bun-version': '1.4.2'}},
                        {'name': 'Verify actual protected policy and latest exact-commit gates', 'run': 'bun .github/ci/release-gates.ts',
                         'env': {'GH_TOKEN': '${{ github.token }}', 'GH_POLICY_READ_TOKEN': '${{ secrets.RELEASE_POLICY_READ_TOKEN }}'}}]}
    if kind in ['infra', 'docs']:
        steps = [checkout()]
    else:
        steps = workflow(profile)['jobs']['gates']['steps'][:-1]
        # Release builders never inherit test fixture config or matrix conditions.
        steps = [step for step in steps if 'printf' not in step.get('run', '')]
    public_vars = ['NEXT_PUBLIC_AUTH_APP_URL', 'NEXT_PUBLIC_APP_URL', 'NEXT_PUBLIC_API_URL', 'NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_ANON_KEY', 'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS']
    # Install dev/build dependencies before setting NODE_ENV for the build command.
    env = {}
    if kind == 'app':
        env.update({key: '${{ vars.' + key + ' }}' for key in public_vars})
        steps.append({'uses': actions['oven-sh/setup-bun'], 'with': {'bun-version': '1.4.2'}})
        steps.append(run('bun .github/ci/release-gates.ts --public-build', directory))
    command = profile['gates'].get('build')
    if command and kind not in ['infra']:
        steps.append(run(command, directory))
    if kind in ['infra', 'fe-infra', 'docs']:
        artifact_kind = 'tracked-documentation-source' if kind == 'docs' else 'tracked-operational-source'
        paths = ['source.tar']
    elif kind == 'lumia':
        artifact_kind = 'compiled-design-system-packages'
        paths = ['packages', 'package.json', 'pnpm-lock.yaml']
    elif kind == 'app':
        artifact_kind = 'compiled-next-production-output'
        paths = ['.next', 'public', 'package.json', 'pnpm-lock.yaml']
    else:
        artifact_kind = 'compiled-bun-bundle' if kind.startswith('bun') else 'compiled-library'
        repo = args.workspace_root / profile['repository']
        lock = 'bun.lock' if (repo / 'bun.lock').is_file() else 'bun.lockb'
        paths = ['dist', 'package.json', lock if kind.startswith('bun') else 'pnpm-lock.yaml']
    versions = {'bun': '1.4.2'} if kind.startswith('bun') else {'node': '22.22.0', 'pnpm': '10.23.0' if kind == 'lumia' else '10.33.0'}
    if kind == 'app': versions['bun'] = '1.4.2'
    if kind == 'infra': versions = {}
    if kind == 'fe-infra': versions = {'node': '22.22.0'}
    if kind == 'docs': versions = {}
    record = {'artifact_kind': artifact_kind, 'linked_source_commits': peers if kind == 'app' else {}, 'runtime_versions': versions}
    # Never archive installed dependencies, build caches or ignored env files.
    # A source archive in infra is explicitly distinguished from a runtime build.
    package = ('set -euo pipefail\n'
               'artifact_dir="${RUNNER_TEMP:?}/release-candidate"\nmkdir -p "$artifact_dir"\n'
               'python3 - <<\'PY\'\n'
               'import json,os,pathlib\n'
               'record=json.loads(' + repr(json.dumps(record, sort_keys=True)) + ')\n'
               'record.update(repository=os.environ["GITHUB_REPOSITORY"], source_commit=os.environ["GITHUB_SHA"], workflow_run_id=os.environ["GITHUB_RUN_ID"], workflow_run_attempt=os.environ["GITHUB_RUN_ATTEMPT"])\n'
               'pathlib.Path(os.environ["RUNNER_TEMP"],"release-candidate","build-record.json").write_text(json.dumps(record,sort_keys=True,indent=2)+"\\n")\nPY\n')
    if kind == 'infra':
        package += 'python3 scripts/release/public_bundle.py --commit "$GITHUB_SHA" --output "$artifact_dir/source.tar"\n'
        source_root = '$artifact_dir'
    elif kind in ['fe-infra', 'docs']:
        package += 'git archive --format=tar --output="$artifact_dir/source.tar" HEAD' + (" -- '*.md' '*.mdx' LICENSE" if kind == 'docs' else '') + '\n'
        source_root = '$artifact_dir'
    else:
        source_root = '$PWD'
    package += ('tar --exclude=node_modules --exclude=.next/cache --exclude=coverage --exclude="*.tsbuildinfo" --exclude=".env" --exclude=".env.*" --exclude="*/.env" --exclude="*/.env.*" '
                '-czf "$artifact_dir/candidate.tar.gz" -C "' + source_root + '" ' + ' '.join(paths) + ' -C "$artifact_dir" build-record.json\n'
                'printf \'digest=sha256:%s\\n\' "$(sha256sum "$artifact_dir/candidate.tar.gz" | cut -d\' \' -f1)" >> "${GITHUB_OUTPUT:?}"')
    package_step = run(package, directory)
    package_step.update(id='package', name='Package compiled outputs and source/run record')
    steps.extend([package_step, {'uses': actions['actions/upload-artifact'], 'with': {'name': 'release-candidate-${{ github.sha }}', 'path': '${{ runner.temp }}/release-candidate/candidate.tar.gz', 'if-no-files-found': 'error', 'retention-days': 14}}])
    builder = {'name': 'Build release candidate', 'needs': ['preflight', 'policy'], 'if': guard, 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 45,
               'environment': 'release', 'permissions': {'contents': 'read'}, 'defaults': {'run': {'shell': 'bash'}},
               'outputs': {'digest': '${{ steps.package.outputs.digest }}'}, 'steps': steps}
    if env: builder['env'] = env
    attest = {'name': 'Attest protected release candidate', 'needs': ['preflight', 'policy', 'build'],
              'if': guard,
              'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10, 'environment': 'release',
              'permissions': {'contents': 'read', 'id-token': 'write', 'attestations': 'write'},
              'steps': [{'uses': actions['actions/attest'], 'with': {'subject-name': 'candidate.tar.gz', 'subject-digest': '${{ needs.build.outputs.digest }}', 'push-to-registry': False, 'create-storage-record': False}}]}
    return {'name': 'Protected release provenance', 'on': {'workflow_dispatch': {}}, 'permissions': {'contents': 'read'},
            'concurrency': {'group': 'release-${{ github.repository }}', 'cancel-in-progress': False},
            'jobs': {'preflight': preflight, 'policy': policy, 'build': builder, 'attest': attest}}


errors = []
for profile in profiles:
    repo = args.checkout if args.checkout else args.workspace_root / profile['repository']
    if not (repo / '.git').exists(): errors.append(f'{profile["repository"]}: checkout missing'); continue
    for filename, renderer in [('quality-gates.yml', workflow), ('release-provenance.yml', release_workflow)]:
        if args.workflow and filename != args.workflow:
            continue
        rendered = renderer(profile)
        target = repo / '.github/workflows' / filename
        expected = '# Generated from xynes-infra scripts/setup/ci-quality-profiles.json.\n' + '\n'.join(yaml(rendered)) + '\n'
        if args.check:
            if not target.is_file() or target.read_text() != expected: errors.append(f'{profile["repository"]}: {filename} export differs')
        else:
            if target.is_symlink() or target.parent.is_symlink() or target.parent.parent.is_symlink(): raise RuntimeError('Refusing symlink output')
            if target.exists() and not target.read_text().startswith('# Generated from xynes-infra '): raise RuntimeError('Refusing unrelated workflow overwrite')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected)
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(profiles)} quality workflow exports ' + ('match' if args.check else 'written'))
