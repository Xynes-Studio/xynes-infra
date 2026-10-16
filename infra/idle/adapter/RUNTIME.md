# Idle runtime boundary

Status: runnable local roles with verified Linux socket/process boundaries; hosted integration and complete sleep acceptance remain open. Do not enable the runtime in hosted Compose yet.

Build the standard-library-only `cmd/xynes-idle` entry point with an approved patched Go compiler. This checkpoint used the official Go 1.27.2 macOS ARM64 archive, SHA-256 `76812b213b1b2302c978d28fa52fa92d541704b9e7d9d5db8002c50e4018c4c5`, downloaded to a private temporary directory. The [official download manifest](https://go.dev/dl/?mode=json) supplies the checksum. No global compiler installation was changed. Release builds must repeat provenance/security checks against their approved exact source and toolchain.

```sh
xynes-idle -role broker -config /etc/xynes/idle/qa/broker.json
xynes-idle -role proxy -config /etc/xynes/idle/qa/auth-proxy.json
```

Both roles currently require Linux. The broker requires effective UID 0. Proxy UID/GID assignments are proposed dedicated service identities: QA 12002, staging 12003, production 12004. Check the full host user/group inventory for collisions before provisioning; do not reuse deployment SSH identities. The named deploy identities in the read-only host audit have different numeric IDs, which does not prove these proposed IDs are globally free.

All configuration ancestors must be root-owned, real directories, and unwritable by group/other. Intermediate `/etc/xynes/idle` and `/run/xynes/idle` directories must allow traversal by the appropriate proxy; each stage leaf is `0750 root:<stage proxy group>`. Config files are root-owned, single-link regular files, at most 64 KiB, with no group write/execute or other permissions. Use `0600 root:root` for broker configuration and `0640 root:<stage proxy group>` for proxy configuration. Symlink/foreign-owner/oversized files and unknown/trailing JSON fields are refused. Configuration is never logged.

The token file is exactly `/etc/xynes/idle/<stage>/control.token`, `0440 root:<stage proxy group>`, containing 64 lower-case hexadecimal characters and optionally one terminal newline. Generate it securely during authorized host provisioning; never place its value in source or process arguments. The control socket is exactly `/run/xynes/idle/<stage>/control.sock`, `0660 root:<stage proxy group>`. Its parent must be `0750 root:<stage proxy group>`. Each socket has a protected `0600` same-owner `.lock` file recording its path, device/inode, final change timestamp, and random past modification timestamp (version 2). The modification timestamp identifies this socket generation even if Linux reuses an inode within one change-time clock tick. Publication verifies its 50-bit marker at portable microsecond precision; filesystems that lose the marker are refused. This is an ownership marker, not authentication against the UID that already controls the socket directory and lock record. Startup holds an exclusive nonblocking lock. It may unlink only an exact recorded stale socket with the expected UID/GID/mode and a refused connection; unknown files, symlinks, sockets, invalid records and reachable listeners are preserved and refused. Closing admission retains the lock until handlers drain. Final cleanup removes only the unchanged owned socket generation. Version 1 records are refused and preserved; an upgrade must drain and stop the old runtime normally before replacing its binary. Any leftover unstamped socket or old record requires explicit owner-reviewed recovery, never automatic deletion. A crash during initial record publication can leave an invalid record requiring explicit operator recovery; completed-record recovery is verified separately.

Broker JSON contains `environment`, `control_socket`, `docker_socket` (fixed `/var/run/docker.sock`), `token_file`, `proxy_uid`, `socket_gid`, and `targets`. Targets map only `<stage>-auth` or `<stage>-cms` to `container_id` (full 64-character ID), `image` (qualified registry SHA-256 digest), and `service` (`auth-app` or `cms-console-web`). Render these from an accepted release and inspected container identity before installing the root-owned config. A replaced container requires a newly validated broker configuration. Kernel `SO_PEERCRED` must match the stage proxy UID before stage-token checks; HTTP headers cannot supply peer identity. Each lifecycle action rechecks the approved container/image/ownership labels.

Proxy JSON contains `environment`, `control_socket`, `token_file`, `listen` (fixed `0.0.0.0:8080`), `idle_seconds`, `wake_seconds`, `max_in_flight`, `state_file`, and `policy`. Policy contains `group`, `target`, `hosts`, `max_body_bytes`, `max_queued_requests`, and `trusted_ingress`. Only the stage's Auth/CMS alias and port are permitted. Configure exact Caddy peer IPs as `/32` or `/128`; other peers are denied before work admission. Loopback may call passive health/metrics routes only. These probes describe ingress, not application readiness. No control route, Docker socket, container ID or Docker configuration exists in this role. Its eventual container must mount only its protected configuration/token and private control socket, with no host-published port.

Idle values accept 60–86400 seconds; intended defaults remain QA 900, staging 1800, prod 3600. Wake is bounded to 1–120 seconds. Bodies/admission/header reads are bounded; aggregate admitted body capacity must be at most64MiB; active response/stream leases remain held until forwarding returns. Unsupported HTTP upgrades/CONNECT receive JSON501 before waking/forwarding; upgrade-dependent workloads remain ineligible. SIGTERM disables new stops/wakes, drains ordinary HTTP requests for at most 90 seconds and leaves workloads running. Durable owned warm/deploy holds survive clean restart and the verified completed-record SIGKILL recovery path. Startup/external-start reconciliation, interrupted-publication/daemon recovery, WebSocket/cancellation and actual deployment-lock acceptance remain required. Do not treat this ordinary HTTP shutdown check as completed WebSocket or real application drain acceptance.

Repeat the OS boundary test from the infra root:

```sh
# Image must already be a verified linux/amd64 local ID or registry digest.
XYNES_IDLE_FIXTURE_IMAGE='<immutable fixture image>' \
  scripts/test/idle-adapter-unix.test.sh --runtime
```

The fixture uses one exact newly created container, no external network, no real Docker socket/data, a read-only test binary and isolated temporary coverage output. Narrow capabilities support only fixture ownership and non-root child creation/termination. A synthetic Unix Docker API exercises actual root/private and non-root/proxy roles. Temporary `/tmp`, config and sockets are in container tmpfs. It checks OS credentials, socket permissions, unchanged unknown paths, stage denial, original cold write delivery and shutdown; native Go coverage includes subprocess records via the [integration coverage tooling](https://go.dev/doc/build-cover). The script checks the actual container exit and at least 80% adapter statement coverage, then removes only its own container and temporary files.

Latest complete Linux fixture: exit 0, 82.1% adapter statements for the extended durable-lease suite. Historical pre-persistence macOS race suite: exit 0, 68.4% adapter / 44.4% CLI statements because privileged Linux paths and executable signal entry are not exercised there. `go vet`, shellcheck and diff checks pass. These reports remain distinct; CLI release coverage and full hosted/security acceptance are not certified by the adapter fixture. The earlier full Linux attempt failed only because its fixture omitted writable `/tmp`; the repeatable script fixes that fixture mount and passed the complete run.

The proxy now requires `state_file` exactly `/var/lib/xynes/idle/<stage>/<auth|cms>/warm.json`. Its leaf directory is0700 owned by the dedicated proxy UID; all ancestors remain protected root directories. State is a0600 single-link file with version/stage/group, owner and original expiry in Unix nanoseconds. File and directory fsync precede acknowledgement. A retained0600 `warm.lock` enforces one writer; missing prior state, corruption or failed writes disable stops and new warm grants while ordinary readiness/routing can continue. Unknown/symlink/foreign files are never overwritten.

Root deployment control uses a separate private Unix listener at `/run/xynes/idle/<stage>/<auth|cms>/leases.sock`: parent0700 and socket0600 owned by the proxy UID. It requires kernel peer UID0 plus the stage token. POST `/warm` accepts exactly `{group,owner,ttl_seconds}` (1–86400 seconds); POST `/release` accepts exactly `{group,owner}`. Ownership is retained across restart and release is durable. This listener is never part of public ingress. These paths need authorized host provisioning and narrowly scoped writable bind mounts; no host Docker socket belongs in the proxy.

Quiesce disables admission/stops without releasing the writer lock. The runtime releases that lock after both ordinary HTTP servers finish draining. Actual Linux process tests verify a flushed stream survives SIGTERM, a replacement receives the specific busy refusal while that stream remains active, then restarts and releases the exact persisted owner. Independent review found the earlier premature-unlock race; the red regression and live process test reproduce/prevent it, and re-review confirms the normal-drain fix. The extended Linux fixture also kills the owned non-root proxy, observes its retained stale socket, restarts it through recorded-inode reconciliation and releases the restored exact warm owner. Independent read-only review found no consequential issue in the socket changes. Force-close at90s, interrupted ownership publication, daemon recovery and real workload eligibility remain separate unfinished gates.

Forwarding now uses one fresh HTTP/1 connection for each request. This preserves application headers while preventing Go from replaying an already dispatched zero-body write carrying either idempotency header after a reused-connection disconnect. Both cases reproduced two deliveries before the fix; the regressions require one delivery and safe503. HTTPS ALPN is explicitly HTTP/1.1 while retaining the cloned TLS trust settings. Independent review found inherited h2 ALPN could otherwise negotiate an unavailable HTTP2 handler; an actual HTTP2-capable TLS server reproduced503/zero deliveries, and the corrected regression returns201/one HTTP1 delivery. Fresh connections introduce TCP/TLS overhead that requires hosted measurement; no performance acceptance follows from these local tests.

## Hosted native controller contract

The reviewed host coordinator provisions one root broker and two unprivileged
Auth/CMS proxies per stage. The static Linux AMD64 binary is controller tooling,
with its exact source tree, pinned Go compiler, and binary digest recorded by
provisioning. It is separate from the fifteen application/processor images.
The host installer must rebuild the binary from the reviewed committed source;
a planning build does not authorize installation or establish image attestation.

Host proxy configuration adds `host_network: true` and `start_drained`. In this
mode `listen` must use an inspected private IPv4 bridge gateway and the fixed
stage/group port: QA Auth/CMS 18081/18082, staging 18181/18182, production 18281/18282.
The upstream is the inspected private IPv4 application address, port 3100 for
Auth or 3000 for CMS. Root provisioning verifies the Docker network, exact
container/image/environment labels, Caddy peer, and both endpoint addresses;
the broker independently verifies exact container identity on every operation.
Only the exact Caddy peer `/32` is admitted. No public address or wildcard
listener is accepted. Backend services, databases, queues and processors remain
awake. Default idle intervals are 900, 1800 and 3600 seconds for QA/staging/prod.

The private root lease socket accepts `/drain` and `/resume` with exactly
`{group,owner}`, in addition to `/warm` and `/release`. Both require an existing
unexpired owned warm lease. Drain closes new application admission and waits
for full active responses/streams; a timeout leaves admission closed and the
warm hold retained. Release is refused while drained. New generation proxies
start drained, load the original durable warm expiry, and passively observe
the exact broker target. Root validates replacement identities before resume;
rollback uses the same rebind protocol. Resume is an idempotent no-op for an
already open generation with the same existing warm owner. Passive ingress
status includes `draining`; cold failures remain JSON 503 with `Retry-After`.

On a normal proxy restart, authenticated passive `/observe` checks the exact
approved container without starting it. A healthy running container receives a
new completion-based idle clock; a stopped container stays stopped until real
traffic. No probe or controller startup starts an unused application.

## Native host-mode fixture and current endpoint binding

`scripts/test/idle-adapter-host.test.sh --runtime` runs the native host-network
contract in one disposable container on its own internal Docker bridge, with
no published ports, real Docker socket, credentials or application data. The
fixture root adds a second private address inside that container's network
namespace; its additional `NET_ADMIN` capability is limited to this disposable
fixture. Runtime proxy children still use the dedicated non-root stage identity.
The test verifies private listener binding, actual trusted and foreign source
addresses, cold POST preservation, JSON drain responses, active-stream drain,
new exact container/image generation, and durable expiry through abrupt proxy
restart. It then changes the approved endpoint IP while the old address serves
a foreign response and proves that address receives no application request.
Both the owned container and internal network are removed by the script.
This is local native evidence; hosted HTTPS, real app/OAuth, systemd reboot and
all-stage resource/soak acceptance remain required.

Host-network proxy policy requires `dynamic_target: true`. Each broker target
also requires its reviewed stage `network_id`. After acquiring the work lease,
every application request uses authenticated private POST `/endpoint` with
exactly `{group}`. The root broker inspects the exact CID/image/stage labels and
network ID, then returns the current private IPv4 endpoint on fixed port 3100
or 3000. The proxy validates the closed endpoint response before its sole
upstream dispatch. It never falls back to the boot-time IP for host-mode work.
A failed or foreign endpoint produces JSON 503 without forwarding the request.
Passive startup observation also rechecks the running target's network binding;
health probes remain passive and do not resolve/wake application targets.
