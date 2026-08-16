# Corpus notes — verified against the local parquet files

Read with `pyarrow.parquet.ParquetFile(...).schema_arrow` and `.metadata`, not
from the dataset card.

## Documents

`dataset/EnterpriseRAG-Bench/data/documents/test.parquet` — 1.4 GB.

| | |
|---|---|
| Rows | 511,962 |
| Row groups | **1** |
| Columns | `doc_id: string`, `source_type: string`, `title: string`, `content: string` |

**The whole corpus is a single row group.** This is the one corpus fact that
changes code: the obvious streaming loop, `for rg in range(num_row_groups):
read_row_group(rg)`, degenerates to reading all 1.4 GB into memory in one call
on this file, which is precisely the out-of-memory it was written to avoid. A
synthetic test parquet built with default settings has several row groups and
hides the problem.

Stream with `ParquetFile.iter_batches(batch_size=..., columns=[...])` instead,
which yields record batches from inside a row group. Measured: 20,000 rows at
`batch_size=2000` with three columns projected peaks at ~110 MB RSS.

The same fact governs point lookups. "Fetch one document by id" cannot be
"read the row group that contains it", because that is the entire file. Two
workable shapes, depending on the caller:

- **Slice-scale** (hundreds of documents, needed repeatedly for span validation
  and snippets): materialise the bodies once into SQLite during slice ingestion
  and read them from there.
- **Corpus-scale** (one pass over everything): stay in `iter_batches` and do the
  work streaming, never seeking.

Structure beyond these four columns — authors, timestamps, threads, channels,
recipients — lives inside `content` as source-specific text, so every parser
reads representative documents before assuming a template.

## Questions

`dataset/EnterpriseRAG-Bench/data/questions/test.parquet` — 500 rows, 1 row group.

| Column | Type | Access |
|---|---|---|
| `question_id` | string | allowed |
| `question_type` | string | allowed |
| `source_types` | list\<string\> | allowed |
| `question` | string | allowed |
| `expected_doc_ids` | list\<string\> | **forbidden** |
| `gold_answer` | string | **forbidden** |
| `answer_facts` | list\<string\> | **forbidden** |

The answer key ships in the same file as the question text, so the firewall is a
column whitelist inside the single reader permitted to open this file
(`parquet_reader.read_questions`). Everything else in the pipeline receives
question text that has already passed through it.

Note that `source_types`, though allowed, is a hint about where an answer lives.
It is fine for scoping an evaluation run and should stay out of retrieval, which
has to find the right sources on its own.

## Related firewalled material

`external/EnterpriseRAG-Bench/generated_data/employee_directory.yaml` is the
generator's identity oracle — every person with their email, title, and manager.
It is quarantined in the gitignored `eval-oracle/` and used only to score
entity-resolution precision and recall. See `docs/refs.lock.md`.
