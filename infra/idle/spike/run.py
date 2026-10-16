"""Run disposable upstream compatibility checks; expected gaps are reported separately.

All Docker mutations target a fresh UUID Compose project or its nested fixture daemon.
No real host socket/data is mounted. This is evidence, not hosted acceptance.
"""
import concurrent.futures
import argparse
import http.client
import io
import tarfile
import json
import pathlib
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT = 'apex-idle-' + uuid.uuid4().hex[:10]
COMPOSE = ['docker', 'compose', '-p', PROJECT, '-f', str(ROOT / 'compose.yml')]
checks, gaps, samples = [], [], []
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cycles', type=int, default=20)
arguments = parser.parse_args()
if not 1 <= arguments.cycles <= 20:
    parser.error('cycles must be between 1 and 20')


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def compose(*args):
    return command(COMPOSE + list(args))


def nested(*args):
    return compose('exec', '-T', 'daemon', 'docker', *args)


def wait(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except (subprocess.CalledProcessError, OSError):
            pass
        time.sleep(.2)
    raise AssertionError('bounded wait expired')


def request(base, path, method='GET', body=None, headers=None, timeout=25):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print('PASS', name, flush=True)


def running():
    return nested('inspect', '-f', '{{.State.Running}}', 'apex-fixture') == 'true'


def stopped():
    wait(lambda: not running())


def journal():
    return json.loads(nested('exec', 'apex-fixture', 'cat', '/tmp/fixture-journal.json'))


def ready_api(name='apex-fixture', duration='10s', timeout='5s'):
    return request(api, '/api/strategies/blocking?names=' + name + '&session_duration=' + duration + '&timeout=' + timeout)


try:
    compose('up', '-d', 'daemon', 'caller')
    wait(lambda: nested('info', '--format', '{{.ServerVersion}}') == '28.1.1', 60)
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for filename in ['fixture.py', 'fixture.Dockerfile']:
            archive.add(ROOT / filename, arcname=filename)
    daemon = compose('ps', '-q', 'daemon')
    subprocess.run(['docker', 'exec', '-i', daemon, 'sh', '-c',
                    'mkdir -p /tmp/fixture && tar -xf - -C /tmp/fixture && docker build -t apex-fixture:local -f /tmp/fixture/fixture.Dockerfile /tmp/fixture'],
                    input=context.getvalue(), check=True)
    for name, port, delay, enabled in [('apex-fixture', '8080', '.25', 'true'),
                                        ('apex-unhealthy', '8081', '99999', 'true'),
                                        ('apex-data', '8082', '.25', 'false'),
                                        ('apex-prod-mock', '8083', '.25', 'true')]:
        nested('create', '--name', name, '--label', 'sablier.enable=' + enabled,
               '--label', 'sablier.group=' + name, '--label', 'com.xynes.apex.spike=disposable',
               '-p', port + ':8080', '-e', 'READY_DELAY=' + delay, 'apex-fixture:local')
    compose('up', '-d', '--build', 'sablier', 'caddy')
    ingress = 'http://' + compose('port', 'caddy', '8080')
    api = 'http://' + compose('port', 'sablier', '10000')
    wait(lambda: request(ingress, '/health')[0] == 200)
    check('health probe does not start fixture', not running())
    for index in range(arguments.cycles):
        stopped()
        before = time.monotonic()
        verb = 'POST' if index % 2 else 'GET'
        payload = b'{"synthetic":true}' if verb == 'POST' else None
        status, body, _ = request(ingress, '/echo?q=retained', verb, payload,
                                  {'Authorization': 'Bearer fixture-only', 'X-Fixture-ID': str(index)})
        samples.append(time.monotonic() - before)
        response = json.loads(body)
        if 'method' not in response:
            print('DIAGNOSTIC synthetic wake response', response, flush=True)
            print('DIAGNOSTIC nested health', nested('inspect', '-f', '{{json .State}}', 'apex-fixture'), flush=True)
            print('DIAGNOSTIC synthetic fixture logs', nested('logs', '--tail', '10', 'apex-fixture'), flush=True)
        check('cycle %02d cold method/body/query/header retained' % index,
              status == 200 and response.get('method') == verb and response['path'] == '/echo?q=retained'
              and response['authorization'] == 'Bearer fixture-only'
              and response['body'] == (payload.decode() if payload else ''))
        check('cycle %02d delivered exactly once' % index,
              sum(row['id'] == str(index) for row in journal()) == 1)
        if index == 0:
            live = json.loads(nested('stats', '--no-stream', '--format', '{{json .}}', 'apex-fixture'))
            print('METRIC awake_memory', live['MemUsage'], flush=True)
        idle_deadline = time.monotonic() + 30
        while running():
            check_deadline = time.monotonic() < idle_deadline
            if not check_deadline:
                raise AssertionError('probe-only inactivity did not stop within 30 seconds')
            request(ingress, '/health')  # ingress-only probe must not refresh workload session
            time.sleep(.3)
    stopped()
    check('container stopped, no remaining PID', nested('inspect', '-f', '{{.State.Pid}}', 'apex-fixture') == '0')
    print('METRIC stopped_memory', nested('stats', '--no-stream', '--format', '{{.MemUsage}}', 'apex-fixture'), flush=True)
    # Burst uses private ingress route, never a bypass directly to the sleeper.
    stamp = int(time.time())
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        burst = list(pool.map(lambda i: request(ingress, '/echo', 'POST', b'burst', {'X-Fixture-ID': 'burst-' + str(i)}), range(12)))
    check('concurrent wake completes 12 requests', all(item[0] == 200 for item in burst))
    check('concurrent wake delivered writes once', all(sum(row['id'] == 'burst-' + str(i) for row in journal()) == 1 for i in range(12)))
    events = nested('events', '--since', str(stamp), '--until', str(int(time.time()) + 1), '--filter', 'container=apex-fixture', '--filter', 'event=start', '--format', '{{.Action}}')
    check('concurrent burst has one Docker start event', events.splitlines() == ['start'])
    stopped()
    private = compose('exec', '-T', 'caller', 'python', '-c',
        "import urllib.request,json; print(json.load(urllib.request.urlopen('http://caddy:8080/echo?internal=true')))")
    check('private caller wakes through same ingress path', "internal=true" in private)
    stopped()
    status, body, _ = request(ingress, '/callback?code=synthetic&state=retained')
    check('cold OAuth fixture callback retains query', status == 200 and json.loads(body)['path'] == '/callback?code=synthetic&state=retained')
    status, body, _ = ready_api('apex-data')
    check('unlabelled data refused', status == 404 and nested('inspect', '-f', '{{.State.Running}}', 'apex-data') == 'false')
    status, _, _ = ready_api('does-not-exist')
    check('unknown instance cannot start', status >= 400)
    status, _, _ = ready_api('apex-prod-mock')
    check('observed missing exact-env boundary', status == 200)
    gaps.append('Any sablier.enable=true named instance can be controlled; labels are not an exact environment authorization boundary.')
    upstream_status, _, _ = ready_api('apex-unhealthy', timeout='3s')
    edge_status, edge_body, headers = request(ingress, '/broken')
    check('unhealthy wake bounded and not forwarded', upstream_status == 504 and b'timeout' in edge_body.lower())
    check('observed plugin status loss', edge_status == 200 and 'json' in headers.get('Content-Type', ''))
    gaps.append('Caddy plugin v1.0.2 turns Sablier 504 into HTTP 200; Retry-After is not propagated.')
    stopped()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(request, ingress, '/long', 'POST', b'long', {'X-Fixture-ID': 'long'})
        wait(running)
        wait(lambda: not running(), 25)
        try:
            result = future.result(timeout=8)
            interrupted = result[0] != 200
        except (OSError, http.client.HTTPException):
            interrupted = True
    check('observed long request interrupted at expiry', interrupted)
    gaps.append('36s POST outlives idle timeout plus Docker stop grace; no in-flight drain. No safe group adoption without adapter.')
    stopped()
    try:
        status, body, _ = request(ingress, '/stream')
        incomplete = len(body) != 25
    except (OSError, http.client.HTTPException):
        incomplete = True
    check('observed stream interruption', incomplete)
    gaps.append('Streams outlive arrival-based TTL and are interrupted. SSE/WebSocket/RSC streaming must hold work leases.')
    request(ingress, '/echo')
    compose('stop', 'sablier')
    wait(lambda: running(), 2)
    time.sleep(12)
    check('controller stopped causes no new expiration stop', running())
    status, _, _ = request(ingress, '/echo')
    check('observed warm ingress fails with controller down', status >= 500)
    gaps.append('Warm traffic fails when controller is unavailable; stock plugin cannot satisfy fail-open warm routing.')
    compose('start', 'sablier')
    # Random host ports can change when a container is restarted. Private Caddy
    # service DNS remains stable; the external fixture API must be rediscovered.
    api = 'http://' + compose('port', 'sablier', '10000')
    time.sleep(12)
    check('controller restart does not stop active container with auto-stop disabled', running())
    gaps.append('In-memory session state is lost on controller restart; auto-stop=false avoids destructive startup but leaves existing containers awake until re-leased.')
    wait(lambda: ready_api()[0] == 200)
    # Warm duration renewal is not a deploy lock or owned lease.
    ready_api(duration='25s')
    time.sleep(12)
    check('manual warm session keeps container running', running())
    gaps.append('Warm API session is unauthenticated/unowned; no tested deploy lock, completion-based idle policy or lease adapter yet.')
    stopped()
    nested('start', 'apex-fixture')
    time.sleep(12)
    check('external manual start remains awake without auto-warm policy', running())
    gaps.append('Manual external start is not expired with auto-warm disabled; reconciliation must be configured and proven.')
    report = {'checks': len(checks), 'cycles': arguments.cycles,
              'cold_seconds_p95': sorted(samples)[max(0, (95 * len(samples) + 99) // 100 - 1)], 'cold_seconds_max': max(samples),
              'cold_seconds_median': statistics.median(samples), 'gaps': gaps,
              'hosted_acceptance': False, 'stock_adoption': False}
    print('RESULT ' + json.dumps(report, sort_keys=True), flush=True)
finally:
    subprocess.run(COMPOSE + ['logs', '--tail', '8', 'sablier'], check=False)
    # Only the fresh project is removed; never prune global containers/images/volumes.
    subprocess.run(COMPOSE + ['down', '--volumes', '--remove-orphans'], check=False)
