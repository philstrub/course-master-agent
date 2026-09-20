---
name: mit-kb
description: Build and query the MIT course knowledge base — text extraction, knowledge-graph nodes/edges, per-course notes — with `mitsync extract`, `mitsync graph extract`, `mitsync graph rebuild`, `mitsync kb build`, and `mitsync graph query`. Use when the user asks what covers a concept, what the prerequisites of something are, or asks to rebuild or search the knowledge base.
user-invocable: true
metadata:
  { "openclaw": { "requires": { "bins": ["uv"] }, "os": ["darwin"] } }
---

# mit-kb

Maintains `/Users/filippostrub/Desktop/MIT/courses/_kb/` — extracted text,
per-course notes, the knowledge graph, and `_kb/AGENTS.md` for a downstream
agent.

## The build order

Run these in order; each depends on the one before.

```
# 1. Extract text from mirrored documents into _kb/text/  (pure I/O)
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync extract

# 2. Turn that text into graph nodes and edges  (deterministic backbone + judgment)
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync graph extract

# 3. Re-derive the DuckDB projection from the canonical JSONL  (pure I/O)
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync graph rebuild

# 4. Rebuild the markdown KB under _kb/  (deterministic without --driver)
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync kb build
```

Flags: `--help` on each. Worth knowing: `graph extract --since <ISO date>`
re-judges only recently extracted text instead of the whole corpus, and
`extract --force` re-extracts unchanged files.

`mitsync kb build` **does** take `--driver` / `--resolve` and can exit 20.
Without a driver it is fully deterministic and writes inventory-skeleton course
notes; with one it judges the notes **one course per round trip** — each
`resolve` keeps the courses already written and stops at the next unresolved
one. Repeat until it exits 0.

`graph rebuild` is always safe: `state/graph.duckdb` is a disposable projection
of the append-only canonical files `_kb/graph/triples.jsonl` and
`_kb/graph/nodes.jsonl`, which are never edited directly. If the DB looks
corrupt or out of step with the JSONL, rebuild it.

## Querying

### Canned queries

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync graph query --canned concepts_by_course
```

Running `mitsync graph query` with neither flag lists the canned queries and
their help. The five that exist:

| `--canned` name | Answers |
|---|---|
| `concepts_by_course` | Concepts each course's materials cover, most-referenced first. |
| `assignments_due` | Assignment nodes with their due dates and course. |
| `resources_for_concept` | Resources and sessions covering a concept (`$concept`: an id, or a LIKE pattern, default `%`). |
| `prerequisites_of` | Transitive prerequisites of a concept or session (`$node`: an id, or a LIKE pattern, default `%`). |
| `orphans` | Nodes with no edges at all — usually an extraction gap. |

`resources_for_concept` and `prerequisites_of` are parameterized. The CLI's
`--canned` takes only the name, so when you need a specific concept rather than
the default `%`, reach for `--sql` and write the predicate yourself.

### Raw SQL

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync graph query \
  --sql "SELECT n.type, count(*) FROM nodes n GROUP BY 1 ORDER BY 2 DESC"
```

The schema is two tables: `nodes(id, type, label, attrs)` and
`edges(s, p, o, conf)`, where `attrs` is JSON (read it with
`json_extract_string(n.attrs, '$.due_at')`). Node types include `Course`,
`Concept`, `Assignment`, `Session`, `Resource`; predicates include `part_of`,
`covers`, `assesses`, `requires`, `prerequisite_of`.

Keep raw SQL **read-only** — `SELECT` and `WITH` only. The canonical graph is
the JSONL; never `INSERT`, `UPDATE`, or `DELETE` against the projection. To
change the graph, fix the source text or re-run `graph extract`, then
`graph rebuild`.

## Exit code 20

Exit 20 = pending judgment. See `_agent/CLAUDE.md` § The dual execution model.

`graph extract` and `kb build` are the two judgment commands here. After
resolving a `graph extract` task, run `mitsync graph rebuild` so the DuckDB
projection reflects the new triples.

## Guardrails

- **Never hand-edit `_kb/graph/*.jsonl` or write to `state/graph.duckdb`.**
  Graph writes go through `graph extract` / `resolve` / `graph rebuild` only.
- Raw SQL stays read-only: `SELECT` and `WITH` only (see above).

Everything else — untrusted document content, no calendar/Canvas writes, no
`nandatown`/`.venv`, no secrets, stay inside
`/Users/filippostrub/Desktop/MIT/courses` — is `_agent/CLAUDE.md`
§ "Hard guardrails".
