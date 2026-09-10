#!/usr/bin/env bash
# Part E.1 runner: three stages, each kept busy for >= MIN_STAGE_S seconds (default 120 s
# = 24 Prometheus scrapes at 5 s), toggling the fault by re-creating the agent container
# with a different FIXIT_FAULT value.
#
#   scripts/experiment_fault.sh [slow|flaky] [tasks-per-stage]
#
#   stage 1 baseline : FIXIT_FAULT=none  -> load.py
#   stage 2 fault    : FIXIT_FAULT=<mode> -> docker compose up -d agent -> load.py
#   stage 3 recovery : FIXIT_FAULT=none  -> docker compose up -d agent -> load.py
#
# Prints the UTC start/end of every stage (paste into Grafana / Kibana time pickers) and
# writes raw rows to scripts/results/*.json.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-slow}"
TASKS="${2:-30}"
MIN_STAGE_S="${MIN_STAGE_S:-120}"
CONCURRENCY="${CONCURRENCY:-1}"
PY="${PYTHON:-python3}"
RESULTS=scripts/results; mkdir -p "$RESULTS"
SUMMARY="$RESULTS/fault_${MODE}_$(date -u +%Y%m%dT%H%M%SZ).txt"

set_fault() {
  echo "==> restarting agent with FIXIT_FAULT=$1"
  FIXIT_FAULT="$1" docker compose up -d agent >/dev/null 2>&1
  for _ in $(seq 1 60); do
    if curl -sf localhost:8000/health | grep -q "\"fault\":\"$1\""; then return 0; fi
    sleep 1
  done
  echo "agent did not come up with fault=$1" >&2; exit 1
}

stage() {
  local name="$1" fault="$2"
  set_fault "$fault"
  sleep 10   # two scrapes of the new fixit_build_info before traffic starts
  local start end
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "==> stage $name (FIXIT_FAULT=$fault) start $start"
  "$PY" scripts/load.py --tasks "$TASKS" --concurrency "$CONCURRENCY" --min-duration "$MIN_STAGE_S" --json --label "${MODE}_${name}" | tee -a "$SUMMARY"
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "==> stage $name end   $end"
  printf '%-10s %-6s %s  ->  %s\n' "$name" "$fault" "$start" "$end" >> "$SUMMARY.stages"
}

echo "fault experiment mode=$MODE tasks/stage=$TASKS min-stage=${MIN_STAGE_S}s" | tee "$SUMMARY"
stage baseline none
stage fault    "$MODE"
stage recovery none
echo
echo "=== stage time ranges (UTC) ==="; cat "$SUMMARY.stages"
echo "summary: $SUMMARY"
