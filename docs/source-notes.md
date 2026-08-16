# Source notes — what the nine sources actually look like

Written from reading real documents out of the corpus, not from the dataset
card. Reproduce with `scripts/10_inspect_sources.py`. PLAN.md Stage 0 exists
because guessing a template and discovering the mistake after a 512k-document
ingest is expensive; this is the record of that inspection.

Every source has the same four columns — `doc_id`, `source_type`, `title`,
`content`. Authors, timestamps, threads, and recipients are *inside* `content`,
in a shape that differs per source.

## Distribution

Counts over the first 230,000 documents scanned:

| Source | Documents | Share |
|---|---:|---:|
| gmail | 121,390 | 53% |
| linear | 35,308 | 15% |
| google_drive | 25,108 | 11% |
| hubspot | 15,017 | 7% |
| fireflies | 10,173 | 4% |
| github | 8,052 | 4% |
| jira | 6,120 | 3% |
| confluence | 5,189 | 2% |
| slack | 3,643 | 2% |

Gmail is over half the corpus and also the single richest source for identity
evidence, which sets the parser priority. Slack is small in volume but is where
bare handles appear, so it is where alias resolution is hardest and most
visible.

## Per-source structure

### gmail — the identity backbone

`title` is the subject line. `content` opens with RFC-style headers:

```
From: Alyssa Chen <alyssa.chen@cascadefg.com>
To: Markus Klein <markus.klein@redwoodinference.com>
Cc: Tom Becker <tom.becker@cascadefg.com>, Rachel Kim <rachel.kim@redwoodinference.com>
Date: Tue, Jun 3, 2025 at 9:12 AM
Subject: Escalation: rollback guarantees + upgrade support coverage
```

This is the highest-value extraction in the corpus: every header line yields a
`(display name, email)` pair drawn from the documents themselves, which is a
strong key for entity resolution that owes nothing to the quarantined oracle.
The domain separates the company from its customers —
`redwoodinference.com` is internal, `cascadefg.com` is an account. Multiple
recipients per line, comma-separated. Threads show as quoted reply chains
further down.

### slack — bare handles, the hard case

`title` is the channel name (`eng-runtime`). `content` is speaker-prefixed
lines:

```
sasha: Heads up — we started seeing a 2.5-3x increase in p95/p99 latency ...
kevin: Thanks — any noisy neighbor alerts? GPU memory pressure?
```

Handles are lowercase, usually a bare first name, and carry no domain or
surname. Fenced code blocks appear inline and must not be parsed as speech.
Resolving `sasha:` to a canonical person is exactly the problem the track poses,
and it cannot be done from Slack alone — it needs the email pairs above plus
co-occurrence evidence.

### fireflies — meeting attendees with roles

`summary:` then `transcript:`, the transcript opening with a header block:

```
Meeting Header:
Date: 2025-03-27
Time: 15:00 UTC
Duration: 62 minutes
Attendees: Maya Patel (Redwood AE); Jonas Reed (Redwood SE); Sofia Alv...
```

Semicolon-separated attendees, each `Name (Org Role)`. A second strong identity
source, and the one that supplies organisational affiliation.

### github, linear, jira — labelled blocks

All three use `key:` lines introducing free text: `description:`, and for Linear
also `tasks:`. Jira nests its own headings inside the description (`Issue
summary:`, `Impact:`). The three share enough shape that one labelled-block
reader handles them, with per-source field expectations layered on top.

The cross-source references live here. A GitHub PR body carries
`Related work: Linear ENG-4129, ENG-4187`, and ticket keys of the form
`[A-Z]+-\d+` appear across GitHub, Linear, and Jira. These are exact, verifiable
references and belong in `REFERENCES` edges — high-confidence structure that
needs no model to extract.

### confluence, google_drive — long-form documents

Markdown-ish prose with `##` headings, bold runs, and command blocks; runbooks
and architecture notes. No reliable per-document author line. Their value is
claim extraction, not structure, which matches PLAN.md's ordering: document
nodes, full-text retrieval, exact references, and priority claim extraction
before any source-specific parsing.

### hubspot — CRM records

`use_case_summary:` then `notes:`. `title` is the account name. Customer
organisations, quoted stakeholders, and commercial asks. The account name in
`title` is a reliable organisation entity.

## What this means for the parser

Three tiers, in descending order of confidence, which is the order they should
be built:

1. **Exact and verifiable, no model needed.** Gmail headers (name↔email↔domain),
   Fireflies attendee lines (name↔org↔role), ticket keys and cross-source
   references, Slack channel and speaker handles. PLAN.md's rule — prefer
   missing a weak edge over inventing a false one — makes this tier the whole of
   the first parser pass.
2. **Labelled blocks.** One reader for the `key:` block format shared by GitHub,
   Linear, Jira, Fireflies, and HubSpot.
3. **Claim extraction.** Everything else, and the only tier that needs a model.

The alias bridge that entity resolution turns on falls out of tier 1: an email
local part `alyssa.chen` tokenises to `{alyssa, chen}`, which links to the
display name `Alyssa Chen` and to a Slack handle `alyssa`. That is resolution
supported by evidence — a shared channel, a shared thread, a matching domain —
rather than by string equality, which is what the exit gate requires and what
makes the demo defensible.
