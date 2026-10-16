# `infra/uptime-kuma/` — Uptime Kuma status page + 9 public HTTP monitors (story F-5)

> Single source of truth for the bring-up sequence is this README. The
> [`infra/release/INCIDENT-RUNBOOK.md`](../release/INCIDENT-RUNBOOK.md) §0.1
> records the chosen alert channel (Discord webhook OR email) and the live
> admin URL after the first-run wizard completes.

## What lives here

| File | Purpose | VPS path |
|---|---|---|
| `docker-compose.kuma.yml` | Hardened single-service compose file for Uptime Kuma. Joins `caddy_net` only, no host ports, image pinned to `louislam/uptime-kuma:1.23.16-alpine`. | `/srv/xynes/uptime-kuma/docker-compose.kuma.yml` |
| `README.md` | This file. | `/srv/xynes/uptime-kuma/README.md` (optional). |

The compose file is hardened with `cap_drop: [ALL]`, `no-new-privileges:true`, a single bind-mounted writable volume (`./data → /app/data` for the SQLite DB), a 64 MB tmpfs at `/tmp`, and 128M / 0.25 cpus per [`ENVIRONMENTS.md`](../release/ENVIRONMENTS.md) §6. The `caddy_net` Docker network is declared `external: true` because it is created by `vps-bootstrap.sh section_2_11_networks` (story E-5).

## Prerequisites

Before bringing Uptime Kuma up, confirm:

- **F-1 landed**: DNS for `status.erp.xynes.com` resolves to the VPS public IP. See [F-1 in the archived group-F plan](../../docs/plans/2026-05-13-mvp-release-stories/archive/group-F-caddy-dns-benchmark.md) (active stub at the non-archive path tracks only the open follow-ups).
- **F-3 landed**: Caddy is up on the prod CA and has obtained an LE cert for `status.erp.xynes.com`. Until F-3 lands, the public URL returns 502 (Caddy has no upstream).
- **F-4 landed**: Caddy is joined to `xynes_dev_net` + `xynes_qa_net` + `xynes_prod_net` declaratively. (F-4 is not a hard dependency of F-5 — Uptime Kuma sits on `caddy_net` only — but the public monitors in §3 below cover all three envs, so an env-net misroute on Caddy would surface as 9 silently-red monitors.)
- **E-4 landed**: `/srv/xynes/uptime-kuma/data/` exists with `0755 root:root` ownership. The Uptime Kuma container runs as the `node` user (uid 1000) and chowns the directory at first boot — no manual chown needed.
- **E-5 landed**: `caddy_net` Docker network exists. Verify with `docker network ls --filter name=caddy_net`.

## Per-env upstream alias contract (F-3-FU-3)

The status page is reached over Caddy via the `uptime_kuma:3001` upstream alias (with **underscore**, NOT kebab-case). The Caddyfile carries:

```caddyfile
status.erp.xynes.com {
    import security_headers_prod
    reverse_proxy uptime_kuma:3001 {
        import common_proxy_opts
    }
}
```

The compose file matches with:

```yaml
networks:
  caddy_net:
    aliases:
      - uptime_kuma
```

The offline test `scripts/test/f-5-uptime-kuma-compose.test.sh` regression-guards both directions (alias + image pin + caps + no host ports + no `:latest`); the offline test `scripts/test/f-3-caddy-staging-to-prod.test.sh` regression-guards the Caddyfile side.

## Rollout sequence (matches VPS-SETUP.md §2.14)

1. **scp** the compose file to the VPS:

    ```bash
    scp infra/uptime-kuma/docker-compose.kuma.yml \
        xynes@<VPS-IP>:/tmp/docker-compose.kuma.yml
    ```

2. **Move into place on VPS** (E-4 already created the parent directory at `/srv/xynes/uptime-kuma/` and the data subdirectory at `0755 root:root`):

    ```bash
    ssh xynes@<VPS-IP>
    sudo mv /tmp/docker-compose.kuma.yml /srv/xynes/uptime-kuma/docker-compose.kuma.yml
    sudo chown root:root /srv/xynes/uptime-kuma/docker-compose.kuma.yml
    sudo chmod 0644      /srv/xynes/uptime-kuma/docker-compose.kuma.yml
    ```

3. **Bring up Uptime Kuma**:

    ```bash
    cd /srv/xynes/uptime-kuma
    sudo docker compose -f docker-compose.kuma.yml up -d
    sudo docker compose -f docker-compose.kuma.yml ps
    # Expected: STATUS = Up X seconds (health: starting), then (healthy) after ~60s.
    sudo docker logs uptime-kuma --tail 50
    # Expected: "Listening on 3001", "Database connected".
    ```

4. **Verify Caddy can route to it** (without leaving the VPS):

    ```bash
    sudo docker exec caddy wget -qO- http://uptime_kuma:3001/ | head -c 200
    # Expected: HTML / JS bootstrap from the Uptime Kuma UI.
    ```

5. **Verify the public URL serves the status-page bootstrap** (from the operator's laptop):

    ```bash
    curl -I https://status.erp.xynes.com/
    # Expected: HTTP/2 200, server: Caddy, valid LE prod cert chain.
    # If you get 502, Uptime Kuma is not on caddy_net or the alias is wrong
    # (must be `uptime_kuma` with underscore, not `uptime-kuma`).
    ```

6. **Open `https://status.erp.xynes.com/` in a browser**. Uptime Kuma will display its first-run wizard.

7. **Complete the first-run wizard**. The admin username + password are set in this browser flow only — there is **no env var** for the bootstrap credential. Pick a strong password; store it in the operator's password manager. The credential lives in the SQLite database under `/srv/xynes/uptime-kuma/data/kuma.db`.

8. **Configure the alert channel** (one of):
    - **Discord webhook**: Settings → Notifications → Add notification → "Discord" → paste webhook URL.
    - **Email (SMTP)**: Settings → Notifications → Add notification → "Email (SMTP)". Hosted MAIL-4 Resend credentials are intentionally NOT reused here — Uptime Kuma should use a separate SMTP channel (Mailgun, AWS SES, or a dedicated Resend project) so an outage of the primary mail provider does not silently swallow alerts. Operator chooses; record the chosen channel name + admin URL in [`INCIDENT-RUNBOOK.md`](../release/INCIDENT-RUNBOOK.md) §0.1.

9. **Add the 9 HTTP(S) monitors** from §3 below.

10. **Configure the public status page** at `https://status.erp.xynes.com/status/main` (Settings → Status Pages → New status page → slug `main`, attach the 9 monitors).

## 3. The 9 public HTTP monitors

| # | Name (Uptime Kuma "Friendly Name") | URL | Interval | Type | Notification channel | Notes |
|---|---|---|---|---|---|---|
| 1 | `Dev — API gateway /health` | `https://dev-api.erp.xynes.com/health` | 60s | HTTP(s) | (chosen channel) | Expect HTTP 200 once Dev gateway is up (J-group). Until then: expect 502 — that is the F-5 acceptance signal. |
| 2 | `Dev — Auth app` | `https://dev-auth.erp.xynes.com/` | 60s | HTTP(s) | (chosen channel) | Expect HTTP 200 once Dev auth-app is up. Until then: 502. |
| 3 | `Dev — CMS console` | `https://dev-cms.erp.xynes.com/` | 60s | HTTP(s) | (chosen channel) | Expect HTTP 200 once Dev cms-console-web is up. Until then: 502. |
| 4 | `QA — API gateway /health` | `https://qa-api.erp.xynes.com/health` | 60s | HTTP(s) | (chosen channel) | QA carries a basic-auth gate (F-3). Set monitor to expect HTTP 401 OR HTTP 200 (Uptime Kuma supports "Accepted status codes: 200, 401"). Without that, the monitor will flap red on the basic-auth gate. |
| 5 | `QA — Auth app` | `https://qa-auth.erp.xynes.com/` | 60s | HTTP(s) | (chosen channel) | Same basic-auth treatment as #4. |
| 6 | `QA — CMS console` | `https://qa-cms.erp.xynes.com/` | 60s | HTTP(s) | (chosen channel) | Same basic-auth treatment as #4. |
| 7 | `Prod — API gateway /health` | `https://api.erp.xynes.com/health` | 60s | HTTP(s) | (chosen channel) | Expect HTTP 200 once Prod gateway is up. |
| 8 | `Prod — Auth app` | `https://auth.erp.xynes.com/` | 60s | HTTP(s) | (chosen channel) | Expect HTTP 200 once Prod auth-app is up. |
| 9 | `Prod — CMS console` | `https://cms.erp.xynes.com/` | 60s | HTTP(s) | (chosen channel) | Expect HTTP 200 once Prod cms-console-web is up. |

**Recommended monitor settings** (apply to all 9):
- **Retries**: 2 (Uptime Kuma re-probes twice before flipping to DOWN — reduces flap on transient TLS handshake hiccups).
- **Heartbeat retry interval**: 20s.
- **Request timeout**: 10s (fail-fast on hung upstream).
- **Accepted status codes**: `200-299` for prod + dev; `200-299, 401` for QA (basic-auth gate).
- **HTTPS — verify certificate**: enabled (catches expired LE cert before the cert reaches the user).
- **Notify when down**: enabled.
- **Notify when up**: enabled (recovery confirmation).

## 4. The public status page

After the 9 monitors are added:

1. Settings → **Status Pages** → **+ New status page**.
2. **Slug**: `main` (URL becomes `https://status.erp.xynes.com/status/main`).
3. **Friendly name**: "Xynes Status".
4. **Theme**: Auto (light/dark per visitor preference).
5. **Published**: ✓ (must be checked, otherwise the page returns 404 to the public).
6. **Show powered by**: operator preference (we recommend leaving on — Uptime Kuma is FOSS, no harm crediting it).
7. **Monitors**: drag all 9 into a single group named "Public services" (or split into "Dev" / "QA" / "Prod" groups — operator preference).
8. **Public domain**: leave blank (Caddy is the public surface; the public domain is `status.erp.xynes.com` and is set by the Caddyfile, not Uptime Kuma).
9. **Save**.

## 5. Acceptance verification (matches group-F plan §F-5 acceptance criteria)

| # | Check | Expected result |
|---|---|---|
| 1 | `docker ps --filter "name=uptime-kuma"` on the VPS | `uptime-kuma` container is running, status `(healthy)` |
| 2 | `docker network inspect caddy_net --format '{{range .Containers}}{{.Name}} {{end}}'` | Includes `caddy` AND `uptime-kuma` |
| 3 | `curl -I https://status.erp.xynes.com/` from operator laptop | HTTP/2 200 with valid LE prod cert |
| 4 | `https://status.erp.xynes.com/status/main` is reachable + lists 9 monitors | ✓ |
| 5 | All 9 monitors show DOWN (red) | ✓ — there are no upstreams yet (J-group brings them up). Red is the F-5 acceptance signal. |
| 6 | Stop a placeholder upstream → alert fires within ≤ 60s | (To reproduce after J-group brings real upstreams up: `docker stop xynes_prod_gateway`; expect the alert channel notification within the monitor interval + retry budget.) |

## 6. Backups

Uptime Kuma's state lives entirely in `/srv/xynes/uptime-kuma/data/`. Per [`BACKUP-RESTORE.md`](../release/BACKUP-RESTORE.md) §1, this directory should be included in the nightly backup snapshot. To export the SQLite DB on demand:

```bash
sudo tar -czf "/tmp/uptime-kuma-$(date -u +%Y%m%d).tar.gz" \
  -C /srv/xynes/uptime-kuma data
sudo chown root:root "/tmp/uptime-kuma-$(date -u +%Y%m%d).tar.gz"
sudo chmod 0600      "/tmp/uptime-kuma-$(date -u +%Y%m%d).tar.gz"
# Then scp to operator-controlled offline storage.
```

To restore, stop the container, replace `data/`, restart:

```bash
cd /srv/xynes/uptime-kuma
sudo docker compose -f docker-compose.kuma.yml down
sudo rm -rf data
sudo tar -xzf /path/to/uptime-kuma-YYYYMMDD.tar.gz -C .
sudo docker compose -f docker-compose.kuma.yml up -d
```

## 7. Rollback

To take Uptime Kuma down without destroying the SQLite DB:

```bash
cd /srv/xynes/uptime-kuma
sudo docker compose -f docker-compose.kuma.yml down
# /srv/xynes/uptime-kuma/data/ is preserved on the host.
```

To take Uptime Kuma down AND wipe the SQLite DB (destroys monitor history, admin user, and the public status page configuration):

```bash
cd /srv/xynes/uptime-kuma
sudo docker compose -f docker-compose.kuma.yml down
sudo rm -rf data
```

> The `down -v` flag in the F-5 story snippet is a docs shorthand. Because the data volume is a **bind mount** (`./data:/app/data`), not a Docker-managed named volume, `down -v` does NOT remove the host directory — the operator must `rm -rf data` explicitly if they want a clean reset. The hardened compose file deliberately uses bind-mount semantics so the operator's destroy step is explicit, not implicit.

## 8. Image bumps

Bumping the pinned image tag is a deliberate operator action:

1. Read the [Uptime Kuma release notes](https://github.com/louislam/uptime-kuma/releases) for the candidate tag.
2. Edit `image: louislam/uptime-kuma:<old>-alpine` → `image: louislam/uptime-kuma:<new>-alpine` in `infra/uptime-kuma/docker-compose.kuma.yml` (off the VPS).
3. Update the offline test `scripts/test/f-5-uptime-kuma-compose.test.sh` if the test pins the version literal.
4. Open a PR; CI runs the offline test.
5. After merge: scp the updated compose file to the VPS, `docker compose -f docker-compose.kuma.yml pull && up -d`.
6. Verify all 9 monitors return to green within one full check interval.

Pinned version pointer: see the `image:` line in `infra/uptime-kuma/docker-compose.kuma.yml`. The Alpine variant is preferred because it ships with BusyBox + a much smaller surface; the official `latest` and `latest-debian` tags are explicitly NOT allowed by `scripts/test/f-5-uptime-kuma-compose.test.sh`.

## 9. Cross-references

- [`infra/caddy/Caddyfile`](../caddy/Caddyfile) — the `status.erp.xynes.com` site block that fronts this container.
- [`infra/release/CADDY.md`](../release/CADDY.md) §3 — the canonical Caddyfile (the file in `infra/caddy/` is a copy).
- [`infra/release/ENVIRONMENTS.md`](../release/ENVIRONMENTS.md) §6 — resource budget.
- [`infra/release/ENVIRONMENTS.md`](../release/ENVIRONMENTS.md) §7 — reverse-proxy routing summary.
- [`infra/release/INCIDENT-RUNBOOK.md`](../release/INCIDENT-RUNBOOK.md) §0.1 — alert channel decision + live admin URL.
- [`infra/release/VPS-SETUP.md`](../release/VPS-SETUP.md) §2.14 — operator checklist.
- [`scripts/test/f-5-uptime-kuma-compose.test.sh`](../../scripts/test/f-5-uptime-kuma-compose.test.sh) — offline contract test.
