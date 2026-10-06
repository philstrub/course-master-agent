"""
# Knowledge Graph

Append-only JSONL truth, a regenerated deterministic backbone, a rebuildable
DuckDB projection, and the ontology (`ontology.py`) every record must satisfy.

## 1. What This Module Does

Stores nodes and edges and projects them into a queryable database.
`build_backbone` regenerates the
deterministic part of the graph from the files on disk; `add_records`
validates and appends the nodes and edges the driving agent wrote itself;
`rebuild` re-derives everything downstream from the JSONL; `query` runs a
canned query by name or raw SQL; `check` lists every structural violation,
which is the sync loop's stopping condition.

## 2. Why This Module Exists

An agent helping with coursework needs to answer "what does this concept
appear in?" across a whole term, which no single document can answer. The
graph is that cross-document index.

Its storage split exists because agent-written facts accumulate slowly and
expensively and must never be lost to a schema change or a corrupted database.
`_kb/graph/nodes.jsonl` and `_kb/graph/triples.jsonl` are append-only and are
the ONLY source of agent-written truth. `_kb/graph/backbone.jsonl` is derived
state: `build_backbone` overwrites it from the disk on every run, so a deleted
or re-filed file leaves no ghost behind. `state/graph.duckdb` is a cache: deleting it and
running `rebuild()` must reproduce identical query results, and a test asserts
exactly that.

## 3. How It Fits in the Architecture

Walks the course folders (never the Canvas mirror) with `extract.iter_sources`.
`check` also reads `organize.unfiled`, so Canvas files waiting to be filed
block the sync loop.
Nothing here judges anything: the backbone is derived from paths and content
hashes, and every other fact arrives through `graph add` from an agent that
read the documents itself.

## 4. Key Concepts

**Nodes and edges.** Not vertices, not entities, not relationships. Node
types and edge types.

**The ontology is code.** `ontology.py` declares the node types, edge types,
typed ids and structural rules as pydantic models. Every node and edge is
validated against it before it is stored or projected, and a violation raises
`OntologyError` naming the offending record.

**The deterministic backbone, from the course folders.** The graph mirrors
what the student keeps, not what Canvas dumped: one `Course` node per mapped
folder; one `File` (document) / `DataFile` (data, code) node per file in it,
carrying its `path` and, once extracted, the path of its `text`; and, wherever
the folder a file is filed in says so unambiguously, its parent item
(`assignments/<item>/`, `recitations/<item>/`, `syllabus/`, a file in
`lectures/` whose name states its number, `other/` / `notes/`). Every Canvas
and Gradescope assignment of a mapped course is an `Assignment` node carrying
the student's submission status, joined to its `assignments/<item>/` node by
title key. Every repo is a `Repo` node with its parent: one `courses.yml`
declares hangs off its assignment or else its course, and an
`assignments/<item>/` folder holding a `.git` is that assignment's repo.

**The agent supervises the rest.** `check` lists what needs judgment:
`lecture_unattached` (a lecture file with no number in its name),
`no_concepts` (an item whose files have text but no concept yet), `unfiled`,
and `canvas_unfiled` (a Canvas file not yet in a course folder). The agent
fixes the first three with `graph add`, and puts the last in a filing plan
the student applies. The loop stops when nothing blocking is left.

**Stale edges.** An agent edge whose endpoint the backbone no longer produces
(the file was deleted or re-filed) is kept in the append-only file, left out of
every projection, and reported by `check` as `stale_edge`. That is the
documented skip in `load_graph`.

**`graph add` is all or nothing.** A node needs `id`, `type`, `label` and
`src` (its source documents); an edge needs `s`, `p`, `o` and `src`; unknown
keys are refused, and an edge endpoint must already exist or be defined in the
same file. One bad line rejects the whole file with every error listed by line
number, so a half-applied batch can never leave dangling facts behind.

**Projection rules.** Backbone lines are read first, then agent lines. For
nodes, attrs merge across lines (later keys win), the last label wins and
`src` unions; for edges, `(s, p, o, src)` is the dedupe key. Appending the
same fact twice is free, which is what makes the append-only file safe to
re-run against.

**Regeneration leaves mtimes alone.** `write_if_changed` only writes on a real
change, so a rebuild over unchanged inputs looks like a no-op to every tool
watching the tree.

**Why exceptions are caught here.** Two handlers, both at an input boundary.
A malformed line in the canonical JSONL becomes a `MitsyncError` naming the
file and line number -- mitsync wrote that file, so malformed means mitsync
wrote garbage, and the append-only source of truth must never be read past a
bad line. In `add_records`, `json.JSONDecodeError` and `OntologyError` on an
agent-written line are collected rather than raised one at a time, so the
agent sees every problem in its file at once; the file is still rejected as a
whole.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from rich.console import Console
from rich.table import Table

from mitsync.core.clock import now_iso
from mitsync.core.errors import MitsyncError, OntologyError
from mitsync.core.logging import get_logger
from mitsync.filing.course_map import (
    existing_course_folders,
    load_course_map,
)
from mitsync.knowledge.extract import DOCUMENT_EXTENSIONS, iter_sources, sha256_of
from mitsync.knowledge.ontology import (
    BUCKET_EDGES,
    DOCUMENT_SUFFIXES,
    Assignment,
    Lecture,
    Recitation,
    Violation,
    course_id,
    item_id,
    item_label,
    slug,
    structure_violations,
    syllabus_id,
    title_key,
    validate_edge,
    validate_node,
)

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)

EXTRACTOR_VERSION = "1"

NODES_FILE = "nodes.jsonl"
TRIPLES_FILE = "triples.jsonl"
BACKBONE_FILE = "backbone.jsonl"


# --------------------------------------------------------------------------
# JSONL store (the source of truth)
# --------------------------------------------------------------------------
@dataclass
class GraphReport:
    nodes: int = 0
    edges: int = 0
    documents: int = 0
    chunks: int = 0
    appended_nodes: int = 0
    appended_edges: int = 0
    changed: bool = False
    db: Path | None = None

    def summary(self) -> str:
        bits = [f"{self.nodes} nodes", f"{self.edges} edges"]
        if self.documents:
            bits.append(f"{self.documents} files")
        if self.appended_nodes or self.appended_edges:
            bits.append(f"+{self.appended_nodes} new nodes/+{self.appended_edges} new edges")
        return ", ".join(bits)


def nodes_jsonl(settings: Settings) -> Path:
    return settings.paths.kb_graph / NODES_FILE


def triples_jsonl(settings: Settings) -> Path:
    return settings.paths.kb_graph / TRIPLES_FILE


def backbone_jsonl(settings: Settings) -> Path:
    return settings.paths.kb_graph / BACKBONE_FILE


def _backbone_records(settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Backbone lines split into (nodes, edges); an edge is a line with `p`."""
    records = list(_read_jsonl(backbone_jsonl(settings)))
    return [r for r in records if "p" not in r], [r for r in records if "p" in r]


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                # mitsync wrote this file. Malformed means mitsync wrote garbage,
                # and the append-only source of truth must never be read past it.
                raise MitsyncError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
            if isinstance(record, dict):
                yield record


def _version_key(value: Any) -> tuple[int, str]:
    text = str(value or "0")
    return (int(text), "") if text.isdigit() else (0, text)


def normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    src = node.get("src") or []
    if isinstance(src, str):
        src = [src]
    return {
        "id": str(node.get("id", "")),
        "type": str(node.get("type", "")),
        "label": str(node.get("label") or node.get("id", "")),
        "attrs": dict(node.get("attrs") or {}),
        "src": sorted({str(s) for s in src}),
        "ts": str(node.get("ts") or now_iso()),
        "extractor_version": str(node.get("extractor_version") or EXTRACTOR_VERSION),
    }


def normalize_edge(edge: dict[str, Any]) -> dict[str, Any]:
    conf = edge.get("conf")
    return {
        "s": str(edge.get("s", "")),
        "p": str(edge.get("p", "")),
        "o": str(edge.get("o", "")),
        "attrs": dict(edge.get("attrs") or {}),
        "src": str(edge.get("src") or ""),
        "conf": float(conf) if isinstance(conf, int | float) else 1.0,
        "ts": str(edge.get("ts") or now_iso()),
        "extractor_version": str(edge.get("extractor_version") or EXTRACTOR_VERSION),
    }


def edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(edge.get("s")), str(edge.get("p")), str(edge.get("o")), str(edge.get("src") or ""))


def merge_node(prior: dict[str, Any] | None, node: dict[str, Any]) -> dict[str, Any]:
    """A later line for the same id: attrs merge (later keys win), `src` unions."""
    if prior is None:
        return node
    return {
        **node,
        "attrs": {**prior["attrs"], **node["attrs"]},
        "src": sorted(set(prior["src"]) | set(node["src"])),
    }


def load_nodes(settings: Settings) -> dict[str, dict[str, Any]]:
    """Project the backbone, then `nodes.jsonl`, merging lines per id."""
    out: dict[str, dict[str, Any]] = {}
    backbone, _ = _backbone_records(settings)
    for raw in [*backbone, *_read_jsonl(nodes_jsonl(settings))]:
        node = normalize_node(raw)
        if not node["id"]:
            raise MitsyncError(f"{nodes_jsonl(settings)} has a node record with no id: {raw!r}")
        out[node["id"]] = merge_node(out.get(node["id"]), node)
    return dict(sorted(out.items()))


def load_edges(settings: Settings) -> list[dict[str, Any]]:
    """Project the backbone's edges and `triples.jsonl`, deduped on (s,p,o,src).

    A later line with a newer `extractor_version` supersedes an older one for
    the same key, so re-extraction replaces rather than accumulates.
    """
    best: dict[tuple[str, str, str, str], tuple[tuple[int, str], int, dict[str, Any]]] = {}
    _, backbone = _backbone_records(settings)
    for index, raw in enumerate([*backbone, *_read_jsonl(triples_jsonl(settings))]):
        edge = normalize_edge(raw)
        if not (edge["s"] and edge["p"] and edge["o"]):
            raise MitsyncError(
                f"{triples_jsonl(settings)} has an edge record missing s/p/o: {raw!r}"
            )
        key = edge_key(edge)
        rank = (_version_key(edge["extractor_version"]), index)
        prior = best.get(key)
        if prior is None or (rank[0], rank[1]) >= (prior[0], prior[1]):
            best[key] = (rank[0], rank[1], edge)
    return [edge for _, (_, _, edge) in sorted(best.items())]


def load_graph(
    settings: Settings,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(nodes, live edges, stale edges).

    A stale edge names an endpoint no line defines any more -- the backbone
    stopped producing a file or item the agent once pointed at. It stays in the
    append-only file, is left out of every projection, and `check` reports it.
    """
    nodes = load_nodes(settings)
    edges = load_edges(settings)
    live = [e for e in edges if e["s"] in nodes and e["o"] in nodes]
    stale = [e for e in edges if e["s"] not in nodes or e["o"] not in nodes]
    return nodes, live, stale


def _append(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def append_nodes(settings: Settings, nodes: Iterable[dict[str, Any]]) -> int:
    """Append nodes whose merged result validates, skipping ones that change nothing."""
    existing = load_nodes(settings)
    fresh: list[dict[str, Any]] = []
    for raw in nodes:
        node = normalize_node(raw)
        prior = existing.get(node["id"])
        merged = merge_node(prior, node)
        validate_node(merged)
        if prior is not None and all(
            prior[k] == merged[k] for k in ("type", "label", "attrs", "src", "extractor_version")
        ):
            continue
        existing[node["id"]] = merged
        fresh.append(node)
    if fresh:
        _append(nodes_jsonl(settings), fresh)
    return len(fresh)


def append_edges(settings: Settings, edges: Iterable[dict[str, Any]]) -> int:
    """Append validated edges, skipping (s,p,o,src) keys already at this version."""
    node_types = {nid: node["type"] for nid, node in load_nodes(settings).items()}
    seen = {edge_key(e): _version_key(e["extractor_version"]) for e in load_edges(settings)}
    fresh: list[dict[str, Any]] = []
    for raw in edges:
        edge = normalize_edge(raw)
        validate_edge(edge, node_types)
        key = edge_key(edge)
        version = _version_key(edge["extractor_version"])
        if key in seen and seen[key] >= version:
            continue
        seen[key] = version
        fresh.append(edge)
    if fresh:
        _append(triples_jsonl(settings), fresh)
    return len(fresh)


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
@runtime_checkable
class GraphBackend(Protocol):
    def upsert_nodes(self, nodes: Iterable[dict]) -> int: ...
    def upsert_edges(self, edges: Iterable[dict]) -> int: ...
    def query(self, q: str, params: dict | None = None) -> list[dict]: ...
    def rebuild(self) -> None: ...


_NODE_COLUMNS = (
    "{'id':'TEXT','type':'TEXT','label':'TEXT','attrs':'JSON','src':'JSON','ts':'TIMESTAMP'}"
)
_EDGE_COLUMNS = (
    "{'s':'TEXT','p':'TEXT','o':'TEXT','attrs':'JSON','src':'TEXT',"
    "'conf':'DOUBLE','ts':'TIMESTAMP'}"
)
_CREATE_NODES = (
    "CREATE TABLE nodes(id TEXT PRIMARY KEY, type TEXT, label TEXT, "
    "attrs JSON, src JSON, ts TIMESTAMP)"
)
_CREATE_EDGES = (
    "CREATE TABLE edges(s TEXT, p TEXT, o TEXT, attrs JSON, src TEXT, conf DOUBLE, ts TIMESTAMP)"
)


class DuckDBBackend:
    """Default backend: a DuckDB file projected from the JSONL truth."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.paths.graph_db

    # --- connection -------------------------------------------------------
    def _connect(self):
        import duckdb

        con = duckdb.connect(str(self.path))
        con.execute(_CREATE_NODES.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
        con.execute(_CREATE_EDGES.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
        return con

    # --- writes -----------------------------------------------------------
    def upsert_nodes(self, nodes: Iterable[dict]) -> int:
        rows = [normalize_node(n) for n in nodes]
        if not rows:
            return 0
        con = self._connect()
        try:
            con.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["id"],
                        r["type"],
                        r["label"],
                        json.dumps(r["attrs"], sort_keys=True),
                        json.dumps(r["src"]),
                        r["ts"],
                    )
                    for r in rows
                ],
            )
        finally:
            con.close()
        return len(rows)

    def upsert_edges(self, edges: Iterable[dict]) -> int:
        rows = [normalize_edge(e) for e in edges]
        if not rows:
            return 0
        con = self._connect()
        try:
            for r in rows:
                con.execute(
                    "DELETE FROM edges WHERE s = ? AND p = ? AND o = ? AND src = ?",
                    [r["s"], r["p"], r["o"], r["src"]],
                )
            con.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["s"],
                        r["p"],
                        r["o"],
                        json.dumps(r["attrs"], sort_keys=True),
                        r["src"],
                        r["conf"],
                        r["ts"],
                    )
                    for r in rows
                ],
            )
        finally:
            con.close()
        return len(rows)

    # --- reads ------------------------------------------------------------
    def query(self, q: str, params: dict | None = None) -> list[dict]:
        con = self._connect()
        try:
            cur = con.execute(q, params) if params else con.execute(q)
            columns = [d[0] for d in cur.description or []]
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        finally:
            con.close()

    # --- projection -------------------------------------------------------
    def rebuild(self) -> None:
        """Drop every table and reload from the JSONL files (stale edges left out)."""
        by_id, edges, _ = load_graph(self.settings)
        nodes = list(by_id.values())
        for node in nodes:
            validate_node(node)
        node_types = {n["id"]: n["type"] for n in nodes}
        for edge in edges:
            validate_edge(edge, node_types)

        con = self._connect()
        try:
            con.execute("DROP TABLE IF EXISTS edges")
            con.execute("DROP TABLE IF EXISTS nodes")
            con.execute(_CREATE_NODES)
            con.execute(_CREATE_EDGES)
            with tempfile.TemporaryDirectory(prefix="mitsync-graph-") as tmp:
                node_file = Path(tmp) / "nodes.ndjson"
                edge_file = Path(tmp) / "edges.ndjson"
                _write_ndjson(node_file, nodes, ("id", "type", "label", "attrs", "src", "ts"))
                _write_ndjson(edge_file, edges, ("s", "p", "o", "attrs", "src", "conf", "ts"))
                con.execute(
                    "INSERT INTO nodes SELECT id, type, label, attrs, src, ts FROM "
                    f"read_json_auto(?, columns={_NODE_COLUMNS}, format='newline_delimited')",
                    [str(node_file)],
                )
                con.execute(
                    "INSERT INTO edges SELECT s, p, o, attrs, src, conf, ts FROM "
                    f"read_json_auto(?, columns={_EDGE_COLUMNS}, format='newline_delimited')",
                    [str(edge_file)],
                )
        finally:
            con.close()


def write_if_changed(path: Path, body: str) -> None:
    """Write only on a real change, so regeneration leaves mtimes alone."""
    text = body.rstrip() + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def _write_ndjson(path: Path, records: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps({c: record.get(c) for c in columns}, default=str) + "\n")


def get_backend(settings: Settings) -> GraphBackend:
    backend = settings.graph.backend
    if backend == "duckdb":
        return DuckDBBackend(settings)
    raise MitsyncError(
        f"unsupported graph backend {backend!r}: canned queries run on 'duckdb'. "
        "Neo4j is a second projection, not a backend: `mitsync graph push` fills it and "
        "`mitsync graph cypher` reads it. Set graph.backend: duckdb in config/settings.yml."
    )


# --------------------------------------------------------------------------
# rebuild / query
# --------------------------------------------------------------------------
def rebuild(settings: Settings) -> GraphReport:
    """Re-derive the DuckDB projection from JSONL."""
    backend = get_backend(settings)
    backend.rebuild()
    nodes, edges, _ = load_graph(settings)
    report = GraphReport(nodes=len(nodes), edges=len(edges), db=settings.paths.graph_db)
    log.info("graph rebuild: %s", report.summary())
    print(f"graph rebuild: {report.summary()} -> {report.db}")
    return report


@dataclass(frozen=True)
class Canned:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    help: str = ""


_ITEM_OF_COURSE = "('lecture_of_course', 'assignment_of_course', 'recitation_of_course')"
_CONCEPT_IN = "('concept_in_lecture', 'concept_in_assignment', 'concept_in_recitation')"
_FILE_OF_ITEM = (
    "('file_of_lecture', 'file_of_assignment', 'file_of_recitation', 'file_of_syllabus')"
)

CANNED: dict[str, Canned] = {
    "concepts_by_course": Canned(
        sql=f"""
            SELECT c.label AS course, n.label AS concept, count(DISTINCT e.o) AS items
            FROM edges e
            JOIN nodes n ON n.id = e.s AND n.type = 'Concept'
            JOIN edges pe ON pe.s = e.o AND pe.p IN {_ITEM_OF_COURSE}
            JOIN nodes c ON c.id = pe.o
            WHERE e.p IN {_CONCEPT_IN}
            GROUP BY 1, 2
            ORDER BY 1, 3 DESC, 2
        """,
        help="Concepts each course teaches or assesses, by number of lectures/recitations/"
        "assignments.",
    ),
    "assignments_due": Canned(
        sql="""
            SELECT DISTINCT c.label AS course,
                   n.label AS assignment,
                   json_extract_string(n.attrs, '$.due_at') AS due_at,
                   json_extract_string(n.attrs, '$.submission_status') AS status,
                   n.id AS node_id
            FROM nodes n
            JOIN edges e ON e.s = n.id AND e.p = 'assignment_of_course'
            JOIN nodes c ON c.id = e.o
            WHERE n.type = 'Assignment'
            ORDER BY due_at NULLS LAST, course, assignment
        """,
        help="Assignments with their course, due date and the student's submission status.",
    ),
    "files_for_concept": Canned(
        sql=f"""
            SELECT DISTINCT t.label AS concept,
                   i.type AS item_type,
                   i.label AS item,
                   json_extract_string(f.attrs, '$.path') AS path
            FROM edges ce
            JOIN nodes t ON t.id = ce.s AND t.type = 'Concept'
            JOIN nodes i ON i.id = ce.o
            JOIN edges fe ON fe.o = i.id AND fe.p IN {_FILE_OF_ITEM}
            JOIN nodes f ON f.id = fe.s
            WHERE ce.p IN {_CONCEPT_IN}
              AND (t.id = $concept OR lower(t.label) LIKE lower($concept))
            ORDER BY concept, item_type, item, path
        """,
        params={"concept": "%"},
        help="Where a concept is taught or assessed, and the files to read "
        "($concept: id or LIKE pattern).",
    ),
    "files_of": Canned(
        sql="""
            SELECT DISTINCT i.label AS item,
                   e.p AS edge,
                   json_extract_string(e.attrs, '$.role') AS role,
                   json_extract_string(f.attrs, '$.path') AS path,
                   json_extract_string(f.attrs, '$.text') AS text
            FROM edges e
            JOIN nodes i ON i.id = e.o
            JOIN nodes f ON f.id = e.s AND f.type IN ('File', 'DataFile')
            WHERE i.id = $item OR lower(i.label) LIKE lower($item)
            ORDER BY item, edge, path
        """,
        params={"item": "%"},
        help="Files attached to a lecture, recitation, assignment, syllabus or course, "
        "with their extracted text ($item: id or LIKE pattern).",
    ),
    "submitted": Canned(
        sql="""
            SELECT a.label AS assignment,
                   json_extract_string(a.attrs, '$.submission_status') AS status,
                   json_extract_string(a.attrs, '$.submitted_via') AS via,
                   json_extract_string(f.attrs, '$.path') AS submitted_file
            FROM nodes a
            LEFT JOIN edges e ON e.o = a.id AND e.p = 'file_of_assignment'
                 AND json_extract_string(e.attrs, '$.role') = 'submission'
            LEFT JOIN nodes f ON f.id = e.s
            WHERE a.type = 'Assignment'
              AND (json_extract_string(a.attrs, '$.submission_status')
                     IN ('submitted', 'late', 'graded') OR f.id IS NOT NULL)
            ORDER BY assignment, submitted_file
        """,
        help="What the student has turned in, and which file it was.",
    ),
    "orphans": Canned(
        sql="""
            SELECT n.id, n.type, n.label
            FROM nodes n
            WHERE NOT EXISTS (SELECT 1 FROM edges e WHERE e.s = n.id OR e.o = n.id)
            ORDER BY n.type, n.id
        """,
        help="Nodes with no edges at all -- usually an extraction gap.",
    ),
}


def query(
    settings: Settings,
    sql: str | None = None,
    canned: str | None = None,
    params: dict[str, Any] | None = None,
    show: bool = True,
) -> list[dict[str, Any]]:
    """Run a canned query by name or raw SQL; returns rows and, if `show`, prints a table."""
    if not settings.paths.graph_db.exists():
        rebuild(settings)
    backend = get_backend(settings)

    if canned:
        spec = CANNED.get(canned)
        if spec is None:
            raise MitsyncError(
                f"unknown canned query {canned!r}; available: {', '.join(sorted(CANNED))}"
            )
        merged = {**spec.params, **(params or {})}
        rows = backend.query(spec.sql, merged or None)
        title = f"canned: {canned}"
    elif sql:
        rows = backend.query(sql, params)
        title = "sql"
    else:
        rows = backend.query(
            "SELECT 'nodes' AS table_name, count(*) AS rows FROM nodes "
            "UNION ALL SELECT 'edges', count(*) FROM edges ORDER BY table_name"
        )
        title = "graph summary"
        if show:
            print("Canned queries: " + ", ".join(sorted(CANNED)))
            for name, spec in sorted(CANNED.items()):
                print(f"  {name}: {spec.help}")

    if show:
        print_rows(title, rows)
    return rows


def print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    """Print a table whose columns are whatever keys the first row carries."""
    console = Console()
    if not rows:
        console.print(f"[dim]{title}: no rows[/dim]")
        return
    table = Table(title=title)
    for column in rows[0]:
        table.add_column(str(column), overflow="fold")
    for row in rows:
        table.add_row(*["" if v is None else str(v) for v in row.values()])
    console.print(table)


# --------------------------------------------------------------------------
# the deterministic backbone: files on disk -> nodes and edges
# --------------------------------------------------------------------------
# What the graph knows about. Beyond the extractable documents: data, code and
# LaTeX sources, which is what an assignment folder is mostly made of.
GRAPH_EXTENSIONS = DOCUMENT_EXTENSIONS | frozenset(
    {".zip", ".py", ".jl", ".r", ".rmd", ".tex", ".tsv", ".parquet", ".dat", ".mod", ".sql"}
)

# A lecture number the filename states outright: `Lec03_2026`, `Lecture-04`,
# `Fall_2026_15_C57-L5`, `Class_2`, `3_sparse_linear_regression`. Names like
# `Week 1 - 2 - What Is an AI Agent` or `LinearAlgebra1` state none, so the
# agent numbers those (`graph check` lists them as `lecture_unattached`).
_LECTURE_RX = re.compile(
    r"^0*(\d{1,2})[\s_.-]+(.*)$|(?:^|[^a-z])(?:lec(?:ture)?|l|class|session)[\s_-]*0*(\d{1,2})(?!\d)"
)
_NUMBER_RX = re.compile(r"(\d+)$")


def lecture_number(stem: str) -> tuple[int, str | None] | None:
    """`(number, title)` when a file name in `lectures/` states its lecture number."""
    m = _LECTURE_RX.search(stem.lower())
    if m is None:
        return None
    if m.group(1):
        return int(m.group(1)), " ".join(m.group(2).replace("_", " ").split()) or None
    return int(m.group(3)), None


def file_id(rel: str) -> str:
    """Id of the file node whose canonical path is `rel`: stable across edits."""
    return "file:" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]  # noqa: S324


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, UTC).isoformat()


def build_backbone(settings: Settings) -> GraphReport:
    """Regenerate `_kb/graph/backbone.jsonl` from the student's course folders.

    Only the folders `config/courses.yml` maps are walked. The `_canvas/`
    mirror is the raw dump, not the student's material: a Canvas file enters
    the graph once it is filed into a course folder, and `graph check` lists
    the rest as `canvas_unfiled`. Files are grouped by content hash: every copy
    filed in a bucket is its own node, and a loose copy of the same bytes is
    recorded on it as a duplicate for the human. No judgment is involved, so
    this is safe to run unattended from `kb build`.
    """
    from mitsync.knowledge.extract import course_roots, text_path_for

    ws = settings.paths.workspace
    folders = existing_course_folders(settings)
    groups: dict[str, list[str]] = defaultdict(list)
    digests: dict[str, str | None] = {}
    mtimes: dict[str, float] = {}
    for path in iter_sources(settings, GRAPH_EXTENSIONS, roots=course_roots(settings)):
        rel = settings.paths.safe_relative(path).as_posix()
        digest = sha256_of(path)
        key = digest or rel  # too large to hash: a group of its own
        groups[key].append(rel)
        digests[key] = digest
        mtimes[rel] = path.stat().st_mtime

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    entries = [e for e in load_course_map(settings) if e.get("folder") in folders]
    for entry in entries:  # mapped but not on disk: `doctor` reports it
        attrs = {"folder": entry["folder"]}
        attrs |= {k: entry[k] for k in ("course_number", "canvas_id") if entry.get(k)}
        cid = course_id(entry["folder"])
        nodes[cid] = {
            "id": cid, "type": "Course", "label": entry["folder"], "attrs": attrs,
            "src": [], "ts": _iso(0),
        }  # fmt: skip

    def item(nid: str, ntype: str, label: str, attrs: dict[str, Any], rel: str, ts: str) -> None:
        node = nodes.setdefault(
            nid, {"id": nid, "type": ntype, "label": label, "attrs": attrs, "src": [], "ts": ts}
        )
        node["src"] = sorted({*node["src"], rel})
        node["ts"] = max(node["ts"], ts)

    for key, rels in sorted(groups.items()):
        rels = sorted(rels)
        filed = [r for r in rels if len(r.split("/")) > 2 and r.split("/")[1] in BUCKET_EDGES]
        loose = [r for r in rels if r not in filed]
        # Each filed copy is its own node: one dataset legitimately filed under two
        # recitations belongs to both. A loose copy of filed content is a duplicate
        # for the human, and merges into the first.
        heads = filed or [loose[0]]
        extras = [r for r in loose if r not in heads]
        for n, canonical in enumerate(heads):
            folder = canonical.split("/")[0]
            copies = sorted({canonical, *extras} if n == 0 else {canonical})
            ts = _iso(max(mtimes[r] for r in copies))
            suffix = Path(canonical).suffix.lower()
            fid = file_id(canonical)
            attrs: dict[str, Any] = {
                "path": canonical,
                "title": " ".join(Path(canonical).stem.replace("_", " ").replace("-", " ").split()),
                "content_type": suffix.lstrip("."),
            }
            text = text_path_for(settings, canonical)
            if text.is_file():
                attrs["text"] = text.relative_to(ws).as_posix()
            if n == 0 and extras:
                attrs["duplicates"] = extras
            if digests[key]:
                attrs["sha256"] = digests[key]
            ntype = "File" if suffix in DOCUMENT_SUFFIXES else "DataFile"
            nodes[fid] = {
                "id": fid, "type": ntype, "label": Path(canonical).name, "attrs": attrs,
                "src": copies, "ts": ts,
            }  # fmt: skip

            cid = course_id(folder)
            parts = canonical.split("/")
            bucket = parts[1] if len(parts) > 2 else ""
            parent: tuple[str, str, dict[str, Any]] | None = None
            if bucket in ("assignments", "recitations") and len(parts) > 3:
                kind = Assignment if bucket == "assignments" else Recitation
                iid = item_id(kind, folder, parts[2])
                number = _NUMBER_RX.search(parts[2])
                iattrs: dict[str, Any] = {"folder": "/".join(parts[:3]), "title": parts[2]}
                if kind is Recitation and number:
                    iattrs["number"] = int(number.group(1))
                if kind is Assignment and parts[2].startswith("hw-"):
                    iattrs["kind"] = "homework"
                label = item_label(parts[2].replace("-", " ").replace("_", " "))
                item(iid, kind.__name__, label, iattrs, canonical, ts)
                edges.append({"s": iid, "p": f"{kind.__name__.lower()}_of_course", "o": cid,
                              "src": canonical, "ts": ts})  # fmt: skip
                parent = (f"file_of_{kind.__name__.lower()}", iid, {})
            elif bucket == "syllabus":
                sid = syllabus_id(folder)
                item(sid, "Syllabus", "Syllabus", {}, canonical, ts)
                edges.append({"s": cid, "p": "course_follows_syllabus", "o": sid,
                              "src": canonical, "ts": ts})  # fmt: skip
                parent = ("file_of_syllabus", sid, {})
            elif bucket == "lectures" and (found := lecture_number(Path(canonical).stem)):
                number, title = found
                lid = item_id(Lecture, folder, f"{number:02d}")
                lattrs: dict[str, Any] = {"number": number} | ({"title": title} if title else {})
                item(lid, "Lecture", f"Lecture {number}", lattrs, canonical, ts)
                edges.append({"s": lid, "p": "lecture_of_course", "o": cid,
                              "src": canonical, "ts": ts})  # fmt: skip
                parent = ("file_of_lecture", lid, {})
            elif bucket in ("other", "notes"):
                parent = ("file_of_course", cid, {"reason": f"filed under {bucket}/ on disk"})
            if parent is not None:
                p, target, eattrs = parent
                edges.append({"s": fid, "p": p, "o": target, "attrs": eattrs,
                              "src": canonical, "ts": ts})  # fmt: skip

    # Repos first: an assignment folder holding only a repo needs its node
    # before Canvas facts are joined onto the folders' nodes.
    _add_repos(settings, entries, nodes, edges)
    _add_assignments(settings, nodes, edges)
    records = [normalize_node(n) for n in nodes.values()]
    for node in records:
        validate_node(node)
    node_types = {n["id"]: n["type"] for n in records}
    lines = [normalize_edge(e) for e in edges]
    for edge in lines:
        validate_edge(edge, node_types)
    body = "".join(
        json.dumps(r, sort_keys=True) + "\n"
        for r in sorted(records, key=lambda n: n["id"]) + sorted(lines, key=edge_key)
    )
    path = backbone_jsonl(settings)
    report = GraphReport(documents=len(groups))
    report.changed = not path.exists() or path.read_text(encoding="utf-8") != body
    if report.changed:
        path.write_text(body, encoding="utf-8")
    _project(settings, report)
    log.info("graph backbone: %s", report.summary())
    return report


def _add_repos(
    settings: Settings,
    entries: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Every repo in a course folder, with its parent, metadata only.

    Guardrail 5: nothing reads inside a repo. Two sources:

    - The repos a course entry in `config/courses.yml` declares (`repos: [{path,
      remote_url, description, assignment}]`, `path` relative to the course
      folder). With `assignment` the parent is that assignment, without it the
      course: the entry states the course outright, so this is a fact and not a
      last resort.
    - Any `assignments/<item>/` folder holding a `.git` is that assignment's
      repo. Only the `.git` entry itself is looked at.

    So every repo is linked to a course or an assignment by the backbone, and
    `graph check` reports `no_course` only if this ever stops being true. An
    assignment folder that holds nothing but the repo still gets its node.
    """
    ws = settings.paths.workspace
    for entry in entries:
        folder = entry["folder"]
        declared_repos = entry.get("repos") or []
        repos = [{**r, "id": f"repo:{slug(Path(r['path']).name)}"} for r in declared_repos]
        declared = {r["path"].strip("/") for r in repos}
        for git in sorted((ws / folder / "assignments").glob("*/.git")):
            item = git.parent.name
            if f"assignments/{item}" not in declared:
                repos.append({"path": f"assignments/{item}", "assignment": item,
                              "id": f"repo:{slug(folder)}-{slug(item)}"})  # fmt: skip
        for repo in repos:
            rel = f"{folder}/{repo['path'].strip('/')}"
            if not (ws / rel).is_dir():
                continue  # declared but not checked out
            name = Path(repo["path"]).name
            rid = repo["id"]
            attrs = {"name": name, "path": rel}
            attrs |= {k: repo[k] for k in ("remote_url", "description") if repo.get(k)}
            nodes[rid] = {
                "id": rid, "type": "Repo", "label": name, "attrs": attrs,
                "src": [rel], "ts": _iso(0),
            }  # fmt: skip
            if not repo.get("assignment"):
                reason = "declared in config/courses.yml with no assignment"
                edges.append({"s": rid, "p": "repo_of_course", "o": course_id(folder), "src": rel,
                              "attrs": {"reason": reason}, "ts": _iso(0)})  # fmt: skip
                continue
            item = repo["assignment"]
            aid = item_id(Assignment, folder, item)
            if aid not in nodes:
                iattrs: dict[str, Any] = {"folder": f"{folder}/assignments/{item}", "title": item}
                if item.startswith("hw-"):
                    iattrs["kind"] = "homework"
                nodes[aid] = {
                    "id": aid, "type": "Assignment", "attrs": iattrs, "src": [rel], "ts": _iso(0),
                    "label": item_label(item.replace("-", " ").replace("_", " ")),
                }  # fmt: skip
                edges.append({"s": aid, "p": "assignment_of_course", "o": course_id(folder),
                              "src": rel, "ts": _iso(0)})  # fmt: skip
            edges.append({"s": rid, "p": "repo_of_assignment", "o": aid, "src": rel,
                          "ts": _iso(0)})  # fmt: skip


def _add_assignments(
    settings: Settings, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    """Canvas and Gradescope assignments, with the student's submission state.

    A fact joins the `assignments/<item>/` node of its course with the same
    `title_key` when exactly one does, so `hw-01/` and "HW 1: Linear
    Optimization" are one node carrying both the files and the status. Two
    facts of one course sharing a key ("Homework 2" and "Homework 2 (Extra)")
    join nothing and are keyed by their full titles instead.
    """
    from mitsync.schedule.deadlines import assignment_facts

    facts = assignment_facts(settings)
    folder_items: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for node in nodes.values():
        if node["type"] == "Assignment":
            course = node["attrs"]["folder"].split("/")[0]
            folder_items[(course, title_key(node["attrs"]["title"]))].append(node["id"])
    shared = Counter((f["course"], title_key(f["title"])) for f in facts)
    fields = set(Assignment.model_fields)
    for fact in facts:
        key = (fact["course"], title_key(fact["title"]))
        unique = shared[key] == 1
        if unique and len(folder_items[key]) == 1:
            aid = folder_items[key][0]
        else:
            numbered = len(key[1]) == 2 and key[1][1].isdigit()
            name = f"{key[1][0]}-{int(key[1][1]):02d}" if numbered else "-".join(key[1])
            aid = item_id(Assignment, fact["course"], (name if unique else "") or fact["title"])
        attrs = {k: v for k, v in fact.items() if k in fields and v is not None}
        src = [fact["src"]] if isinstance(fact["src"], str) else fact["src"]
        ts = fact["ts"] or _iso(0)
        node = nodes.setdefault(
            aid, {"id": aid, "type": "Assignment", "attrs": {}, "src": [], "ts": ts}
        )
        node["label"] = item_label(fact["title"])
        node["attrs"] = {**node["attrs"], **attrs}
        node["src"] = sorted({*node["src"], *src})
        node["ts"] = max(node["ts"], ts)
        edges.append({"s": aid, "p": "assignment_of_course", "o": course_id(fact["course"]),
                      "src": src[0], "ts": ts})  # fmt: skip


def _project(settings: Settings, report: GraphReport) -> None:
    get_backend(settings).rebuild()
    nodes, edges, _ = load_graph(settings)
    report.nodes, report.edges = len(nodes), len(edges)
    report.db = settings.paths.graph_db


def check(settings: Settings) -> list[Violation]:
    """Every structural violation of the projected graph, its stale edges, and
    every Canvas file of a mapped course that is neither filed nor skipped.

    The last is what makes the sync supervised: the graph is built from the
    course folders, so a Canvas file is missing from it until the agent writes
    a filing plan (or a skip, with its reason) and the student applies it.
    """
    from mitsync.filing.organize import unfiled

    nodes, edges, stale = load_graph(settings)
    return (
        structure_violations(nodes, edges)
        + [
            Violation(
                code="stale_edge",
                severity="info",
                node=e["s"],
                message=f"{e['s']} -{e['p']}-> {e['o']} names a node that no longer exists",
            )
            for e in stale
        ]
        + [
            Violation(
                code="canvas_unfiled",
                severity="human",
                node=f"canvas:{f['file_id']}",
                message=f"{f['mirror_path']} is not filed in {f['course']!r}: put it in a "
                "filing plan or a skip (`mitsync unfiled --json`); the student applies it",
            )
            for f in unfiled(settings)["files"]
            if f["course"]
        ]
    )


# --------------------------------------------------------------------------
# graph add: records the driving agent wrote
# --------------------------------------------------------------------------
NODE_KEYS = {"id", "type", "label", "attrs", "src"}
EDGE_KEYS = {"s", "p", "o", "attrs", "src", "conf"}
NODE_REQUIRED = ("id", "type", "label", "src")
EDGE_REQUIRED = ("s", "p", "o", "src")


def _check_src(value: Any, many: bool) -> None:
    items = value if many and isinstance(value, list) else [value]
    if many and not isinstance(value, list | str):
        raise OntologyError("`src` must be a workspace-relative path or a list of them")
    if not items:
        raise OntologyError("`src` must name at least one source document")
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise OntologyError("`src` entries must be non-empty strings")
        parts = Path(item).parts
        if item.startswith(("/", "~")) or ".." in parts:
            raise OntologyError(f"`src` {item!r} must be workspace-relative")
        if parts and parts[0] in ("_agent", "_kb"):
            raise OntologyError(f"`src` {item!r} points into machinery, not course content")


# Only the backbone mints these: an agent may enrich an existing one, never invent one.
BACKBONE_ONLY_TYPES = frozenset({"Course", "File", "DataFile", "Repo"})


def _check_record(record: Any, existing: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Classify one agent-written line as a node or an edge and check its shape."""
    if not isinstance(record, dict):
        raise OntologyError("each line must be a JSON object")
    is_edge = any(k in record for k in ("s", "p", "o"))
    is_node = "id" in record or "type" in record
    if is_edge == is_node:
        raise OntologyError("a line is a node (id, type, label, src) or an edge (s, p, o, src)")
    kind, allowed, required = (
        ("edge", EDGE_KEYS, EDGE_REQUIRED) if is_edge else ("node", NODE_KEYS, NODE_REQUIRED)
    )
    unknown = sorted(set(record) - allowed)
    if unknown:
        raise OntologyError(f"{kind} has unknown key(s) {unknown} (allowed: {sorted(allowed)})")
    missing = [k for k in required if record.get(k) in (None, "", [])]
    if missing:
        raise OntologyError(f"{kind} is missing required field(s) {missing}")
    _check_src(record["src"], many=kind == "node")
    if kind == "node":
        if not isinstance(record["label"], str):
            raise OntologyError("node label must be a string")
        node = normalize_node(record)
        prior = existing.get(node["id"])
        if prior is not None and prior["type"] != node["type"]:
            raise OntologyError(
                f"node {node['id']!r} already exists as a {prior['type']}, not a {node['type']}"
            )
        if prior is None and node["type"] in BACKBONE_ONLY_TYPES:
            raise OntologyError(
                f"{node['type']} nodes come from the files on disk; {node['id']!r} is not one "
                "(run `mitsync graph backbone`, then attach to the id it prints)"
            )
        validate_node(merge_node(prior, node))
        return kind, node
    conf = record.get("conf")
    if conf is not None and (
        not isinstance(conf, int | float) or isinstance(conf, bool) or not 0 <= conf <= 1
    ):
        raise OntologyError("edge conf must be a number between 0 and 1")
    return kind, normalize_edge(record)


def validate_records(
    settings: Settings, path: Path | str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check an agent-written JSONL file of nodes and edges; returns them, cleaned.

    Every line must be a node or an edge that satisfies `ontology.py`,
    and every edge endpoint must be a node already in the graph or defined in
    the same file. Any error rejects the **whole file**, and the `MitsyncError`
    lists every failing line by number. `graph add --dry-run` stops here, which
    is how a subagent checks its facts file without touching the graph.
    """
    path = Path(path)
    if not path.is_file():
        raise MitsyncError(f"graph records file not found: {path}")
    existing = load_nodes(settings)

    errors: list[str] = []
    nodes: list[dict[str, Any]] = []
    edges: list[tuple[int, dict[str, Any]]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: not valid JSON ({exc.msg})")
                continue
            try:
                kind, clean = _check_record(record, existing)
            except OntologyError as exc:
                errors.append(f"line {lineno}: {exc}")
                continue
            if kind == "node":
                nodes.append(clean)
            else:
                edges.append((lineno, clean))

    node_types = {nid: n["type"] for nid, n in existing.items()}
    node_types.update({n["id"]: n["type"] for n in nodes})
    for lineno, edge in edges:
        try:
            validate_edge(edge, node_types)
        except OntologyError as exc:
            errors.append(f"line {lineno}: {exc}")
    if errors:
        raise MitsyncError(
            f"{path}: rejected, nothing was added ({len(errors)} error(s)):\n  "
            + "\n  ".join(errors)
        )
    return nodes, [e for _, e in edges]


def add_records(settings: Settings, path: Path | str) -> GraphReport:
    """Validate an agent-written JSONL file (`validate_records`), then append it."""
    nodes, edges = validate_records(settings, path)
    report = GraphReport()
    report.appended_nodes = append_nodes(settings, nodes)
    report.appended_edges = append_edges(settings, edges)
    _project(settings, report)
    log.info("graph add: %s", report.summary())
    return report
