#!/usr/bin/env bash
# Tear everything down: containers, networks AND named volumes (Prometheus data,
# Elasticsearch indices, Grafana state, Filebeat registry). Also removes sandbox copies
# and restores the sample repo. Safe: it only touches this compose project.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down -v --remove-orphans
rm -rf /tmp/fixit
bash sample_repo/reset.sh
echo "fixit: all containers, networks and volumes removed"
