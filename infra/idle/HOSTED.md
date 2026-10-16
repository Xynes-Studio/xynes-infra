# Hosted native idle integration

The hosted adapter uses one root broker and two unprivileged proxies per stage.
Only Auth and CMS are sleepable. The deployment controller owns generation
changes; request data cannot select a container, image, stage, socket or command.

The nine unit files in `infra/release/systemd/xynes-idle-*.service` are exact
mirrors of `scripts/release/host_idle.py:unit`. Install them as root-owned `0644`
files. The executable is `/usr/local/lib/xynes-idle/xynes-idle`, root-owned
`0555`, built from the independently approved infra commit with Go 1.27.2. The
administrator records the reviewed source and executable SHA-256 values in the
private host policy. A local binary build does not authorize deployment.

Before enabling a stage, provision its dedicated nonlogin user and group:

| Stage | UID/GID | Auth listener | CMS listener | Default idle seconds |
| --- | --- | --- | --- | --- |
| QA | 12002 | 172.21.0.1:18081 | 172.21.0.1:18082 | 900 |
| Staging | 12003 | 172.21.0.1:18181 | 172.21.0.1:18182 | 1800 |
| Production | 12004 | 172.21.0.1:18281 | 172.21.0.1:18282 | 3600 |

These users have no Docker or shared secret group membership. The listeners bind
the inspected `caddy_net` bridge gateway, and accept only the inspected Caddy
IPv4 address as a `/32`. Auth/CMS upstreams use their freshly inspected stage
network IPv4 addresses and fixed ports 3100/3000. DNS and other Caddy routes are
managed separately by the administrator.

Each `/etc/xynes/idle/<stage>/host.json` is root-owned `0600` with exactly:

```json
{
  "schema_version": 1,
  "stage": "qa",
  "binary_sha256": "<reviewed executable SHA-256>",
  "source_sha256": "<reviewed adapter source SHA-256>",
  "compiler_version": "go1.27.2",
  "idle_seconds": 900,
  "network": {"name":"xynes_qa_net","id":"<inspected ID>","gateway":"172.19.0.1","subnet":"172.19.0.0/16"},
  "ingress": {"name":"caddy_net","id":"<inspected ID>","gateway":"172.21.0.1","subnet":"172.21.0.0/16"},
  "caddy": {"id":"<inspected container ID>","image_id":"sha256:<inspected image ID>","ip":"172.21.0.2"}
}
```

Provision `/etc/xynes/idle/<stage>` as root:stage-group `0750`, its
`control.token` as root:stage-group `0440`, and `/run/xynes/idle/<stage>` as
root:stage-group `0750`. The `auth` and `cms` leaf directories beneath both
`/run/xynes/idle/<stage>` and `/var/lib/xynes/idle/<stage>` belong to the stage
UID/GID with mode `0700`; all their ancestors remain root-owned and unwritable
by other users. Preserve warm state across service restarts. Do not remove a
warm lock or owner record to bypass failed reconciliation.

Install `infra/release/systemd/xynes-idle-tmpfiles.conf` as root-owned `0644`
`/etc/tmpfiles.d/xynes-idle.conf`. It recreates only the eleven ephemeral `/run`
directories at boot; it does not touch persistent warm files. Units run after
tmpfiles setup and require their stage generation file, so initial installation
can enable the units without starting unconfigured proxies.

The initial setup plan permits exactly two shared parent mode changes:
`/var/lib/xynes` and `/etc/xynes/idle`, root-owned `0700` to `0711`. This allows
traversal while retaining directory-listing restrictions. Existing stage config
directories change from root:root `0700` to root:stage-group `0750`. Before any
change, record the exact root inode, owner, old mode and existing child content
hashes; preserve those children and the original disabled policies in private
recovery copies. Refuse foreign identities, links, unexpected modes or unknown
existing files. Apply no recursive ownership or mode changes, and verify native
cross-stage/private-file read denials after setup.

Set the existing root-owned `deployment-mode.json` explicitly to enabled only
after these prerequisites and the binary provenance have been checked. Its
closed shape is `{"schema_version":1,"stage":"qa","mode":"enabled","groups":["auth","cms"]}`.
Disabled mode continues to require no active idle units or sockets.

On an initial enabled deployment, the warm context runs before applications
exist. It requires absent runtime generation/configuration, inactive proxy units
and absent warm files/locks, then creates the documented durable deployment
owner in each warm store. It creates no accepted release or migration state.

For an upgrade, the controller acquires both durable warm holds and drains both
proxies before Compose replaces applications. After all fourteen services are
healthy, it checks the exact app container IDs, immutable images, labels and
network identities, stops the three owned units, writes a new drained runtime
generation, and starts the broker before both proxies. The restored warm owner
survives this restart. Only a second native binding check permits resume, before
the controller's product smoke tests. Rollback follows this same path with the
previous accepted images and newly inspected process IDs/IPs.

A failure after resume attempts to drain both proxies again; if private control
is unavailable, it stops the exact owned proxy units. Warm holds and the private
deployment owner remain available for reconciliation. The controller never
claims acceptance from these filesystem or process checks alone.

The host proxy uses `dynamic_target: true`; each broker target contains its exact
stage `network_id`. After taking the request work lease, the proxy asks the
private broker for the current endpoint. The broker rechecks the approved CID,
image, labels and network, and returns only its current private IPv4 address on
the fixed application port. A cached IP is never a host-mode fallback. This
preserves correct routing when an application wakes at another address and
prevents a different container at the old address from receiving the request.

Local evidence includes `scripts/test/idle-adapter-host.test.sh --runtime`: one
fresh internal bridge and one disposable Linux container, without public ports
or the real Docker socket. It runs actual root-broker/nonroot-proxy processes,
changes the approved upstream address while the old address serves a foreign
response, and verifies cold-write preservation, peer denial, drained JSON 503,
active-stream completion, exact-generation rebind, and durable warm expiry
through abrupt restart. Its extra network administration capability creates
only a private address alias inside that disposable fixture. The existing Unix
fixture supplies the separate 80% statement-coverage gate. These local tests do
not establish hosted HTTPS/OAuth, reboot, resource or soak acceptance.
