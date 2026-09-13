#!/usr/bin/env bash
# Part E.1 runner: three stages, each kept busy for >= MIN_STAGE_S seconds (default 120 s
# = 24 Prometheus scrapes at 5 s), toggling the fault by re-creating the api container
# with a different TERMLAB_FAULT value.
#
#   scripts/experiment_fault.sh [cold_start|cpu_hog] [users-per-stage]
#
#   stage 1 baseline : TERMLAB_FAULT=none   -> load.py
#   stage 2 fault    : TERMLAB_FAULT=<mode> -> docker compose up -d api -> load.py
#   stage 3 recovery : TERMLAB_FAULT=none   -> docker compose up -d api -> load.py
#
# Re-creating the api reaps every running sandbox (reason=admin) and, for cpu_hog, removes
# the stressors again on the next restart (orphan cleanup). Prints the UTC start/end of every
# stage (paste into Grafana / Kibana time pickers) and writes raw rows to scripts/results/.
# Run under `caffeinate -i` on a Mac: a sleeping laptop pauses the Docker VM mid-stage.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-cold_start}"
USERS="${2:-8}"
MIN_STAGE_S="${MIN_STAGE_S:-120}"
CONCURRENCY="${CONCURRENCY:-4}"
SESSION_SECONDS="${SESSION_SECONDS:-0}"   # keep each terminal typing this long (cpu_hog: 20 so sandboxes outlive the 10 s stats sampler)
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
RESULTS=scripts/results; mkdir -p "$RESULTS"
SUMMARY="$RESULTS/fault_${MODE}_$(date -u +%Y%m%dT%H%M%SZ).txt"
unset VIRTUAL_ENV

set_fault() {
  echo "==> re-creating api with TERMLAB_FAULT=$1"
  TERMLAB_FAULT="$1" docker compose up -d api >/dev/null 2>&1
  for _ in $(seq 1 60); do
    if curl -sf localhost:8000/health | grep -q "\"fault\":\"$1\""; then return 0; fi
    sleep 1
  done
  echo "api did not come up with fault=$1" >&2; exit 1
}

stage() {
  local name="$1" fault="$2"
  set_fault "$fault"
  sleep 10   # two scrapes of the new termlab_build_info (and, for cpu_hog, a warmed-up stressor) before traffic
  local start end
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "==> stage $name (TERMLAB_FAULT=$fault) start $start"
  "$UV" run scripts/load.py --users "$USERS" --concurrency "$CONCURRENCY" --min-duration "$MIN_STAGE_S" --session-seconds "$SESSION_SECONDS" --json --label "${MODE}_${name}" | tee -a "$SUMMARY"
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "==> stage $name end   $end"
  printf '%-10s %-10s %s  ->  %s\n' "$name" "$fault" "$start" "$end" >> "$SUMMARY.stages"
}

echo "fault experiment mode=$MODE users/stage>=$USERS concurrency=$CONCURRENCY session-seconds=$SESSION_SECONDS min-stage=${MIN_STAGE_S}s" | tee "$SUMMARY"
stage baseline none
stage fault    "$MODE"
stage recovery none
echo
echo "=== stage time ranges (UTC) ==="; cat "$SUMMARY.stages"
echo "summary: $SUMMARY"
