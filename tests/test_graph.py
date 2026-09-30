"""The knowledge graph: JSONL truth, the backbone, `graph add`, projection, queries, `check`.

Record-level ontology rules are tested in `test_ontology.py`; here they are
exercised only through the storage paths that must enforce them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from mitsync.core.config import Settings
from mitsync.core.errors import MitsyncError, OntologyError
from mitsync.knowledge import extract as extract_mod
from mitsync.knowledge import graph as graph_mod
from tests.test_extract import make_notebook, make_pdf

ML = "Machine Learning"
LEC = f"{ML}/lectures/lec01.pdf"
HW = f"{ML}/assignments/hw-01/hw1.ipynb"
LOOSE = f"{ML}/Lec1.pdf"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def seeded(workspace: Path, settings: Settings) -> Settings:
    """A filed lecture, a filed assignment, a loose duplicate, a mirror copy, nandatown."""
    ml = workspace / ML
    make_pdf(ml / "lectures" / "lec01.pdf", ("regularization and ridge regression",))
    make_notebook(ml / "assignments" / "hw-01" / "hw1.ipynb")
    (ml / "assignments" / "hw-01" / "data.zip").write_bytes(b"PK\x03\x04 not really")
    (ml / "Lec1.pdf").write_bytes((ml / "lectures" / "lec01.pdf").read_bytes())
    (ml / "other").mkdir()
    (ml / "other" / "textbook.pdf").write_bytes(b"%PDF-1.4 textbook")

    course = workspace / "_canvas" / "15.095 Machine Learning Under a Modern Optimization Lens"
    (course / "_meta").mkdir(parents=True)
    (course / "_meta" / "courses.json").write_text(json.dumps({"course_canvas_id": 38524}))
    (course / "Lectures").mkdir()
    os.link(ml / "lectures" / "lec01.pdf", course / "Lectures" / "lec01.pdf")
    make_pdf(course / "Lectures" / "Lecture05.pdf", ("trees",))  # mirrored, not yet filed

    (settings.paths.config_dir / "courses.yml").write_text(
        f"courses:\n  - folder: {ML}\n    canvas_id: 38524\n    course_number: '15.095'\n"
        "  - folder: AI_Studio\n"
    )
    junk = workspace / "AI_Studio" / "nandatown" / ".venv" / "lib" / "site-packages"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "notes.md").write_text("# venv notes about regression")
    (workspace / "AI_Studio" / "nandatown" / "README.md").write_text("# nandatown")

    extract_mod.extract_all(settings)
    return settings


def write_jsonl(path: Path, records: list[Any]) -> Path:
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records))
    return path


def by_path(settings: Settings) -> dict[str, dict[str, Any]]:
    return {
        n["attrs"]["path"]: n
        for n in graph_mod.load_nodes(settings).values()
        if n["type"] in ("File", "DataFile")
    }


LEC01 = "lecture:machine-learning:01"
HW01 = "assignment:machine-learning:hw-01"
COURSE = "course:machine-learning"


def agent_facts(tmp_path: Path) -> Path:
    """What an agent writes after reading lec01 and hw1: concepts and where they occur."""
    return write_jsonl(
        tmp_path / "facts.jsonl",
        [
            {
                "id": "concept:regularization",
                "type": "Concept",
                "label": "Regularization",
                "attrs": {"name": "Regularization"},
                "src": [LEC],
            },
            {
                "id": "concept:ridge-regression",
                "type": "Concept",
                "label": "Ridge regression",
                "attrs": {"name": "Ridge regression", "aliases": ["L2 regularization"]},
                "src": LEC,
            },
            {"s": "concept:regularization", "p": "concept_in_lecture", "o": LEC01, "src": LEC},
            {
                "s": "concept:ridge-regression",
                "p": "concept_in_assignment",
                "o": HW01,
                "attrs": {"depth": "applied"},
                "src": HW,
                "conf": 0.8,
            },
        ],
    )


def seed_graph(settings: Settings) -> None:
    """A small hand-built graph exercising every canned query."""
    graph_mod.append_nodes(
        settings,
        [
            {"id": "course:ae", "type": "Course", "label": "Analytics Edge",
             "attrs": {"folder": "Analytics Edge"}},
            {"id": "lecture:ae:05", "type": "Lecture", "label": "Trees", "attrs": {"number": 5}},
            {"id": "file:slides", "type": "File", "label": "trees.pdf",
             "attrs": {"path": "Analytics Edge/lectures/trees.pdf"},
             "src": ["Analytics Edge/lectures/trees.pdf"]},
            {"id": "file:sub", "type": "File", "label": "hw2.pdf",
             "attrs": {"path": "Analytics Edge/assignments/hw-02/hw2.pdf"}},
            {"id": "assignment:ae:hw-02", "type": "Assignment", "label": "HW2",
             "attrs": {"due_at": "2026-09-25T23:59:00Z", "submission_status": "submitted"}},
            {"id": "concept:cart", "type": "Concept", "label": "CART", "attrs": {"name": "CART"}},
            {"id": "concept:lonely", "type": "Concept", "label": "Lonely",
             "attrs": {"name": "Lonely"}},
        ],
    )  # fmt: skip
    graph_mod.append_edges(
        settings,
        [
            {"s": "lecture:ae:05", "p": "lecture_of_course", "o": "course:ae", "src": "a.pdf"},
            {"s": "assignment:ae:hw-02", "p": "assignment_of_course", "o": "course:ae",
             "src": "a.pdf"},
            {"s": "file:slides", "p": "file_of_lecture", "o": "lecture:ae:05", "src": "a.pdf",
             "attrs": {"role": "slides"}},
            {"s": "file:sub", "p": "file_of_assignment", "o": "assignment:ae:hw-02",
             "src": "a.pdf", "attrs": {"role": "submission"}},
            {"s": "concept:cart", "p": "concept_in_lecture", "o": "lecture:ae:05", "src": "a.pdf",
             "conf": 0.9},
            {"s": "concept:cart", "p": "concept_in_assignment", "o": "assignment:ae:hw-02",
             "src": "a.pdf"},
        ],
    )  # fmt: skip
    graph_mod.rebuild(settings)


# --------------------------------------------------------------------------
# JSONL truth and projection
# --------------------------------------------------------------------------
def test_append_validates_against_the_ontology(settings: Settings) -> None:
    with pytest.raises(OntologyError):
        graph_mod.append_nodes(settings, [{"id": "x:1", "type": "Widget", "label": "x"}])
    assert not graph_mod.nodes_jsonl(settings).exists()


def test_jsonl_is_append_only_and_attrs_merge_on_projection(settings: Settings) -> None:
    node = {"id": "course:ae", "type": "Course", "label": "Analytics Edge",
            "attrs": {"folder": "Analytics Edge"}}  # fmt: skip
    assert graph_mod.append_nodes(settings, [node]) == 1
    assert graph_mod.append_nodes(settings, [node]) == 0  # unchanged: nothing appended

    lec = {"id": "lecture:ae:01", "type": "Lecture", "label": "L1", "src": ["x.pdf"]}
    graph_mod.append_nodes(settings, [lec])
    edge = {"s": "lecture:ae:01", "p": "lecture_of_course", "o": "course:ae", "src": "x.pdf"}
    assert graph_mod.append_edges(settings, [edge]) == 1
    assert graph_mod.append_edges(settings, [edge]) == 0

    # a later line for the same id is a correction: attrs merge, the last label wins
    graph_mod.append_nodes(
        settings, [{**node, "label": "AE", "attrs": {"course_number": "15.072"}}]
    )
    merged = graph_mod.load_nodes(settings)["course:ae"]
    assert merged["label"] == "AE"
    assert merged["attrs"] == {"folder": "Analytics Edge", "course_number": "15.072"}
    assert len(graph_mod.nodes_jsonl(settings).read_text().strip().splitlines()) == 3


def test_rebuild_projects_jsonl_into_duckdb(settings: Settings) -> None:
    seed_graph(settings)
    assert graph_mod.query(settings, sql="SELECT count(*) AS n FROM nodes")[0]["n"] == 7
    assert graph_mod.query(settings, sql="SELECT count(*) AS n FROM edges")[0]["n"] == 6
    attrs = graph_mod.query(
        settings,
        sql="SELECT json_extract_string(attrs, '$.due_at') AS due FROM nodes "
        "WHERE id = 'assignment:ae:hw-02'",
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


def test_entity_pages_are_written_for_every_node(settings: Settings) -> None:
    seed_graph(settings)
    entities = settings.paths.kb_graph / "entities"
    body = (entities / graph_mod.entity_filename("concept:cart")).read_text()
    assert "type: Concept" in body
    assert "## Incoming edges" in body and "## Outgoing edges" in body
    assert "`concept_in_lecture`" in body
    assert "## Source documents" in body


# --------------------------------------------------------------------------
# canned queries
# --------------------------------------------------------------------------
def test_canned_concepts_by_course(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, canned="concepts_by_course")
    assert rows == [{"course": "Analytics Edge", "concept": "CART", "items": 2}]


def test_canned_assignments_due(settings: Settings) -> None:
    seed_graph(settings)
    assert graph_mod.query(settings, canned="assignments_due") == [
        {
            "course": "Analytics Edge",
            "assignment": "HW2",
            "due_at": "2026-09-25T23:59:00Z",
            "status": "submitted",
            "node_id": "assignment:ae:hw-02",
        }
    ]


def test_canned_files_for_concept(settings: Settings) -> None:
    seed_graph(settings)
    everything = graph_mod.query(settings, canned="files_for_concept")
    assert [(r["item"], r["path"]) for r in everything] == [
        ("HW2", "Analytics Edge/assignments/hw-02/hw2.pdf"),
        ("Trees", "Analytics Edge/lectures/trees.pdf"),
    ]
    targeted = graph_mod.query(
        settings, canned="files_for_concept", params={"concept": "concept:cart"}
    )
    assert targeted == everything
    assert graph_mod.query(settings, canned="files_for_concept", params={"concept": "no"}) == []


def test_canned_files_of_and_submitted(settings: Settings) -> None:
    seed_graph(settings)
    rows = graph_mod.query(settings, canned="files_of", params={"item": "lecture:ae:05"})
    assert [(r["role"], r["path"]) for r in rows] == [
        ("slides", "Analytics Edge/lectures/trees.pdf")
    ]
    assert graph_mod.query(settings, canned="submitted") == [
        {
            "assignment": "HW2",
            "status": "submitted",
            "via": None,
            "submitted_file": "Analytics Edge/assignments/hw-02/hw2.pdf",
        }
    ]


def test_canned_orphans(settings: Settings) -> None:
    seed_graph(settings)
    assert [r["id"] for r in graph_mod.query(settings, canned="orphans")] == ["concept:lonely"]


def test_unknown_canned_query_names_the_alternatives(settings: Settings) -> None:
    seed_graph(settings)
    with pytest.raises(MitsyncError, match="unknown canned query"):
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
# the deterministic backbone
# --------------------------------------------------------------------------
def test_one_course_node_for_the_folder_and_its_canvas_mirror(seeded: Settings) -> None:
    graph_mod.build_backbone(seeded)
    courses = [n for n in graph_mod.load_nodes(seeded).values() if n["type"] == "Course"]
    assert [c["id"] for c in courses] == ["course:ai-studio", COURSE]
    assert courses[1]["attrs"] == {"folder": ML, "canvas_id": 38524, "course_number": "15.095"}


def test_a_filed_hardlink_and_its_mirror_original_are_one_file_node(seeded: Settings) -> None:
    graph_mod.build_backbone(seeded)
    files = by_path(seeded)
    lec = files[LEC]
    assert lec["id"] == graph_mod.file_id(LEC)
    assert lec["type"] == "File"
    assert lec["attrs"]["mirror_path"].endswith("/Lectures/lec01.pdf")
    assert lec["attrs"]["duplicates"] == [LOOSE]  # same bytes, pre-existing, not the filed copy
    assert LOOSE not in files and lec["attrs"]["mirror_path"] not in files


def test_the_folder_decides_the_parent_when_it_is_unambiguous(seeded: Settings) -> None:
    graph_mod.build_backbone(seeded)
    files = by_path(seeded)
    parents = {
        (e["s"], e["p"], e["o"]) for e in graph_mod.load_edges(seeded) if e["p"].startswith("file_")
    }
    assert (files[LEC]["id"], "file_of_lecture", LEC01) in parents
    assert (files[HW]["id"], "file_of_assignment", HW01) in parents
    zip_ = files[f"{ML}/assignments/hw-01/data.zip"]
    assert zip_["type"] == "DataFile" and (zip_["id"], "file_of_assignment", HW01) in parents
    textbook = files[f"{ML}/other/textbook.pdf"]["id"]
    assert (textbook, "file_of_course", COURSE) in parents
    nodes = graph_mod.load_nodes(seeded)
    assert nodes[HW01]["attrs"] == {
        "folder": f"{ML}/assignments/hw-01", "title": "hw-01", "kind": "homework",
    }  # fmt: skip
    assert nodes[LEC01]["attrs"] == {"number": 1}


def test_one_dataset_filed_under_two_items_belongs_to_both(seeded: Settings) -> None:
    recs = seeded.paths.workspace / ML / "recitations"
    for item in ("recitation-02", "recitation-03"):
        (recs / item).mkdir(parents=True)
        (recs / item / "loans.csv").write_text("id,amount\n1,2\n")
    graph_mod.build_backbone(seeded)
    files = by_path(seeded)
    for item in ("recitation-02", "recitation-03"):
        node = files[f"{ML}/recitations/{item}/loans.csv"]
        assert "duplicates" not in node["attrs"]
    assert not [v for v in graph_mod.check(seeded) if "loans" in v.message]


def test_what_the_folder_does_not_decide_is_left_for_the_agent(seeded: Settings) -> None:
    graph_mod.build_backbone(seeded)
    unfiled = [v for v in graph_mod.check(seeded) if v.code == "unfiled"]
    mirror_only = by_path(seeded)
    [path] = [p for p in mirror_only if p.endswith("Lecture05.pdf")]
    assert [v.node for v in unfiled] == [mirror_only[path]["id"]]


def test_check_escalates_duplicates_to_the_human(seeded: Settings) -> None:
    graph_mod.build_backbone(seeded)
    [dup] = [v for v in graph_mod.check(seeded) if v.code == "duplicate_content"]
    assert dup.severity == "human" and LOOSE in dup.message


def test_backbone_is_regenerated_not_appended(seeded: Settings) -> None:
    first = graph_mod.build_backbone(seeded)
    second = graph_mod.build_backbone(seeded)
    assert first.changed and not second.changed
    assert (first.nodes, first.edges) == (second.nodes, second.edges)
    assert not graph_mod.nodes_jsonl(seeded).exists(), "the backbone never touches agent truth"

    (seeded.paths.workspace / ML / "other" / "textbook.pdf").unlink()
    third = graph_mod.build_backbone(seeded)
    assert third.changed and third.nodes == first.nodes - 1
    assert f"{ML}/other/textbook.pdf" not in by_path(seeded)


def test_backbone_never_touches_nandatown(seeded: Settings) -> None:
    graph_mod.build_backbone(seeded)
    blob = json.dumps([graph_mod.load_nodes(seeded), graph_mod.load_edges(seeded)], default=str)
    assert "nandatown" not in blob
    assert "site-packages" not in blob
    entities = seeded.paths.kb_graph / "entities"
    assert not [p for p in entities.glob("*.md") if "nandatown" in p.read_text()]


# --------------------------------------------------------------------------
# graph add: facts the agent wrote
# --------------------------------------------------------------------------
def test_graph_add_appends_valid_agent_facts(seeded: Settings, tmp_path: Path) -> None:
    graph_mod.build_backbone(seeded)
    report = graph_mod.add_records(seeded, agent_facts(tmp_path))

    assert (report.appended_nodes, report.appended_edges) == (2, 2)
    nodes = graph_mod.load_nodes(seeded)
    assert nodes["concept:ridge-regression"]["src"] == [LEC]
    rows = graph_mod.query(seeded, canned="files_for_concept", params={"concept": "ridge%"})
    assert {r["path"] for r in rows} == {HW, f"{ML}/assignments/hw-01/data.zip"}
    assert (seeded.paths.kb_graph / "entities" / "concept-regularization.md").exists()


def test_graph_add_is_idempotent(seeded: Settings, tmp_path: Path) -> None:
    graph_mod.build_backbone(seeded)
    facts = agent_facts(tmp_path)
    graph_mod.add_records(seeded, facts)
    again = graph_mod.add_records(seeded, facts)
    assert (again.appended_nodes, again.appended_edges) == (0, 0)


def test_an_agent_attaching_an_unfiled_file_closes_the_violation(
    seeded: Settings, tmp_path: Path
) -> None:
    graph_mod.build_backbone(seeded)
    [path] = [p for p in by_path(seeded) if p.endswith("Lecture05.pdf")]
    fid = by_path(seeded)[path]["id"]
    lec5 = "lecture:machine-learning:05"
    graph_mod.add_records(
        seeded,
        write_jsonl(
            tmp_path / "f.jsonl",
            [
                {"id": lec5, "type": "Lecture", "label": "Lecture 5", "attrs": {"number": 5},
                 "src": [path]},
                {"s": lec5, "p": "lecture_of_course", "o": COURSE, "src": path},
                {"s": fid, "p": "file_of_lecture", "o": lec5, "src": path},
            ],
        ),
    )  # fmt: skip
    assert "unfiled" not in {v.code for v in graph_mod.check(seeded)}


def test_an_edge_to_a_vanished_file_is_stale_not_fatal(seeded: Settings, tmp_path: Path) -> None:
    graph_mod.build_backbone(seeded)
    textbook = by_path(seeded)[f"{ML}/other/textbook.pdf"]["id"]
    graph_mod.add_records(
        seeded,
        write_jsonl(
            tmp_path / "f.jsonl",
            [{"s": textbook, "p": "file_of_course", "o": COURSE, "src": LEC,
              "attrs": {"reason": "the course textbook, used by every lecture"}}],
        ),
    )  # fmt: skip
    (seeded.paths.workspace / ML / "other" / "textbook.pdf").unlink()
    graph_mod.build_backbone(seeded)  # projects without the stale edge
    stale = [v for v in graph_mod.check(seeded) if v.code == "stale_edge"]
    assert [(v.node, v.severity) for v in stale] == [(textbook, "info")]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("{not json", "not valid JSON"),
        ('["a", "list"]', "JSON object"),
        ({"id": "widget:1", "type": "Widget", "label": "W", "src": [LEC]}, "unknown node type"),
        ({"id": "concept:x", "type": "Concept", "src": [LEC]}, "missing required field(s)"),
        ({"id": "concept:x", "type": "Concept", "label": "X"}, "['src']"),
        (
            {"id": "concept:x", "type": "Concept", "label": "X", "src": [LEC], "kind": "node"},
            "unknown key(s) ['kind']",
        ),
        (
            {"id": "concept:x", "type": "Concept", "label": "X", "src": [LEC],
             "attrs": {"name": "X", "z": 1}},
            "no attribute(s) ['z']",
        ),
        ({"id": "concept:x", "type": "Concept", "label": "X", "src": [LEC]}, "name: Field"),
        (
            {"id": "concept:x", "type": "Concept", "label": "X", "src": ["/etc/passwd"]},
            "workspace-relative",
        ),
        (
            {"id": "concept:x", "type": "Concept", "label": "X", "src": ["_agent/README.md"]},
            "machinery",
        ),
        (
            {"id": "course:new", "type": "Course", "label": "New", "src": [LEC],
             "attrs": {"folder": "New"}},
            "come from the files on disk",
        ),
        (
            {"id": "file:slides", "type": "DataFile", "label": "x", "src": [LEC],
             "attrs": {"path": "x"}},
            "already exists as a File",
        ),
        (
            {"s": "concept:ghost", "p": "concept_in_lecture", "o": "lecture:ae:05", "src": LEC},
            "not a known node",
        ),
        ({"s": "concept:cart", "p": "teleports", "o": "concept:cart", "src": LEC}, "unknown edge"),
        (
            {"s": "concept:cart", "p": "lecture_of_course", "o": "course:ae", "src": LEC},
            "cannot start at",
        ),
        ({"s": "concept:cart", "p": "concept_in_lecture", "o": "lecture:ae:05"}, "['src']"),
        (
            {"s": "concept:cart", "p": "concept_in_lecture", "o": "lecture:ae:05", "src": LEC,
             "conf": 3},
            "between 0 and 1",
        ),
        (
            {"s": "file:slides", "p": "file_of_course", "o": "course:ae", "src": LEC},
            "reason: Field required",
        ),
        ({"label": "neither"}, "a line is a node"),
    ],
)  # fmt: skip
def test_graph_add_rejects_the_whole_file_on_any_bad_line(
    settings: Settings, tmp_path: Path, line: Any, message: str
) -> None:
    seed_graph(settings)
    before = (
        graph_mod.nodes_jsonl(settings).read_text(),
        graph_mod.triples_jsonl(settings).read_text(),
    )
    good = {"id": "concept:fine", "type": "Concept", "label": "Fine", "src": [LEC],
            "attrs": {"name": "Fine"}}  # fmt: skip
    path = write_jsonl(tmp_path / "facts.jsonl", [good, line])

    with pytest.raises(MitsyncError) as excinfo:
        graph_mod.add_records(settings, path)

    text = str(excinfo.value)
    assert "nothing was added" in text
    assert "line 2:" in text and message in text
    assert "line 1:" not in text
    after = (
        graph_mod.nodes_jsonl(settings).read_text(),
        graph_mod.triples_jsonl(settings).read_text(),
    )
    assert after == before, "a rejected file must not append anything"


def test_graph_add_lists_every_bad_line(settings: Settings, tmp_path: Path) -> None:
    seed_graph(settings)
    path = write_jsonl(
        tmp_path / "facts.jsonl",
        ["{broken", "", {"id": "x:1", "type": "Widget", "label": "W", "src": [LEC]}],
    )
    with pytest.raises(MitsyncError) as excinfo:
        graph_mod.add_records(settings, path)
    assert "line 1:" in str(excinfo.value) and "line 3:" in str(excinfo.value)
    assert "2 error(s)" in str(excinfo.value)


def test_graph_add_accepts_edges_to_nodes_defined_later_in_the_file(
    settings: Settings, tmp_path: Path
) -> None:
    seed_graph(settings)
    path = write_jsonl(
        tmp_path / "facts.jsonl",
        [
            {"s": "concept:gini", "p": "concept_in_lecture", "o": "lecture:ae:05", "src": LEC},
            {"id": "concept:gini", "type": "Concept", "label": "Gini impurity", "src": [LEC],
             "attrs": {"name": "Gini impurity"}},
        ],
    )  # fmt: skip
    report = graph_mod.add_records(settings, path)
    assert (report.appended_nodes, report.appended_edges) == (1, 1)


def test_graph_add_needs_the_file(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(MitsyncError, match="not found"):
        graph_mod.add_records(settings, tmp_path / "missing.jsonl")


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def test_duckdb_is_the_default_backend(settings: Settings) -> None:
    assert isinstance(graph_mod.get_backend(settings), graph_mod.DuckDBBackend)
    assert isinstance(graph_mod.get_backend(settings), graph_mod.GraphBackend)


def test_an_unimplemented_backend_fails_with_a_clear_message(settings: Settings) -> None:
    settings.graph.backend = "neo4j"  # type: ignore[assignment] -- not in the Literal
    with pytest.raises(MitsyncError) as exc:
        graph_mod.get_backend(settings)
    assert "graph.backend" in str(exc.value)
    assert "neo4j" in str(exc.value)


def test_backend_upserts_are_replacements_not_duplicates(settings: Settings) -> None:
    seed_graph(settings)
    backend = graph_mod.get_backend(settings)
    edge = {"s": "concept:cart", "p": "concept_in_lecture", "o": "lecture:ae:05", "src": "a.pdf",
            "conf": 0.1}  # fmt: skip
    backend.upsert_edges([edge])
    backend.upsert_edges([edge])
    rows = backend.query("SELECT conf FROM edges WHERE s='concept:cart' AND p='concept_in_lecture'")
    assert rows == [{"conf": 0.1}]


# --------------------------------------------------------------------------
# corruption in the append-only source of truth is loud, never skipped
# --------------------------------------------------------------------------
def test_a_malformed_jsonl_line_raises_naming_the_file_and_line(settings: Settings) -> None:
    """mitsync wrote nodes.jsonl. Garbage in it means mitsync wrote garbage."""
    seed_graph(settings)
    path = graph_mod.nodes_jsonl(settings)
    path.write_text(path.read_text() + "{not json\n")
    with pytest.raises(MitsyncError, match="not valid JSON"):
        graph_mod.load_nodes(settings)


def test_a_node_record_with_no_id_raises(settings: Settings) -> None:
    seed_graph(settings)
    path = graph_mod.nodes_jsonl(settings)
    path.write_text(path.read_text() + json.dumps({"type": "Concept", "label": "x"}) + "\n")
    with pytest.raises(MitsyncError, match="no id"):
        graph_mod.load_nodes(settings)
