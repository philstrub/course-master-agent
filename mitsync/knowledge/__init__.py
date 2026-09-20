"""
# Knowledge

Turning filed documents into something a *downstream* agent can read without
opening a single PDF.

## 1. What This Module Does

`extract` renders PDFs, notebooks and spreadsheets to text under `_kb/text/`.
`graph` extracts ontology-typed nodes and edges into the append-only
`_kb/graph/*.jsonl` and projects them into DuckDB. `kb` assembles the indexes,
per-course notes, `_kb/manifest.json` and `_kb/AGENTS.md`.

## 2. Why This Module Exists

The primary consumer of this repo's output is not a human -- it is another
agent helping with coursework. Everything here is written to be pointed at:
one entry file (`_kb/AGENTS.md`), one machine-readable inventory
(`_kb/manifest.json`), and a graph that can be traversed instead of grepped.

## 3. How It Fits in the Architecture

Reads the mirror and the filed folders; writes only inside `_kb/` and
`state/`. Two of the three commands here take a judgment driver -- `graph
extract` and `kb build` -- and both degrade to a deterministic skeleton when
no judge is available, which is a documented contract rather than a silent
failure.

## 4. Key Concepts

**The JSONL is the source of truth; DuckDB is a projection.** `state/graph.duckdb`
can be deleted and rebuilt from `_kb/graph/*.jsonl` with identical query
results. A backend swap is therefore a rebuild, never a data migration. See
`docs/ADR/0002`.

**Extraction is idempotent.** Every node and edge records its source document
and extractor version, so a re-run replaces rather than duplicates.

**Ontology-driven.** Node and edge types come from `config/ontology.yml`, not
from Python. A broken ontology fails the build loudly.
"""
