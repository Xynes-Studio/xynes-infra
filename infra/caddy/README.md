# `infra/caddy/` — Caddy reverse proxy (story F-3)

> Single source of truth for the live Caddyfile is
> [`xynes/xynes-infra/infra/release/CADDY.md`](../release/CADDY.md) §3. The
> files in this directory are operator-ready copies meant to be `scp`-d to
> `/srv/xynes/caddy/` on the VPS.

## What lives here

| File | Purpose | VPS path |
|---|---|---|
| `Caddyfile` | **Production CA** Caddyfile. Used after staging issuance has been verified. | `/srv/xynes/caddy/Caddyfile` |
| `Caddyfile.staging` | First-boot Caddyfile. Identical to `Caddyfile` plus `acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` in the global block. | `/srv/xynes/caddy/Caddyfile.staging` |
| `docker-compose.caddy.yml` | Hardened single-service compose file. Default boots with `Caddyfile.staging`. | `/srv/xynes/caddy/docker-compose.caddy.yml` |
| `README.md` | This file. | `/srv/xynes/caddy/README.md` (optional). |

The compose file is hardened: `cap_drop: [ALL]` + `cap_add: [NET_BIND_SERVICE]`, `read_only: true` rootfs, `no-new-privileges:true`, resource limits matching `ENVIRONMENTS.md` §6 (256m / 0.5 cpus), volumes scoped to `data/` + `config/` + `log/` + the two Caddyfile mounts (RO). The `caddy_net` Docker network is declared `external: true` because it is created by `vps-bootstrap.sh section_2_11_networks` (story E-5).

## Prerequisites

Before bringing Caddy up, confirm:

- **F-1 landed**: DNS for every `*.erp.xynes.com` and `*.erp.xynes.in` hostname resolves to the VPS public IP. See [F-1 in the archived group-F plan](../../docs/plans/2026-05-13-mvp-release-stories/archive/group-F-caddy-dns-benchmark.md) (active stub at `../group-F-caddy-dns-benchmark.md` tracks F-1-FU-1).
- **E-4 landed**: `/srv/xynes/caddy/{data,log}` exist with the documented mode/owner (`data/` is `0700 root:root`, `log/` is `0755 root:root`).
- **E-5 landed**: `caddy_net` Docker network exists. Verify with `docker network ls --filter name=caddy_net`.
- **Ports 80 + 443 (TCP + UDP) are open** on the VPS firewall. The E-2 `vps-bootstrap.sh` script already adds `ufw allow 80/tcp 443/tcp 443/udp`.

## Per-env upstream alias contract (F-3-FU-3)

> **Group I compose-file requirement** — when Group I authors the per-env app compose files for gateway / auth-app / cms-console-web, they MUST declare distinct upstream aliases per environment. Caddy proxies each `<env-prefix>` site block to an env-specific name, NOT a bare service name.

Why: this single Caddy container is joined to all three `xynes_<env>_net` networks by F-4 (`docker network connect`). If every env's `gateway` container shared the bare alias `gateway`, Docker's embedded DNS would round-robin non-deterministically across the three attached networks — a request meant for `qa-api.erp.xynes.com` could silently land in the dev OR prod gateway. That's a security-boundary violation of `ENVIRONMENTS.md` §3.

The Caddyfile resolves each site block to a deterministic, env-specific upstream:

| Site block | Caddy upstream alias | Compose container in env |
|---|---|---|
| `api.erp.xynes.com` (prod) | `gateway_prod:4100` | container in `xynes_prod_net` |
| `auth.erp.xynes.com` (prod) | `auth_app_prod:3100` | container in `xynes_prod_net` |
| `cms.erp.xynes.com` (prod) | `cms_console_web_prod:3000` | container in `xynes_prod_net` |
| `qa-api.erp.xynes.com` | `gateway_qa:4100` | container in `xynes_qa_net` |
| `qa-auth.erp.xynes.com` | `auth_app_qa:3100` | container in `xynes_qa_net` |
| `qa-cms.erp.xynes.com` | `cms_console_web_qa:3000` | container in `xynes_qa_net` |
| `dev-api.erp.xynes.com` | `gateway_dev:4100` | container in `xynes_dev_net` |
| `dev-auth.erp.xynes.com` | `auth_app_dev:3100` | container in `xynes_dev_net` |
| `dev-cms.erp.xynes.com` | `cms_console_web_dev:3000` | container in `xynes_dev_net` |
| `status.erp.xynes.com` | `uptime_kuma:3001` | single global instance (F-5, `caddy_net`) |

**Group I per-env compose snippet template** (one per env):

```yaml
# /srv/xynes/<env>/compose/docker-compose.<env>.yml
services:
  gateway:
    image: xynes/xynes-gateway:${XYNES_IMAGE_TAG}
    container_name: xynes_<env>_gateway
    networks:
      xynes_<env>_net:
        aliases:
          - gateway_<env>     # ← Caddy's reverse_proxy target
    # ... rest of gateway service config

  auth-app:
    image: xynes/xynes-auth-app:${XYNES_IMAGE_TAG}
    container_name: xynes_<env>_auth_app
    networks:
      xynes_<env>_net:
        aliases:
          - auth_app_<env>    # ← Caddy's reverse_proxy target
    # ... rest

  cms-console-web:
    image: xynes/xynes-cms-console-web:${XYNES_IMAGE_TAG}
    container_name: xynes_<env>_cms_console_web
    networks:
      xynes_<env>_net:
        aliases:
          - cms_console_web_<env>   # ← Caddy's reverse_proxy target
    # ... rest

networks:
  xynes_<env>_net:
    external: true
    name: xynes_<env>_net
```

The offline test `scripts/test/f-3-caddy-staging-to-prod.test.sh` regression-guards this by forbidding any bare `reverse_proxy gateway:` / `auth-app:` / `cms-console-web:` line in the Caddyfile + canonical CADDY.md §3. Group I's compose tests should mirror the assertion from the other side (`aliases: [gateway_<env>]` declared per env).

See also: [`infra/release/CADDY.md`](../release/CADDY.md) §3 (canonical Caddyfile), the [F-3-FU-3 entry in the archived group-F story doc](../../docs/plans/2026-05-13-mvp-release-stories/archive/group-F-caddy-dns-benchmark.md#f-3-fu-3).

## Generate the QA basic-auth bcrypt hash

The `Caddyfile` (and `Caddyfile.staging`) ship with a literal placeholder `$2a$14$EXAMPLEHASHREPLACEME` inside the `qa-api.erp.xynes.com` site block. Caddy will FAIL to parse the Caddyfile if this placeholder is not replaced before bring-up. Generate the real hash with the official Caddy CLI:

```bash
docker run --rm -it caddy caddy hash-password
# Prompts for password twice; prints a bcrypt hash like:
#   $2a$14$WzNuJK5W1c6cYy0X3v9eRufQz3Z4Q5e6X7Y8Z9A0B1C2D3E4F5G6H
```

Replace `$2a$14$EXAMPLEHASHREPLACEME` in BOTH `Caddyfile` and `Caddyfile.staging`. **Do not commit the real hash** to git — replace it inside the operator's scratch copy before `scp`-ing to the VPS, the same way `.env.<env>` secrets are handled in F-2.

## Rollout sequence (matches VPS-SETUP.md §2.13)

1. **scp** the three files to the VPS:

    ```bash
    scp infra/caddy/Caddyfile           xynes@<VPS-IP>:/tmp/Caddyfile
    scp infra/caddy/Caddyfile.staging   xynes@<VPS-IP>:/tmp/Caddyfile.staging
    scp infra/caddy/docker-compose.caddy.yml xynes@<VPS-IP>:/tmp/docker-compose.caddy.yml
    ```

2. **Move into place on VPS** (preserves E-4 directory ownership):

    ```bash
    ssh xynes@<VPS-IP>
    sudo mv /tmp/Caddyfile               /srv/xynes/caddy/Caddyfile
    sudo mv /tmp/Caddyfile.staging       /srv/xynes/caddy/Caddyfile.staging
    sudo mv /tmp/docker-compose.caddy.yml /srv/xynes/caddy/docker-compose.caddy.yml
    sudo chown root:root /srv/xynes/caddy/{Caddyfile,Caddyfile.staging,docker-compose.caddy.yml}
    sudo chmod 0644      /srv/xynes/caddy/{Caddyfile,Caddyfile.staging,docker-compose.caddy.yml}
    ```

3. **Create the compose-side bind-mount targets** (the compose file's relative `./data` / `./config` paths resolve under `/srv/xynes/caddy/`):

    ```bash
    sudo install -d -o root -g root -m 0700 /srv/xynes/caddy/data    # E-4 mandates 0700 root:root
    sudo install -d -o root -g root -m 0755 /srv/xynes/caddy/log     # E-4 mandates 0755 root:root
    sudo install -d -o root -g root -m 0755 /srv/xynes/caddy/config  # writable runtime snapshot
    ```

4. **Bring up Caddy on the staging CA** (default, baked into the compose command):

    ```bash
    cd /srv/xynes/caddy
    docker compose -f docker-compose.caddy.yml up -d
    docker logs -f caddy
    # Look for: "certificate obtained successfully" against the staging CA.
    ```

5. **Verify staging cert issuance** from the operator's laptop:

    ```bash
    # Cert is untrusted — that's expected for the staging CA.
    curl -kI https://api.erp.xynes.com/
    openssl s_client -connect api.erp.xynes.com:443 -servername api.erp.xynes.com </dev/null 2>/dev/null \
      | openssl x509 -noout -issuer
    # Expected issuer: "(STAGING) Let's Encrypt" / "(STAGING) Pretend Pear" / "(STAGING) Wannabe Watercress"
    ```

6. **Swap to the prod CA** in one command:

    ```bash
    docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
    ```

    Caddy re-requests every name from the production LE endpoint and atomically replaces the staging certs in `/data/caddy/certificates`. The staging certs remain in `/data` until `caddy --force-renew` is run; harmless.

    > To make the prod-CA swap survive `docker compose restart`, update the
    > compose file's `CADDY_CONFIG_FILE` env + `command` arg to point at
    > `/etc/caddy/Caddyfile` instead of `/etc/caddy/Caddyfile.staging`, then
    > `docker compose up -d` to recreate the container. The compose change
    > is intentionally an operator step (it is the "we trust prod cert
    > issuance" gate), not baked into the file shipped from this directory.

7. **Verify prod cert issuance**:

    ```bash
    curl -I https://api.erp.xynes.com/health
    openssl s_client -connect api.erp.xynes.com:443 -servername api.erp.xynes.com </dev/null 2>/dev/null \
      | openssl x509 -noout -issuer
    # Expected issuer: "C=US, O=Let's Encrypt, CN=R3" (or R10/R11/R12 — LE intermediate rotation).
    ```

8. **Connect Caddy to each env network** (story F-4 picks this up; listed here so the F-3 closure can confirm the per-env routing actually works):

    ```bash
    docker network connect xynes_dev_net  caddy
    docker network connect xynes_qa_net   caddy
    docker network connect xynes_prod_net caddy
    docker network inspect xynes_dev_net --format '{{range .Containers}}{{.Name}} {{end}}'
    # Expected: includes "caddy".
    ```

## Verification (matches the group-F plan §F-3 acceptance criteria)

| # | Check | Expected result |
|---|---|---|
| 1 | `docker ps --filter "name=caddy"` | `caddy` container is running |
| 2 | `docker network inspect xynes_prod_net --format '{{range .Containers}}{{.Name}} {{end}}'` | includes `caddy` (after step 8) |
| 3 | `curl -I https://dev-api.erp.xynes.com/health` | Returns `502` (no upstream yet — expected) with a valid Let's Encrypt cert |
| 4 | `openssl s_client -connect api.erp.xynes.com:443 -servername api.erp.xynes.com </dev/null 2>/dev/null \| openssl x509 -noout -issuer` | Issuer is the Let's Encrypt prod intermediate (not the staging `(STAGING)` prefix) |
| 5 | `curl -I https://qa-api.erp.xynes.com/health` | 401 (basic-auth) — proves the QA gate is wired |
| 6 | `curl -I https://anything.erp.xynes.in` | 301 redirect to `https://anything.erp.xynes.com` |
| 7 | `curl -I https://api.erp.xynes.com/admin` | 404 (Caddy's `deny_admin_paths` block) |

## Rollback

To take Caddy down:

```bash
cd /srv/xynes/caddy
docker compose -f docker-compose.caddy.yml down
```

The `data/` volume (ACME state + certs) is preserved across `down`. To wipe ACME state too:

```bash
sudo rm -rf /srv/xynes/caddy/data/caddy/{certificates,acme}
# Next bring-up will re-request all certs. This counts against the LE
# rate limit (5 duplicate certs / week) — only do this if a key is
# compromised.
```

DNS records do NOT need to be touched for rollback; Caddy stops accepting connections and the DNS A records simply point at an unreachable port.

## What this PR does NOT do (per the F-3 acceptance criteria)

- **Does not deploy.** The compose file + Caddyfile + Caddyfile.staging are AI-side scaffolding only. The human operator runs the `scp` + `docker compose up -d` steps. The plan's F-3 entry marks the "🤝 Mixed" ownership boundary at the same point.
- **Does not generate the bcrypt hash.** The `$2a$14$EXAMPLEHASHREPLACEME` placeholder is intentional. The operator runs `docker run --rm -it caddy caddy hash-password` (above) on their laptop and replaces the placeholder before `scp`.
- **Does not connect Caddy to env networks.** F-4 owns the `docker network connect xynes_<env>_net caddy` step + the upstream-placeholder smoke test.
- **Does not bring up Uptime Kuma.** F-5 owns the status page container; the `status.erp.xynes.com` Caddy site block already routes to `uptime-kuma:3001` once that container is up on `caddy_net`.

## See also

- [`infra/release/CADDY.md`](../release/CADDY.md) — canonical site blocks + security headers + the `xynes.in` redirect rule.
- [`infra/release/VPS-SETUP.md`](../release/VPS-SETUP.md) §2.13 — the operator checklist this rollout converges against.
- [`infra/release/ENVIRONMENTS.md`](../release/ENVIRONMENTS.md) §3 + §6 — network topology + RAM budget.
- [`docs/plans/2026-05-13-mvp-release-stories/archive/group-F-caddy-dns-benchmark.md`](../../docs/plans/2026-05-13-mvp-release-stories/archive/group-F-caddy-dns-benchmark.md) §F-3 — landed and archived 2026-06-12. Active follow-ups (incl. F-1-FU-1) live at [`group-F-caddy-dns-benchmark.md`](../../docs/plans/2026-05-13-mvp-release-stories/group-F-caddy-dns-benchmark.md).
- [`scripts/test/f-3-caddy-staging-to-prod.test.sh`](../../scripts/test/f-3-caddy-staging-to-prod.test.sh) — offline contract test that locks this directory's invariants.
