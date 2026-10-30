# Apex CI Product Smoke Implementation Plan

> **For Claude:** Implement task by task with test-first changes and independent review; the active deployment coordinator owns native operations.

**Goal:** Run the existing non-storage product checks against the exact built backend images before protected publication approval, shortening release feedback while retaining hosted acceptance checks.

**Architecture:** An unprivileged-to-GitHub CI job downloads the current run's hash-bound admission, build, and migration artifacts. It verifies their controller/source/image identities, starts eight services on an isolated disposable Docker network, runs the existing product flow through a CI-only local transport, and removes its owned resources. Publishing depends on this job; no deployment credentials, real provider keys, signing authority, VPS connection, or image rebuild is involved.

**Tech Stack:** Existing Python release modules, Docker, pinned Postgres/GoTrue images, GitHub Actions, Bun workflow-policy tests.

The disposable GitHub-hosted Linux runner enables Docker's containerd image store before loading verified OCI archives. Preparation refuses existing containers, preserves other daemon configuration, and verifies the store after restart. It never runs on the VPS or a self-hosted runner. This keeps the original OCI bytes and image identities intact instead of rebuilding or converting the images.

## 1. Strict new storage acceptance, compatible historical reads

Files: `scripts/release/host_release_state.py`, `scripts/test/host-release-state.test.py`.

Add failing tests for missing storage flag on new QA/staging/prod acceptance and missing/false storage verification on staging promotion. Preserve an explicit legacy-read case. Require a boolean on new acceptance (false allowed only for QA) and true on the staging record used for promotion. Run the state tests and existing executor tests.

## 2. Exact build-artifact intake

Files: new `scripts/release/ci_smoke.py`, new `scripts/test/ci-smoke.test.py`.

Write failing tests for mismatched controller, admission/build/migration references, image inventory, digest/config/platform, and missing QA/backend images. Reuse `artifact_handoff`, `release_manifest`, `source_context`, and `image_build.inspect_oci`; keep receipt verification ahead of Docker execution. Never execute source from an artifact. Load the verified OCI bytes and check Docker's loaded identity; use owned local tags only after verification. Keep the selected non-storage images immutable throughout the smoke.

## 3. Disposable runtime and real product flow

Files: `scripts/release/ci_smoke.py` (split a helper only if necessary), `scripts/test/ci-smoke.test.py`.

Reuse migration/seed modules and smoke fixture provisioning. Generate job-local dummy secrets and signed service identities. Start Postgres, GoTrue, gateway, accounts, authz, CMS, document, and telemetry services with bounded readiness. Map only the QA gateway/auth origins to loopback endpoints in a CI-only transport; retain existing hosted HTTPS checks unchanged. Run auth/session, workspace isolation/denial, directory, document, Lexical entry publish/read, and cleanup checks. Record storage and browser/hosted behavior as unverified. Prove failure and cleanup paths with fixtures, then exercise the real eight-service job in CI; fixture success alone is insufficient.

## 4. Workflow and policy gate

Files: `.github/workflows/apex-build.yml`, `scripts/lib/apex-ci-policy.ts`, `scripts/test/apex-workflow.test.py`, `scripts/test/apex-ci-policy.test.ts`, existing quality-gate profile files as needed.

Add failing graph/policy tests first. Add `product-smoke` after image building with contents/actions read permissions, no protected environment or secrets. Make publishing require admission, image building, and product smoke. Preserve publisher and signer permissions, independent release approval, protected main, and signing isolation. Bind sanitized smoke results to the current controller and native artifact references; upload no credentials, raw service logs, or database contents.

## 5. Review and validation

Run the focused Python and Bun tests, applicable repository quality/security gates, and an independent review of the final diff. Do not open a PR that claims a fake-transport check is the real built-service gate. Before a protected merge, obtain the existing required independent human approval and passing checks. QA dispatch remains on hold; this change does not authorize it.

## Acceptance and exclusions

A failing real product smoke prevents publication from reaching its release approval. A passing job identifies the exact tested build inputs and leaves no owned containers/network/data behind. Storage verification remains deferred for QA; staging/production still require it. Hosted TLS/DNS, backups/recovery, capacity, idle sleep/wake, promotion and soak remain separate requirements of the deployment epic.
