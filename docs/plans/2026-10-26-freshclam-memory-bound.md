# Freshclam memory limit: evidence and bounded validation

Status: cached signature verification passed at 768 MiB. The release repair awaits protected review and deployment; QA dispatch remains on hold.

## Evidence

The 256 MiB Freshclam container hit its own memory cgroup and the kernel killed a `sigtool` process. The retained kernel receipt (`b4690e6b2d9ba24fefb0ff5c2c390fb4b08b3b0b12a0022420413e6463ce388f`) reports anonymous RSS259684 KiB, file RSS4 KiB, and a time-near256 MiB usage/limit. The kernel victim was uid0, whereas the isolated measurement ran commands as uid1000. The exact historical command arguments and database file hashes were not retained, so its updater/verification phase and its relationship to the later corpus cannot be proved. The old full rehearsal reused the shared definitions volume and failed; that alone does not identify a particular sigtool operation. Do not call this a proven verification-only OOM.

A separately reviewed native amd64 calibration (`3c8b01ab7f9141aee3a1248b2f1dd6b3a87cdbb4d632106445c96fa2929471fb`) succeeded at CPU0.25 and a generous3 GiB leaf cap. All three cached CVD signature checks and the unchanged whole `definitions-ready.sh --verify` passed, retaining the30-second per-file timeout. The observer completed in52.912 seconds. It reported largest-process RSS101584 KiB, observer RSS12580 KiB, an initial cgroup baseline1474560 bytes and sampled cgroup current349614080 bytes. Samples are a LOWER BOUND: this kernel has no `memory.peak`. Successful verification at3 GiB is not evidence that production requires3 GiB.

## Candidate calculation

Use the stronger historical RSS even though the newer verification measurement is smaller:

| Component | MiB |
| --- | ---: |
| Historical sigtool anonymous RSS259684 KiB |253.598 |
| Observer maximum RSS12580 KiB |12.285 |
| Two compressed corpus copies225596310 bytes |215.145 |
| Observed initial cgroup baseline1474560 bytes |1.406 |
| Explicit kernel/process allowance |64.000 |
| Subtotal |546.434 |
| Subtotal with25% safety margin |683.043 |

This is a conservative planning estimate, not a mathematically exact peak: charges may overlap, and transient historical behavior is unknown. The smallest selected128 MiB increment above683.043 MiB is768 MiB. It provides84.957 MiB beyond that estimate. The initial512 MiB idea used only the smaller calibration RSS and was superseded without any512 MiB native execution; it cannot cover the stronger historical estimate.

## Essential implementation

Change only `services.clamav-freshclam.mem_limit` in hosted Compose from256m to768m. Keep CPU0.25, Clamd's2 GiB ceiling, signature verification, signed-build freshness limits, native updater recipe, timeouts, isolation and release protections unchanged. The same common setting applies to QA, staging and production; the rendering regression checks all three stacks.

One independently reviewed native 768 MiB validation passed against the same immutable image/platform and exact public corpus hashes as calibration (receipt SHA256 `ab8accc5fb52c28b21a677038746d70a8778bbff16fe6642ed2de62f72f270cf`). Kernel readback confirmed 768 MiB, zero swap and CPU0.25 under the unchanged 4 GiB/3 CPU/weight25 aggregate slice and guardian. All three signature checks and the unchanged whole freshness check passed within their existing timeouts. The attempt completed in 59.478 seconds, with no cgroup limit/OOM events, no QA changes, a complete event stream, successful owned-resource cleanup and lock release. The sampled cgroup high-water value was 214577152 bytes; it remains a lower bound, and page-cache accounting can differ between runs. This validates cached signature verification only.

## Remaining acceptance limit

This fixture does not run Freshclam's native download, delta application or atomic update path. A successful768 MiB verification validation therefore cannot establish the full updater's operational limit. End-to-end deployment still needs the real updater to become healthy with a valid success receipt under the protected production recipe; any native update failure must be diagnosed without weakening signatures, freshness or CPU/time limits. No claim that the epic or QA deployment is complete follows from this spike.
