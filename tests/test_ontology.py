"""The pydantic ontology: record validation and whole-graph structure rules."""

from __future__ import annotations

from typing import Any

import pytest

from mitsync.core.errors import OntologyError
from mitsync.knowledge import ontology as onto

ML = "Machine Learning"


def node(nid: str, ntype: str, **attrs: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "label": nid, "attrs": attrs, "src": []}


def edge(s: str, p: str, o: str, **attrs: Any) -> dict[str, Any]:
    return {"s": s, "p": p, "o": o, "attrs": attrs, "src": "x.pdf"}


def pdf(nid: str, path: str) -> dict[str, Any]:
    return node(nid, "PdfFile", path=path)


def codes(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[tuple[str, str]]:
    found = onto.structure_violations({n["id"]: n for n in nodes}, edges)
    return [(v.code, v.node) for v in found]


COURSE = node("course:machine-learning", "Course", folder=ML)
HW1 = onto.item_id(onto.Assignment, ML, "hw-01")
LEC3 = onto.item_id(onto.Lecture, ML, "03")


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def test_the_ontology_has_exactly_the_agreed_types() -> None:
    assert set(onto.NODE_TYPES) == {
        "Course", "Syllabus", "Lecture", "Recitation", "Assignment",
        "PdfFile", "DataFile", "Repo", "Concept",
    }  # fmt: skip
    assert set(onto.EDGE_TYPES) == {
        "course_follows_syllabus", "lecture_of_course", "assignment_of_course",
        "recitation_of_course", "file_of_lecture", "file_of_assignment", "file_of_recitation",
        "file_of_syllabus", "file_of_course", "concept_in_lecture", "concept_in_assignment",
        "concept_in_recitation", "repo_of_assignment", "repo_of_course",
    }  # fmt: skip


def test_every_edge_endpoint_is_a_registered_node_type() -> None:
    for spec in onto.EDGE_TYPES.values():
        for t in (*spec.source, *spec.target):
            assert onto.NODE_TYPES[t.__name__] is t


def test_describe_carries_the_docstrings_agents_follow() -> None:
    schema = onto.describe()
    assert "LAST RESORT" in schema["edge_types"]["file_of_course"]["doc"]
    assert schema["edge_types"]["file_of_course"]["required"] == ["reason"]
    assert schema["node_types"]["Course"]["required"] == ["folder"]
    assert schema["edge_types"]["file_of_lecture"]["source"] == ["PdfFile", "DataFile"]


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
def test_a_valid_node_passes() -> None:
    onto.validate_node(node(LEC3, "Lecture", number=3, held_on="2026-09-10"))


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (node("lec:x", "Lecture"), "id must look like 'lecture:"),
        (node("lecture:Machine Learning:3", "Lecture"), "id must look like"),
        (node("concept:x", "Concept", name="x", colour="red"), "no attribute(s) ['colour']"),
        (node("course:x", "Course"), "folder: Field required"),
        (node(HW1, "Assignment", kind="pset"), "kind"),
        (node("widget:1", "Widget"), "unknown node type 'Widget'"),
    ],
)
def test_bad_nodes_are_refused_by_name(record: dict[str, Any], message: str) -> None:
    with pytest.raises(OntologyError) as exc:
        onto.validate_node(record)
    assert message in str(exc.value)
    assert record["id"] in str(exc.value)


TYPES = {"file:a": "PdfFile", "course:ml": "Course", "lecture:ml:01": "Lecture"}


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (edge("course:ml", "lecture_of_course", "lecture:ml:01"), "cannot start at a Course"),
        (edge("file:a", "file_of_lecture", "course:ml"), "cannot point at a Course"),
        (edge("file:a", "file_of_course", "course:ml"), "reason: Field required"),
        (edge("file:a", "file_of_course", "course:ml", reason="short"), "at least 10"),
        (edge("file:a", "file_of_lecture", "lecture:ml:01", role="x"), "role"),
        (edge("file:a", "vibes_with", "course:ml"), "unknown edge type"),
        (edge("file:ghost", "file_of_lecture", "lecture:ml:01"), "not a known node"),
    ],
)
def test_bad_edges_are_refused(record: dict[str, Any], message: str) -> None:
    with pytest.raises(OntologyError, match=None) as exc:
        onto.validate_edge(record, TYPES)
    assert message in str(exc.value)


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------
def test_a_complete_graph_has_no_violations() -> None:
    nodes = [
        COURSE,
        node(HW1, "Assignment"),
        node(LEC3, "Lecture"),
        node("concept:cart", "Concept", name="CART"),
        pdf("file:1", f"{ML}/assignments/hw-01/hw1.pdf"),
        pdf("file:2", f"{ML}/Lec03.pdf"),  # loose pre-existing file: any parent in its course
    ]
    edges = [
        edge(HW1, "assignment_of_course", COURSE["id"]),
        edge(LEC3, "lecture_of_course", COURSE["id"]),
        edge("concept:cart", "concept_in_lecture", LEC3),
        edge("file:1", "file_of_assignment", HW1, role="handout"),
        edge("file:2", "file_of_lecture", LEC3),
    ]
    assert codes(nodes, edges) == []


def test_missing_parents_are_reported_per_node_type() -> None:
    nodes = [
        COURSE,
        node(LEC3, "Lecture"),
        node("concept:cart", "Concept", name="CART"),
        pdf("file:1", f"{ML}/Lec03.pdf"),
        node("repo:nandatown", "Repo", name="nandatown"),
    ]
    assert codes(nodes, []) == [
        ("no_course", LEC3),
        ("no_course", "repo:nandatown"),
        ("orphan_concept", "concept:cart"),
        ("unfiled", "file:1"),
    ]


def test_the_same_fact_from_two_sources_is_one_parent_but_two_parents_are_not() -> None:
    nodes = [COURSE, node(LEC3, "Lecture"), node(HW1, "Assignment"), pdf("file:1", f"{ML}/x.pdf")]
    base = [
        edge(LEC3, "lecture_of_course", COURSE["id"]),
        edge(HW1, "assignment_of_course", COURSE["id"]),
    ]
    twice = [
        edge("file:1", "file_of_lecture", LEC3),
        {**edge("file:1", "file_of_lecture", LEC3), "src": "y"},
    ]
    assert codes(nodes, base + twice) == []
    both = [edge("file:1", "file_of_lecture", LEC3), edge("file:1", "file_of_assignment", HW1)]
    assert codes(nodes, base + both) == [("multiple_parents", "file:1")]


def test_the_folder_on_disk_must_agree_with_the_parent() -> None:
    hw2 = onto.item_id(onto.Assignment, ML, "hw-02")
    nodes = [COURSE, node(HW1, "Assignment"), node(hw2, "Assignment"), node(LEC3, "Lecture")]
    nodes += [pdf("file:1", f"{ML}/assignments/hw-01/a.pdf"), pdf("file:2", f"{ML}/lectures/b.pdf")]
    edges = [edge(i, "assignment_of_course", COURSE["id"]) for i in (HW1, hw2)]
    edges += [edge(LEC3, "lecture_of_course", COURSE["id"])]
    edges += [edge("file:1", "file_of_assignment", hw2), edge("file:2", "file_of_assignment", HW1)]
    assert codes(nodes, edges) == [("wrong_folder", "file:1"), ("wrong_folder", "file:2")]


def test_items_and_files_cannot_cross_courses() -> None:
    other = node("course:optimization", "Course", folder="Optimization")
    nodes = [COURSE, other, node(LEC3, "Lecture"), pdf("file:1", "Optimization/x.pdf")]
    edges = [
        edge(LEC3, "lecture_of_course", other["id"]),
        edge("file:1", "file_of_lecture", LEC3),
    ]
    assert codes(nodes, edges) == [("course_mismatch", "file:1"), ("course_mismatch", LEC3)]


def test_the_misc_edge_is_rationed() -> None:
    files = [pdf(f"file:{i}", f"{ML}/f{i}.pdf") for i in range(10)]
    why = "course-wide dataset used everywhere"
    misc = [edge(f["id"], "file_of_course", COURSE["id"], reason=why) for f in files]
    # 10 files: 15% rounds down to 1, the floor of 2 applies
    assert codes([COURSE, *files], misc[:2]) == [("unfiled", f"file:{i}") for i in range(2, 10)]
    assert ("misc_overuse", COURSE["id"]) in codes([COURSE, *files], misc[:3])


def test_duplicate_content_is_flagged() -> None:
    dup = node("file:1", "PdfFile", path=f"{ML}/lectures/a.pdf", duplicates=[f"{ML}/a.pdf"])
    lec = node(LEC3, "Lecture")
    edges = [edge(LEC3, "lecture_of_course", COURSE["id"]), edge("file:1", "file_of_lecture", LEC3)]
    assert codes([COURSE, lec, dup], edges) == [("duplicate_content", "file:1")]
