# Pinned upstream references

Both directories are gitignored. Re-materialize with `scripts/00_fetch_refs.sh`.

| Path | Upstream | Commit |
|---|---|---|
| `hydradb/` | https://github.com/hydra-db/hydradb | `6a2fbb192f37f51a93690a2ae2d2f5e27e6e4219` |
| `external/EnterpriseRAG-Bench/` | https://github.com/onyx-dot-app/EnterpriseRAG-Bench | `d36685e273713975ee20299bbf1ab64165575b3c` |

## Container image

Pinned by immutable digest in `docker-compose.yml`:

```
ghcr.io/hydra-db/hydradb@sha256:db78309a233be54662db29744047e985a39b51c45a270d1a1f47c31a62cdb709
```

`hydradb/` is the sole source of truth for engine behaviour: `cypher-compat.md`,
`README.md`, `AGENTS.md`. The hosted-service documentation at docs.hydradb.com
describes a different product surface and is not used here.

## Firewalled material

`external/EnterpriseRAG-Bench/generated_data/` is the corpus **generator** input,
not the corpus. Two consequences:

- `generated_data/sources/` (3.7 GB) is the pre-parquet copy of the same nine
  sources already present in `dataset/`. Deleted after cloning; it carries no
  information the parquet lacks.
- `generated_data/employee_directory.yaml` is an **identity oracle**: it maps
  every person to their email, title, and manager. Resolving `Sam` / `@soham`
  against it would answer the exact question Track 1 poses, so it is barred from
  ingestion and answering. It is copied to the gitignored `eval-oracle/` and used
  only to score entity-resolution precision and recall after the fact.

The same rule covers `gold_answer`, `answer_facts`, and `expected_doc_ids` in the
questions parquet: read by evaluation, never by the pipeline.
