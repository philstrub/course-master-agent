"""
# Neo4j Projection

A second projection of the JSONL graph, into Neo4j, for browsing and Cypher.

## 1. What This Module Does

`push` replaces every mitsync node and relationship in the Neo4j database with
the current `load_graph` result, then checks the counts match. `cypher` runs a
query in a read transaction after refusing any write clause, and returns rows
as plain dicts. `records` turns the graph into the property maps Neo4j stores.

## 2. Why This Module Exists

DuckDB answers the canned questions, but a graph is easier to judge by looking
at it, and other agents already speak Cypher. Neo4j Browser / Aura Explore show
the course -> item -> file -> concept structure directly, which is the demo's
view of the memory.

## 3. How It Fits in the Architecture

Reads through `graph.load_graph`, exactly like `DuckDBBackend.rebuild`, so
the JSONL stays the only truth and Neo4j can be wiped and re-pushed at any
time. Nothing reads Neo4j back into mitsync. Connection settings come from
`NEO4J_URI`, `NEO4J_USERNAME` and `NEO4J_PASSWORD` in the environment or
`.env`, never from `settings.yml`.

## 4. Key Concepts

**Labels and types.** A node's only label is its ontology type (`Course`,
`File`, ...), with a uniqueness constraint on `id` per type. A relationship's
type is the edge name upper-cased (`file_of_lecture` -> `FILE_OF_LECTURE`).

**What a push replaces.** Every node labelled with an ontology type, and its
relationships. Nodes with other labels are left alone, so the database may be
shared, but a node someone else labelled `Course` is replaced like ours.

**Flat properties.** Neo4j properties are scalars or lists of scalars, so
`attrs` are stored as top-level properties next to `id`, `label` and `src`,
and `null` attrs are dropped. `name` is the caption the browsers pick up, so
it is set to the label when the attrs have none.

**One relationship per (s, p, o).** JSONL can hold the same edge from several
source documents. Neo4j gets one relationship whose `src` lists them all, so
the picture shows one line per fact.

**Read-only Cypher.** `cypher` refuses `CREATE`, `MERGE`, `DELETE`, `SET`,
`REMOVE`, `DROP`, `LOAD CSV` and procedure calls outside `db.*` before it
sends anything, and runs in a read transaction, so the server refuses a write
the regex missed.

**The one boundary.** `_driver` turns Neo4j's own `ServiceUnavailable` and
`AuthError` into a `MitsyncError` naming the fix. Every `sync` ends with a
push when `NEO4J_URI` is set, so a stopped container must read as one line in
the scheduled run's log, not a traceback. The JSONL and DuckDB are already
refreshed by then.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from mitsync.core.errors import MitsyncError
from mitsync.knowledge.graph import load_graph
from mitsync.knowledge.ontology import NODE_TYPES

if TYPE_CHECKING:  # pragma: no cover
    from neo4j import Driver

    from mitsync.core.config import Settings

__all__ = ["cypher", "push", "records"]

BATCH = 500
URI_SCHEMES = ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc")
_WRITE_RX = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b"
    r"|\bCALL\s+(?!db\.)[a-z]",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------
@contextmanager
def _driver() -> Iterator[Driver]:
    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError, ServiceUnavailable

    missing = [
        k for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD") if not os.environ.get(k)
    ]
    if missing:
        raise MitsyncError(f"neo4j: set {', '.join(missing)} in the environment or _agent/.env")
    uri = os.environ["NEO4J_URI"]
    if uri.split("://")[0] not in URI_SCHEMES or "://" not in uri:
        raise MitsyncError(
            f"neo4j: NEO4J_URI must be a bolt URI ({', '.join(s + '://' for s in URI_SCHEMES)}). "
            "Port 7474 is Neo4j Browser's HTTP port: a local server is bolt://localhost:7687, "
            "an Aura instance is neo4j+s://<id>.databases.neo4j.io"
        )
    driver = GraphDatabase.driver(
        uri, auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
    )
    try:
        try:
            driver.verify_connectivity()
        except ServiceUnavailable as exc:
            raise MitsyncError(
                f"neo4j: nothing answers at {uri}. Start it with `make neo4j-up`, "
                "or unset NEO4J_URI to skip the push"
            ) from exc
        except AuthError as exc:
            raise MitsyncError("neo4j: NEO4J_USERNAME / NEO4J_PASSWORD were refused") from exc
        yield driver
    finally:
        driver.close()


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
def _props(base: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in attrs.items() if v is not None}, **base}


def records(settings: Settings) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """(node props by type, relationship rows by rel type) for the live graph.

    A relationship row carries its endpoint types, so the push can match both
    ends through the per-type id index.
    """
    nodes, edges, _ = load_graph(settings)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for node in nodes.values():
        props = _props({"id": node["id"], "label": node["label"], "src": node["src"]},
                       node["attrs"])  # fmt: skip
        props.setdefault("name", node["label"])
        by_type[node["type"]].append(props)

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (edge["s"], edge["p"], edge["o"])
        prior = merged.get(key)
        src = sorted({edge["src"], *(prior["props"]["src"] if prior else [])})
        props = _props({"src": src, "conf": edge["conf"]}, edge["attrs"])
        ends = {"s_type": nodes[edge["s"]]["type"], "o_type": nodes[edge["o"]]["type"]}
        merged[key] = {"s": edge["s"], "o": edge["o"], "props": props, **ends}
    by_rel: dict[str, list[dict]] = defaultdict(list)
    for (_, p, _), row in merged.items():
        by_rel[p.upper()].append(row)
    return dict(by_type), dict(by_rel)


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------
def _batches(rows: list[dict]) -> Iterator[list[dict]]:
    for i in range(0, len(rows), BATCH):
        yield rows[i : i + BATCH]


def push(settings: Settings) -> dict[str, int]:
    """Replace every ontology-typed node in Neo4j with the live graph; returns counts."""
    by_type, by_rel = records(settings)
    types = sorted(NODE_TYPES)
    ours = "any(l IN labels(n) WHERE l IN $types)"
    with _driver() as driver:
        for ntype in types:
            driver.execute_query(
                f"CREATE CONSTRAINT {ntype.lower()}_id IF NOT EXISTS "
                f"FOR (n:`{ntype}`) REQUIRE n.id IS UNIQUE"
            )
        driver.execute_query(f"MATCH (n) WHERE {ours} DETACH DELETE n", types=types)
        for ntype, rows in by_type.items():
            for batch in _batches(rows):
                driver.execute_query(
                    f"UNWIND $rows AS r CREATE (n:`{ntype}`) SET n = r", rows=batch
                )
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for rel, rows in by_rel.items():
            for row in rows:
                groups[(rel, row["s_type"], row["o_type"])].append(row)
        for (rel, s_type, o_type), rows in groups.items():
            for batch in _batches(rows):
                driver.execute_query(
                    f"UNWIND $rows AS r MATCH (a:`{s_type}` {{id: r.s}}), "
                    f"(b:`{o_type}` {{id: r.o}}) CREATE (a)-[e:`{rel}`]->(b) SET e = r.props",
                    rows=batch,
                )
        stored = driver.execute_query(
            f"MATCH (n) WHERE {ours} OPTIONAL MATCH (n)-[e]->() "
            "RETURN count(DISTINCT n) AS nodes, count(e) AS edges",
            types=types,
        ).records[0]
    expected = {"nodes": sum(map(len, by_type.values())), "edges": sum(map(len, by_rel.values()))}
    if dict(stored) != expected:
        raise MitsyncError(f"neo4j push: stored {dict(stored)} but the graph has {expected}")
    return expected


# --------------------------------------------------------------------------
# read-only cypher
# --------------------------------------------------------------------------
def check_read_only(query: str) -> None:
    hit = _WRITE_RX.search(query)
    if hit:
        raise MitsyncError(
            f"graph cypher is read-only: {hit.group(0).strip()!r} is refused. "
            "Facts go in through `mitsync graph add`, then `mitsync graph push`."
        )


def cypher(query: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Run a read-only Cypher query against the pushed graph."""
    from neo4j import READ_ACCESS

    check_read_only(query)
    with _driver() as driver, driver.session(default_access_mode=READ_ACCESS) as session:
        return session.execute_read(lambda tx: tx.run(query, params or {}).data())
