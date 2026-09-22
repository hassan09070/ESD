#!/bin/sh
# One-shot Elasticsearch/Kibana setup, run by the `setup` compose service before Filebeat starts:
#   1. PUT the ILM policy   canteen-logs-policy   (delete indices 7 days after creation)
#   2. PUT the index template canteen-logs        (field types + ILM binding) for canteen-logs-*
#   3. create the Kibana data view canteen-logs-* (time field @timestamp)
# Idempotent; only needs sh + curl so it runs in curlimages/curl.
set -eu
ES="${ES_URL:-http://elasticsearch:9200}"
KB="${KIBANA_URL:-http://kibana:5601}"
DIR="$(cd "$(dirname "$0")" && pwd)"
POLICY="${ILM_POLICY_FILE:-$DIR/../monitoring/elasticsearch/ilm_policy.json}"
TEMPLATE="${INDEX_TEMPLATE_FILE:-$DIR/../monitoring/elasticsearch/index_template.json}"

echo "waiting for elasticsearch at $ES ..."
until curl -sf "$ES/_cluster/health?wait_for_status=yellow&timeout=5s" >/dev/null; do sleep 3; done
echo "PUT _ilm/policy/canteen-logs-policy"
sed 's/"_comment": "[^"]*",//' "$POLICY" | curl -sf -X PUT "$ES/_ilm/policy/canteen-logs-policy" -H 'Content-Type: application/json' -d @-; echo
echo "PUT _index_template/canteen-logs"
sed 's/"_comment": "[^"]*",//' "$TEMPLATE" | curl -sf -X PUT "$ES/_index_template/canteen-logs" -H 'Content-Type: application/json' -d @-; echo

echo "waiting for kibana at $KB ..."
until curl -sf "$KB/api/status" | grep -q '"level":"available"'; do sleep 5; done
if curl -sf "$KB/api/data_views" | grep -q '"title":"canteen-logs-\*"'; then
  echo "kibana data view canteen-logs-* already exists"
else
  echo "POST kibana data view canteen-logs-*"
  curl -sf -X POST "$KB/api/data_views/data_view" -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
    -d '{"data_view":{"id":"canteen-logs","title":"canteen-logs-*","name":"canteen logs","timeFieldName":"@timestamp"},"override":true}'; echo
fi
echo "setup done"
