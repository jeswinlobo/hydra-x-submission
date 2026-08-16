#!/usr/bin/env bash
# Serve Ask & Inspect. Requires a running HydraDB node and an ingested run.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! curl -sf -o /dev/null http://127.0.0.1:9090/readyz; then
  echo "HydraDB is not running. Start it with scripts/01_hydra_up.sh" >&2
  exit 1
fi

echo "TraceGraph on http://127.0.0.1:${PORT:-8000}"
exec uv run uvicorn tracegraph.api:app --port "${PORT:-8000}" --host 127.0.0.1
