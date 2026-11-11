#!/usr/bin/env bash
# A3 runner: every run of Parts B, C and D with the same fixed-rate workload, HTTP and gRPC
# one after the other, Envoy re-configured between runs and every fault undone afterwards.
#
#   caffeinate -i scripts/experiment_lb.sh            # all runs (~20 min at the defaults)
#   scripts/experiment_lb.sh b_a_only c_rr            # only these runs
#   RATE=40 DURATION=60 scripts/experiment_lb.sh      # workload: requests per second, seconds (both interfaces)
#
# Runs (label -> Envoy mode, fault):
#   b_a_only    a_only         none                  Part B baseline: A handles everything, B and C idle
#   c_rr        round_robin    none                  Part C: equal-weight round robin (the Part D starting point)
#   d_weighted  weighted       none                  Part D.1: A:B:C = 2:1:1
#   d_rr_slow   round_robin    B fixed_delay_ms      Part D.2 run 1: round robin with a slow B
#   d_lr_slow   least_request  B fixed_delay_ms      Part D.2 run 2: least request, same delay and workload
#   d_failure   round_robin    stop B, restart B     Part D.3: at STOP_AT s into the run B is stopped, at START_AT s started again
#
# Each run: set the mode (scripts/envoy_mode.sh waits until every destination is healthy), set or
# clear the fault on B directly (port 8002, never through Envoy), cancel the waiting load-test
# orders of earlier runs (store.cancel_waiting, so GET /shops costs the same every run), wait two scrapes, then
# scripts/fixed_rate.py for HTTP, then for gRPC. The client writes scripts/results/a3_<run>_<iface>.json;
# this script appends each window to scripts/results/a3_runs.tsv, which scripts/lb_summary.py
# turns into the results table (Prometheus per replica + Envoy access-log cross-check).
# The `load` service is paused for the whole session so the only traffic is the workload.
set -euo pipefail
cd "$(dirname "$0")/.."
RATE="${RATE:-40}"; DURATION="${DURATION:-60}"; DELAY_MS="${DELAY_MS:-200}"
STOP_AT="${STOP_AT:-20}"; START_AT="${START_AT:-40}"          # failure run: seconds into the run
B_URL="${B_URL:-http://localhost:8002}"                        # replica B directly
RESULTS=scripts/results; mkdir -p "$RESULTS"; RUNS="$RESULTS/a3_runs.tsv"
PY="${PY:-uv run python}"                                      # grpcio for the gRPC client
ALL=(b_a_only c_rr d_weighted d_rr_slow d_lr_slow d_failure)
if [ $# -gt 0 ]; then SELECTED=("$@"); else SELECTED=("${ALL[@]}"); fi     # (bash 3.2 on macOS: no "${@:-...}" array default)

if docker compose ps --status running --services 2>/dev/null | grep -qx load; then
  echo "==> pausing the load service"; docker compose stop load >/dev/null
  trap 'echo "==> resuming the load service"; docker compose start load >/dev/null' EXIT
fi
[ -f "$RUNS" ] || printf 'run\tinterface\tmode\tfault\tstart\tend\trate\tduration\n' > "$RUNS"

reset_orders() {   # the gRPC workload's orders are never cooked; cancel them so /shops and PlaceOrder cost the same in every run
  echo "==> cancelled $(docker compose exec -T app-a python -c 'from app import store; store.init(); print(store.cancel_waiting("load-test"))') waiting load-test orders"
}

chaos_b() {   # $1 = json body or "reset"
  if [ "$1" = reset ]; then curl -sf -X DELETE "$B_URL/chaos" >/dev/null
  else curl -sf -X PUT "$B_URL/chaos" -H 'Content-Type: application/json' -d "$1" >/dev/null; fi
  echo "==> chaos on B: $(curl -s "$B_URL/chaos")"
}

traffic() {   # $1 = run, $2 = interface, $3 = mode, $4 = fault text, [$5 = extra duration]
  local run=$1 iface=$2 mode=$3 fault=$4 dur=${5:-$DURATION} start end
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if [ "$iface" = http ]; then python3 scripts/fixed_rate.py http --rate "$RATE" --duration "$dur" --label "$run" || true
  else $PY scripts/fixed_rate.py grpc --rate "$RATE" --duration "$dur" --label "$run" || true; fi
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$run" "$iface" "$mode" "$fault" "$start" "$end" "$RATE" "$dur" >> "$RUNS"
}

plain_run() {   # $1 = run, $2 = mode, $3 = fault json or reset, $4 = fault text
  echo; echo "########## $1 (mode $2, fault: $4)"
  scripts/envoy_mode.sh "$2"
  chaos_b "$3"
  reset_orders
  sleep 10                                          # two scrapes of the new state before traffic
  traffic "$1" http "$2" "$4"
  sleep 5
  traffic "$1" grpc "$2" "$4"
  [ "$3" = reset ] || chaos_b reset
}

failure_run() {   # Part D.3: stop B at STOP_AT s, start it at START_AT s, during traffic; run lasts DURATION s
  local run=d_failure mode=round_robin iface
  echo; echo "########## $run (mode $mode, fault: stop B at ${STOP_AT}s, start B at ${START_AT}s)"
  for iface in http grpc; do
    scripts/envoy_mode.sh "$mode"; chaos_b reset; reset_orders; sleep 10
    ( sleep "$STOP_AT";  echo "==> [$(date -u +%H:%M:%S)Z] docker compose stop app-b";  docker compose stop app-b  >/dev/null
      echo "==> [$(date -u +%H:%M:%S)Z] B exited (FinishedAt $(docker inspect canteen-app-b --format '{{.State.FinishedAt}}' | cut -c12-23)Z, exit code $(docker inspect canteen-app-b --format '{{.State.ExitCode}}'))"
      sleep $((START_AT - STOP_AT)); echo "==> [$(date -u +%H:%M:%S)Z] docker compose start app-b"; docker compose start app-b >/dev/null
      echo "==> [$(date -u +%H:%M:%S)Z] B started (StartedAt $(docker inspect canteen-app-b --format '{{.State.StartedAt}}' | cut -c12-23)Z)" ) &
    traffic "$run" "$iface" "$mode" "stop B @${STOP_AT}s, start @${START_AT}s"
    wait
    echo "==> waiting for B to be healthy in Envoy again"; scripts/envoy_mode.sh >/dev/null; sleep 8; scripts/envoy_mode.sh
  done
}

echo "A3 experiments: rate=$RATE req/s duration=${DURATION}s delay_on_B=${DELAY_MS}ms runs: ${SELECTED[*]}"
for run in "${SELECTED[@]}"; do
  case "$run" in
    b_a_only)   plain_run b_a_only   a_only        reset "none" ;;
    c_rr)       plain_run c_rr       round_robin   reset "none" ;;
    d_weighted) plain_run d_weighted weighted      reset "none" ;;
    d_rr_slow)  plain_run d_rr_slow  round_robin   "{\"fixed_delay_ms\": $DELAY_MS}" "B +${DELAY_MS}ms" ;;
    d_lr_slow)  plain_run d_lr_slow  least_request "{\"fixed_delay_ms\": $DELAY_MS}" "B +${DELAY_MS}ms" ;;
    d_failure)  failure_run ;;
    *) echo "unknown run $run" >&2; exit 1 ;;
  esac
done

echo; echo "==> restoring the healthy round-robin setup"; scripts/envoy_mode.sh round_robin; chaos_b reset
echo; echo "=== windows (UTC) ==="; column -t -s $'\t' "$RUNS"
echo; echo "next: python3 scripts/lb_summary.py        # the results table from Prometheus + Elasticsearch"
