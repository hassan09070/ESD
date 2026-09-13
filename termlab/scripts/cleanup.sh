#!/usr/bin/env bash
# Full teardown: the compose stack with its volumes (Prometheus TSDB, Elasticsearch data,
# Grafana state, Filebeat registry) plus every container the api created through the
# Docker socket (user sandboxes, warm sandboxes, cpu_hog stressors) - those are not compose
# services, so `docker compose down` alone would leave them running.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down -v --remove-orphans
ids=$(docker ps -aq --filter label=termlab.sandbox=1)
if [ -n "$ids" ]; then
  echo "removing $(echo "$ids" | wc -l | tr -d ' ') leftover sandbox containers"
  docker rm -f $ids >/dev/null
fi
echo "termlab cleaned up (the sandbox image termlab-sandbox:local is kept; 'docker rmi termlab-sandbox:local' removes it)"
