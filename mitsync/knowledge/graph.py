"""
# Knowledge Graph

Append-only JSONL truth, a rebuildable DuckDB projection, and an ontology that
every record must satisfy.

## 1. What This Module Does

Stores nodes and edges, projects them into a queryable database, and renders
one greppable markdown page per node. `build_backbone` writes the
deterministic part of the graph from extracted text; `add_records` validates
and appends the nodes and edges the driving agent wrote itself; `rebuild`
re-derives everything downstream from the JSONL; `query` runs a canned query
by name or raw SQL.

## 2. Why This Module Exists

An agent helping with coursework needs to answer "what does this concept
appear in?" across a whole term, which no single document can answer. The
graph is that cross-document index.

Its storage split exists because agent-written facts accumulate slowly and
expensively and must never be lost to a schema change or a corrupted database.
`_kb/graph/nodes.jsonl` and `_kb/graph/triples.jsonl` are append-only and are
the ONLY source of truth. `state/graph.duckdb` is a cache: deleting it and
running `rebuild()` must reproduce identical query results, and a test asserts
exactly that. The markdown entity pages exist for the same reason in the other
direction -- an agent with nothing but `grep` can still read the graph.

## 3. How It Fits in the Architecture

Reads `_kb/text/*.md` written by `extract`; is read by `kb`, which lists each
file's node ids in `_kb/manifest.json`. Nothing here judges anything: the
backbone is derived from paths, and every other fact arrives through `graph
add` from an agent that read the documents itself.

## 4. Key Concepts

**Nodes and edges.** Not vertices, not entities, not relationships. Node
types and edge types.

**The ontology is configuration.** `config/ontology.yml` declares the allowed
node types and their attributes, the edge types and theirs, and which node
types an edge may join. Every node and edge is validated against it before it
is stored or projected, and a violation raises `OntologyError` naming the
offending record. The configured repo copy wins; an unconfigured checkout
falls back to the copy shipped beside the package.

**The deterministic backbone.** A `Course` node per folder, a `Resource` node
per document, and a `part_of` edge joining them are always written, with no
model involved. The graph is therefore useful before any agent has added a
single concept.

**`graph add` is all or nothing.** A node needs `id`, `type`, `label` and
`src` (its source documents); an edge needs `s`, `p`, `o` and `src`; unknown
keys are refused, and an edge endpoint must already exist or be defined in the
same file. One bad line rejects the whole file with every error listed by line
number, so a half-applied batch can never leave dangling facts behind.

**Projection rules.** For nodes, the last line wins per id and `src` unions
across lines; for edges, `(s, p, o, src)` is the dedupe key. Appending the
same fact twice is free, which is what makes the append-only file safe to
re-run against.

**Regeneration leaves mtimes alone.** `write_if_changed` only writes on a real
change, so a rebuild over unchanged inputs looks like a no-op to every tool
watching the tree.

**Why exceptions are caught here.** Three handlers, all at an input boundary.
`yaml.YAMLError` on the ontology becomes an `OntologyError` naming the file.
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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml
from rich.console import Console
from rich.table import Table

from mitsync.core.clock import now_iso
from mitsync.core.errors import MitsyncError, OntologyError
from mitsync.core.logging import get_logger
from mitsync.core.paths import REPO_ROOT

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)

EXTRACTOR_VERSION = "1"

NODES_FILE = "nodes.jsonl"
TRIPLES_FILE = "triples.jsonl"


# --------------------------------------------------------------------------
# ontology
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EdgeType:
    name: str
    source: tuple[str, ...]
    target: tuple[str, ...]
    attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ontology:
    version: int
    node_types: dict[str, tuple[str, ...]]
    edge_types: dict[str, EdgeType]
    path: Path | None = None

    # --- validation -------------------------------------------------------
    def validate_node(self, node: dict[str, Any]) -> None:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid.strip():
            raise OntologyError(f"node has no id: {_show(node)}")
        ntype = node.get("type")
        if ntype not in self.node_types:
            raise OntologyError(
                f"unknown node type {ntype!r} (allowed: {', '.join(sorted(self.node_types))}) "
                f"in {_show(node)}"
            )
        attrs = node.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise OntologyError(f"node attrs must be an object in {_show(node)}")
        allowed = set(self.node_types[ntype])
        unknown = sorted(set(attrs) - allowed)
        if unknown:
            raise OntologyError(
                f"node type {ntype} has no attribute(s) {unknown} "
                f"(allowed: {sorted(allowed)}) in {_show(node)}"
            )

    def validate_edge(self, edge: dict[str, Any], node_types: dict[str, str]) -> None:
        pred = edge.get("p")
        spec = self.edge_types.get(pred) if isinstance(pred, str) else None
        if spec is None:
            raise OntologyError(
                f"unknown edge type {pred!r} (allowed: {', '.join(sorted(self.edge_types))}) "
                f"in {_show(edge)}"
            )
        s, o = edge.get("s"), edge.get("o")
        for role, nid in (("s", s), ("o", o)):
            if not isinstance(nid, str) or not nid.strip():
                raise OntologyError(f"edge {role} is missing in {_show(edge)}")
            if nid not in node_types:
                raise OntologyError(f"edge {role} {nid!r} is not a known node in {_show(edge)}")
        unknown = sorted(set(edge.get("attrs") or {}) - set(spec.attributes))
        if unknown:
            raise OntologyError(
                f"edge type {pred} has no attribute(s) {unknown} "
                f"(allowed: {list(spec.attributes)}) in {_show(edge)}"
            )
        s_type, o_type = node_types[s], node_types[o]
        if s_type not in spec.source:
            raise OntologyError(
                f"edge '{pred}' cannot start at a {s_type} "
                f"(allowed sources: {list(spec.source)}) in {_show(edge)}"
            )
        if o_type not in spec.target:
            raise OntologyError(
                f"edge '{pred}' cannot point at a {o_type} "
                f"(allowed targets: {list(spec.target)}) in {_show(edge)}"
            )


def _show(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, default=str)[:400]


def load_ontology(settings: Settings) -> Ontology:
    """Parse and validate `config/ontology.yml`.

    The configured repo wins; a checkout that has not been configured falls back
    to the copy shipped beside the package.
    """
    path = settings.paths.config_dir / "ontology.yml"
    if not path.exists():
        path = REPO_ROOT / "config" / "ontology.yml"
    if not path.exists():
        raise OntologyError(f"ontology file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise OntologyError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise OntologyError(f"{path} must contain a YAML mapping")

    node_types: dict[str, tuple[str, ...]] = {}
    for name, body in (raw.get("node_types") or {}).items():
        body = body or {}
        if not isinstance(body, dict):
            raise OntologyError(f"{path}: node type {name!r} must be a mapping")
        attrs = body.get("attributes") or []
        if not isinstance(attrs, list):
            raise OntologyError(f"{path}: node type {name!r} attributes must be a list")
        node_types[str(name)] = tuple(str(a) for a in attrs)
    if not node_types:
        raise OntologyError(f"{path}: no node_types defined")

    edge_types: dict[str, EdgeType] = {}
    for name, body in (raw.get("edge_types") or {}).items():
        body = body or {}
        if not isinstance(body, dict):
            raise OntologyError(f"{path}: edge type {name!r} must be a mapping")
        source = [str(x) for x in (body.get("source") or [])]
        target = [str(x) for x in (body.get("target") or [])]
        if not source or not target:
            raise OntologyError(f"{path}: edge type {name!r} needs both source and target")
        bad = sorted({t for t in (*source, *target) if t not in node_types})
        if bad:
            raise OntologyError(f"{path}: edge type {name!r} references unknown node types {bad}")
        edge_types[str(name)] = EdgeType(
            name=str(name),
            source=tuple(source),
            target=tuple(target),
            attributes=tuple(str(a) for a in (body.get("attributes") or [])),
        )
    if not edge_types:
        raise OntologyError(f"{path}: no edge_types defined")

    return Ontology(
        version=int(raw.get("version", 0)),
        node_types=node_types,
        edge_types=edge_types,
        path=path,
    )


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
    db: Path | None = None

    def summary(self) -> str:
        bits = [f"{self.nodes} nodes", f"{self.edges} edges"]
        if self.documents:
            bits.append(f"{self.documents} docs")
        if self.appended_nodes or self.appended_edges:
            bits.append(f"+{self.appended_nodes} new nodes/+{self.appended_edges} new edges")
        return ", ".join(bits)


def nodes_jsonl(settings: Settings) -> Path:
    return settings.paths.kb_graph / NODES_FILE


def triples_jsonl(settings: Settings) -> Path:
    return settings.paths.kb_graph / TRIPLES_FILE


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


def load_nodes(settings: Settings) -> dict[str, dict[str, Any]]:
    """Project `nodes.jsonl`: last line wins per id, `src` unions across lines."""
    out: dict[str, dict[str, Any]] = {}
    for raw in _read_jsonl(nodes_jsonl(settings)):
        node = normalize_node(raw)
        if not node["id"]:
            raise MitsyncError(f"{nodes_jsonl(settings)} has a node record with no id: {raw!r}")
        prior = out.get(node["id"])
        if prior is not None:
            node["src"] = sorted(set(prior["src"]) | set(node["src"]))
        out[node["id"]] = node
    return dict(sorted(out.items()))


def load_edges(settings: Settings) -> list[dict[str, Any]]:
    """Project `triples.jsonl`, deduped on (s,p,o,src).

    A later line with a newer `extractor_version` supersedes an older one for
    the same key, so re-extraction replaces rather than accumulates.
    """
    best: dict[tuple[str, str, str, str], tuple[tuple[int, str], int, dict[str, Any]]] = {}
    for index, raw in enumerate(_read_jsonl(triples_jsonl(settings))):
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


def _append(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def append_nodes(settings: Settings, nodes: Iterable[dict[str, Any]]) -> int:
    """Append validated nodes, skipping ones already stored identically."""
    ontology = load_ontology(settings)
    existing = load_nodes(settings)
    fresh: list[dict[str, Any]] = []
    for raw in nodes:
        node = normalize_node(raw)
        ontology.validate_node(node)
        prior = existing.get(node["id"])
        if prior is not None and (
            prior["type"] == node["type"]
            and prior["label"] == node["label"]
            and prior["attrs"] == node["attrs"]
            and set(node["src"]).issubset(set(prior["src"]))
            and prior["extractor_version"] == node["extractor_version"]
        ):
            continue
        if prior is not None:
            node["src"] = sorted(set(prior["src"]) | set(node["src"]))
        existing[node["id"]] = node
        fresh.append(node)
    if fresh:
        _append(nodes_jsonl(settings), fresh)
    return len(fresh)


def append_edges(settings: Settings, edges: Iterable[dict[str, Any]]) -> int:
    """Append validated edges, skipping (s,p,o,src) keys already at this version."""
    ontology = load_ontology(settings)
    node_types = {nid: node["type"] for nid, node in load_nodes(settings).items()}
    seen = {edge_key(e): _version_key(e["extractor_version"]) for e in load_edges(settings)}
    fresh: list[dict[str, Any]] = []
    for raw in edges:
        edge = normalize_edge(raw)
        ontology.validate_edge(edge, node_types)
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
        """Drop every table and reload from the JSONL files."""
        nodes = list(load_nodes(self.settings).values())
        edges = load_edges(self.settings)
        ontology = load_ontology(self.settings)
        for node in nodes:
            ontology.validate_node(node)
        node_types = {n["id"]: n["type"] for n in nodes}
        for edge in edges:
            ontology.validate_edge(edge, node_types)

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
        f"unsupported graph backend {backend!r}: only 'duckdb' is implemented. "
        "A neo4j projection would need a running Neo4j server, connection settings "
        "(uri, user, password env var) and a GraphBackend implementation over the bolt "
        "driver -- none of which exist. Set graph.backend: duckdb in config/settings.yml."
    )


# --------------------------------------------------------------------------
# entity pages
# --------------------------------------------------------------------------
_SLUG_RX = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG_RX.sub("-", str(text).lower()).strip("-") or "unnamed"


def entity_filename(node_id: str) -> str:
    return f"{slug(node_id)}.md"


def write_entity_pages(settings: Settings) -> int:
    """One greppable markdown page per node under `_kb/graph/entities/`."""
    nodes = load_nodes(settings)
    edges = load_edges(settings)
    out_dir = settings.paths.kb_graph / "entities"
    out_dir.mkdir(parents=True, exist_ok=True)

    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(edge["s"], []).append(edge)
        incoming.setdefault(edge["o"], []).append(edge)

    def link(node_id: str) -> str:
        # `validate_edge` has already rejected any edge whose endpoint is not a
        # known node, so every id reaching here is in `nodes`.
        return f"[{nodes[node_id]['label']}]({entity_filename(node_id)})"

    keep: set[str] = set()
    for node_id, node in nodes.items():
        name = entity_filename(node_id)
        keep.add(name)
        lines = [
            "---",
            f"id: {json.dumps(node_id)}",
            f"type: {node['type']}",
            f"label: {json.dumps(node['label'])}",
            "---",
            "",
            f"# {node['label']}",
            "",
            f"**Type:** {node['type']}  ",
            f"**Node id:** `{node_id}`",
            "",
        ]
        if node["attrs"]:
            lines.append("## Attributes")
            lines.append("")
            for key in sorted(node["attrs"]):
                lines.append(f"- **{key}**: {node['attrs'][key]}")
            lines.append("")
        lines.append("## Outgoing edges")
        lines.append("")
        rows = sorted(outgoing.get(node_id, []), key=lambda e: (e["p"], e["o"]))
        lines += [f"- `{e['p']}` -> {link(e['o'])} (conf {e['conf']:.2f})" for e in rows] or [
            "- none"
        ]
        lines.append("")
        lines.append("## Incoming edges")
        lines.append("")
        rows = sorted(incoming.get(node_id, []), key=lambda e: (e["p"], e["s"]))
        lines += [f"- {link(e['s'])} `{e['p']}` -> this (conf {e['conf']:.2f})" for e in rows] or [
            "- none"
        ]
        lines.append("")
        lines.append("## Source documents")
        lines.append("")
        srcs = sorted({*node["src"], *(e["src"] for e in rows if e["src"])})
        lines += [f"- `{s}`" for s in srcs] or ["- none"]
        lines.append("")
        write_if_changed(out_dir / name, "\n".join(lines))

    for stale in out_dir.glob("*.md"):
        if stale.name not in keep:
            stale.unlink()
    return len(nodes)


# --------------------------------------------------------------------------
# rebuild / query
# --------------------------------------------------------------------------
def rebuild(settings: Settings) -> GraphReport:
    """Re-derive the DuckDB projection and the entity pages from JSONL."""
    backend = get_backend(settings)
    backend.rebuild()
    report = GraphReport(
        nodes=len(load_nodes(settings)),
        edges=len(load_edges(settings)),
        db=settings.paths.graph_db,
    )
    write_entity_pages(settings)
    log.info("graph rebuild: %s", report.summary())
    print(f"graph rebuild: {report.summary()} -> {report.db}")
    return report


@dataclass(frozen=True)
class Canned:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    help: str = ""


CANNED: dict[str, Canned] = {
    "concepts_by_course": Canned(
        sql="""
            SELECT c.label AS course, n.label AS concept, count(*) AS mentions
            FROM edges e
            JOIN nodes n ON n.id = e.o AND n.type = 'Concept'
            JOIN edges pe ON pe.s = e.s AND pe.p = 'part_of'
            JOIN nodes c ON c.id = pe.o AND c.type = 'Course'
            WHERE e.p IN ('covers', 'assesses', 'requires')
            GROUP BY 1, 2
            ORDER BY 1, 3 DESC, 2
        """,
        help="Concepts each course's materials cover, most-referenced first.",
    ),
    "assignments_due": Canned(
        sql="""
            SELECT c.label AS course,
                   n.label AS assignment,
                   json_extract_string(n.attrs, '$.due_at') AS due_at,
                   json_extract_string(n.attrs, '$.kind') AS kind,
                   n.id AS node_id
            FROM nodes n
            LEFT JOIN edges e ON e.s = n.id AND e.p = 'part_of'
            LEFT JOIN nodes c ON c.id = e.o AND c.type = 'Course'
            WHERE n.type = 'Assignment'
            ORDER BY due_at NULLS LAST, course NULLS LAST, assignment
        """,
        help="Assignment nodes with their due dates and course.",
    ),
    "resources_for_concept": Canned(
        sql="""
            SELECT t.label AS concept,
                   r.label AS resource,
                   r.type AS resource_type,
                   json_extract_string(r.attrs, '$.path') AS path,
                   e.conf
            FROM edges e
            JOIN nodes r ON r.id = e.s
            JOIN nodes t ON t.id = e.o AND t.type = 'Concept'
            WHERE e.p IN ('covers', 'requires')
              AND (t.id = $concept OR lower(t.label) LIKE lower($concept))
            ORDER BY concept, e.conf DESC, resource
        """,
        params={"concept": "%"},
        help="Resources and sessions covering a concept ($concept: id or LIKE pattern).",
    ),
    "prerequisites_of": Canned(
        sql="""
            WITH RECURSIVE seed AS (
                SELECT id, label FROM nodes
                WHERE id = $node OR lower(label) LIKE lower($node)
            ),
            chain(target, id, depth) AS (
                SELECT s.id, e.s, 1
                FROM seed s JOIN edges e ON e.o = s.id AND e.p = 'prerequisite_of'
                UNION
                SELECT c.target, e.s, c.depth + 1
                FROM chain c JOIN edges e ON e.o = c.id AND e.p = 'prerequisite_of'
            )
            SELECT t.label AS needed_for, n.label AS prerequisite, n.type AS type,
                   min(c.depth) AS depth
            FROM chain c
            JOIN nodes n ON n.id = c.id
            JOIN nodes t ON t.id = c.target
            GROUP BY 1, 2, 3
            ORDER BY 1, 4, 2
        """,
        params={"node": "%"},
        help="Transitive prerequisites of a concept or session ($node: id or LIKE pattern).",
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
) -> list[dict[str, Any]]:
    """Run a canned query by name or raw SQL; returns rows and prints a table."""
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
        print("Canned queries: " + ", ".join(sorted(CANNED)))
        for name, spec in sorted(CANNED.items()):
            print(f"  {name}: {spec.help}")

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
# extraction: extracted text -> nodes and triples
# --------------------------------------------------------------------------
def resource_id(rel: str) -> str:
    return "resource:" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]  # noqa: S324


def course_id(course: str) -> str:
    return "course:" + slug(course)


def _documents(settings: Settings, since: str | None) -> list[dict[str, Any]]:
    import frontmatter

    from mitsync.knowledge.extract import course_of

    docs: list[dict[str, Any]] = []
    text_dir = settings.paths.kb_text
    if not text_dir.is_dir():
        return docs
    for path in sorted(text_dir.glob("*.md")):
        post = frontmatter.load(path)
        rel = str(post.metadata.get("source") or "")
        if not rel or settings.should_ignore(rel):
            continue
        if since and str(post.metadata.get("extracted_at") or "") < since:
            continue
        docs.append(
            {
                "source": rel,
                "course": str(post.metadata.get("course") or course_of(rel)),
                "title": str(post.metadata.get("title") or Path(rel).stem),
                "content_type": str(post.metadata.get("content_type") or "unknown"),
                "text": post.content,
                "text_path": path,
            }
        )
    return docs


def build_backbone(settings: Settings, *, since: str | None = None) -> GraphReport:
    """Write the deterministic backbone for `_kb/text/*.md`: Course, Resource, `part_of`.

    No judgment is involved, so this is safe to run unattended from `kb build`.
    Concepts, sessions, assignments and their relations are written by the
    driving agent through `add_records`.
    """
    docs = _documents(settings, since)
    report = GraphReport(documents=len(docs))

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for doc in docs:
        rel, course = doc["source"], doc["course"]
        rid = resource_id(rel)
        nodes[rid] = {
            "id": rid,
            "type": "Resource",
            "label": doc["title"],
            "attrs": {"title": doc["title"], "path": rel, "content_type": doc["content_type"]},
            "src": [rel],
        }
        if not course:
            continue  # a document outside every course folder has no Course to join
        cid = course_id(course)
        node = nodes.setdefault(
            cid,
            {"id": cid, "type": "Course", "label": course, "attrs": {"name": course}, "src": []},
        )
        node["src"] = sorted({*node["src"], rel})
        edges.append({"s": rid, "p": "part_of", "o": cid, "src": rel, "conf": 1.0})

    report.appended_nodes += append_nodes(settings, list(nodes.values()))
    report.appended_edges += append_edges(settings, edges)
    _project(settings, report)
    log.info("graph backbone: %s", report.summary())
    return report


def _project(settings: Settings, report: GraphReport) -> None:
    report.nodes = len(load_nodes(settings))
    report.edges = len(load_edges(settings))
    get_backend(settings).rebuild()
    write_entity_pages(settings)
    report.db = settings.paths.graph_db


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


def _check_record(
    record: Any, ontology: Ontology, existing: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
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
        ontology.validate_node(node)
        prior = existing.get(node["id"])
        if prior is not None and prior["type"] != node["type"]:
            raise OntologyError(
                f"node {node['id']!r} already exists as a {prior['type']}, not a {node['type']}"
            )
        return kind, node
    conf = record.get("conf")
    if conf is not None and (
        not isinstance(conf, int | float) or isinstance(conf, bool) or not 0 <= conf <= 1
    ):
        raise OntologyError("edge conf must be a number between 0 and 1")
    return kind, normalize_edge(record)


def add_records(settings: Settings, path: Path | str) -> GraphReport:
    """Validate an agent-written JSONL file of nodes and edges, then append it.

    Every line must be a node or an edge that satisfies `config/ontology.yml`,
    and every edge endpoint must be a node already in the graph or defined in
    the same file. Any error rejects the **whole file** -- nothing is appended
    -- and the `MitsyncError` lists every failing line by number.
    """
    path = Path(path)
    if not path.is_file():
        raise MitsyncError(f"graph records file not found: {path}")
    ontology = load_ontology(settings)
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
                kind, clean = _check_record(record, ontology, existing)
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
            ontology.validate_edge(edge, node_types)
        except OntologyError as exc:
            errors.append(f"line {lineno}: {exc}")
    if errors:
        raise MitsyncError(
            f"{path}: rejected, nothing was added ({len(errors)} error(s)):\n  "
            + "\n  ".join(errors)
        )

    report = GraphReport()
    report.appended_nodes = append_nodes(settings, nodes)
    report.appended_edges = append_edges(settings, [e for _, e in edges])
    _project(settings, report)
    log.info("graph add: %s", report.summary())
    return report
