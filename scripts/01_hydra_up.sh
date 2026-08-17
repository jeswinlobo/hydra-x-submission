#!/usr/bin/env bash
# Bring up the local HydraDB node and wait until it actually answers a query.
#
# A listening port is not proof the node works, so this polls the admin
# readiness endpoint and then round-trips a real query over HTTP before
# reporting success.
#
# Uses `docker compose` (or `docker-compose`) when either is installed, and
# otherwise falls back to `docker run` with the identical configuration from
# hydradb/README.md. Both paths pin the same image digest, so the container is
# the same either way; only the launcher differs.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="ghcr.io/hydra-db/hydradb@sha256:db78309a233be54662db29744047e985a39b51c45a270d1a1f47c31a62cdb709"
CONTAINER="tracegraph-hydradb"

# Compose cannot see a bare $UID: it is a shell variable, not an exported one.
TRACEGRAPH_UID="$(id -u)"
TRACEGRAPH_GID="$(id -g)"
export TRACEGRAPH_UID TRACEGRAPH_GID

# LOCAL_PATH must point at a directory that already exists.
mkdir -p hydradb-data/store hydradb-data/cache

# Generated, not literal. A token committed to a public repo is not a secret,
# and this one authenticates a graph the container publishes on a real port.
if [[ ! -f hydradb-data/auth-token ]]; then
  if command -v openssl >/dev/null; then
    openssl rand -hex 32 > hydradb-data/auth-token
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > hydradb-data/auth-token
    printf '\n' >> hydradb-data/auth-token
  fi
  chmod 600 hydradb-data/auth-token
  echo "created hydradb-data/auth-token (random, gitignored)"
fi

start_with_compose() {
  "$@" up -d
}

start_with_docker_run() {
  if [[ -n "$(docker ps -aq -f "name=^${CONTAINER}$")" ]]; then
    docker start "$CONTAINER" >/dev/null
    echo "started existing container $CONTAINER"
    return
  fi
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --user "${TRACEGRAPH_UID}:${TRACEGRAPH_GID}" \
    --memory 6g \
    -p 127.0.0.1:7687:7687 -p 127.0.0.1:8443:8443 -p 127.0.0.1:9090:9090 \
    -v "$PWD/hydradb-data:/data" \
    -e CLOUD_PROVIDER=local \
    -e LOCAL_PATH=/data/store \
    -e GRAPH_NAMESPACE=default \
    -e GRAPH_ID=default \
    -e GRAPH_CELL_ID=cell-0 \
    -e GRAPH_CELLS=cell-0 \
    -e GRAPH_NODE_ID=node-0 \
    -e GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687 \
    -e GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687 \
    -e GRAPH_DATA_CACHE_DIR=/data/cache \
    -e GRAPH_AUTH_TOKEN_FILE=/data/auth-token \
    -e GRAPH_ALLOW_PLAINTEXT=true \
    -e RUST_MIN_STACK=33554432 \
    "$IMAGE" >/dev/null
  echo "created container $CONTAINER"
}

if docker compose version >/dev/null 2>&1; then
  LAUNCHER="docker compose"
  start_with_compose docker compose
elif command -v docker-compose >/dev/null 2>&1; then
  LAUNCHER="docker-compose"
  start_with_compose docker-compose
else
  LAUNCHER="docker run"
  start_with_docker_run
fi
echo "launcher: $LAUNCHER"

TOKEN="$(cat hydradb-data/auth-token)"
HTTP_URL="http://127.0.0.1:8443/v1/graphs/default/query"

echo -n "waiting for readiness"
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null http://127.0.0.1:9090/readyz; then
    echo " ok"
    break
  fi
  echo -n "."
  sleep 1
done

# Readiness only means the process is up. Prove the query path works.
echo -n "round-tripping a query"
for _ in $(seq 1 30); do
  response="$(curl -sS "$HTTP_URL" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'X-Graph-Namespace: default' \
    -H 'Content-Type: application/json' \
    --data '{"cell_id":"cell-0","query":"MATCH (n {id: 0}) RETURN n.id AS id"}' 2>/dev/null || true)"
  if [[ -n "$response" ]] && ! grep -qi '"error"' <<<"$response"; then
    echo " ok"
    echo "$response"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "node did not answer a query within the timeout. Recent logs:" >&2
docker logs --tail 40 "$CONTAINER" >&2 2>/dev/null || true
exit 1
