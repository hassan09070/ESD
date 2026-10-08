#!/usr/bin/env bash
# A2 Part A rate-limit demo: one client places orders as fast as it can. The limit on POST /orders
# is 60 per 60 s fixed window per client (X-Client-ID header, else the client IP; see app/main.py),
# so the first 60 are 201 and the rest are 429 with Retry-After = seconds until the window resets.
#
#   scripts/ratelimit_demo.sh [client-id]        (default: a fresh id, so it never collides with the load service)
set -euo pipefail
URL="${CANTEEN_URL:-http://localhost:8000}"
CLIENT="${1:-demo-$(date +%s)}"
N="${N:-65}"
echo "client X-Client-ID: $CLIENT -> $N x POST /orders"
for i in $(seq 1 "$N"); do
  out=$(curl -s -o /tmp/rl_body.json -w '%{http_code}  remaining=%header{x-ratelimit-remaining}  retry-after=%header{retry-after}' \
        -X POST "$URL/orders" -H 'Content-Type: application/json' -H "X-Client-ID: $CLIENT" -d '{"shop":"grito","item":"corn"}')
  case "$i" in 1|2|3|58|59|60|61|62|63|64|65) printf '%3d  %s\n' "$i" "$out" ;; 4) echo "  …" ;; esac
done
echo "last response body:"; cat /tmp/rl_body.json; echo
