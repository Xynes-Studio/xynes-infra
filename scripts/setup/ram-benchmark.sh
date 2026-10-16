#!/usr/bin/env bash
# ram-benchmark.sh — Story F-6 (MVP release plan, Phase 2 finish).
#
# Generates light synthetic traffic against the three per-env gateway /health
# endpoints, captures memory + container footprint over the run, and writes a
# PASS / BORDERLINE / FAIL verdict per QA-HIBERNATE.md §2:
#
#   PASS         peak RAM ≤ 90% AND no OOM-kill noted in dmesg
#   BORDERLINE   peak RAM 90–95% AND no OOM-kill noted in dmesg
#   FAIL         peak RAM > 95% OR any OOM-kill noted in dmesg
#
# Output artefact:
#   /srv/xynes/benchmark-<YYYYMMDD>.md   (default; override with --output)
#
# The artefact contains:
#   - Start / end UTC timestamps + benchmark parameters.
#   - `free -h` log captured every <interval> seconds while traffic runs.
#   - `docker stats --no-stream` snapshot per container at the end.
#   - dmesg sweep for the last hour looking for `Out of memory` / `oom-kill`.
#   - Verdict block with the percentages that drove the decision.
#
# Inputs / Resources:
#   - QA-HIBERNATE.md §2 (acceptance thresholds).
#   - ENVIRONMENTS.md §6 (per-service RAM budgets — context for the operator).
#
# Tool preference:
#   1. `hey` (preferred — handles -z duration + -q QPS natively).
#   2. `ab` (Apache Bench fallback — sized via `-t` + `-c`; rate approximated).
#   3. None → fail loudly with install instructions.
#
# Behaviour contract:
#   - Bash strict mode (`set -euo pipefail`).
#   - `--dry-run` prints the plan + tool selection + target list and exits 0
#     with NO side effects (no traffic, no /srv/xynes write, no docker call).
#   - `--dry-run` is the documented preview path; it never requires root.
#   - Apply mode requires write access to the output directory (default
#     `/srv/xynes/` → typically root).
#   - The script runs the benchmark in the FOREGROUND. Background loaders
#     are wait-ed on; SIGINT / SIGTERM cleans them up.
#   - The verdict is computed even if some loaders failed; their per-target
#     summary is included in the artefact so the operator can see partial
#     coverage.
#   - DO NOT run this script blindly on the live VPS before reading a
#     `--dry-run` output. The companion test in
#     scripts/test/ram-benchmark.test.sh exercises --help + --dry-run paths.
#
# Usage:
#   bash scripts/setup/ram-benchmark.sh --help
#   bash scripts/setup/ram-benchmark.sh --dry-run
#   sudo bash scripts/setup/ram-benchmark.sh                  # apply, defaults
#   sudo bash scripts/setup/ram-benchmark.sh --duration 60 --rps 2 \
#        --targets https://dev-api.erp.xynes.com/health
#
# Companion docs:
#   - infra/release/QA-HIBERNATE.md §2 (the verdict consumer).
#   - infra/release/VPS-SETUP.md §2.19 (the operator checklist row).
#   - docs/plans/2026-05-13-mvp-release-stories/group-F-caddy-dns-benchmark.md (the story).

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

# Default targets: the three per-env public gateway /health endpoints. These
# are the same hostnames documented in CADDY.md §3 + Uptime Kuma's 3 `<env>-api`
# monitors. Operators can override with --targets (comma-separated).
DEFAULT_TARGETS="https://dev-api.erp.xynes.com/health,https://qa-api.erp.xynes.com/health,https://api.erp.xynes.com/health"

# Benchmark window. 600s × 10 RPS per target is the documented QA-HIBERNATE.md
# §2 procedure ("Light: 10 RPS per env for 10 minutes"). The defaults match.
DEFAULT_DURATION_SECONDS=600
DEFAULT_RPS=10

# Memory sampling interval (seconds). 30s matches QA-HIBERNATE.md §2.
DEFAULT_SAMPLE_INTERVAL_SECONDS=30

# Output dir. Override with --output. The dated filename is computed at
# apply-time so re-runs on the same day overwrite atomically.
DEFAULT_OUTPUT_DIR="/srv/xynes"

# Verdict thresholds (percent of total RAM used). Keep these in lockstep with
# QA-HIBERNATE.md §2 — the offline test enforces the literals.
PASS_THRESHOLD_PERCENT=90
BORDERLINE_THRESHOLD_PERCENT=95

# Mode + parameters (filled in by argv parser).
MODE="apply"                                          # apply | dry-run
DURATION_SECONDS="$DEFAULT_DURATION_SECONDS"
RPS="$DEFAULT_RPS"
SAMPLE_INTERVAL_SECONDS="$DEFAULT_SAMPLE_INTERVAL_SECONDS"
TARGETS_RAW="$DEFAULT_TARGETS"
OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
OUTPUT_OVERRIDE=""                                    # explicit --output <file>

# Tool selection: filled in by detect_load_tool().
LOAD_TOOL=""                                          # hey | ab | (empty until detected)

# Background loader PIDs (cleaned up on signal).
declare -a LOADER_PIDS=()
SAMPLER_PID=""

# ------------------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------------------

print_usage() {
  cat <<EOF
$SCRIPT_NAME — RAM benchmark gate (Story F-6).

Drive light synthetic traffic against the per-env gateway /health endpoints,
capture memory + container footprint, and emit a PASS / BORDERLINE / FAIL
verdict per QA-HIBERNATE.md §2.

USAGE:
  $SCRIPT_NAME [--duration <seconds>] [--rps <int>] [--targets <url,url,...>]
               [--interval <seconds>] [--output <path>] [--dry-run]
  $SCRIPT_NAME --help

OPTIONS:
  --duration <seconds>   Length of the traffic window. Default: $DEFAULT_DURATION_SECONDS.
                         The QA-HIBERNATE.md §2 procedure uses 600 (10 min).
  --rps <int>            Requests per second per target. Default: $DEFAULT_RPS.
                         The QA-HIBERNATE.md §2 procedure uses 10.
  --targets <list>       Comma-separated list of full URLs to load. Default:
                           $DEFAULT_TARGETS
                         When env stacks aren't up, Caddy answers 502 — that
                         still measures Caddy + Uptime Kuma + Supabase footprint
                         and is the documented F-6 fallback.
  --interval <seconds>   \`free -h\` sampling cadence. Default: $DEFAULT_SAMPLE_INTERVAL_SECONDS.
  --output <path>        Absolute output file path. Default:
                           $DEFAULT_OUTPUT_DIR/benchmark-<YYYYMMDD>.md
                         (UTC date stamp resolved at apply time.)
  --dry-run              Print plan + tool selection + target list and exit 0
                         with NO side effects. Safe to run as a non-root user.
  --help, -h             Print this usage and exit 0.

VERDICT THRESHOLDS (QA-HIBERNATE.md §2):
  PASS        peak RAM ≤ ${PASS_THRESHOLD_PERCENT}% AND no OOM-kill in dmesg.
  BORDERLINE  peak RAM ${PASS_THRESHOLD_PERCENT}–${BORDERLINE_THRESHOLD_PERCENT}% AND no OOM-kill in dmesg.
  FAIL        peak RAM > ${BORDERLINE_THRESHOLD_PERCENT}% OR any OOM-kill in dmesg.

LOAD TOOL SELECTION:
  1. \`hey\`  preferred. Install: \`go install github.com/rakyll/hey@latest\`
                                 or, on Debian/Ubuntu: \`sudo apt install hey\`.
  2. \`ab\`   fallback (Apache Bench). On Debian/Ubuntu:
                                 \`sudo apt install apache2-utils\`.

EXIT CODES:
  0   Apply or dry-run completed (verdict written to the output file).
      Note: a FAIL verdict is still a successful run from the script's POV —
      check the artefact for the verdict line.
  1   Misuse: bad flag value, missing required tool, or unwritable output.
  64  Unknown flag.

EXAMPLES:
  # Preview the plan; no traffic, no /srv write.
  bash $SCRIPT_NAME --dry-run

  # Apply with the documented QA-HIBERNATE.md §2 parameters.
  sudo bash $SCRIPT_NAME

  # Short smoke against a single target (e.g. before/after a tuning change).
  sudo bash $SCRIPT_NAME --duration 60 --rps 2 \\
       --targets https://dev-api.erp.xynes.com/health

  # Direct the artefact somewhere else (e.g. for archival).
  sudo bash $SCRIPT_NAME --output /srv/xynes/benchmark-baseline.md
EOF
}

# ------------------------------------------------------------------------------
# Argv parsing
# ------------------------------------------------------------------------------

require_positive_int() {
  # require_positive_int <label> <value>
  local label="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || [[ "$value" -lt 1 ]]; then
    echo "ERROR: $label must be a positive integer (got: $value)" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      [[ $# -ge 2 ]] || { echo "ERROR: --duration requires a value" >&2; exit 1; }
      DURATION_SECONDS="$2"
      shift 2
      ;;
    --duration=*)
      DURATION_SECONDS="${1#*=}"
      shift
      ;;
    --rps)
      [[ $# -ge 2 ]] || { echo "ERROR: --rps requires a value" >&2; exit 1; }
      RPS="$2"
      shift 2
      ;;
    --rps=*)
      RPS="${1#*=}"
      shift
      ;;
    --targets)
      [[ $# -ge 2 ]] || { echo "ERROR: --targets requires a value" >&2; exit 1; }
      TARGETS_RAW="$2"
      shift 2
      ;;
    --targets=*)
      TARGETS_RAW="${1#*=}"
      shift
      ;;
    --interval)
      [[ $# -ge 2 ]] || { echo "ERROR: --interval requires a value" >&2; exit 1; }
      SAMPLE_INTERVAL_SECONDS="$2"
      shift 2
      ;;
    --interval=*)
      SAMPLE_INTERVAL_SECONDS="${1#*=}"
      shift
      ;;
    --output)
      [[ $# -ge 2 ]] || { echo "ERROR: --output requires a value" >&2; exit 1; }
      OUTPUT_OVERRIDE="$2"
      shift 2
      ;;
    --output=*)
      OUTPUT_OVERRIDE="${1#*=}"
      shift
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --help|-h)
      print_usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown flag: $1" >&2
      echo "Run '$SCRIPT_NAME --help' for usage." >&2
      exit 64
      ;;
  esac
done

require_positive_int "--duration" "$DURATION_SECONDS"
require_positive_int "--rps" "$RPS"
require_positive_int "--interval" "$SAMPLE_INTERVAL_SECONDS"

if [[ "$SAMPLE_INTERVAL_SECONDS" -gt "$DURATION_SECONDS" ]]; then
  echo "ERROR: --interval ($SAMPLE_INTERVAL_SECONDS) cannot exceed --duration ($DURATION_SECONDS)" >&2
  exit 1
fi

# ------------------------------------------------------------------------------
# Targets — split + validate
# ------------------------------------------------------------------------------

# Split TARGETS_RAW on `,` into TARGETS[]. Empty entries are rejected.
IFS=',' read -r -a TARGETS <<<"$TARGETS_RAW"
if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  echo "ERROR: --targets produced an empty list" >&2
  exit 1
fi
for t in "${TARGETS[@]}"; do
  # Trim whitespace.
  t_trimmed="${t##[[:space:]]}"
  t_trimmed="${t_trimmed%%[[:space:]]}"
  if [[ -z "$t_trimmed" ]]; then
    echo "ERROR: --targets contains an empty entry" >&2
    exit 1
  fi
  if [[ ! "$t_trimmed" =~ ^https?:// ]]; then
    echo "ERROR: target must start with http:// or https:// (got: $t_trimmed)" >&2
    exit 1
  fi
done

# ------------------------------------------------------------------------------
# Output path resolution
# ------------------------------------------------------------------------------

DATE_STAMP="$(date -u +%Y%m%d)"
if [[ -n "$OUTPUT_OVERRIDE" ]]; then
  OUTPUT_FILE="$OUTPUT_OVERRIDE"
else
  OUTPUT_FILE="$OUTPUT_DIR/benchmark-$DATE_STAMP.md"
fi

# ------------------------------------------------------------------------------
# Load-tool detection
# ------------------------------------------------------------------------------

detect_load_tool() {
  if command -v hey >/dev/null 2>&1; then
    LOAD_TOOL="hey"
    return 0
  fi
  if command -v ab >/dev/null 2>&1; then
    LOAD_TOOL="ab"
    return 0
  fi
  return 1
}

# ------------------------------------------------------------------------------
# Cleanup on signal
# ------------------------------------------------------------------------------

# shellcheck disable=SC2329 # invoked indirectly via `trap`
cleanup() {
  local rc=$?
  if [[ -n "$SAMPLER_PID" ]] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
  for pid in "${LOADER_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  exit "$rc"
}
trap cleanup INT TERM

# ------------------------------------------------------------------------------
# Dry-run path — no side effects.
# ------------------------------------------------------------------------------

if [[ "$MODE" == "dry-run" ]]; then
  # Detect tool but tolerate absence (still print a clear plan).
  if detect_load_tool; then
    TOOL_NOTE="$LOAD_TOOL (detected on PATH)"
  else
    TOOL_NOTE="NONE detected — apply mode would fail until 'hey' or 'ab' is installed"
  fi

  cat <<EOF
WOULD: ram-benchmark plan
  mode:        dry-run
  duration:    ${DURATION_SECONDS}s
  rps:         $RPS per target
  interval:    ${SAMPLE_INTERVAL_SECONDS}s ('free -h' sampling cadence)
  targets:     ${#TARGETS[@]} URL(s)
EOF
  for t in "${TARGETS[@]}"; do
    echo "                 - $t"
  done
  cat <<EOF
  output:      $OUTPUT_FILE
  load tool:   $TOOL_NOTE
  thresholds:  PASS ≤ ${PASS_THRESHOLD_PERCENT}% / BORDERLINE ${PASS_THRESHOLD_PERCENT}–${BORDERLINE_THRESHOLD_PERCENT}% / FAIL > ${BORDERLINE_THRESHOLD_PERCENT}%

WOULD: per-target load (background)
EOF
  for t in "${TARGETS[@]}"; do
    if [[ "$TOOL_NOTE" == hey* ]]; then
      echo "  WOULD: hey -z ${DURATION_SECONDS}s -q $RPS -c 4 -disable-keepalive=false $t"
    elif [[ "$TOOL_NOTE" == ab* ]]; then
      echo "  WOULD: ab -t $DURATION_SECONDS -c 4 -k -n 100000000 $t"
    else
      echo "  WOULD: <load tool>  $t"
    fi
  done
  cat <<EOF

WOULD: sample memory every ${SAMPLE_INTERVAL_SECONDS}s for ${DURATION_SECONDS}s
  WOULD: free -h            (each sample)
  WOULD: docker stats --no-stream   (once, at end)
  WOULD: dmesg | grep -i 'out of memory\\|oom-kill'

WOULD: write verdict to $OUTPUT_FILE
EOF
  exit 0
fi

# ------------------------------------------------------------------------------
# Apply mode — guard rails first.
# ------------------------------------------------------------------------------

if ! detect_load_tool; then
  cat >&2 <<EOF
ERROR: no load tool found on PATH.

Install one of:
  hey  (preferred)  go install github.com/rakyll/hey@latest
                    or, on Debian/Ubuntu: sudo apt install hey
  ab   (fallback)   on Debian/Ubuntu:     sudo apt install apache2-utils

Then re-run: $SCRIPT_NAME
EOF
  exit 1
fi

# Ensure the output directory exists and is writable.
OUTPUT_PARENT="$(dirname "$OUTPUT_FILE")"
if [[ ! -d "$OUTPUT_PARENT" ]]; then
  echo "ERROR: output parent does not exist: $OUTPUT_PARENT" >&2
  echo "Create it first: sudo install -d -o root -g root -m 0755 $OUTPUT_PARENT" >&2
  exit 1
fi
if [[ ! -w "$OUTPUT_PARENT" ]]; then
  echo "ERROR: output parent is not writable: $OUTPUT_PARENT (run as root?)" >&2
  exit 1
fi

# `free` is essential for the memory sample. Without it we cannot produce a
# verdict; bail loudly rather than write a useless artefact.
if ! command -v free >/dev/null 2>&1; then
  echo "ERROR: 'free' is required but not on PATH (procps package)." >&2
  exit 1
fi

# Memory-stat samples accumulate here.
TMP_DIR="$(mktemp -d -t ram-benchmark.XXXXXX)"
MEM_LOG="$TMP_DIR/mem.log"
LOADER_LOG_DIR="$TMP_DIR/loaders"
mkdir -p "$LOADER_LOG_DIR"

# Belt-and-braces: tmp dir cleanup is part of the trap.
# shellcheck disable=SC2329 # invoked indirectly via `trap`
cleanup_with_tmp() {
  local rc=$?
  if [[ -n "$SAMPLER_PID" ]] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
  for pid in "${LOADER_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  # Preserve the tmp dir if we exit non-zero so the operator can inspect.
  if [[ $rc -eq 0 ]]; then
    rm -rf "$TMP_DIR" 2>/dev/null || true
  else
    echo "NOTE: preserving $TMP_DIR for debugging (rc=$rc)" >&2
  fi
  exit "$rc"
}
trap cleanup_with_tmp INT TERM EXIT

# ------------------------------------------------------------------------------
# Memory sampler — runs in the background; writes one record per interval.
# ------------------------------------------------------------------------------

run_sampler() {
  local total="$1"
  local interval="$2"
  local out="$3"
  local elapsed=0
  local total_mb used_mb
  while [[ "$elapsed" -lt "$total" ]]; do
    # Capture a numeric MB sample per tick. The closure docs + Codex P1
    # contract require deriving peak RAM from in-run samples (NOT a post-
    # traffic single snapshot which can miss a spike that decays before
    # loaders exit). Format is fixed-position so the peak walker can parse it
    # with cheap awk without re-running `free`.
    read -r total_mb used_mb < <(free -m | awk '/^Mem:/ {print $2, $3}')
    {
      printf -- "----- t=%ds (UTC %s) -----\n" "$elapsed" "$(date -u +%H:%M:%S)"
      printf "MEMSAMPLE total_mb=%s used_mb=%s\n" "${total_mb:-0}" "${used_mb:-0}"
      free -h
      printf "\n"
    } >>"$out"
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
}

# ------------------------------------------------------------------------------
# Per-target loader — spawned per target, runs in the background.
# ------------------------------------------------------------------------------

run_loader() {
  local target="$1"
  local out="$2"
  case "$LOAD_TOOL" in
    hey)
      # -z duration, -q RPS, -c 4 concurrency floor for the runner.
      hey -z "${DURATION_SECONDS}s" -q "$RPS" -c 4 "$target" >"$out" 2>&1 || true
      ;;
    ab)
      # ab cannot rate-limit natively. Approximate via -t (time bound) and a
      # large -n cap; concurrency 4 mirrors `hey`'s default.
      ab -t "$DURATION_SECONDS" -c 4 -k -n 100000000 "$target" >"$out" 2>&1 || true
      ;;
  esac
}

# ------------------------------------------------------------------------------
# Run the benchmark.
# ------------------------------------------------------------------------------

START_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Pre-traffic baseline sample (t=-1) so the in-run peak walker always has at
# least one data point — even if a loader fails fast before the first sampler
# tick lands.
{
  printf -- "----- pre-traffic baseline (UTC %s) -----\n" "$(date -u +%H:%M:%S)"
  _baseline_pair="$(free -m | awk '/^Mem:/ {print $2, $3}')"
  _baseline_total="$(echo "$_baseline_pair" | awk '{print $1}')"
  _baseline_used="$(echo "$_baseline_pair" | awk '{print $2}')"
  printf "MEMSAMPLE total_mb=%s used_mb=%s\n" "${_baseline_total:-0}" "${_baseline_used:-0}"
  free -h
  printf "\n"
} >>"$MEM_LOG"

# OOM-kill BASELINE cursor (Codex P2 contract): record current OOM-line count
# from the FULL kernel ring buffer BEFORE the load run, so the post-run sweep
# can report only NEW entries from this benchmark window. Stale dmesg entries
# from earlier hours / days / boots must NOT trip a FAIL verdict.
OOM_BASELINE_COUNT=0
if command -v dmesg >/dev/null 2>&1; then
  if _dmesg_baseline="$(dmesg 2>/dev/null)"; then
    OOM_BASELINE_COUNT=$(printf "%s\n" "$_dmesg_baseline" \
      | grep -cE -i 'out of memory|oom[-_]kill|killed process' 2>/dev/null || echo 0)
  fi
fi

# Start the memory sampler.
run_sampler "$DURATION_SECONDS" "$SAMPLE_INTERVAL_SECONDS" "$MEM_LOG" &
SAMPLER_PID=$!

# Start one loader per target.
i=0
for t in "${TARGETS[@]}"; do
  loader_out="$LOADER_LOG_DIR/loader-$i.log"
  run_loader "$t" "$loader_out" &
  LOADER_PIDS+=("$!")
  i=$((i + 1))
done

# Wait for every loader to finish (sampler will exit on its own when the
# duration elapses).
for pid in "${LOADER_PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

# Sampler should be done by now (interval × steps ≤ duration). Give it 5s
# of grace then signal it.
sleep 1
if kill -0 "$SAMPLER_PID" 2>/dev/null; then
  wait "$SAMPLER_PID" 2>/dev/null || true
fi

END_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# ------------------------------------------------------------------------------
# Compute peak RAM percentage (Codex P1 contract).
# ------------------------------------------------------------------------------
# Strategy: walk every MEMSAMPLE line written by run_sampler() during the load
# window — including the t=-1 pre-traffic baseline — and pick the maximum
# used_mb/total_mb ratio. The verdict gates on the in-run peak, NOT a post-
# traffic single snapshot, because RAM can spike above threshold during the
# run and decay before loaders exit; the post-run snapshot would silently
# report PASS while the run actually failed its own acceptance criteria.
#
# Captured for the artefact: PEAK_PCT (integer %, rounded down for the
# threshold compare), PEAK_USED_MB, PEAK_TOTAL_MB, PEAK_SOURCE (provenance —
# `in-run-samples` on the happy path, `post-run-snapshot` when the sampler
# log is unavailable so the operator knows the verdict's data quality).

PEAK_PCT=0
PEAK_USED_MB=0
PEAK_TOTAL_MB=0
PEAK_SAMPLE_COUNT=0
PEAK_SOURCE="in-run-samples"

if [[ -s "$MEM_LOG" ]]; then
  # awk picks the line with the largest used*100/total, rounded down to int.
  # Skips malformed rows (total=0) defensively so a degenerate first sample
  # cannot poison the max.
  read -r PEAK_USED_MB PEAK_TOTAL_MB PEAK_PCT PEAK_SAMPLE_COUNT < <(
    awk '
      /^MEMSAMPLE total_mb=/ {
        total = 0; used = 0
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^total_mb=/)  { sub(/^total_mb=/, "", $i); total = $i + 0 }
          if ($i ~ /^used_mb=/)   { sub(/^used_mb=/,  "", $i); used  = $i + 0 }
        }
        if (total > 0) {
          n++
          pct = int((used * 100) / total)
          if (pct > best_pct) {
            best_pct   = pct
            best_used  = used
            best_total = total
          }
        }
      }
      END { printf "%d %d %d %d\n", best_used + 0, best_total + 0, best_pct + 0, n + 0 }
    ' "$MEM_LOG"
  )
fi

# Fallback ONLY if the sampler log carried no usable MEMSAMPLE rows (sampler
# crashed before any tick landed). Surface the degraded provenance in
# PEAK_SOURCE so the operator reading the artefact can flag it.
if [[ "$PEAK_SAMPLE_COUNT" -eq 0 ]]; then
  PEAK_SOURCE="post-run-snapshot (sampler unavailable)"
  FREE_RAW="$(free -m | awk '/^Mem:/ {print $2, $3}')"
  PEAK_TOTAL_MB="$(echo "$FREE_RAW" | awk '{print $1}')"
  PEAK_USED_MB="$(echo "$FREE_RAW" | awk '{print $2}')"
  if [[ "${PEAK_TOTAL_MB:-0}" -gt 0 ]]; then
    PEAK_PCT=$(( (PEAK_USED_MB * 100) / PEAK_TOTAL_MB ))
  fi
fi

# Kept for backwards compatibility with the artefact prose (the verdict block
# below references MEM_TOTAL_MB / MEM_USED_MB in some downstream callers).
MEM_TOTAL_MB="$PEAK_TOTAL_MB"
MEM_USED_MB="$PEAK_USED_MB"

# ------------------------------------------------------------------------------
# OOM-kill sweep (bounded to this benchmark window — Codex P2 contract).
# ------------------------------------------------------------------------------
# The grep pattern stays the same (the offline test asserts the literal regex
# `out of memory|oom[-_]kill|killed process`), but the COUNT is bounded:
# we report `current_total - OOM_BASELINE_COUNT` (captured pre-traffic above),
# so stale dmesg entries from earlier hours/days/boots do NOT flip a clean
# benchmark to FAIL and incorrectly activate the Supabase fallback path.
# Detail lines surfaced in the artefact are limited to the *new* entries from
# this run (tail to the delta count) so the operator sees only run-relevant
# evidence.

OOM_HITS=0
OOM_DETAIL=""
if command -v dmesg >/dev/null 2>&1; then
  # Some hosts restrict dmesg to root — we tolerate failure.
  if dmesg_out="$(dmesg 2>/dev/null)"; then
    OOM_ALL="$(printf "%s\n" "$dmesg_out" | grep -iE 'out of memory|oom[-_]kill|killed process' 2>/dev/null || true)"
    OOM_TOTAL_COUNT=0
    if [[ -n "$OOM_ALL" ]]; then
      OOM_TOTAL_COUNT=$(printf "%s\n" "$OOM_ALL" | wc -l | tr -d ' ')
    fi
    # Delta = total - baseline. Clamp negative to 0 (defensive: dmesg can be
    # rotated mid-run on hosts with very small ring buffers).
    OOM_HITS=$(( OOM_TOTAL_COUNT - OOM_BASELINE_COUNT ))
    if [[ "$OOM_HITS" -lt 0 ]]; then
      OOM_HITS=0
    fi
    if [[ "$OOM_HITS" -gt 0 ]]; then
      # Surface only the new (delta) entries from this run, capped at 20 lines.
      _tail_n=$(( OOM_HITS < 20 ? OOM_HITS : 20 ))
      OOM_DETAIL="$(printf "%s\n" "$OOM_ALL" | tail -n "$_tail_n")"
    elif [[ "$OOM_BASELINE_COUNT" -gt 0 ]]; then
      OOM_DETAIL="(0 new OOM lines in this run; $OOM_BASELINE_COUNT pre-existing dmesg entries ignored)"
    fi
  else
    OOM_DETAIL="(dmesg unavailable to this user — run with sudo for OOM detection)"
  fi
else
  OOM_DETAIL="(dmesg not on PATH — cannot scan for OOM-kill)"
fi

# ------------------------------------------------------------------------------
# Verdict.
# ------------------------------------------------------------------------------

VERDICT="PASS"
VERDICT_REASON="peak RAM ${PEAK_PCT}% ≤ ${PASS_THRESHOLD_PERCENT}% and no OOM-kill detected"
if [[ "$OOM_HITS" -gt 0 ]]; then
  VERDICT="FAIL"
  VERDICT_REASON="OOM-kill detected in dmesg ($OOM_HITS line(s))"
elif [[ "$PEAK_PCT" -gt "$BORDERLINE_THRESHOLD_PERCENT" ]]; then
  VERDICT="FAIL"
  VERDICT_REASON="peak RAM ${PEAK_PCT}% > ${BORDERLINE_THRESHOLD_PERCENT}%"
elif [[ "$PEAK_PCT" -gt "$PASS_THRESHOLD_PERCENT" ]]; then
  VERDICT="BORDERLINE"
  VERDICT_REASON="peak RAM ${PEAK_PCT}% in the ${PASS_THRESHOLD_PERCENT}–${BORDERLINE_THRESHOLD_PERCENT}% window"
fi

# ------------------------------------------------------------------------------
# Docker stats snapshot (best-effort).
# ------------------------------------------------------------------------------

DOCKER_STATS=""
if command -v docker >/dev/null 2>&1; then
  if DOCKER_STATS="$(docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' 2>&1)"; then
    :
  else
    DOCKER_STATS="(docker stats failed — is the daemon up? error: $DOCKER_STATS)"
  fi
else
  DOCKER_STATS="(docker not on PATH — container snapshot skipped)"
fi

# ------------------------------------------------------------------------------
# Write artefact.
# ------------------------------------------------------------------------------

{
  cat <<EOF
# RAM benchmark — $START_UTC

Story F-6 (MVP release plan, Phase 2 finish).
Procedure: QA-HIBERNATE.md §2.

## Parameters

| Field | Value |
|---|---|
| Start (UTC) | $START_UTC |
| End (UTC)   | $END_UTC |
| Duration    | ${DURATION_SECONDS}s |
| RPS per target | $RPS |
| Sample interval | ${SAMPLE_INTERVAL_SECONDS}s |
| Load tool | $LOAD_TOOL |
| Targets | ${#TARGETS[@]} URL(s) |

Targets:
EOF
  for t in "${TARGETS[@]}"; do
    echo "- $t"
  done

  cat <<EOF

## Verdict

**$VERDICT** — $VERDICT_REASON

Thresholds (QA-HIBERNATE.md §2):
- PASS:       peak RAM ≤ ${PASS_THRESHOLD_PERCENT}% AND no OOM-kill
- BORDERLINE: peak RAM ${PASS_THRESHOLD_PERCENT}–${BORDERLINE_THRESHOLD_PERCENT}% AND no OOM-kill
- FAIL:       peak RAM > ${BORDERLINE_THRESHOLD_PERCENT}% OR any OOM-kill

Peak RAM: ${PEAK_USED_MB}MB used / ${PEAK_TOTAL_MB}MB total = ${PEAK_PCT}%.
Peak source: ${PEAK_SOURCE} (${PEAK_SAMPLE_COUNT} sample(s) considered).
OOM baseline cursor: ${OOM_BASELINE_COUNT} pre-existing dmesg line(s) ignored; ${OOM_HITS} new line(s) attributed to this run.

## memory sample log (\`free -h\` every ${SAMPLE_INTERVAL_SECONDS}s)

\`\`\`
EOF
  cat "$MEM_LOG"
  cat <<EOF
\`\`\`

## docker stats --no-stream (post-run)

\`\`\`
$DOCKER_STATS
\`\`\`

## OOM-kill scan (dmesg)

\`\`\`
$( [[ -n "$OOM_DETAIL" ]] && printf "%s\n" "$OOM_DETAIL" || echo "(no OOM-kill / out-of-memory messages found)" )
\`\`\`

## Per-target loader output

EOF
  i=0
  for t in "${TARGETS[@]}"; do
    loader_out="$LOADER_LOG_DIR/loader-$i.log"
    echo "### $t"
    echo
    echo '```'
    if [[ -f "$loader_out" ]]; then
      # Truncate to last 50 lines per loader — full output is too verbose.
      tail -n 50 "$loader_out"
    else
      echo "(loader log missing)"
    fi
    echo '```'
    echo
    i=$((i + 1))
  done

  cat <<EOF
## Next step

Record this verdict in \`infra/release/QA-HIBERNATE.md\` §2 (date + headline numbers).
Then proceed per the F-6 decision tree:

- **PASS**       → Group G is skipped entirely.
- **BORDERLINE** → Activate Group G sub-story G-2 (QA hibernate).
- **FAIL**       → Activate Group G full Supabase Cloud fallback.
EOF
} >"$OUTPUT_FILE"

# ------------------------------------------------------------------------------
# Summary to stderr (the operator sees this on their terminal).
# ------------------------------------------------------------------------------

cat >&2 <<EOF

ram-benchmark complete.
  start:    $START_UTC
  end:      $END_UTC
  verdict:  $VERDICT
  reason:   $VERDICT_REASON
  peak RAM: ${PEAK_PCT}% (${MEM_USED_MB}MB / ${MEM_TOTAL_MB}MB at snapshot)
  artefact: $OUTPUT_FILE

Next: paste the artefact into the PR / closure block and update QA-HIBERNATE.md §2.
EOF

exit 0
