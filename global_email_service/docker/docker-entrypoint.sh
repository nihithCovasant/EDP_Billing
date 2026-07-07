#!/bin/bash
set -e

echo "==================================="
echo "Global Email Service Starting"
echo "==================================="
echo "Host: ${HOST:-0.0.0.0}"
echo "Port: ${PORT:-9200}"
echo "Log Level: ${LOG_LEVEL:-INFO}"
echo "Dry Run: ${EMAIL_DRY_RUN:-false}"
echo "Graph Sender: ${EMAIL_GRAPH_SENDER:-rms@covasant.com}"
echo "==================================="

exec "$@"
