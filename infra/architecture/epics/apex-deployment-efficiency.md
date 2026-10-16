# Apex deployment and idle efficiency

Deliver usable QA, staging and production for the existing Xynes Auth/Workspace
Admin and CMS platform. Preserve hosted Dev while its ownership/use is checked.
One deployment epic and one final PR into protected main; the private history
archive remains separate from the clean public repository.

Deploy the existing seven backend services, two frontend apps and required native
processors using Compose, Caddy and Supabase on the existing VPS. Stop proven
idle stateless groups, wake them on first use and drain active requests before
sleep. Defaults: QA 15 minutes, staging 30 minutes, production 60 minutes, with
operator overrides and warm leases. Data, Auth, ingress, controllers, monitoring,
backup and background workers stay awake until independently proven safe.

Use immutable source/base/tool/image pins, independent reviews and protected
GitHub build/publish/sign/deploy jobs. Promote shared backend digests; frontend
URLs require three image sets built from the same sources. Serialize deployments,
apply source-bound migrations and seeds, wait for readiness, smoke product flows
and roll back application images/configuration on failure. Forward-fix databases.

Completion requires working HTTPS stacks, a proven production sleep group,
actual build/deploy/promotion/rollback runs, encrypted offsite backups and isolated
restore, configured alert delivery, all-awake peak RAM <=90% with no OOM and a
24-hour QA/staging soak. Local fixtures and green policy checks are intermediate
evidence. The final PR must receive the configured independent review and merge
authorization, followed by actual production acceptance.

No Kubernetes, HA, cloud migration, database sleep, new CRM UI, broad cleanup or
hidden app-source patches. Any necessary sibling fix is a one-PR scope conflict
that must be raised with its minimal patch before proceeding.

[Implementation plan](../../../docs/plans/apex-deployment-implementation-plan.md)
