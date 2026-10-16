# Hosted env templates

> Companion to [`../SECRETS.md`](../SECRETS.md) and [`../ENVIRONMENTS.md`](../ENVIRONMENTS.md).

This directory carries **committed, placeholder-only** templates for the runtime env files that each VPS environment loads. The **filled** versions live ONLY on the VPS, **never in git**.

## Files

| Template | Filled location (VPS) | Loaded by |
|---|---|---|
| `.env.dev.template` | `/srv/xynes/dev/env/.env.dev` | `docker compose --env-file ...` on VPS Dev |
| `.env.qa.template` | `/srv/xynes/qa/env/.env.qa` | `docker compose --env-file ...` on VPS QA |
| `.env.staging.template` | `/srv/xynes/staging/env/.env.staging` | `docker compose --env-file ...` on VPS staging |
| `.env.prod.template` | `/srv/xynes/prod/env/.env.prod` | `docker compose --env-file ...` on VPS Prod |

## Security contract

1. **Templates carry placeholders only.** Every `replace-with-...` value is intentionally non-functional. CI sweeps these files for real-secret patterns (`AKIA*`, `xynes_live_*`, `re_*`, `X-Amz-Signature`, raw JWTs, etc.) via [`../../../scripts/test/env-templates-no-secrets.test.sh`](../../../scripts/test/env-templates-no-secrets.test.sh). A failure there is a P0 leak.

2. **Filled files are mode `0640`, owner `root:xynes`, on the VPS only.** Never copied to a developer laptop, never `scp`'d off the VPS, never `cat`'d into chat or PR comments.

3. **Per-env secrets MUST be distinct.** A secret reused across Dev, QA, staging or Prod is a configuration bug. The template field naming (e.g. `STORAGE_CREDENTIAL_XYNES__STORAGE__R2_PROD_*` vs `R2_QA_*` vs `R2_DEV_*`) is designed to make accidental cross-env reuse visible at code review time.

4. **Rotation cadences live in [`../SECRETS.md` §3](../SECRETS.md).** Every secret has a documented rotation cadence and emergency rotation trigger.

## Provisioning workflow

```bash
# On a trusted operator machine (NOT the VPS):
# 1. Fork the template into a draft locally — git-ignored
cp infra/release/templates/.env.prod.template /tmp/env.prod.draft
$EDITOR /tmp/env.prod.draft   # fill placeholders

# 2. Transfer to the VPS over SSH (no intermediate disk copy)
scp /tmp/env.prod.draft <ssh-user>@<vps>:/tmp/env.prod.draft

# 3. On the VPS as root: move into place + set ownership + mode
sudo mv /tmp/env.prod.draft /srv/xynes/prod/env/.env.prod
sudo chown root:xynes /srv/xynes/prod/env/.env.prod
sudo chmod 0640 /srv/xynes/prod/env/.env.prod

# 4. Shred the local draft on the operator machine
shred -u /tmp/env.prod.draft

# 5. Deploy
sudo systemctl restart xynes-prod   # or `docker compose --env-file ... up -d`
```

## Future direction — hosted SecretManagerClient

The current MVP pattern is "filled env file at `/srv/xynes/<env>/env/.env.<env>`". The codebase ALREADY supports a vendor-neutral hosted-secrets path via the `SecretManagerClient` interface — see [`../SECRETS.md` §"Hosted secrets resolution path"](../SECRETS.md) for the migration path to AWS Secrets Manager / Doppler / Vault / Infisical / GCP Secret Manager.

### XYN-SEC-003 identity rollout (all stages)

Provision separate gateway/accounts private keys and public trust files before
rolling out the updated services. Dev includes individual mounts in its canonical
Compose. For stage-owned QA/Prod manifests apply
[`infra/compose/internal-request-identities.yml`](../../compose/internal-request-identities.yml)
last. Follow [identity ownership, UID permissions and rotation](../INTERNAL-REQUEST-IDENTITIES.md).
Shared credentials no longer authenticate protected accounts/authz actions.
