# Hosted application and processor configuration

Status: locally rendered and policy-tested; deployment integration and image/runtime acceptance remain in progress under the Apex plan.

Apply `hosted.yml`, one of `qa.yml`/`staging.yml`/`prod.yml`, then the canonical `internal-request-identities.yml`. The base file is JSON syntax, which Docker Compose accepts as YAML. It defines seven backends, Auth/CMS frontends and three storage processor/definition services, with separate environment network/project names. It does not create or initialize the Supabase data/auth stack.

Every image input is required and must be a full SHA-256 digest reference. Use the approved release manifest to provide them; no latest-tag fallback exists. Service secrets are explicitly mapped to their owners rather than injecting a shared env file into every container. Identity files live under `/srv/xynes/<environment>/identities` and retain the canonical service-specific private/trust mounts. Filled environment files and rendered configuration contain secrets and stay protected on the host.

No application/processor ports are published; Caddy uses environment-specific gateway/Auth/CMS network aliases. Processor root filesystems remain read-only except the upstream Freshclam initializer's documented narrow capability/writable-root requirements. Definition storage is environment-owned; other host/volume mounts are refused by the validator.

`scripts/release/validate-hosted.py <qa|staging|prod> <protected-rendered-config.json>` validates the Docker-rendered JSON and prints only a safe success/refusal category. It rejects mutable images, wrong env/identity paths, credential spread, extra services/networks/volumes/capabilities and broad host access. This must be integrated before any deploy command; the validator itself performs no host mutation.

Run `bash scripts/test/hosted-release.test.sh` for inert real Docker CLI render/refusal checks. Those checks do not pull/start images or prove readiness, storage processing, auth/product behavior, memory capacity, sleep/wake, pipeline operation or production acceptance. Those remain required in APEX-04 through APEX-10.
