#!/usr/bin/env bash
# Part E.1 runner: three stages of >= MIN_STAGE_S seconds (default 120 s = 24 Prometheus
# scrapes), same load in each, the fault toggled over HTTP between them:
#
#   scripts/experiment_fault.sh [slow|closed]
#
#   baseline : POST /chaos/reset                                   -> load.py
#   fault    : slow   -> POST /chaos {"slow_every_n":5,"delay_ms":500}  (the brief's example)
#              closed -> POST /chaos {"fail_shop": "tapal"}
#   recovery : POST /chaos/reset                                   -> load.py
#
# Prints each stage's UTC window (paste into Grafana/Kibana time pickers) and appends it to
# scripts/results/fault_<mode>_<ts>.txt.stages; load.py writes its raw rows next to it.
# On a Mac run under `caffeinate -i` so the laptop does not sleep mid-stage.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-slow}"
MIN_STAGE_S="${MIN_STAGE_S:-120}"
CONCURRENCY="${CONCURRENCY:-4}"
URL="${CANTEEN_URL:-http://localhost:8000}"
RESULTS=scripts/results; mkdir -p "$RESULTS"
# The `load` service must not add traffic during the stages: pause it, resume on exit.
if docker compose ps --status running --services 2>/dev/null | grep -qx load; then
  echo "==> pausing the load service for the run"; docker compose stop load >/dev/null
  trap 'echo "==> resuming the load service"; docker compose start load >/dev/null' EXIT
fi
SUMMARY="$RESULTS/fault_${MODE}_$(date -u +%Y%m%dT%H%M%SZ).txt"

case "$MODE" in
  slow)   FAULT_BODY='{"slow_every_n": 5, "delay_ms": 500}' ;;
  closed) FAULT_BODY='{"fail_shop": "tapal"}' ;;
  *) echo "mode must be slow or closed" >&2; exit 1 ;;
esac

set_chaos() {   # $1 = json body, or "reset"
  if [ "$1" = reset ]; then curl -sf -X POST "$URL/chaos/reset" >/dev/null
  else curl -sf -X POST "$URL/chaos" -H 'Content-Type: application/json' -d "$1" >/dev/null; fi
  echo "==> chaos now: $(curl -s "$URL/chaos")"
}

stage() {   # $1 = name, $2 = chaos body or reset
  set_chaos "$2"
  sleep 10                                  # two scrapes of the new state before traffic
  local start end
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "==> stage $1 start $start"
  python3 scripts/load.py --duration "$MIN_STAGE_S" --concurrency "$CONCURRENCY" --json --label "${MODE}_$1" | tee -a "$SUMMARY"
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "==> stage $1 end   $end"
  printf '%-10s %-40s %s  ->  %s\n' "$1" "$2" "$start" "$end" >> "$SUMMARY.stages"
}

echo "fault experiment mode=$MODE concurrency=$CONCURRENCY min-stage=${MIN_STAGE_S}s" | tee "$SUMMARY"
stage baseline reset
stage fault    "$FAULT_BODY"
stage recovery reset
echo; echo "=== stage time ranges (UTC) ==="; cat "$SUMMARY.stages"
echo "summary: $SUMMARY"
