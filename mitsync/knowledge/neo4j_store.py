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

**Labels and types.** Every node carries the `MitNode` label (the uniqueness
constraint on `id` and the scope of the wipe) plus its ontology type (`Course`,
`File`, ...). A relationship's type is the edge name upper-cased
(`file_of_lecture` -> `FILE_OF_LECTURE`).

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

if TYPE_CHECKING:  # pragma: no cover
    from neo4j import Driver

    from mitsync.core.config import Settings

__all__ = ["cypher", "push", "records"]

NODE_LABEL = "MitNode"
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
        driver.verify_connectivity()
        yield driver
    finally:
        driver.close()


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
def _props(base: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in attrs.items() if v is not None}, **base}


def records(settings: Settings) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """(node props by type, relationship rows by rel type) for the live graph."""
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
        merged[key] = {"s": edge["s"], "o": edge["o"], "props": props}
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
    """Replace mitsync's part of the Neo4j database with the live graph; returns counts."""
    by_type, by_rel = records(settings)
    with _driver() as driver:
        driver.execute_query(
            f"CREATE CONSTRAINT mitnode_id IF NOT EXISTS "
            f"FOR (n:{NODE_LABEL}) REQUIRE n.id IS UNIQUE"
        )
        driver.execute_query(f"MATCH (n:{NODE_LABEL}) DETACH DELETE n")
        for ntype, rows in by_type.items():
            for batch in _batches(rows):
                driver.execute_query(
                    f"UNWIND $rows AS r CREATE (n:{NODE_LABEL}:`{ntype}`) SET n = r", rows=batch
                )
        for rel, rows in by_rel.items():
            for batch in _batches(rows):
                driver.execute_query(
                    f"UNWIND $rows AS r MATCH (a:{NODE_LABEL} {{id: r.s}}), "
                    f"(b:{NODE_LABEL} {{id: r.o}}) CREATE (a)-[e:`{rel}`]->(b) SET e = r.props",
                    rows=batch,
                )
        stored = driver.execute_query(
            f"MATCH (n:{NODE_LABEL}) OPTIONAL MATCH (n)-[e]->(:{NODE_LABEL}) "
            "RETURN count(DISTINCT n) AS nodes, count(e) AS edges"
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
