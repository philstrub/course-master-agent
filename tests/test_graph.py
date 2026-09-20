"""The knowledge graph: ontology validation, JSONL truth, projection, queries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mitsync import extract as extract_mod
from mitsync import graph as graph_mod
from mitsync.config import Settings
from mitsync.errors import OntologyError
from mitsync.llm.base import JudgeTask, validate_result
from tests.test_extract import make_notebook, make_pdf


class StubJudge:
    """A Judge test double: returns a fixed (or computed) result, no model."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.tasks: list[JudgeTask] = []

    def judge(self, task: JudgeTask) -> dict[str, Any]:
        self.tasks.append(task)
        return self._result(task) if callable(self._result) else self._result


class EmptyJudge:
    """What the rules driver does for a task it has no handler for."""

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, task: JudgeTask) -> dict[str, Any]:  # noqa: ARG002 -- interface
        self.calls += 1
        return {}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def seeded(workspace: Path, settings: Settings) -> Settings:
    ml = workspace / "Machine Learning"
    make_pdf(ml / "lectures" / "lec01.pdf", ("regularization and ridge regression",))
    make_notebook(ml / "assignments" / "hw1.ipynb")
    (workspace / "Analytics Edge" / "lectures").mkdir(parents=True)
    (workspace / "Analytics Edge" / "lectures" / "trees.md").write_text("# CART\n\nTrees.\n")

    junk = workspace / "AI_Studio" / "nandatown" / ".venv" / "lib" / "site-packages"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "notes.md").write_text("# venv notes about regression")
    (workspace / "AI_Studio" / "nandatown" / "README.md").write_text("# nandatown")

    extract_mod.extract_all(settings)
    return settings


def concept_judge(task: JudgeTask) -> dict[str, Any]:
    """A plausible model answer, derived from the chunk's own source path."""
    rid = graph_mod.resource_id(task.payload["source"])
    return {
        "nodes": [
            {
                "id": "concept:regularization",
                "type": "Concept",
                "label": "Regularization",
                "attrs": {"name": "Regularization"},
            },
            {
                "id": "concept:linear-regression",
                "type": "Concept",
                "label": "Linear Regression",
                "attrs": {"name": "Linear Regression"},
            },
        ],
        "edges": [
            {"s": rid, "p": "covers", "o": "concept:regularization", "conf": 0.9},
            {
                "s": "concept:linear-regression",
                "p": "prerequisite_of",
                "o": "concept:regularization",
                "conf": 0.8,
            },
        ],
    }


def seed_graph(settings: Settings) -> None:
    """A small hand-built graph exercising every canned query."""
    graph_mod.append_nodes(
        settings,
        [
            {"id": "course:ae", "type": "Course", "label": "Analytics Edge", "attrs": {}},
            {
                "id": "resource:slides",
                "type": "Resource",
                "label": "Trees slides",
                "attrs": {"path": "Analytics Edge/lectures/trees.pdf"},
                "src": ["Analytics Edge/lectures/trees.pdf"],
            },
            {
                "id": "assignment:hw2",
                "type": "Assignment",
                "label": "HW2",
                "attrs": {"due_at": "2026-09-25T23:59:00Z", "kind": "pset"},
            },
            {"id": "concept:cart", "type": "Concept", "label": "CART", "attrs": {}},
            {"id": "concept:entropy", "type": "Concept", "label": "Entropy", "attrs": {}},
            {"id": "concept:probability", "type": "Concept", "label": "Probability", "attrs": {}},
            {"id": "concept:lonely", "type": "Concept", "label": "Lonely", "attrs": {}},
        ],
    )
    graph_mod.append_edges(
        settings,
        [
            {"s": "resource:slides", "p": "part_of", "o": "course:ae", "src": "a.pdf"},
            {"s": "assignment:hw2", "p": "part_of", "o": "course:ae", "src": "a.pdf"},
            {
                "s": "resource:slides",
                "p": "covers",
                "o": "concept:cart",
                "src": "a.pdf",
                "conf": 0.9,
            },
            {"s": "assignment:hw2", "p": "assesses", "o": "concept:cart", "src": "a.pdf"},
            {"s": "concept:entropy", "p": "prerequisite_of", "o": "concept:cart", "src": "a.pdf"},
            {
                "s": "concept:probability",
                "p": "prerequisite_of",
                "o": "concept:entropy",
                "src": "a.pdf",
            },
        ],
    )
    graph_mod.rebuild(settings)


# --------------------------------------------------------------------------
# ontology
# --------------------------------------------------------------------------
def test_load_ontology_parses_the_shipped_file(settings: Settings) -> None:
    onto = graph_mod.load_ontology(settings)
    assert "Concept" in onto.node_types
    assert onto.edge_types["covers"].target == ("Concept",)
    assert "Resource" in onto.allowed_edge_types()["covers"]["source"]


def test_ontology_rejects_an_unknown_node_type(settings: Settings) -> None:
    onto = graph_mod.load_ontology(settings)
    bad = {"id": "widget:1", "type": "Widget", "label": "w", "attrs": {}}
    with pytest.raises(OntologyError) as exc:
        onto.validate_node(bad)
    assert "Widget" in str(exc.value)
    assert "widget:1" in str(exc.value)  # the offending record is named


def test_ontology_rejects_an_unknown_attribute(settings: Settings) -> None:
    onto = graph_mod.load_ontology(settings)
    with pytest.raises(OntologyError, match="colour"):
        onto.validate_node(
            {"id": "concept:x", "type": "Concept", "label": "x", "attrs": {"colour": "red"}}
        )


def test_ontology_rejects_a_backwards_edge(settings: Settings) -> None:
    onto = graph_mod.load_ontology(settings)
    types = {"course:ae": "Course", "concept:cart": "Concept", "resource:r": "Resource"}
    # `part_of` goes Session/Assignment/Resource -> Course, never Course -> Concept.
    with pytest.raises(OntologyError) as exc:
        onto.validate_edge({"s": "course:ae", "p": "part_of", "o": "concept:cart"}, types)
    assert "cannot start at a Course" in str(exc.value)

    # `covers` points AT a Concept; a Resource covering a Course is invalid too.
    with pytest.raises(OntologyError, match="cannot point at a Course"):
        onto.validate_edge({"s": "resource:r", "p": "covers", "o": "course:ae"}, types)


def test_ontology_rejects_an_unknown_edge_type_and_dangling_endpoints(
    settings: Settings,
) -> None:
    onto = graph_mod.load_ontology(settings)
    with pytest.raises(OntologyError, match="unknown edge type"):
        onto.validate_edge(
            {"s": "course:ae", "p": "vibes_with", "o": "course:ae"}, {"course:ae": "Course"}
        )
    with pytest.raises(OntologyError, match="not a known node"):
        onto.validate_edge(
            {"s": "resource:x", "p": "part_of", "o": "course:ae"}, {"course:ae": "Course"}
        )


def test_append_validates_against_the_ontology(settings: Settings) -> None:
    with pytest.raises(OntologyError):
        graph_mod.append_nodes(settings, [{"id": "x:1", "type": "Widget", "label": "x"}])
    assert not graph_mod.nodes_jsonl(settings).exists()


def test_a_custom_ontology_in_the_config_dir_wins(settings: Settings) -> None:
    (settings.paths.config_dir / "ontology.yml").write_text(
        "version: 9\nnode_types:\n  Thing:\n    attributes: [name]\n"
        "edge_types:\n  relates_to:\n    source: [Thing]\n    target: [Thing]\n"
    )
    onto = graph_mod.load_ontology(settings)
    assert onto.version == 9
    assert set(onto.node_types) == {"Thing"}
    with pytest.raises(OntologyError, match="unknown node type 'Concept'"):
        onto.validate_node({"id": "concept:x", "type": "Concept", "label": "x"})


# --------------------------------------------------------------------------
# JSONL truth and projection
# --------------------------------------------------------------------------
def test_jsonl_is_append_only_and_dedupes_on_projection(settings: Settings) -> None:
    node = {"id": "course:ae", "type": "Course", "label": "Analytics Edge"}
    assert graph_mod.append_nodes(settings, [node]) == 1
    assert graph_mod.append_nodes(settings, [node]) == 0  # unchanged: nothing appended

    edge = {"s": "course:ae", "p": "part_of", "o": "course:ae", "src": "x.pdf"}
    graph_mod.append_nodes(
        settings, [{"id": "resource:r", "type": "Resource", "label": "R", "src": ["x.pdf"]}]
    )
    edge = {"s": "resource:r", "p": "part_of", "o": "course:ae", "src": "x.pdf"}
    assert graph_mod.append_edges(settings, [edge]) == 1
    assert graph_mod.append_edges(settings, [edge]) == 0

    # a later line for the same id is a correction, last write wins
    graph_mod.append_nodes(settings, [{**node, "label": "Analytics Edge (15.071)"}])
    assert graph_mod.load_nodes(settings)["course:ae"]["label"] == "Analytics Edge (15.071)"
    assert len(graph_mod.nodes_jsonl(settings).read_text().strip().splitlines()) == 3


def test_rebuild_projects_jsonl_into_duckdb(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, sql="SELECT count(*) AS n FROM nodes")
    assert rows[0]["n"] == 7
    rows = graph_mod.query(settings, sql="SELECT count(*) AS n FROM edges")
    assert rows[0]["n"] == 6
    attrs = graph_mod.query(
        settings,
        sql="SELECT json_extract_string(attrs, '$.due_at') AS due FROM nodes "
        "WHERE id = 'assignment:hw2'",
    )
    assert attrs[0]["due"] == "2026-09-25T23:59:00Z"


def test_deleting_the_duckdb_and_rebuilding_gives_identical_results(
    settings: Settings,
) -> None:
    """The DB is a projection: JSONL alone must fully reproduce it."""
    seed_graph(settings)
    before = {name: graph_mod.query(settings, canned=name) for name in graph_mod.CANNED}
    dump_before = (
        graph_mod.query(settings, sql="SELECT * FROM nodes ORDER BY id"),
        graph_mod.query(settings, sql="SELECT * FROM edges ORDER BY s, p, o, src"),
    )

    settings.paths.graph_db.unlink()
    assert not settings.paths.graph_db.exists()
    graph_mod.rebuild(settings)

    after = {name: graph_mod.query(settings, canned=name) for name in graph_mod.CANNED}
    dump_after = (
        graph_mod.query(settings, sql="SELECT * FROM nodes ORDER BY id"),
        graph_mod.query(settings, sql="SELECT * FROM edges ORDER BY s, p, o, src"),
    )
    assert after == before
    assert dump_after == dump_before


def test_rebuild_is_idempotent(settings: Settings) -> None:
    seed_graph(settings)
    first = graph_mod.rebuild(settings)
    second = graph_mod.rebuild(settings)
    assert (first.nodes, first.edges) == (second.nodes, second.edges)
    assert graph_mod.query(settings, sql="SELECT count(*) AS n FROM edges")[0]["n"] == 6


def test_entity_pages_are_written_for_every_node(settings: Settings) -> None:
    seed_graph(settings)
    entities = settings.paths.kb_graph / "entities"
    pages = {p.name for p in entities.glob("*.md")}
    assert graph_mod.entity_filename("concept:cart") in pages
    body = (entities / graph_mod.entity_filename("concept:cart")).read_text()
    assert "type: Concept" in body
    assert "## Incoming edges" in body and "## Outgoing edges" in body
    assert "`covers`" in body or "covers" in body
    assert "## Source documents" in body


# --------------------------------------------------------------------------
# canned queries
# --------------------------------------------------------------------------
def test_canned_concepts_by_course(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, canned="concepts_by_course")
    assert {"course": "Analytics Edge", "concept": "CART", "mentions": 2} in rows


def test_canned_assignments_due(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, canned="assignments_due")
    assert rows == [
        {
            "course": "Analytics Edge",
            "assignment": "HW2",
            "due_at": "2026-09-25T23:59:00Z",
            "kind": "pset",
            "node_id": "assignment:hw2",
        }
    ]


def test_canned_resources_for_concept(settings: Settings) -> None:
    seed_graph(settings)
    everything = graph_mod.query(settings, canned="resources_for_concept")
    assert [r["resource"] for r in everything] == ["Trees slides"]
    targeted = graph_mod.query(
        settings, canned="resources_for_concept", params={"concept": "concept:cart"}
    )
    assert targeted == everything
    assert (
        graph_mod.query(settings, canned="resources_for_concept", params={"concept": "nope"}) == []
    )


def test_canned_prerequisites_of_is_transitive(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, canned="prerequisites_of", params={"node": "concept:cart"})
    assert [(r["prerequisite"], r["depth"]) for r in rows] == [("Entropy", 1), ("Probability", 2)]


def test_canned_orphans(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, canned="orphans")
    assert [r["id"] for r in rows] == ["concept:lonely"]


def test_unknown_canned_query_names_the_alternatives(settings: Settings) -> None:
    seed_graph(settings)
    with pytest.raises(Exception, match="unknown canned query"):
        graph_mod.query(settings, canned="nope")


def test_query_with_no_arguments_returns_a_summary(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings)
    assert {r["table_name"]: r["rows"] for r in rows} == {"nodes": 7, "edges": 6}


def test_query_rebuilds_a_missing_database(settings: Settings) -> None:
    seed_graph(settings)
    settings.paths.graph_db.unlink()
    assert graph_mod.query(settings, sql="SELECT count(*) AS n FROM nodes")[0]["n"] == 7


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def test_extract_graph_without_a_judge_builds_the_deterministic_backbone(
    seeded: Settings,
) -> None:
    report = graph_mod.extract_graph(seeded, None)
    assert report.documents == 3
    nodes = graph_mod.load_nodes(seeded)
    assert nodes[graph_mod.course_id("Machine Learning")]["type"] == "Course"
    assert nodes[graph_mod.resource_id("Machine Learning/lectures/lec01.pdf")]["attrs"]["path"] == (
        "Machine Learning/lectures/lec01.pdf"
    )
    assert all(e["p"] == "part_of" for e in graph_mod.load_edges(seeded))
    assert "no judge" in report.judge_note


def test_extract_graph_is_idempotent(seeded: Settings) -> None:
    first = graph_mod.extract_graph(seeded, StubJudge(concept_judge))
    second = graph_mod.extract_graph(seeded, StubJudge(concept_judge))
    assert (second.appended_nodes, second.appended_edges) == (0, 0)
    assert (first.nodes, first.edges) == (second.nodes, second.edges)
    assert graph_mod.query(seeded, sql="SELECT count(*) AS n FROM edges")[0]["n"] == first.edges


def test_extract_graph_uses_the_judge_and_stores_valid_triples(seeded: Settings) -> None:
    judge = StubJudge(concept_judge)
    report = graph_mod.extract_graph(seeded, judge)
    assert report.judge_used
    assert report.rejected == []
    assert judge.tasks, "the judge was never called"

    task = judge.tasks[0]
    assert task.name == "graph_extract"
    assert "Concept" in task.payload["allowed_node_types"]
    assert task.payload["allowed_edge_types"]["covers"]["target"] == ["Concept"]
    assert task.payload["text"]
    assert "untrusted" in (task.instructions + task.rules).lower()
    # the stub's answer is exactly what the schema demands
    validate_result(task, concept_judge(task))

    nodes = graph_mod.load_nodes(seeded)
    assert nodes["concept:regularization"]["type"] == "Concept"
    covers = [e for e in graph_mod.load_edges(seeded) if e["p"] == "covers"]
    assert covers and all(e["o"] == "concept:regularization" for e in covers)
    rows = graph_mod.query(
        seeded, canned="prerequisites_of", params={"node": "concept:regularization"}
    )
    assert [r["prerequisite"] for r in rows] == ["Linear Regression"]


def test_extract_graph_rejects_judged_records_that_violate_the_ontology(
    seeded: Settings,
) -> None:
    bad = {
        "nodes": [{"id": "widget:1", "type": "Widget", "label": "W", "attrs": {}}],
        "edges": [{"s": "widget:1", "p": "covers", "o": "widget:1", "conf": 0.9}],
    }
    report = graph_mod.extract_graph(seeded, StubJudge(bad))
    assert report.rejected, "a bad node type should be rejected, not stored"
    assert "widget:1" not in graph_mod.load_nodes(seeded)
    assert not [e for e in graph_mod.load_edges(seeded) if e["p"] == "covers"]


def test_extract_graph_rejects_results_that_fail_the_json_schema(seeded: Settings) -> None:
    report = graph_mod.extract_graph(seeded, StubJudge({"nodes": "not-a-list", "edges": []}))
    assert report.rejected
    assert not report.judge_used


def test_extract_graph_degrades_when_the_judge_returns_nothing(seeded: Settings) -> None:
    """The rules driver has no `graph_extract` handler; it returns {}."""
    judge = EmptyJudge()
    report = graph_mod.extract_graph(seeded, judge)
    assert judge.calls > 0
    assert report.judge_empty == judge.calls
    assert not report.judge_used
    assert "empty result" in report.judge_note
    # the deterministic backbone still landed
    assert graph_mod.load_edges(seeded)
    assert all(e["p"] == "part_of" for e in graph_mod.load_edges(seeded))


def test_extract_graph_never_touches_nandatown(seeded: Settings) -> None:
    graph_mod.extract_graph(seeded, StubJudge(concept_judge))
    blob = json.dumps([graph_mod.load_nodes(seeded), graph_mod.load_edges(seeded)], default=str)
    assert "nandatown" not in blob
    assert "site-packages" not in blob
    entities = (settings_dir := seeded.paths.kb_graph / "entities")
    assert entities.is_dir()
    assert not [p for p in settings_dir.glob("*.md") if "nandatown" in p.read_text()]


def test_extract_graph_since_filters_documents(seeded: Settings) -> None:
    report = graph_mod.extract_graph(seeded, None, since="2999-01-01T00:00:00+00:00")
    assert report.documents == 0


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def test_duckdb_is_the_default_backend(settings: Settings) -> None:
    assert isinstance(graph_mod.get_backend(settings), graph_mod.DuckDBBackend)
    assert isinstance(graph_mod.get_backend(settings), graph_mod.GraphBackend)


def test_neo4j_backend_is_a_clear_stub(settings: Settings) -> None:
    settings.graph.backend = "neo4j"
    with pytest.raises(NotImplementedError) as exc:
        graph_mod.get_backend(settings)
    assert "graph.backend" in str(exc.value)


def test_backend_upserts_are_replacements_not_duplicates(settings: Settings) -> None:
    seed_graph(settings)
    backend = graph_mod.get_backend(settings)
    edge = {
        "s": "resource:slides",
        "p": "covers",
        "o": "concept:cart",
        "src": "a.pdf",
        "conf": 0.1,
    }
    backend.upsert_edges([edge])
    backend.upsert_edges([edge])
    rows = backend.query(
        "SELECT conf FROM edges WHERE s='resource:slides' AND p='covers' AND o='concept:cart'"
    )
    assert rows == [{"conf": 0.1}]
