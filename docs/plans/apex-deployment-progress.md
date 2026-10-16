# Apex deployment progress

This is an engineering checkpoint, not hosted deployment acceptance. The full
QA, staging and production delivery goal remains in progress.

## Verified locally on 2026-10-12

- Infra security and required lint jobs execute the workflow role checker. Only
  `apex-build.yml` can use the strictly checked package publisher role; ordinary
  PR jobs and the independent signer retain the shared policy restrictions.
- The required test job includes native admission, batch publication and GitHub
  reader regressions. All 32 unrelated workflow exports retain their reviewed
  byte digest.
- Native admission refreshes the publisher job, release environment, independent
  reviewer and approval after check-run qualification. Removed approvals,
  canceled jobs and changed protection settings refuse admission.
- The GitHub reader enforces an absolute response deadline across headers,
  body reads and chunk framing, using actual HTTPResponse/socket regressions.
  Synchronous DNS resolution remains subject to the system resolver; elapsed
  deadlines refuse after it returns.
- PostHog project token and regional host are supplied at runtime only to the
  gateway in each environment. Browser-key placement and debug logging are
  rejected by hosted configuration validation.

Verification: 124 Bun policy/source tests, 11 CI integration tests, 8 hosted
configuration tests, 10 native admission tests, 7 publication batch tests and
11 GitHub reader tests passed. Strict TypeScript checking passed using the
locked compiler tools. These checks do not exercise hosted product flows.

## Remaining delivery work

Provider validation/import, live build/publish runs, deploy/promotion workflows
and the host deployment boundary are still pending. Full image exports and private
publication, runtime credentials/bootstrap, all three hosted stacks, production
sleep eligibility, all-awake resource measurements, backup/restore and alerts,
rollback and the 24-hour QA/staging soak remain required. The single final PR,
independent approval, merge and post-merge production acceptance are outstanding.

## October 13 integration

The manual protected image workflow and admission/build/publisher entrypoints
are now wired. Native artifact metadata and ZIP digests bind their data handoffs
to the exact run/controller. The publisher retains the complete receipt, uploads
only that data file and passes verified image subjects to the isolated signer.
Required CI includes these regressions; all unrelated workflow mirrors retain
their baseline bytes. Local stage settings are prepared privately and Resend TLS
authentication succeeded for all three stages without sending messages.

Fresh local checks: 125 Bun tests; 15 artifact handoff, 6 downloader, 7 publisher,
11 builder, 3 workflow, 13 provider and 7 environment tests; strict TypeScript and
workflow policy checks passed. This is not a live image release. The source
snapshot still requires approval; public build-input configuration, private
registry namespace qualification and native attestation verification remain.
HestiaCP security work is deferred; the existing DNS delegation is preserved.

## Registry namespace qualification — October 13

The publisher now validates the complete fifteen-image batch before any registry
write, including inert namespace creation. Missing packages are recognized only
from authenticated native package API 404 responses; other errors refuse. New
namespaces contain zero-layer metadata only and must read back as private and
linked to this repository before any application image is uploaded.

Local regression checks passed: 9 publisher, 7 batch, 10 namespace and 12 GitHub
reader tests; 11 CI integration checks, including the unchanged 32 sibling
workflow mirrors. A bounded independent code review found no blockers. These
checks have not created registry packages or released application images.

The Auth source audit supports a minimal stock PostgreSQL/Supabase Auth/Caddy
candidate; live runtime compatibility and hosted product flows are still pending.
HestiaCP hardening remains deferred by the user. No panel or DNS change was made.

## Provider corrections and Auth runtime — October 13

Production R2 read verification now succeeds for its own bucket and returns 403
for the QA and staging buckets. Backblaze authenticates with the required file
capabilities and its single allowed bucket matches the declared backup target.
These were read-only checks; no provider objects were created.

All three private prepared environment files were refreshed from the corrected
provider handoff. Existing database passwords, JWT/anon/service keys, privacy
salts and public URLs were preserved. Independent Auth database login passwords
were added. Nothing has been installed on the host by this refresh.

A disposable local amd64 PostgreSQL 17.10 + Supabase Auth v2.196.0 fixture applied
all 70 Auth migrations using a login without superuser, database creation, role
creation, replication or RLS bypass. Signup, password login, token refresh, HS256
issuer/audience/role verification and Auth restart session persistence passed.
This fixture uses synthetic credentials and email autoconfirm solely for testing;
production retains email confirmation. No email or provider request was sent by
the runtime fixture. Required CI now runs this fixture with immutable images.

The pinned Auth runtime still needs GOTRUE_JWT_DEFAULT_GROUP_NAME=authenticated:
removing it produces an empty user-token role despite its deprecation warning.
The runtime regression checks the actual role claim. GoTrue owns its Auth tables,
ledger and uid/role/email/jwt functions; the bootstrap does not duplicate them.

Fresh checks: environment 17, Auth contract 5, public bundle 3, CI integration 11
and secret gate 7 passed. Current-file secret scan found only the reviewed public
SDK Git commit reference and no unreviewed findings; full committed history still
requires publication verification. Auth pins/config integration into the signed
release inventory, ingress API-key gate, HTTPS/OAuth/email/product flows, host
installation and the remaining full deployment acceptance are pending.

## Database/Auth release integration — October 13

The closed source/release image inventory now includes the pinned PostgreSQL and
Supabase Auth images. External images must match their approved vendor digests
and cannot masquerade as locally built app images. The build still produces 15
application/processor images; the database/Auth images are external pinned inputs.

The combined hosted configuration now includes database/Auth in each stage. The
validator checks fixed stage data/bootstrap paths, restricted database container
identity, separate Auth login credentials, immutable image references, always-on
operation, exact health commands, matching JWT issuer/secret and authenticated
role, implicit TLS SMTP settings, confirmation-required production configuration
and a unique Auth ingress network alias. Cross-stage paths, admin database login
for Auth, secret mismatch, health bypass and unreviewed settings are refused.

Fresh checks: 31 source-context, 23 release-record, 10 combined hosted, 5 Auth
contract, 9 publisher, 7 batch and 11 builder tests passed. The hardened amd64
runtime fixture again applied 70 Auth migrations and passed signup/login/refresh,
JWT claims and restart persistence; its exact compose digest was recorded.
This remains local evidence, not hosted acceptance.

Authoritative DNS reads found all four production hostnames already pointing at
the deployment VPS. QA and staging's eight hostnames have no A records. The user
is checking protected Hestia DNS access; no DNS record or panel setting changed.
The next delivery work remains host trust/forced deployment boundary, actual
GitHub image signing and deployment, protected environment installation, HTTPS
product flows, backup/restore, resources, rollback, soak and the one final PR.

## Manual DNS verification and publication records — October 13

The user added QA/staging DNS manually and prohibits automatic DNS changes.
All twelve Auth/CMS/API/Supabase hostnames now resolve to the deployment VPS
through the authoritative server and both Google and Cloudflare public DNS.
Production's four records were already present; no production AAAA/CNAME
conflict was found. Actual TTL is 14400 seconds on all twelve records.
No record, nameserver or panel setting was changed by the agent.

The publication data handoff supports the exact single publisher receipt with
native run/repository/commit/name and ZIP digest binding, 8MiB ZIP and 256KiB JSON
bounds. Publication qualification checks the complete ordered fifteen-image
inventory, sources, recipes, public build inputs, package identities, publisher
flags and reconstructed original build-receipt hash. Six publication regression
tests and sixteen artifact handoff tests passed; required CI includes them and
eleven CI integration checks passed. This does not grant signing/deploy authority.

Official GitHub metadata identified CLI2.102.0 (September30) and its immutable
asset digests; verifier source audit is in progress. The global installed CLI
remains unchanged. Hosted signature verification/deployment remain pending.

## Native verifier and restricted request path — October 13

GitHub CLI2.102.0 was prepared separately for macOS and Linux from official
release assets. Archive digests matched GitHub metadata and checksum entries.
A real upstream CLI artifact passed native Sigstore/provenance verification.
The exact Linux binary is now installed root-owned on the deployment VPS and
its version/hash were checked. Applications, database and DNS were not changed.

The native verification adapter checks the owned pinned binary, uses exact
repository/workflow/commit/ref/runner constraints, keeps registry/API auth in
private ephemeral configuration and bounds process output and runtime. Native
result binding additionally requires the repository ID, protected workflow,
first run attempt, exact image subject and provenance invocation/dependencies.
Fresh stage approval remains separate; artifact proof does not authorize a deploy.

A bounded stdin request parser, deployment-candidate acquisition and unprivileged
SSH entry are implemented. Candidates bind exact native run/source/artifact bytes,
all fifteen publications, private package IDs and all fifteen signature proofs,
then recheck packages/run/main. The SSH entry selects stage only from the OS
account, accepts the fixed command and relays only canonical validated data via
exact sudo arguments. It returns a non-deployable acknowledgment and discards
arbitrary privileged output. It is not installed: the root executor, fresh stage
admission, locking, deadlines, acceptance/recovery and exact sudoers must exist
and pass before this boundary replaces legacy deployment account privileges.

Fresh parent verification: 6 binding, 4 native-process, 12 request, 13 candidate,
9 SSH-entry and 3 operational bundle tests passed. Native cryptographic proof
was verified for the upstream tool; candidate tests use inert fixtures and do
not prove Apex image signatures or hosted acceptance. Current-file scanning
found no unreviewed secrets. The remaining full epic/one-PR work is unchanged.

## Production DNS recheck and host integration — October 13

At the user's production DNS follow-up, all four production A records were
read again from the authoritative server and 1.1.1.1/8.8.8.8. Each resolved to
84.247.176.134; authoritative TTL remained 14400. No production AAAA or CNAME
answer was present. No DNS changes occurred; duplicate records are unnecessary.

Fresh independent parent verification passed request (12), candidate (13), SSH
entry (9), stage admission (11), deployment dispatch (11), durable host state
(16), and Compose driver (16) tests. Backup staging support passed its suite of
23 tests with one inherited real-age verification skipped. These are local
fixtures, not hosted/offsite acceptance. The manual deployment workflow binds
native build/deploy run and job identities, uses separate stage environments
and strict SSH host keys, and carries no deployment-success assertion.

DB/Auth bootstrap starts only two pinned services before migration/seed and
all-service startup. No DB image change/reset/foreign-resource adoption is
allowed. The new admission/dispatch/state/driver tests are now required by the
infra CI profile. With pinned Bun 1.4.2, the actual workflow role checker, its
49 tests, all 11 CI integration tests, and infra-only exporter check passed.
All 32 sibling generated workflow mirrors remain at their reviewed baseline.

Root executor installation, restricted account convergence, source-bound
migration transport, live app deployment, real backups/restore, product smoke,
sleep acceptance, capacity measurements and the 24-hour soak remain pending.
No app or database was deployed during this continuation; the goal stays active.

## Concrete migration, backup and root integration — October 13

All twelve pinned source repositories were re-read using command-scoped archan96
credentials and are public/nonarchived. The credential-free exact-source fetch has
no repository visibility blocker. The build now writes a separate deterministic
migration artifact from exact Git sources, under a clean process environment;
it leaves the existing sixteen-file image artifact unchanged. Native migration
intake rechecks run/main/source bytes and reconstructs the forty-unit plan before
SQL use. Eighteen ZIP handoff, thirteen build, four workflow and five native
migration intake tests passed; source transport's nine tests passed independently.

Concrete host SQL and original-image seed phases are implemented. The SQL phase
binds the real pending state, applies only the reviewed catalog, checks the same
healthy owned database before/after, and verifies all forty ledger rows. Retaining
the first accepted origin bundle permits unchanged SQL across subsequent app
releases without rewriting provenance. Seed SQL uses platform.workspaces; the
incorrect identity.workspaces reference was caught and corrected. Ten migration,
twelve seed, seventeen coordinator and sixteen boundary tests passed. These use
inert operational adapters and do not establish hosted acceptance. The coordinator
CLI still refuses until actual target/warm/backup/smoke/persistence and process-tree
supervision are wired; no forced-command boundary or privilege revocation occurred.

The real isolated local candidate bootstrap passed with PostgreSQL 17.10 and
GoTrue's full Auth migration set. All forty actual candidate SQL units applied and
replayed; the ledger completed, routes/tables existed, and password/refresh plus
HS256 issuer/audience survived application SQL. The Dev-only provider seed was
excluded. Receipt is held privately with local_fixture=true, source_approved=false,
deployable=false. Original seed image execution and hosted acceptance remain
unverified. The first fixture failed PostgreSQL startup (126); making the readonly
public bootstrap shell snapshot executable fixed the fixture. Exact owned fixture
cleanup was verified. No hosted database was touched.

Official release assets for age 1.3.2 and Backblaze CLI 5.0.0 were hash-verified.
Their fixed Linux binaries are installed root-owned in separate versioned VPS
paths, and actual versions/upload-help syntax were checked. Large laptop-to-VPS
SSH stdin transfers reset the connection; direct HTTPS downloads on the VPS
succeeded. Small SSH control commands remain usable. The recovery identity stays
privately on the laptop; only a public recipient is prepared for future host use.
No provider credential or private age identity was installed with these binaries.
Twenty-three backup helper tests passed with real native age, no encryption skip.

The concrete host backup phase now uses the unchanged helper, strict wrappers,
actual native age, per-tool credential environments, critical table evidence,
private ciphertext/receipts and fresh monitor checks. Ciphertext forwarding and
hashing stream in bounded chunks. Nine tests passed with inert Docker/B2; they do
not prove an actual offsite upload or recovery. Required infra CI now includes
migration intake, host SQL/seeds, backup, coordinator and boundary checks. All
eleven CI integration checks and the infra-only export check passed, preserving
all thirty-two sibling workflow mirrors.

Current-file Gitleaks scan covered 351 files. Three candidates were reviewed:
the existing public SDK commit reference and two published age executable hashes.
The two new exceptions bind exact candidate and toolchain file SHA-256 values;
no generic allowlist was added. Zero unreviewed candidates remain in that snapshot.
The uncommitted epic tree still requires final history/publication checks. Agent
account limits stopped delegation; direct parent integration continues. The full
QA/staging/production deployment, independent approval/one final PR, actual
provider/product flows, recovery, capacity, sleep acceptance and soak remain open.

## Durable release storage and backup state binding — October 13

A root-owned durable asset store now copies exact rendered Compose data, source
manifest, public inputs, publication receipt, migration data and the first accepted
origin bundle outside temporary deployment work. Exclusive writes, per-file hashes,
private permissions, directory/file fsync and exact file/directory inventory checks
precede its completion record. Accepted ledger records must match before loading;
unaccepted/incomplete, changed, symlinked, hardlinked, extra and cross-stage assets
refuse. Interrupted writes preserve pending state and private source data for
reconciliation. This data store grants no deployment or source approval; the
coordinator still requalifies loaded/persisted assets before mutation/acceptance.

Six real filesystem persistence tests passed. The coordinator's backup operation
contract now passes the actual held release state and pending token, rather than
forcing the concrete backup phase to reacquire the host lock or infer authority.
The regression failed before wiring and all seventeen coordinator tests then
passed. Required infra CI includes persistence checks; all eleven CI integration
and sixteen boundary tests passed, the export is current, sibling mirrors are
preserved, and the tracked diff whitespace check passed. These remain local
checks. No host mutation, image publication, PR or application deployment occurred
in this continuation. Actual runtime target audits/warm leases/query/product-smoke
operations and whole-tree process supervision must be wired before enabling the
currently refusing CLI and installing/revoking the deployment account boundary.


## Concrete runtime integration and host preparation

The prior goal continuation made progress: authoritative DNS was rechecked, then
the actual deployment host and concrete root runtime were advanced. Fresh scoped
fetches preserve the public main baseline f4d8dc783555f7866ed7d1a0cf7d7838b4d3550c
and private-history develop 1faea30dc68362e445db3af1835923ab2a682be7. Dirty work
is preserved; global GitHub authentication is unchanged.

The fixed runtime now binds the private policy and registry clients, separate
artifact downloader/native signature verifier, target audit/warm controls, held
state/token offsite backup, durable assets, actual seed implementations and
mandatory product smoke. The root CLI delegates to a systemd supervisor with
45-minute control-group limit, real sudo/OS identity, InvocationID/MainPID/cgroup
binding and Linux parent-death pidfd watchdog. Accepted acknowledgments require
an accepted record in the root ledger. Independent review found and corrected
the sealed wrapper mode mismatch (0555), actual -ISB interpreter argv and normal
activating-to-running/PID-assignment handling. Native VPS supervision qualification
is being tested in isolated owned services; no hosted release acceptance follows.

Dedicated confirmed example.invalid smoke accounts and two owned workspaces are
provisioned through protected recovery journals, with no email, invitation, user
listing, password reset or unknown resource adoption. Original source CMS seeds
run again after fixture workspace creation, before current/rollback product smoke.
HTTPS has total deadlines and bounded framing/body reads. Foreign-stage probes
are explicitly synthetic and refreshed only after their protected prior identity
and signature are checked; actual sibling GoTrue session acceptance is separate.

Fresh parent checks: runtime 9, coordinator 18, controls 19, supervisor 17, setup
12, product-smoke 12, Compose 17, boundary 17, assets 6, public bundle 3, source
checks 14 and CI integration 11 passed. Strict TypeScript checking exited 0. The
infra workflow exporter matches; all 32 sibling mirrors remain at baseline.
These local tests do not establish hosted product/provider/sleep acceptance.

Real native GitHub verification passed 79 required checks for all 12 pinned
sources, including job/run/workflow/exact-source bindings. See
[the curated receipt](apex-deployment-evidence/native-source-checks.json). Prior
CLI refusals were traced to transient native GitHub API 502 responses. The source
reader now permits at most three GET attempts for 502/503/504 under the original
15-second abort budget; auth failures and malformed data never retry. Approval
and deployable remain false. This is source evidence, not an approved release.

The coordinator previewed, applied and independently rechecked actual VPS
preparation: a locked nologin staging account with no supplementary groups;
root-owned QA/staging/prod deployment paths; verified empty DB directories at the
pinned PostgreSQL UID/GID70; private state/work/evidence/release stores and
supervisor locks. Initial deployment policies were created only after native
broker/proxy/container/socket absence checks and keep eligible apps awake during
bootstrap. Convergence has zero remaining operations for this preparation. Dev,
credentials, existing files and application services were preserved. No resources
were deleted. Caddy and monitoring remain the only application containers.

The full goal remains active. No app image publication, hosted DB/Auth/application
start, final PR or merge occurred. Remaining delivery includes native supervisor
qualification, exact forced-command installation/legacy privilege revocation,
protected env/identity/provider configuration, ingress and idle integration,
premerge/final PR review, protected build/sign/deploy runs, job-safe upgrades and
QA rollback, actual storage/product/OAuth tests, backup/independent restore/alert,
capacity and sleep measurements, 24-hour QA/staging soak and production acceptance.


Native VPS supervisor qualification now passed both actual Linux/systemd binding
and parent-death pidfd cases. Each verified the literal service properties, real
InvocationID/MainPID/cgroup/ExecStart and that its worker and child stopped. See
[the native fixture receipt](apex-deployment-evidence/native-linux-supervisor.json).
The first harness was blocked by /run's noexec mount; an executable root-owned
fixture location was used after correcting harness import/string generation and
checking both generated programs' syntax. Successful fixture resources were
removed by the owned harness. No application services, credentials, deployment
state or DNS changed; deployable remains false. Final entrypoint installation,
source approval and actual release acceptance remain required.


Final current-file scanning covered 365 selected public files with pinned
Gitleaks8.30.1: three exact reviewed public-reference/checksum findings and zero
unreviewed findings. This scan does not substitute for the final committed
history scan. The two failed, task-owned supervisor fixture directories were
removed only after complete file ownership/inventory and native unit-absence
checks; no other resources or application services were removed.

## Native SSH compatibility and staging access — October 14

Fresh native VPS reads showed repeated `listenaddress`, `hostkey`, `allowusers`
and `acceptenv` lines. The boundary parser now preserves their complete ordered
values while refusing duplicate single-value settings. Explicit user denials
override allowlists; host/group rules without matching facts fail closed.
Regression RED reproduced the parsing/denial defects; all 21 boundary tests pass.

The coordinator applied only the exact staging account addition to the existing
global SSH allowlist after native dry-run validation and comparison of every
existing account profile. A root-only original configuration copy is retained.
A fresh recovery SSH connection succeeded after reload, native `sshd -t` passed,
and another preview reported no remaining changes. The verification receipt is
`apex-deployment-evidence/staging-ssh-allowlist.json`. No app service, credential,
DNS, Dev directory or existing account changed. The forced-command boundary is
not yet installed.

Three distinct deployment Ed25519 keys were generated in protected local storage
(mode 0600, parent 0700); none is installed on the host or GitHub yet. The retained
Dev deployment account has UID/GID 1004, docker/xynes supplemental groups and zero
active UID processes. Privilege parking remains pending independent preservation
and access checks.

Legacy deployment-user apply now refuses an existing or symlinked Apex boundary
marker before any key-file read or host preflight, preventing later restoration
of unrestricted privileges. Ten filesystem/CLI regressions pass. Required CI
includes that test through the canonical profile and regenerated infra workflow;
all eleven CI integration tests pass, with 32 sibling mirrors preserved.

A fresh sixteen-suite host/runtime batch passed fifteen suites but exposed one
supervisor startup-timeout cleanup failure. The bounded supervisor owner is
investigating the concrete failed assertion; the batch is not recorded as green.
The Storage source quiescence candidate remains isolated for review, with no
sibling-source, build-context, image or live deployment change authorized by
these local tests. Full deployment, independent approval, rollback, backup/restore,
capacity, soak and the single final PR remain incomplete.

## Follow-up verification and source exception request — October 14

Fresh command-scoped archan96 fetches succeeded: public main remains
`f4d8dc783555f7866ed7d1a0cf7d7838b4d3550c`; private-history main remains
`d9bf301574cb5ffea2272414e32c667a1e358bab` and develop remains
`1faea30dc68362e445db3af1835923ab2a682be7`. No worktree reset or global
GitHub account switch occurred. The existing verified VPS Ed25519 key was
prepared for the future protected Actions variable; no GitHub key/variable was
installed.

QA/staging/prod committed templates now match the actual deployment DNS,
environment-specific cookie domains and Supabase OAuth provider callback.
The private generated settings already used those values and were not changed.
RED reproduced three template mismatches; all 18 environment tests and 113
no-secret template assertions pass. Retained Dev templates remain unchanged.

The supervisor failure was fixture scheduling: its inert service originally
waited for request intake before publishing native ownership. The corrected
fixture publishes ownership first and advances only its injected timeout clock
after observed lifecycle evidence. Production supervisor SHA remains
`800394f59492be70fe028fcfca058b0668a3d964dc6569f74c87c65421b3c03a`.
Parent independently reran two concurrent 18-test suites (both exit0); the
agent's four concurrent runs also passed. Prior batch failure remains recorded.

Retained Dev parking is implemented and integrated in the sealed library and
required CI, with 13 passing tests. It has not been applied to the VPS. Fresh
read-only audit records 14 preserved file/directory entries (12117 bytes), zero
Dev UID processes/sessions and twelve root-owned stage paths. Native sudo
explicitly denies the Dev account; its zero exit code caused the first audit
to mark that fact false too conservatively. No sudo rule or account was changed.

The isolated Storage candidate passed parent verification of 116 focused tests,
TypeScript and ESLint, and exact eight-file/patch hashes. Its proposed source
exception is pending with the user: one Storage prerequisite PR followed by
the single final infrastructure PR. No sibling source change is authorized yet.
A separate code review then reproduced a P1 false drain proof: fail-fast parallel
job execution can reject before another admitted runner settles. The private
candidate owner is adding a regression and fixing that case; the previous
`9f2bb34018c9bed9bc16c9fda6377d2a910339e84af7f6045ce815e0eda5603d`
patch is superseded for production readiness. No patch is in a Git source repo,
image, build context or hosted service. Independent review continues.

## Retained Dev privilege removal and Storage prerequisite authorization

The user explicitly approved one Storage prerequisite PR, followed by the
single final infrastructure PR. This is the only approved exception to the
original one-PR requirement; other sibling changes still require a concrete
conflict decision. No source PR or merge exists yet.

The corrected Storage patch SHA is
`0334341ed9f21320eadb678590c1602f215f9928ba82ce8de7d31179c28a9af8`.
Parent verified 120 focused tests, typecheck and lint; a separate reviewer
reran both failed scan/native drain cases and immediate resume using actual
pollers. Failed work never grants a proof and resumed polling remains active.
The prior candidate is superseded. The reviewed patch is now applied to the
isolated source branch `fix/apex-storage-deployment-drain` at
`.worktrees/apex-storage-deployment-drain`, based on fresh Storage develop
`59e9386dad55245b2f4239e0c5f251790add2eee`. Storage main is much older
(`14b56b0376389b93ebd2b4c0ef0a30d36fecac24`); the prerequisite targets develop
to preserve the small reviewed diff and the existing source-selection contract.
Primary source-repository dirty work is untouched. Whole local suite/coverage
and native default-entrypoint compatibility are being verified before publication.

The coordinator applied the native-validated retained Dev restriction. Only
`xynes_dev_deploy` was added to global DenyUsers, its shell set to nologin, and
its supplemental docker/xynes groups removed. The original SSH config is
retained in a root-only, plan-bound backup. Three real fresh recovery SSH
connections succeeded before and during revocation; native profiles preserve
recovery and stage settings. Dev file preservation SHA stayed identical.

Independent post-checks verified the exact Dev UID/GID and sole primary group,
zero active UID sessions/processes, explicit native sudo denial, native SSH
user denial and sixteen actual OS permission denials for Docker socket and
stage/control directories. Dev files, database directory, Caddy/services and
DNS were unchanged. Curated evidence is `apex-deployment-evidence/retained-dev-parking.json`.
QA/prod legacy groups remain pending full forced-command boundary installation
and independent checks; whole host isolation is not claimed. No filled stage
credentials, application service or release has been installed/deployed.

## Storage prerequisite PR opened — October 14

The user-authorized Storage prerequisite is now [PR #36](https://github.com/Xynes-Studio/xynes-storage-service/pull/36), targeting develop with signed commit `63adeb13b748bf9d754b9e093440d4cc153c4ccc`. Local signature verification passed; GitHub reports `unknown_key` because the author account has no registered public GPG key. Public-key registration is requested; no private key was exported. The selected independent reviewer Aman9804 has verified write access and a native REST review request. No merge is authorized or performed.

The final local suite passed 1,738 tests with 50 explicit integration/fixture skips; coverage was 92.46% functions / 94.46% lines. ESLint, TypeScript, staged whitespace and Gitleaks passed. The Linux amd64 native release fixture executed 219 passing tests, including real default production entrypoint, private Unix authentication and unqualified PID1 SIGTERM refusal. These fixtures do not establish hosted DB/provider or deployment acceptance. Native GitHub CI is running.

The final infrastructure PR remains the single deployment PR. Storage controller integration continues independently; no source pin, published image, stage credential installation or hosted app deployment has changed. Required CI, independent review and explicit merge authorization remain gates. Curated source/native evidence: `apex-deployment-evidence/storage-pr-checkpoint.json`.

## Production overlay correction and read-only upgrade catalog — October 14

Automated review of Storage PR #36 identified that its existing production overlay lacked mandatory private control assets. Same-PR follow-up signed commit `0b869c8eaec2a1792eb1d2b7421dbf8eee8465fc` enables the flag and replaces inherited mounts with only the private writable control directory and read-only token, both operator-supplied with host auto-creation disabled. Actual Compose RED reproduced the unbootable overlay; GREEN confirms two private mounts, inherited mount removal and refusal of either missing path. The full Linux amd64 verifier reran: 219 passed, zero failed, log SHA `e0cde9dac93d04bf0ff865476734897af56048dadf563455bc764dddb0680de0`. All eleven native GitHub CI checks passed on the new head. The PR remains unmerged, awaiting independent review, signature qualification and explicit merge authorization.

The first public GPG export was empty despite GPG returning zero; this was corrected using the configured binary and exact signing fingerprint, with explicit nonempty public-header validation. The verified 2562-byte export SHA is `227aa5c6671f395786276874c3875e1caa3d5b7057fd36f5a286c69e6cdf8996`. A visible workspace-root copy is available. The user reports registering it and requests read-only verification, explicitly prohibiting commit modification/recreation. Native verification is pending; the key's existing identity differs from the commit email. Do not alter key identities or commits without subsequent authorization.

Infrastructure migrations now read back the exact accepted unchanged 40-unit catalog without replaying schema SQL. RED demonstrated both accepted/corrupt ledger paths wrote SQL before validation; GREEN passes all14 host-migrate tests and all18 executor regressions. The read-only native result binds pending state, accepted origin and exact database CID; unknown ledger never falls back to repair SQL. No actual hosted migration was run.

Storage controller/provisioner integration is incomplete. Agent work stopped at account usage limits: new host_quiescence.py and associated tests are draft, Storage-control helper is missing, Compose/assets/executor/runtime wiring is pending. Current draft real Unix tests fail because fixture/production socket addresses are too long. Agreed fix uses host `/run/xynes/storage/<stage>/<64hex>` (container destination remains `/run/xynes/storage-control`), token `/etc/xynes/storage-control/<stage>/control.token`, label `com.xynes.storage-generation`. Guard draft still needs these agreed path/label edits. Production init peer binding must verify Bun child cgroup+ancestry, not PID equality. Unknown remote sidecar drain refuses actual replacement; unchanged sidecars stay awake with explicit no-recreate. No draft qualifies executor_complete or host install.

Native read-only GitHub verification now recognizes signing key713BC5E1A6B66855 but marks both Storage commits unverified (`bad_email`). The key identity does not match their committer email, and its registered email is unverified. No commit or key was modified after the user's read-only request. Signature qualification and independent human approval remain pending; all11 native CI gates on current head passed. The full deployment goal remains active and incomplete.

## Storage controller, recovery persistence and actual Linux peer proof — October 14

The protected provisioner passed10 filesystem/socket tests: existing tokens stay unchanged; private ownership/modes, prefix collisions, root tombstones, exact native container binding and original-socket-only retirement are enforced. Generation metadata is64hex; the disk key is the first32hex under `/run/xynes/storage/<stage>/` so native Unix addresses fit. Tombstones reserve disk keys forever.

Controller11 tests pass after actual failures were reproduced and fixed: live CPU accounting must not invalidate a process (PID start time/parent/cgroup are checked); failed partial recreation must reacquire a new process drain for rollback only after exact oldCID absence. Existing accepted Storage absence fails closed. Actual Unix-stream fixtures reject active jobs, wrong ownership, false/short proof and unknown sidecar replacement.

The integrated Compose driver passed30 tests including real native render/hash checks, generation mounts/flag, explicit no-recreate for identical accepted sidecars, and unchanged native sidecar CID/labels. Root executor20 tests pass: guard binding precedes bootstrap, SQL is qualified before migration, and rollback persists its fresh generation before smoke. Store17 tests preserve immutable accepted assets/origin while retaining a separately hash-linked operational recovery history; unknown/incomplete/tampered recovery data refuses. Native runtime9, boundary21, CI11 and public bundle3 tests passed. The two new helpers are in the sealed library and canonical required CI; sibling mirrors remain unchanged. Independent review of the full integration is pending.

The primary coordinator pulled only the immutable public Node fixture image and ran an isolated, nonce-owned Linux UID1001 Node/tini socket fixture on the VPS. Actual kernel peer credentials, child ancestry and exact container cgroup passed; wrongCID and reversed ancestry refused. A labeled injected start-tick discontinuity refused; actual OS PID reuse was not forced. Exact fixture cleanup passed. Application services were unchanged; this verifies only the peer fence, not hosted Storage or deployment acceptance. Private raw facts stay private; curated proof is `apex-deployment-evidence/storage-controller-integration.json`.

The user authorized adding the matching personal email identity to the existing GPG key. Updated public certificate3427bytes SHA `8c72bc4dbe4633f22304aa357e4eed42d70905b51d19f277c4dcec7873f8efa1`; fingerprint and both commits are unchanged, local signature still valid. User replacement of the registered public certificate and verified email is pending, as is independent Storage PR review. No source pin/image publication/application deployment/final infrastructure PR has occurred. The full goal remains active.

Independent full integration review finished with no actionable P1/P2 findings; reviewer reran87 scoped tests (guard11/driver30/executor20/assets17/runtime9), all passed. It verified partial-recreation reacquisition, exact retained sidecar config-hash/CIDs, persisted rollback generation and unchanged accepted source pointers. No reviewer host access or implementation edits occurred. This is code verification, not stage deployment acceptance.


## 2026-10-14 signing identity and boundary gate checkpoint

The authorized personal-email identity was added to the existing GPG key, retaining
fingerprint `A62ADEFA8178E6E7F573F5F4713BC5E1A6B66855`. Both Storage commits remain
unchanged. The refreshed public export is 3427 bytes with SHA-256
`8c72bc4dbe4633f22304aa357e4eed42d70905b51d19f277c4dcec7873f8efa1`.
A fresh native GitHub read still reports `bad_email` for both commits: the currently
registered certificate contains only the unverified work-email identity. The user
must register the refreshed public certificate and verify the matching commit
email before the next signature qualification. Independent human review is also
pending; no merge or release pin change occurred.

Fresh command-scoped fetches preserve public main `f4d8dc783555f7866ed7d1a0cf7d7838b4d3550c`,
private develop `1faea30dc68362e445db3af1835923ab2a682be7` and Storage develop
`59e9386dad55245b2f4239e0c5f251790add2eee`. Working files were preserved.
Read-only VPS inventory confirmed the current SSH configuration and deployment
accounts. All 21 boundary tests passed. A new private dry plan binds 49 library
files and 60 proposed assets; no files were installed and no groups revoked.

Independent readiness audit confirms that the coordinator must supply real,
exact-bundle native execution/refusal evidence before installation. Offline mocks
and component fixtures do not establish successful hosted execution. Revocation
additionally requires genuine native admission, fresh administrator recovery,
sealed paths and no stale deployment sessions. Positive protected admission depends
on the final workflow being merged to main; the implementation plan now explicitly
separates premerge safety/code checks from postmerge workflow and app acceptance.
The full deployment goal remains active and incomplete.


## 2026-10-14 native GitHub signature qualification

After the user registered the refreshed certificate, a fresh native GitHub read
marked both unchanged Storage commits `63adeb13b748bf9d754b9e093440d4cc153c4ccc`
and `0b869c8eaec2a1792eb1d2b7421dbf8eee8465fc` verified with reason `valid`.
The matching personal-email key identity is verified. Independent human approval
is still absent; the source PR remains unmerged and release pins unchanged.


## 2026-10-14 native sealed bundle and public secret audit

The exact 49-module proposed deployment bundle passed nine actual Linux/root
component checks on the authorized VPS: original bootstraps refuse missing sudo
identity, root SSH and wrong shell commands; unmocked default operation adapters
construct for all three stages; malformed requests avoid protected config/network/
process access; altered source, extra files and mutable modes refuse before imports.
Only fresh unreferenced fixture paths were used and exact cleanup was verified.
The receipt explicitly excludes executor completeness and deployment acceptance.
No SSH/sudo policy, groups, application services, providers or databases changed.
Independent exact-bundle code readiness review is pending before installation.

A fresh Gitleaks8.30.1 full working-tree scan (including decoding/archives) found
three candidates, all matching the existing exact-file/exact-candidate exceptions
for public executable checksums and a public Git commit reference. Zero unreviewed
findings; no scan exceptions were added. The public repository history gate also
passed with zero candidates. Final prepublication scanning remains required after
any further changes.


## 2026-10-14 live restricted deployment boundary installation

Independent exact-bundle review qualified installation/code readiness with no
actionable P1/P2. The coordinator installed the 49-module bundle, three isolated
bootstraps, root-owned stage public keys, literal-stage sudo rules and the managed
SSH suffix. Native sudo/SSH validation and installed-byte audit passed; private
inputs were cleaned exactly and an administrator recovery copy was preserved.
Fresh administrator SSH succeeded after reload. All three actual stage-key logins
refused malformed requests and generic commands, and QA key login to production
was denied (eight native SSH checks).

No legacy supplemental group was removed, no provider/stage credentials were
installed and no application/database deployment occurred. The boundary remains
`boundary_ready: false`: positive native admission, sealed paths, stale-session
checks, group revocation and full OS/cross-environment isolation are still pending.
Storage signatures now qualify; its independent human review remains required.


## 2026-10-14 merged Storage source and actual planning build

The user merged Storage PR #36. Native GitHub confirms merged develop commit
`12791e9d609abea0546573a54e385b9ffe323b1c` is Verified and all 11 native checks
pass. No human approval is recorded; the user performed the merge. The planning
source pin now uses that commit. A fresh native qualification binds all 12 sources
and 79 checks/jobs/runs/workflow blobs; release approval and deployability remain false.

Five missing processor shell assets were restored from reviewed checkpoint bytes;
all 13 processor-readiness tests pass. The public export exact allowlist now includes
those assets and four hosted overlays; three real Git archive tests pass.

An actual isolated local amd64 OCI build completed and inspected seven backend
images, then stopped on the LibreOffice pinned package installation (exit 100).
Completed archives and private logs remain preserved; no image was published.
Independent audit found processor digests change on unrelated release provenance,
blocking safe subsequent upgrades. A stable verified content identity and actual
reproducibility checks are being implemented; exact host replacement refusal stays.

Real authenticated GitHub responses produced three native negative admission
witnesses: completed nondeployment producer/job identities are refused by the exact
installed admission guards. These are safety evidence, not positive admission or
product acceptance. A prior progress entry interpreted admission smoke as requiring
a positive postmerge run before group revocation; that interpretation is being
corrected with explicitly typed negative producer evidence. Fresh recovery, sealed
paths, absence of old UID sessions/processes and postrevocation isolation checks
remain mandatory before distributing deployment/provider credentials.


## 2026-10-14 inert hosted assets and isolated local identities

Native VPS installation verified all 54 public hosted Compose, auth bootstrap/template
and processor files against the independently reviewed immutable manifest (exit0).
Created 67 owned file/directory entries; no application activation, DNS change or
private provider material. Existing environment files and workloads were preserved.
Local preparation generated 18 distinct internal Ed25519 private identities and
18 receiver trust files across QA/staging/prod, all private mode0600; none uploaded.

Docker VM disk exhaustion caused the failed LibreOffice build; direct gpgv validation
confirmed valid Debian signatures. Removed only cache from the isolated current
Apex builder after all jobs stopped, freeing 3.536GB. Eight complete planning OCI
archives passed independent content inspection and remain preserved on the laptop.
Existing images, unrelated containers, databases and volumes were retained.


## 2026-10-15 host isolation and private configuration handoff

Native revocation removed only the unused QA/production deployment accounts’
legacy docker/xynes supplemental groups. All18 OS isolation denials passed; fresh
postrevocation SSH tests passed all8 cases. Admin recovery and retained Dev were
preserved. Independent review and12 fixture tests qualified the fixed private
installer. Following successful isolation, it installed and verified all42 private
configuration/identity/provider/public-recipient files, preserving exact old envs
as protected recovery copies. Apps, databases and DNS were not activated/changed.
The backup recovery private key remains laptop-only. The three processor identity
modules now require a new reviewed host bundle before release admission.

An automatic approval review could not initially run because of a usage limit;
that upload did not execute. After the user restored usage, the same reviewed
upload succeeded (exit0). No approval check was bypassed.


## 2026-10-15 complete planning build and native host handoff

All fifteen Linux amd64 planning images were built and inspected as one complete
receipt set. Nothing was published or admitted as a release. Production Auth's
transient pinned-package download failure was retried with the same source/recipe;
Docker Desktop was restarted after its daemon stopped. Existing archives and
unrelated local workloads were preserved.

Native VPS component checks passed all ten cases for bundle `5083e193a11016cf672022ba4c650dfaf94de0d23a1f28e70637a921aa724e37`.
The reviewed code-only upgrade installed that bundle, preserving the old bundle,
accounts/groups, SSH profiles and recovery copy. All eight fresh SSH checks passed.
The installed root-only backup command delivered one labeled TEST message through
the existing configured Discord channel. Three backup timer templates are installed
but not enabled; actual encrypted offsite backup/restore remains unverified.

GitHub now has the three public build maps, release-scoped policy-read credential,
stage-scoped SSH keys and pinned host-key variables. Fresh native checks confirmed
main-only environment policies, no administrator bypass, and independent Aman9804
release/production approval. No workflow was dispatched. The user accepted the
current registry credential's broader scope temporarily; replacement with a
read-packages-only token remains a follow-up.

Local Auth signup/password/refresh/restart and all eight PostgreSQL migration
runtime tests passed. Native age and Gitleaks tests ran without their earlier
missing-tool skips. Pinned Go race/vet and the Linux Unix-boundary test passed
(82.1% runtime statement coverage); they do not prove hosted sleep behavior.

The final PR is not ready: hosted frontend sleep runtime provisioning, ingress
routing, generation rebinding during deploy/rollback and real app cold-flow
acceptance are being completed. Database/auth infrastructure and backend groups
remain awake. Backup release-binding/retention changes also require a subsequent
reviewed sealed-library upgrade before scheduled timers are enabled.


## 2026-10-16 hosted integration code and premerge runtime qualification

The final reviewed fifty-file controller bundle `750f624278bcec519d62783b2cca951a24cb3f2d2be1c6e5ab620da0f097b2d4`
is installed on the VPS. Its ten native component checks and eight fresh SSH
checks passed, with both predecessor libraries and recovery copies preserved.
No idle services or application stacks have been activated. The three DB targets
are empty directories owned by the intended PostgreSQL UID/GID.

The hosted idle coordinator now drains frontend requests before replacement,
rebinds exact container generations on deployment and rollback, preserves durable
warm holds, and restores runtime directories at boot. Each request resolves its
upstream through authenticated broker inspection of the exact approved container,
image and stage network; an actual Linux fixture verified that an obsolete IP
serving foreign content receives zero requests. QA/staging wake limits are 90 seconds and
production 30 seconds; measured hosted cold budgets remain an acceptance gate. The broad
Linux Unix-boundary suite passed with 82.1% statement coverage; the separate host
fixture has 64.3% coverage of its narrower exercised scope. Neither proves hosted
product acceptance.

All 76 local canonical quality commands have passing results. Three sandbox socket
refusals were rerun with local IPC permission and passed; no code gate was weakened.
Five native runtime commands were verified separately. All six actual frontend OCI
images passed isolated nonroot/read-only startup and route checks with no HTTP 500
or OOM. CMS used an explicit inert loopback gateway health fixture. Two cold starts
per image are readiness evidence, not the required 20 managed-group cycles, real
auth enforcement, provider OAuth login or production budget acceptance.

A fresh disposable PostgreSQL 17.10/GoTrue runtime applied all 40 pinned migration
units, produced 53 enabled routes and 33 application tables, and replayed without
adding ledger rows. All 40 payload hashes match the current infra working tree and
pinned sibling Git blobs. The dev-only provider seed was excluded. No hosted data
was read or changed.

The actual Caddy binary accepted the proposed 274-line configuration in a native
dry run. Existing bytes are preserved except two production frontend upstreams;
new QA/staging/Supabase routes and private email templates are prepared. Live
routing and DNS remain unchanged; all 12 DNS names resolve to the deployment VPS.

Remaining premerge work is signed-source native binary qualification, reviewed
inert idle provisioning and final diff/security consolidation. The one final PR
and independent review/merge authorization still precede protected publication
and actual QA/staging/production deployment, rollback, recovery, capacity and 24-hour
soak acceptance. The deployment goal remains incomplete.

## Final publication review checkpoint

The native Linux idle binary reproduced byte-for-byte from signed Git source
with the verified Go 1.27.2 archive (binary SHA-256
`25b1a0072c03ac20ec8bd3f2ebac8e1b1c824f835371970ea9ae9645e6f78a5f`).
Inert host provisioning still requires the final clean signed source receipt
and a fresh preflight bound to the installed controller ledger.

Final review removed generated Python interpreter caches from the unpublished
source snapshot and added ignore rules. A real postcommit Gitleaks run exposed
an optional scanner metadata field absent from the gate schema; its compatibility
fix must retain exact blob/candidate exception matching and reject malformed
metadata. No secret-scan exception or release approval requirement is waived.

The final regression checks passed: 16 admission, 13 deployment admission,
16 idle lifecycle and 9 secret-gate tests, including the pinned native scanner.
The affected release integration cohort also passed 133 tests. Native source
approval now binds the actual merged PR, independently reviewed final head,
complete Git tree and exact source manifest. Longer source-review reads precede
the final job/environment authorization refresh. Disk restart policy stays
drained through replacement and smoke; failure paths retain warmth and close
admission. The signed Git source and inert host installation must be refreshed
to these reviewed bytes before activation.
