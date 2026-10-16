# Apex deployment implementation plan

Status: active engineering; hosted application acceptance is incomplete.
Product scope is confirmed: existing Xynes Auth/Workspace Admin and CMS.
The clean public repository has protected main, native secret scanning/push
protection and independent review. Original internal reports/history stay private.

## Essential delivery sequence

| Story | Work and acceptance | Current evidence |
|---|---|---|
| APEX-01 | Reconcile fresh source refs, target ownership, dependencies and one-PR boundary. | Product scope confirmed; selected prerequisite bytes imported. Final release qualification remains. |
| APEX-02 | Prove safe idle/wake, bounded cold starts, drain and exactly-once forwarding. | Local adapter fixtures exist; actual app/OAuth/host acceptance remains. |
| APEX-03 | Converge compatible host/runtime versions and owned paths/networks; protect SSH and retained data. | Isolated paths/networks, final reviewed forced-command controller and inert signed-source idle runtime installed. Real OS/SSH isolation checks passed; app activation remains. |
| APEX-04 | Configure isolated DB/Auth, service identities, providers and public inputs for all three stacks. | Three private stage configurations and identities installed after native isolation checks. Local Auth runtime passes; hosted bootstrap/product acceptance remain. |
| APEX-05 | Build fifteen Linux amd64 OCI candidates, verify native processors and image/source bindings. | All fifteen amd64 planning OCI images built and inspected. Hosted runtime qualification remains. |
| APEX-06 | Wire protected source admission, private GHCR publication and isolated artifact signing. | Workflow variables/environment keys and concrete PR source reference installed behind main-only independent approval gates. Native final-head review/merge, real publication and attestations remain. |
| APEX-07 | Deploy QA, verify migrations/seeds and real product flows, force failed-smoke rollback. | Source-bound SQL planning/ledger fixtures pass; no hosted acceptance. |
| APEX-08 | Promote staging, enable proven idle groups, measure resources and run 24-hour soak. | Pending QA acceptance. |
| APEX-09 | Prove encrypted offsite backup, independent isolated restore and configured alert delivery. | Native configured-channel TEST delivered; timer templates inert. Actual offsite backup, isolated restore and scheduler acceptance remain. |
| APEX-10 | Consolidate the final PR, obtain independent review/merge authorization and complete protected production acceptance. | Single final infra [PR #1](https://github.com/Xynes-Studio/xynes-infra/pull/1) is open; final-head CI and xynes-admin independent review are pending; the user authorized the one-time code-owner handover and merge after final approval and passing checks. |

## Environment isolation

Production keeps the ERP namespace. QA and staging namespaces sit outside
that production cookie domain. All twelve DNS names currently resolve to the
deployment VPS; HTTPS and actual OAuth redirect flows remain deployment acceptance
gates. Provider handoff comments name the intended callback URLs.
Database/Auth keys and internal signing identities are generated separately.
Storage buckets/credentials are stage-specific. Filled files and keys live in
protected storage; public templates contain placeholders only.

Do not infer readiness from existing filenames or distinct placeholder hashes.
Validate credential/provider functionality, JWT issuer/audience, cookies/CORS,
root-owned mounts and actual DB freshness before changing runtime state. Populated
or unknown targets require adoption/recovery planning; do not reset them.

## Pipeline and operations

1. Fetch refs with command-scoped archan96 authentication; preserve dirty sibling
   checkouts. Qualify exact successful source checks and reviewed dependency pins.
2. Build with credential-free runners and exact recipes. Store authenticated,
   same-run artifact references and complete OCI receipts; partial batches refuse.
3. Immediately before publication, inspect actual protected-main checks, current
   workflow/job identity, independent release approval and private package access.
   Approval history lacks rerun-attempt binding: use a fresh dispatch for reruns.
4. Sign only the verified same-run image subjects in a separate protected role.
   Signing jobs execute no build or source code. Metadata validation alone is not
   artifact trust; verify the resulting attestations before host admission.
5. Use a root-owned forced-command deploy boundary, fixed stage scope and host
   locks. Hold warm leases, validate backup/migrations/identities, start pinned
   images and wait for readiness. Commit success only after product smoke passes.
6. Restore previous app images/configuration on failed smoke. Retain DB changes,
   certificates, volumes, envs and current/previous releases. Remove only explicitly
   owned, unreferenced resources outside retention; no global prune.

The protected workflows require their controller and workflow to be on `main`.
Before the one final infra PR is merged, finish code review, local checks, image
recipe verification and bounded native host safety checks. These checks must not
claim successful protected deployment admission. After independent review and
explicit merge authorization, run the real protected workflows, then finish QA,
staging and production acceptance and retain their operational receipts without a
second code PR. A defect requiring another code PR is a scope conflict to report.

Host boundary installation and privilege revocation have separate gates. Bind the
installation verifier to the exact reviewed library, wrapper and plan hashes and
actual Linux execution/refusal evidence; mocked operation callbacks cannot prove
the default executor works. Keep installation marked `boundary_ready: false`.
Before revoking legacy QA/production groups, verify each stage's actual SSH
refusals, authenticated native producer refusals, sealed paths, absence of old
sessions and a fresh administrator recovery login. The native safety witness
binds the installed admission/reader hashes, plan, repository, actual completed
security run/job/check-run IDs, canonical response hashes and capture time. It
must show the unchanged admission guards reject that completed nondeployment
producer for every stage; API/auth failures and fixtures do not qualify. This
negative safety test permits privilege removal before installing credentials.
It does not grant deployment admission or change release reviews, protected-main
requirements, production approval or accepted staging promotion. Positive native
admission is still required by each genuine protected deployment after merge.
Keep filled stage credentials and Actions deployment keys out of the legacy
Docker-root accounts until revocation and fresh cross-stage, Docker-socket,
secret-access and sudo denial checks pass. Keep `boundary_ready: false` until
the actual post-revocation OS and SSH checks qualify.

## Acceptance evidence

Record source/controller commits, image digests and authenticated workflow receipts.
Verify HTTPS/nonprod protection, login/workspace, directory-first CMS editing and
publishing, file processing, API-key scope/revocation and cross-environment denial.
Test cold GET/POST/OAuth, streams/long work, concurrent wake, deploy-during-idle,
restart recovery and forced rollback without replaying a forwarded write.
Measure actual memory/CPU released and all-awake capacity including retained Dev,
processors and background work. Require peak RAM <=90%, no OOM and 24-hour
QA/staging soak. Verify offsite encrypted backup, independent restore and recovered
service smoke within the existing RPO/RTO, plus delivery to the configured alert
channel. Fixed VPS charges may remain unchanged after memory is released.

Use the existing tools and reports as inputs, not completion claims. Keep private
credentials, dumps, customer data and internal vulnerability reports out of commits,
logs and public artifacts. Final code and sanitized evidence belong in the one PR.
