# Root-owned deployment boundary

`scripts/release/host_boundary.py` is a coordinator-only planner and installer.
It is not reachable through the SSH request protocol and does not implement the
privileged deployment executor. No host acceptance follows from its local tests.

## Installation prerequisites

Before preparing a plan, provide the exact reviewed SHA-256 for every path in
`LIBRARY_FILES`, including `root_executor.py`, `migration_transport.py` and
`pg_container_bridge.py`. Missing files, unexpected hash keys and mismatched
bytes refuse. Templates, filled env files, private keys, source checkouts and
internal reports are not transferred.

The explicit allowlist also includes the concrete migration/seed adapters,
environment/provider parser code, `validate-hosted.py`,
`scripts/lib/backup_recovery.py` and `infra/release/backup-toolchain.json`.
Additional runtime dependencies require an explicit reviewed allowlist update;
the installer never copies an entire source directory as a fallback.

Provide three distinct Ed25519 deployment public keys and checked native account
records for QA, staging and production: name, UID, GID, home, shell and groups.
The installer does not create accounts. Missing staging requires coordinator
provisioning after checking UID/GID collisions and existing host assumptions.
Every account must have its own primary group. Root/admin memberships refuse.
The current SSH allowlist must already admit each target account; its global
policy is preserved rather than guessed or replaced.

The root executor must be complete before installation is authorized. Its
coordinator readiness capability must bind the plan hash, bundle hash and exact
executor hash. The boundary module neither generates this capability nor treats
caller-supplied approval booleans as proof. The executor must validate the sudo
caller before entering its fixed supervisor, then enforce whole-process-tree
containment with `RuntimeMaxSec=2700`, `TimeoutStopSec=15` and
`KillMode=control-group`. SSH dispatch has its own 3600-second deadline; the
root supervisor independently limits the outer invocation to 2775 seconds.

## Prepared assets and checks

`prepare(source=..., reviewed_hashes=..., public_keys=..., accounts=...,
sshd_config=...)` returns an immutable `Plan`. Its `bundle_sha256` names the
versioned library under `/usr/local/lib/xynes-deploy/releases/<hash>`. Its
`plan_sha256` binds every destination, file hash, mode, account record and original
SSH configuration hash. `plan.files` contains concrete bytes and destinations;
`plan.source_hashes` contains the complete transfer allowlist. Inspect this
concrete plan before using its readiness capability.

Library files are root-owned `0444`, with sealed `0555` package directories.
The four root-owned `0555` bootstraps use `/usr/bin/python3 -ISB`, disable
environment/site/cwd import discovery, and verify the entire fixed library's
ownership, links, inventory and hashes before importing it:

- `/usr/local/libexec/xynes-deploy-ssh` invokes `ssh_entry.main()`.
- `/usr/local/libexec/xynes-deploy-root` invokes `root_executor.main()`.
- `/usr/local/libexec/xynes-deploy-shell` accepts only sshd's exact fixed
  `-c /usr/local/libexec/xynes-deploy-ssh` invocation and `apex-deploy` original
  command. It avoids mutable home-directory shell startup scripts.
- `/usr/local/libexec/xynes-backup` invokes the root-only `host_backup.main()`.
  It accepts only `run`, `alert` or `alert-test`, followed by literal `qa`,
  `staging` or `prod`. No stage sudoers or SSH authority includes this entry.

The managed SSH rule targets only `xynes_qa_deploy`, `xynes_staging_deploy` and
`xynes_prod_deploy`, fixes `ForceCommand`, and disables TTYs, forwarding, passwords
and user RC scripts. Root-owned public keys live in
`/etc/ssh/xynes-deploy-keys/<account>` with mode `0644` beneath `0755` directories.
Those keys must remain readable because sshd reads them under the account UID;
they cannot be changed through mutable deployment-user homes.

Each root-owned `0440` sudoers file grants only this literal command, with the
matching literal stage:

```text
xynes_qa_deploy ALL=(root) NOPASSWD: /usr/local/libexec/xynes-deploy-root qa
```

`install(plan, executor_token=..., verify_executor=...)` validates native account
and SSH assumptions, installs the allowlisted files, checks every sudoers file
with `/usr/sbin/visudo -c -f`, checks full SSH configuration with
`/usr/sbin/sshd -t -f`, and inspects per-account effective configuration with
`sshd -T -C`. The `xynes` recovery profile must remain identical. It sets only
the three deployment login shells and reloads `ssh` after validation. Failed
convergence restores owned configuration changes and prior login shells; the
coordinator must inspect actual host state if rollback or reload is uncertain.
Unknown substitutions are preserved, never overwritten or recursively removed.

The original main SSH configuration is preserved byte-for-byte, with only a
managed final `Match all`/`Include` suffix. This avoids inserting Match blocks
into an early include that could invalidate later global settings.

## Privilege removal and acceptance

Installation leaves legacy group memberships intact. After actual entry-refusal,
authenticated native producer-refusal and recovery-login smoke checks, sealed
managed deployment/env paths and absence of old deployment sessions, the coordinator can call
`revoke(plan, token=..., verify=...)`. Every stage must qualify before the first
membership change. Only supplemental `docker` and `xynes` membership is removed,
using fixed `/usr/bin/gpasswd --delete <account> <group>` commands. Primary-group
ownership and native remaining groups are checked independently.

For this precredential safety gate, `native_admission_smoke` means a verified
negative admission test. A private typed witness binds stage, installed plan and
admission/reader hashes, repository ID, actual completed security-run/job/check-run
IDs, canonical native response hashes and observation time. The exact installed
run/job guards must reject the unmodified completed nondeployment producer for
all three stages. The coordinator supplies fresh genuine native/SSH receipts and
binds their canonical hashes separately from the receipt payload before issuing
the opaque revocation capability. Transport/auth/API errors and fixture callbacks
cannot qualify. This is not positive release/deployment admission and does not
change protected-main, source/image verification, independent release/production
approval or accepted staging promotion. Genuine deployments still revalidate
their positive admission at every existing write boundary.

`xynes` recovery access, hosted Dev, existing home keys, application data, env
contents, databases and volumes are not changed. There is no recursive chown.
Sealing existing deployment-owned `/srv/xynes` paths is a separate reviewed
coordinator action; removing groups alone does not seal owner-writable files.
Already running processes retain old supplementary groups, so existing sessions
must be resolved before revocation rather than inferred away.

`audit(plan)` independently verifies installed bytes, root ownership, modes,
single links, ancestors and library inventory. Installation and revocation both
return `boundary_ready:false`: fresh SSH tests must still prove cross-stage,
generic-command, Docker/socket, secret-file and sudo denial. Retain the prior
immutable library and protected convergence ledger for recovery. Fixture-root
execution exercises file transactions without granting live-host readiness.

## Private failure diagnostics

The SSH/workflow output stays the fixed `Deployment dispatch refused`. On any
executor, runtime or worker failure the root worker first tries to write one
record to `/var/lib/xynes/deploy/diagnostics/<stage>/`: `<token>.json` for a
begun attempt, `run-<run>-<attempt>-<job>.json` before `begin`, or
`worker-<job>.json` for supervisor-level failures. The directories are root
`0700` and are created only beneath a root-owned, non-group/other-writable
parent; files are `0600`, exclusively created, fsynced and at most 64 KiB.
Symlinks, foreign owners or wrong modes refuse the write. A record holds only
fixed labels: stage, token, controller/deployment ids, phase, last completed
step, UTC time, exception class names along the cause/context chain, release
module frames as (file name, line, function), and for native commands the
fixed subcommand label, outcome class, exit code and stream byte counts. It
never contains exception messages or arguments, environment, argv values,
command output, SQL, paths or credentials. Writing is best effort and never
changes the refusal, ledger reconciliation or rollback behavior.

## Scheduled encrypted backups and failure alerts

Install the reviewed `xynes-backup@.service`, `xynes-backup@.timer` and
`xynes-backup-alert@.service` templates as root-owned systemd units after the
sealed backup bootstrap and pinned tools pass native checks. Enable one timer
per hosted stage. The schedule is 00:00, 06:00, 12:00 and 18:00 UTC, with up to ten
minutes of random delay and missed-run recovery. Each backup service has a
30-minute process-group limit; its separate failure alert has a 30-second limit.

A scheduled run acquires the same host and stage locks as deployment. Active
lock contention defers quietly. No accepted release or no database also skips;
an unresolved pending attempt refuses. The run never manufactures a deployment
request. It records the actual accepted token/source, verifies the full critical
table set, encrypts with the public age recipient, uploads to the configured B2
bucket and checks the fresh monitor result. The accepted ledger and database
container identity must remain unchanged. Scheduled backups persist their closed
accepted token/source and database-container binding in both the encrypted
manifest and the uploaded receipt; older version-1 receipts without that optional
field remain readable. After a successful backup and monitor check, the fixed
local policy prunes uploaded ciphertext copies older than seven days. It retains
the newest healthy offsite copy and unuploaded copies, refuses unexpected files
inside deletion candidates, and leaves unrelated paths and offsite objects alone. Dump/B2 child credentials stay separate;
the private age identity remains off-host. Wrapper directories are content-named
so a new sealed library can coexist with the prior recovery version.

`/etc/xynes/deploy/alerts.json` is root-owned `0600` and has exactly
`schema_version: 1`, `provider: "discord"`, and `webhook_url` from the existing
configured notification channel. Only HTTPS `discord.com/api/webhooks/...` URLs
are accepted. Caller-selected URLs/messages, redirects and mentions are refused.
HTTP headers/body share a 15-second deadline and a 4096-byte response limit.
Run `xynes-backup alert-test qa` as root for a clearly labelled channel test;
that test explicitly says no backup failure is being reported. Do not place the
webhook or other credentials in journals, evidence, Git or command arguments.
Local fixture checks establish no VPS backup, offsite recovery or alert delivery;
those require separate native evidence before acceptance.

## Idle admission across deployment and proxy restart

Before draining an existing idle generation, the coordinator persists
`start_drained: true` for both frontend proxies and updates the exact generation
hashes. It completes those writes before the first `/drain` call or runtime
replacement. A systemd restart therefore reloads closed admission while the
existing durable warm leases prevent sleep. Rebinding may resume live proxies
for product smoke; their persisted restart policy stays closed until `finish`
rechecks the exact healthy container/image/network bindings and configuration
hashes after successful deployment work.

A partial normal-policy write restores both closed configurations and matching
generation hashes. A failed warm release or deployment-owner unlink reacquires
both warm holds and drains admission. If durable or IPC recovery cannot be
verified, the coordinator attempts to stop both exact owned proxy units, even if
one stop fails. It retains the deployment owner and reports reconciliation as
required; no such failure qualifies the release. These are coordinator changes;
the qualified Go runtime and its source/compiler identity remain unchanged.
