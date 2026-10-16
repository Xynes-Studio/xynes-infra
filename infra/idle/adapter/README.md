# Idle adapter foundations

Status: tested components and locally runnable private/public roles, not a deployed controller or complete sleep/wake release. See [runtime boundary and evidence](RUNTIME.md).

Stock Sablier/Caddy compatibility checks found incorrect timeout status propagation, untracked long work/streams, insufficient exact-env control and warm-routing/restart gaps. This small infra-owned adapter keeps the desired behavior: completion-based work leases, bounded readiness/admission and owned warm leases; a reverse proxy holds the lease through the whole response/stream and forwards a dispatched write once; a broker restricts lifecycle actions to exact image/container IDs and approved frontend roles.

The public proxy must run without Docker access. Compose it with a local manager whose provider is an authenticated private broker client. Only the root-owned private broker role may instantiate the Unix Docker client. The Docker client supports inspect/start/stop only, accepts full container IDs and projects no Env/mount/unknown-label data. Broker configuration currently allows audited Auth/CMS frontends in QA/staging/prod; it cannot enable data, security core or job workers by an opt-in label.

Passive health/metrics responses state `scope: ingress`; they are not application-readiness claims. Actual provider readiness and warmed product smoke remain separate checks. Request bodies/admission are bounded, raw control/provider errors are redacted, and transport failure invalidates cached readiness for the next request. The stop timer counts idle from work completion and does not stop active work/streams or owned warm leases. A stop fault disables further stops; shutdown does not stop containers.

Run from this directory with an approved test-listener environment:

```sh
GOCACHE=/private/tmp/apex-go-cache GOPATH=/private/tmp/apex-go-path \
  go test -race -count=1 -coverprofile=/private/tmp/apex-idle-adapter-coverage.out ./...
GOCACHE=/private/tmp/apex-go-cache GOPATH=/private/tmp/apex-go-path go vet ./...
```

Private lifecycle control includes stage-token authentication, a bounded strict group DTO, fixed start/stop routes and a Unix control client. A proved pre-dispatch control disconnect disables further stopping while preserving cached warm routing; ambiguous/dispatched/denied stop failures remain fail-closed. Runnable Linux roles now enforce root-authored strict config, protected tokens, root/group socket permissions and kernel peer credentials. Public ingress admits only configured exact Caddy peers and passive loopback probes.

Historical foundation checkpoint evidence: 32 tests passed with race detector/vet clean and 81.4% statement coverage; the opt-in Docker integration is skipped by default. Explicit integration 54214 passed (`/private/tmp/apex-native-idle-docker-fixture.log`): twenty first-use writes caused real starts and stops of one fresh, hardened, labelled synthetic Node container through authenticated control and actual Docker API; method/query/body/auth/status/delivery count were retained and a long real request survived its idle threshold. That container was removed. The public proxy never receives the Docker socket; this fixture's private handler uses it only for its exact approved container. This is not hosted application or OS-peer/daemon-restart acceptance. Local development compiler is Go 1.24.1; production needs a reviewed, supported patched compiler/base and its own build/scan evidence.

Current runtime verification: repeatable isolated Linux process/socket suite passes at 81.5% adapter statements with supported Go 1.27.2; separate macOS race suite and vet pass. The CLI is cross-built for linux/amd64 locally; no artifact is approved/published. Full details and coverage limits are in RUNTIME.md.

Still required before enabling sleep: actual deployment-lock integration using the durable private warm endpoint; conservative startup/external-start and stale-socket reconciliation; controller-outage/restart evidence; cancellation, WebSocket and deployment-lock integration; actual trusted ingress peer configuration; daemon/controller restart fixtures; target-architecture approved release binaries/config and CLI gate; real QA/staging/prod product/resource/cold-start acceptance. No current module wiring grants a public HTTP process the root Docker socket. Do not claim the full feature from these checks.
