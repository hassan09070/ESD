#!/usr/bin/env bash
# Full teardown: every container of the stack and its named volumes (Prometheus TSDB,
# Elasticsearch indices, Grafana state, Filebeat registry). Images are kept.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down -v --remove-orphans
echo "canteen cleaned up (images kept; 'docker compose down' without -v would have kept the data volumes)"
