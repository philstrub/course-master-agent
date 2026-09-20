# ADR 0002: Knowledge graph canonical storage is append-only JSONL, projected into DuckDB

## Status

Accepted

## Context

`mitsync` builds an ontology-driven knowledge graph over course content
(documents, topics, assignments, deadlines) so a downstream agent can
query relationships instead of grepping text. The real ontology will be
supplied by the student later; a v0 placeholder ships first. This means
the graph's shape (node types, allowed predicates) will change at least
once, and the storage design needs to survive that without losing or
silently corrupting history. A Neo4j+Cypher backend is a plausible future
alternative if the graph grows large or needs richer traversal queries,
and the interface should not preclude that.

## Decision

The canonical, source-of-truth representation of the graph is two
append-only JSONL files: `_kb/graph/nodes.jsonl` and
`_kb/graph/triples.jsonl`. Every write is an append; a correction to a
node is a later line with the same `id` (last-write-wins on projection); a
retraction of a triple is a new line referencing the original with
`"retracted": true`, never an in-place edit or delete. Each triple record
carries an `ontology_version` field.

A DuckDB database (`state/graph.duckdb`, tables `nodes` and `edges`) is
built from the JSONL files by `mitsync graph rebuild`, which truncates and
fully replays the JSONL on every run. The DuckDB file is a disposable,
rebuildable projection — it is never a second source of truth and is never
hand-edited. `mitsync graph query` reads from this projection.

The graph module's public interface (`append_triples`, `rebuild`, `query`)
does not expose DuckDB-specific types, so a Neo4j/Cypher implementation of
the same interface remains possible later without touching the JSONL
format or any caller.

## Consequences

- An ontology migration (when the real ontology arrives) is safe: update
  the ontology mapping, run `graph rebuild`, and the projection reflects
  the new ontology without rewriting or losing any historical JSONL line.
  Triples that don't map cleanly under the new ontology are flagged by
  `doctor` (`OntologyMismatchError`) rather than silently dropped or
  guessed at.
- Corruption of `state/graph.duckdb` (crash mid-write, disk issue) is a
  non-event: delete it and re-run `graph rebuild`. There is no backup
  strategy needed for the DuckDB file itself, only for the JSONL.
- Every historical extraction is preserved for audit/debugging — you can
  always see what the graph believed at any point in time by replaying
  JSONL up to a given line/timestamp.
- Query performance is bounded by how large the JSONL replay grows;
  acceptable for a single student's 7-course knowledge base, but the
  append-only design means a full rebuild's cost grows with total history,
  not just current state — a future optimization (periodic compaction of
  retracted/superseded lines) is possible without changing the interface.

## Alternatives considered

- **DuckDB as the sole store, writing nodes/edges directly.** Rejected:
  makes every ontology change a destructive schema migration against the
  only copy of the data, and complicates any future backend swap (e.g. to
  Neo4j) since callers would already be coupled to SQL semantics.
- **Neo4j from day one.** Rejected for v0: adds an external database
  dependency (a running Neo4j instance) for a single-user, modest-sized
  graph where DuckDB (embedded, zero-ops) is sufficient; kept as an
  explicitly possible future backend behind the same interface instead.
- **Mutable JSON files (rewrite nodes.json/edges.json in place).**
  Rejected: loses history on every correction, and makes concurrent
  writes (e.g. `kb build` running while `graph query` reads) unsafe
  without additional locking that an append-only log avoids by
  construction.
