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
