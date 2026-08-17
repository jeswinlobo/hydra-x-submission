#!/usr/bin/env bash
# Take a fresh clone to a working system, in order, checking as it goes.
#
# Each stage depends on the one before it, and each is resumable — re-running
# after an interruption skips what is already done rather than duplicating it.
# The whole thing is safe to run twice.
#
#   ./scripts/bootstrap.sh              # everything
#   ./scripts/bootstrap.sh --fast       # skip the slice, ~6 minutes
#
# Requires: Docker, uv, an ANTHROPIC_API_KEY in .env, and the corpus at
# dataset/EnterpriseRAG-Bench/data/.
set -euo pipefail

cd "$(dirname "$0")/.."

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mfailed: %s\033[0m\n' "$1" >&2; exit 1; }

# --- preconditions, checked before anything slow runs ------------------------

step "checking prerequisites"
command -v docker >/dev/null || fail "docker is not installed"
command -v uv >/dev/null || fail "uv is not installed (https://docs.astral.sh/uv/)"

[[ -f .env ]] || fail "no .env — copy .env.example and add ANTHROPIC_API_KEY"
grep -q '^ANTHROPIC_API_KEY=.\+' .env || \
  fail "ANTHROPIC_API_KEY is empty in .env; extraction and synthesis need it"

CORPUS="dataset/EnterpriseRAG-Bench/data/documents/test.parquet"
[[ -f "$CORPUS" ]] || fail "corpus not found at $CORPUS"
echo "  ok: docker, uv, .env, corpus"

step "installing dependencies"
uv sync --quiet
echo "  ok"

step "fetching pinned references"
./scripts/00_fetch_refs.sh >/dev/null 2>&1 || \
  echo "  note: reference fetch had trouble; engine docs are optional to run"
echo "  ok"

# --- the database ------------------------------------------------------------

step "starting HydraDB and proving a round trip"
./scripts/01_hydra_up.sh >/dev/null || fail "HydraDB did not come up"
echo "  ok: node answers a real query, not just a listening port"

# --- corpus scale ------------------------------------------------------------
#
# The lexical index and the id registry cover all 511,962 documents. The graph
# does not: a label index caps at 250,000 vertices, so the graph holds only what
# questions reach. This is the one slow stage, about five minutes.

step "indexing the corpus (511,962 documents, ~5 minutes)"
uv run python scripts/70_register_corpus.py
echo "  ok"

step "building the document locator"
uv run python - <<'PY'
from tracegraph import config
from tracegraph.parquet_reader import RowLocator
locator = RowLocator.build(config.DOCUMENTS_PARQUET, config.REGISTRY_DB)
print(f"  ok: {locator.build_report}")
locator.close()
PY

# --- the graph ---------------------------------------------------------------

if [[ $FAST -eq 0 ]]; then
  step "ingesting a slice and resolving identities through the graph"
  uv run python scripts/30_load_slice.py

  step "backfilling document timestamps"
  uv run python scripts/26_backfill_timestamps.py

  step "extracting claims for the slice"
  uv run python scripts/45_extract_claims.py --docs 30 || \
    echo "  note: extraction had trouble; on-demand ingestion still covers questions"

  step "detecting conflicts"
  uv run python scripts/55_conflicts.py --show 0
else
  echo
  echo "  --fast: skipping the slice. Questions still work — documents are"
  echo "  enriched on demand — but the resolution and conflict panels start empty."
fi

# --- proof -------------------------------------------------------------------

step "verifying"
uv run pytest -q -m "not live" 2>&1 | tail -1
[[ $FAST -eq 0 ]] && uv run python scripts/35_verify_gate.py 2>&1 | tail -1

cat <<'EOF'

Ready.

  ./scripts/60_serve.sh          Ask & Inspect at http://127.0.0.1:8000
  uv run python scripts/50_ask.py "your question"
  uv run python scripts/80_demo_check.py --rounds 10

The first question about a document takes 25-40 seconds, because the document is
parsed and extracted while the question is answered. Asking about it again is
fast.
EOF
