# Xynes infrastructure

Operational source and placeholder configuration for the Xynes platform.
This public repository starts from a reviewed source snapshot; earlier internal
reports and repository history are preserved privately.

The Apex deployment epic will deliver QA, staging and production through one
consolidated pull request. The current baseline is not a completed hosted release.

## Contribution safeguards

Never commit filled environment files, credentials, SSH keys, database dumps,
customer data, internal incident reports or generated deployment artifacts.
Use protected server files or scoped GitHub environment secrets for credentials.
Keep diagnostic output redacted and commit only sanitized evidence.

Pull requests run on ephemeral hosted runners with read-only repository access.
The history secret gate uses a verified pinned Gitleaks binary, disables inline
ignore comments, and blocks findings without a reviewed exact-content exception.
Raw reports are temporary and are never uploaded. Native GitHub secret scanning
and push protection supplement this gate; availability and settings must be
verified after repository creation.

Production workflows require protected main and independent release approval.
Do not add deployment credentials to pull-request workflows or execute builds
in signing jobs. The final deployment PR will supply the complete pipeline,
source and image pins, environment configuration, recovery and acceptance evidence.

## Protected build cleanup

The build coordinator removes only the Buildx instance it successfully created,
using its isolated Docker config and exact run-bound name. On explicitly
GitHub-hosted Linux runners, `buildx rm --force --keep-daemon` removes the Buildx
context; the disposable job VM owns final daemon/cache disposal. Buildx `--force`
alone still waits for daemon shutdown, which blocked run 43575137307 for the
entire 300-second cleanup budget after all fifteen OCI archives were qualified.
The underlying Docker failure in that run was not exposed by its diagnostics.

Missing runner identity, self-hosted and non-Linux runners retain full teardown.
Every cleanup command still has a 300-second bound and errors remain fatal.
Neither cleanup mode can qualify invalid images or make a failed build
publishable. Artifact uploads still require coordinator success, followed by
real CI product smoke, protected publication and independent attestations.
No shared/local/VPS resource pruning or deployment behavior is changed.

## Hosted smoke storage cleanup

The pinned storage runtime supports ordinary object deletion but does not wire
its optional reference repository. Smoke cleanup uses the documented
non-reference delete only for its own freshly created object, after verifying
workspace, creator, nonce-bound filename, single upload and explicit
`dedupHit: false`. A dedup hit or missing freshness signal never authorizes
object deletion. Validated fresh fixtures are still cleaned up after a failed
provider PUT; shared objects and application reference semantics are unchanged.

Staging and production still require actual upload, processing and download
verification, followed by successful owned cleanup. A cleanup failure remains
fatal and cannot create acceptance or promotion authority.
