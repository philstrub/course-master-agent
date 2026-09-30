"""The Neo4j projection: record shapes, the read-only guard, connection errors.

Nothing here talks to a server: `push` wipes mitsync's nodes in whatever
database NEO4J_URI names, so it is exercised by hand (`mitsync graph push`),
never against the student's database from a test run.
"""

from __future__ import annotations

import pytest

from mitsync.core.config import Settings
from mitsync.core.errors import MitsyncError
from mitsync.knowledge import neo4j_store
from tests.test_graph import seed_graph


def test_records_flatten_attrs_label_by_type_and_upper_case_edges(settings: Settings) -> None:
    seed_graph(settings)
    by_type, by_rel = neo4j_store.records(settings)

    assert set(by_type) == {"Course", "Lecture", "File", "Assignment", "Concept"}
    hw = next(r for r in by_type["Assignment"] if r["id"] == "assignment:ae:hw-02")
    assert hw["submission_status"] == "submitted"  # attrs are top-level properties
    assert hw["name"] == "Homework 2"  # the caption browsers pick up
    assert "attrs" not in hw
    assert all(v is not None for rows in by_type.values() for r in rows for v in r.values())

    assert "FILE_OF_LECTURE" in by_rel
    (slides,) = by_rel["FILE_OF_LECTURE"]
    assert (slides["s"], slides["o"]) == ("file:slides", "lecture:ae:05")
    assert slides["props"]["role"] == "slides"


def test_the_same_fact_from_two_sources_is_one_relationship(settings: Settings) -> None:
    seed_graph(settings)
    from mitsync.knowledge import graph as graph_mod

    graph_mod.append_edges(
        settings,
        [{"s": "concept:cart", "p": "concept_in_lecture", "o": "lecture:ae:05", "src": "b.pdf"}],
    )
    _, by_rel = neo4j_store.records(settings)
    (row,) = by_rel["CONCEPT_IN_LECTURE"]
    assert row["props"]["src"] == ["a.pdf", "b.pdf"]


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (n:X) RETURN n",
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) SET n.x = 1",
        "merge (n:X {id: 1})",
        "MATCH (n) REMOVE n.label",
        "DROP CONSTRAINT mitnode_id",
        "LOAD CSV FROM 'file:///x' AS row RETURN row",
        "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})",
    ],
)
def test_cypher_refuses_writes_before_connecting(query: str) -> None:
    with pytest.raises(MitsyncError, match="read-only"):
        neo4j_store.cypher(query)


def test_cypher_allows_reads_and_db_procedures() -> None:
    neo4j_store.check_read_only("MATCH (c:Course)<-[:LECTURE_OF_COURSE]-(l) RETURN c.label, l")
    neo4j_store.check_read_only("CALL db.labels()")
    neo4j_store.check_read_only("MATCH (n) WHERE n.label = 'Created offset' RETURN n")


def test_an_http_uri_is_refused_with_the_bolt_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URI", "localhost:7474")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    with pytest.raises(MitsyncError, match="bolt://localhost:7687"):
        neo4j_store.cypher("MATCH (n) RETURN n")


def test_missing_credentials_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(MitsyncError, match="NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD"):
        neo4j_store.cypher("MATCH (n) RETURN n")
