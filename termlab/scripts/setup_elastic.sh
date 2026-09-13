#!/bin/sh
# One-shot Elasticsearch/Kibana setup (runs in the `setup` compose service, or by hand):
#   1. PUT the ILM policy  termlab-logs-policy  (delete after 7 days)
#   2. PUT the index template termlab-logs (mappings + ILM binding) for termlab-logs-*
#   3. Create the Kibana data view termlab-logs-* (time field @timestamp)
# Idempotent: safe to re-run. Uses only curl + sh so it works in curlimages/curl.
set -eu
ES="${ES_URL:-http://elasticsearch:9200}"
KB="${KIBANA_URL:-http://kibana:5601}"
DIR="$(cd "$(dirname "$0")" && pwd)"
POLICY="${ILM_POLICY_FILE:-$DIR/../monitoring/elasticsearch/ilm_policy.json}"
TEMPLATE="${INDEX_TEMPLATE_FILE:-$DIR/../monitoring/elasticsearch/index_template.json}"

echo "waiting for elasticsearch at $ES ..."
until curl -sf "$ES/_cluster/health?wait_for_status=yellow&timeout=5s" >/dev/null; do sleep 3; done

echo "PUT _ilm/policy/termlab-logs-policy"
sed 's/"_comment": "[^"]*",//' "$POLICY" | curl -sf -X PUT "$ES/_ilm/policy/termlab-logs-policy" -H 'Content-Type: application/json' -d @- ; echo
echo "PUT _index_template/termlab-logs"
sed 's/"_comment": "[^"]*",//' "$TEMPLATE" | curl -sf -X PUT "$ES/_index_template/termlab-logs" -H 'Content-Type: application/json' -d @- ; echo

echo "waiting for kibana at $KB ..."
until curl -sf "$KB/api/status" | grep -q '"level":"available"'; do sleep 5; done
if curl -sf "$KB/api/data_views" | grep -q '"title":"termlab-logs-\*"'; then
  echo "kibana data view termlab-logs-* already exists"
else
  echo "POST kibana data view termlab-logs-*"
  curl -sf -X POST "$KB/api/data_views/data_view" -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
    -d '{"data_view":{"title":"termlab-logs-*","name":"termlab logs","timeFieldName":"@timestamp"},"override":true}' ; echo
fi
echo "setup done"
