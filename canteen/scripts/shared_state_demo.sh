#!/usr/bin/env bash
# A3 Part A: which state is shared by the replicas and which is not, shown with real requests
# through Envoy in round-robin mode (scripts/envoy_mode.sh round_robin first).
#
#   scripts/shared_state_demo.sh
#
#   1. Data (SQLite on the shared volume): an order placed via one replica is readable via the others.
#   2. Idempotency records (same SQLite): the same Idempotency-Key repeated through round robin lands
#      on different replicas and still returns ONE order (duplicate prevention survives balancing).
#   3. Rate-limit counters (in memory, per replica): one client sends 3 x limit + 3 orders through
#      round robin; each replica allows its own full window, so ~3 x limit succeed before any 429.
#      Against one replica directly, exactly `limit` succeed.
set -euo pipefail
URL="${CANTEEN_URL:-http://localhost:8000}"          # Envoy
A="${A_URL:-http://localhost:8001}"                  # replica A directly
LIMIT=$(curl -s "$A/openapi.json" | python3 -c 'import sys,json,re; d=json.load(sys.stdin); m=re.search(r"\((\d+) orders per", json.dumps(d)); print(m.group(1) if m else 60)')

echo "== 1. data is shared: place through Envoy, read back from each replica directly"
oid=$(curl -s -X POST "$URL/orders" -H 'Content-Type: application/json' -d '{"shop":"tapal","item":"biryani"}' -D /tmp/h.txt | python3 -c 'import sys,json;print(json.load(sys.stdin)["order_id"])')
echo "   placed order $oid via replica $(grep -i x-replica /tmp/h.txt | tr -d '\r' | cut -d' ' -f2)"
for p in 8001 8002 8003; do printf '   replica on :%s sees it: ' "$p"; curl -s "http://localhost:$p/orders/$oid" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["state"], "shop", d["shop"])'; done

echo; echo "== 2. idempotency records are shared: one key, 6 requests via round robin"
key="demo-$(date +%s)"
for i in 1 2 3 4 5 6; do
  curl -s -X POST "$URL/orders" -H 'Content-Type: application/json' -H "Idempotency-Key: $key" -H 'X-Client-ID: idem-demo' \
       -d '{"shop":"grito","item":"corn"}' -D /tmp/h.txt -o /tmp/b.txt
  printf '   try %s: replica %s  order %s  replayed=%s\n' "$i" "$(grep -i x-replica /tmp/h.txt | tr -d '\r' | cut -d' ' -f2)" \
         "$(python3 -c 'import json;print(json.load(open("/tmp/b.txt"))["order_id"])')" "$(grep -ic idempotent-replayed /tmp/h.txt)"
done

echo; echo "== 3. rate-limit counters are NOT shared: limit is $LIMIT per client per window"
burst() {   # $1 = base url, $2 = client id, $3 = count -> prints 201 and 429 counts
  local ok=0 limited=0 code
  for _ in $(seq 1 "$3"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$1/orders" -H 'Content-Type: application/json' -H "X-Client-ID: $2" -d '{"shop":"cafetogo","item":"roll"}')
    if [ "$code" = 201 ]; then ok=$((ok+1)); elif [ "$code" = 429 ]; then limited=$((limited+1)); fi
  done
  echo "   $3 orders from client '$2' -> 201: $ok, 429: $limited"
}
n=$((LIMIT * 3 + 3))
echo "   a) one replica directly (A):"; burst "$A" "rl-direct-$(date +%s)" "$n"
echo "   b) through Envoy round robin, the same client id:"; burst "$URL" "rl-envoy-$(date +%s)" "$n"
echo "   -> through the balancer the client gets ~3x the limit: each replica counts only what it saw."
echo "      Fix (not implemented): keep the counters in the shared store or in Redis, or rate limit in Envoy."
