"""
# Knowledge

Turning filed documents into something a *downstream* agent can read without
opening a single PDF.

## 1. What This Module Does

`extract` renders PDFs, notebooks and spreadsheets to text under `_kb/text/`.
`graph` keeps ontology-typed nodes and edges in the append-only
`_kb/graph/*.jsonl` -- a deterministic backbone plus whatever the agent adds
with `graph add` -- and projects them into DuckDB. `kb` assembles the indexes,
`_kb/manifest.json` and `_kb/AGENTS.md`.

## 2. Why This Module Exists

The primary consumer of this repo's output is not a human -- it is another
agent helping with coursework. Everything here is written to be pointed at:
one entry file (`_kb/AGENTS.md`), one machine-readable inventory
(`_kb/manifest.json`), and a graph that can be traversed instead of grepped.

## 3. How It Fits in the Architecture

Reads the mirror and the filed folders; writes only inside `_kb/` and
`state/`. Nothing here judges: every command is deterministic, and the facts
that need reading comprehension -- concepts, topic notes -- are written by the
driving agent, validated here, never generated here. `kb build` never touches
an agent-written `NOTES.md`.

## 4. Key Concepts

**The JSONL is the source of truth; DuckDB is a projection.** `state/graph.duckdb`
can be deleted and rebuilt from `_kb/graph/*.jsonl` with identical query
results. A backend swap is therefore a rebuild, never a data migration.

**Extraction is idempotent.** Every node and edge records its source document
and extractor version, so a re-run replaces rather than duplicates.

**Ontology-enforced.** Node and edge types, typed ids and the structural
rules are pydantic models in `ontology.py`; a record that violates them is
refused before it is stored, and `graph check` reports a graph that is
incomplete.
"""
