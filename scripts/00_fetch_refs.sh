#!/usr/bin/env bash
# Re-materialise the pinned upstream references.
#
# Both trees are gitignored: they are read-only reference material, not part of
# this project. Commits are recorded in docs/refs.lock.md so a checkout can be
# reproduced.
set -euo pipefail

cd "$(dirname "$0")/.."

HYDRADB_COMMIT="6a2fbb192f37f51a93690a2ae2d2f5e27e6e4219"
BENCH_COMMIT="d36685e273713975ee20299bbf1ab64165575b3c"

fetch() {
  local dir="$1" url="$2" commit="$3"
  if [[ -d "$dir/.git" ]]; then
    echo "$dir already present at $(git -C "$dir" rev-parse --short HEAD)"
    return
  fi
  echo "cloning $url -> $dir at $commit"
  # Fetch the recorded commit rather than whatever HEAD happens to be. A shallow
  # clone of HEAD plus a warning is not a pin: docs/engine-notes.md describes the
  # behaviour of one specific tree, and silently reading a different one is how a
  # verified contract stops matching the engine it was verified against.
  git init -q "$dir"
  git -C "$dir" remote add origin "$url"
  if git -C "$dir" fetch -q --depth 1 origin "$commit" 2>/dev/null; then
    git -C "$dir" checkout -q FETCH_HEAD
  else
    echo "  recorded commit is not fetchable; falling back to current HEAD" >&2
    echo "  Re-read hydradb/cypher-compat.md and re-run the contract tests" >&2
    echo "  before trusting docs/engine-notes.md, then update docs/refs.lock.md." >&2
    git -C "$dir" fetch -q --depth 1 origin HEAD
    git -C "$dir" checkout -q FETCH_HEAD
  fi
  echo "  at $(git -C "$dir" rev-parse HEAD)"
}

fetch hydradb https://github.com/hydra-db/hydradb.git "$HYDRADB_COMMIT"
fetch external/EnterpriseRAG-Bench \
      https://github.com/onyx-dot-app/EnterpriseRAG-Bench.git "$BENCH_COMMIT"

# generated_data/sources is a 3.7 GB pre-parquet copy of the same nine sources
# already in dataset/. It carries nothing the parquet lacks, and the disk budget
# is tight.
if [[ -d external/EnterpriseRAG-Bench/generated_data/sources ]]; then
  echo "removing redundant generated_data/sources (3.7 GB)"
  rm -rf external/EnterpriseRAG-Bench/generated_data/sources
fi

# The identity oracle is quarantined: it maps every person to their email and
# manager, which is the answer to the entity-resolution problem this project
# exists to solve. Evaluation may score against it; the pipeline never reads it.
mkdir -p eval-oracle
ORACLE=external/EnterpriseRAG-Bench/generated_data/employee_directory.yaml
if [[ -f "$ORACLE" ]]; then
  # Moved, not copied. A copy leaves the oracle sitting inside the reference
  # tree where a future glob over external/ would sweep it back into the
  # pipeline, which is the one thing the quarantine exists to prevent.
  mv "$ORACLE" eval-oracle/
  echo "quarantined employee_directory.yaml -> eval-oracle/ (scoring only)"
fi

echo
echo "references ready. docs/refs.lock.md records the pinned commits."
