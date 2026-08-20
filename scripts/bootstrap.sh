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

# Scripts print Unicode (arrows, checkmarks) straight to stdout. On Windows
# that stream defaults to the system codepage (cp1252), not UTF-8, and the
# process dies mid-run on the first such character. PYTHONUTF8 forces UTF-8
# text I/O regardless of locale; it is a no-op on platforms already UTF-8.
export PYTHONUTF8=1

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
QUESTIONS="dataset/EnterpriseRAG-Bench/data/questions/test.parquet"
[[ -f "$CORPUS" ]] || fail "corpus not found at $CORPUS
  uv run --with huggingface_hub hf download onyx-dot-app/EnterpriseRAG-Bench \\
      --repo-type dataset --local-dir dataset/EnterpriseRAG-Bench"
[[ -f "$QUESTIONS" ]] || fail "questions not found at $QUESTIONS (needed by scripts/75_retrieval_eval.py)"

# Existence is not enough, because the failure it misses is the likely one. The
# corpus is Git LFS, so cloning the dataset repo without `git lfs` installed
# leaves a ~130-byte pointer file at exactly the right path: an existence check
# passes and the run then dies inside pyarrow with an error about magic bytes.
#
# Check the magic bytes rather than the size. A first attempt used a 1 MB floor
# and rejected the genuine questions file, which is only 408 KB — the size of a
# valid parquet says nothing, while every parquet begins with "PAR1" and no LFS
# pointer does.
for f in "$CORPUS" "$QUESTIONS"; do
  magic=$(head -c 4 "$f" 2>/dev/null || true)
  [[ "$magic" == "PAR1" ]] || fail "$f is not a parquet file (starts with '$magic').
  A Git LFS pointer lands at the right path when git-lfs is not installed.
  Install git-lfs and re-pull, or download the corpus with:
      uv run --with huggingface_hub hf download \\
          onyx-dot-app/EnterpriseRAG-Bench --repo-type dataset \\
          --local-dir dataset/EnterpriseRAG-Bench"
done

# ~7GB: the 1.4GB corpus, a 1.4GB re-chunked copy, and a 2.5GB lexical index.
FREE_GB=$(df -BG --output=avail . | tail -1 | tr -dc '0-9')
if [[ -n "$FREE_GB" && "$FREE_GB" -lt 7 ]]; then
  fail "only ${FREE_GB}GB free; this needs about 7GB"
fi
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

# The corpus ships as one row group holding all 511,962 documents, and parquet
# decodes a row group whole, so fetching one document reads the entire 1.4GB
# file — four and a half seconds, four times per question. Re-chunking is a
# lossless copy that turns that into a twelve-millisecond lookup.
step "re-chunking the corpus and building the document locator"
uv run python scripts/71_repartition_corpus.py

# --- the graph ---------------------------------------------------------------

if [[ $FAST -eq 0 ]]; then
  step "ingesting a slice and resolving identities through the graph"
  uv run python scripts/30_load_slice.py

  step "backfilling document timestamps"
  uv run python scripts/26_backfill_timestamps.py

  step "extracting claims for the slice"
  uv run python scripts/45_extract_claims.py --docs 30 || \
    echo "  note: extraction had trouble; on-demand ingestion still covers questions"

  # Identity first, then conflicts. Conflict adjudication groups by the
  # resolved identity, so a sweep that runs before identities are settled
  # groups by name instead — and nothing reruns it afterwards.
  step "reconciling identity decisions"
  uv run python scripts/37_rebuild_resolution.py --apply

  step "detecting conflicts"
  uv run python scripts/55_conflicts.py --show 0
else
  echo
  echo "  --fast: skipping the slice. Questions still work — documents are"
  echo "  enriched on demand — but the resolution and conflict panels start empty."
fi

# --- proof -------------------------------------------------------------------

# Verification reports; it does not abort. Under `set -e` with `pipefail` a
# single failing test used to exit here, so ten minutes of successful build
# ended at one line of `tail -1` with no next step — on a system that would
# have served perfectly well. A failure is worth seeing in full and worth
# continuing past.
step "verifying"
VERIFY_FAILED=0

if ! pytest_out=$(uv run pytest -q -m "not live" 2>&1); then
  VERIFY_FAILED=1
  printf '%s\n' "$pytest_out" | tail -25
else
  printf '%s\n' "$pytest_out" | tail -1
fi

if [[ $FAST -eq 0 ]]; then
  if ! gate_out=$(uv run python scripts/35_verify_gate.py 2>&1); then
    VERIFY_FAILED=1
    printf '%s\n' "$gate_out" | tail -20
  else
    printf '%s\n' "$gate_out" | tail -1
  fi
fi

echo
echo "Ready."
echo
echo "  ./scripts/60_serve.sh          Ask & Inspect at http://127.0.0.1:8000"
echo '  uv run python scripts/50_ask.py "your question"'
if [[ $FAST -eq 0 ]]; then
  # Needs an ingested run, which --fast deliberately skips.
  echo "  uv run python scripts/80_demo_check.py --rounds 10"
fi
cat <<'EOF'

The first question about a document takes 25-40 seconds, because the document is
parsed and extracted while the question is answered. Asking about it again is
fast.
EOF

if [[ $VERIFY_FAILED -eq 1 ]]; then
  echo
  echo "Note: verification above reported a failure. The system is built and will"
  echo "serve, but something it checks is not holding. Re-run to see it again:"
  echo "  uv run pytest -q -m 'not live'"
  echo "  uv run python scripts/35_verify_gate.py"
  echo "  uv run python scripts/36_repair_graph.py    # if the gate names pending mentions"
fi
