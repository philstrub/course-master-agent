"""
# Ontology

The knowledge graph's node types and edge types, as pydantic models, plus the
structural rules a finished graph must satisfy.

## 1. What This Module Does

Declares every node type as a pydantic model of its `attrs` and every edge type
as a model of its `attrs` together with the node types it may join. Validates
single records (`validate_node`, `validate_edge`) and whole graphs
(`structure_violations`), and describes itself as JSON (`describe`) for agents
that write records.

## 2. Why This Module Exists

The graph is the memory other agents query ("which files belong to HW2?",
"what did I submit?", "where is CART taught?"). Those answers are only as good
as the graph's shape, so the shape is enforced, not suggested: a record with a
misspelled attribute, a backwards edge or a mistyped id is refused before it is
stored, and a graph with an unfiled file or a lecture that belongs to no course
fails `mitsync graph check`, which is the stopping condition of the sync loop.

The docstrings on the models are written for the agent that writes records.
`describe()` ships them verbatim, so they are the instructions.

## 3. How It Fits in the Architecture

A leaf under `knowledge`: imports only pydantic and `core.errors`. `graph`
calls `validate_node` / `validate_edge` before anything is appended to the
JSONL truth or projected, and `structure_violations` for `graph check`.
`graph.build_backbone` builds ids with `item_id` and friends so deterministic
and agent-written records agree on naming.

## 4. Key Concepts

**Envelope vs attrs.** A stored node is `{id, type, label, attrs, src}` and an
edge `{s, p, o, attrs, src, conf}`; the envelope is checked by `graph`. The
models here describe `attrs` only, with `extra="forbid"`.

**Typed ids.** Every id is `<prefix>:<key>` in lowercase, hyphenated, and the
prefix must match the node type:

| node type | id |
|---|---|
| Course | `course:<course-slug>` (slug of the workspace folder) |
| Syllabus | `syllabus:<course-slug>` |
| Lecture | `lecture:<course-slug>:<NN>` (zero-padded) or `:<slug>` |
| Recitation | `recitation:<course-slug>:recitation-<NN>` |
| Assignment | `assignment:<course-slug>:<item>` (`hw-01`, `midterm`) |
| File, DataFile | `file:<16 hex>` (hash of the canonical path) |
| Repo | `repo:<slug>` |
| Concept | `concept:<slug>` (shared across courses) |

**Edges read left to right.** `file_of_lecture` goes file -> lecture,
`concept_in_assignment` goes concept -> assignment, `course_follows_syllabus`
goes course -> syllabus.

**Parents.** Every file has exactly one parent edge (`file_of_*`); every
lecture, recitation and assignment exactly one `*_of_course`; every syllabus
exactly one course; every repo exactly one of `repo_of_assignment` /
`repo_of_course`; every concept at least one `concept_in_*`. `CARDINALITY`
is that table.

**Miscellaneous edges are rationed.** `file_of_course` and `repo_of_course`
require a `reason` and may cover at most `MISC_SHARE` of a course's files
(never fewer than `MISC_FLOOR`). They are for material that truly belongs to
no lecture, recitation, assignment or syllabus.

**Disk agrees with the graph.** A file stored under a per-item folder
(`<Course>/assignments/hw-01/`) must hang off that item, and a file in
`lectures/`, `syllabus/`, `other/` or `notes/` off an item of that kind. Files
outside those buckets (loose pre-existing files, the `_canvas/` mirror) can be
attached anywhere in their own course: the graph can file what guardrail 1
forbids moving on disk.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mitsync.core.errors import OntologyError

MISC_SHARE = 0.15
MISC_FLOOR = 2

_SLUG_RX = re.compile(r"[^a-z0-9]+")
_KEY_RX = re.compile(r"^[a-z0-9][a-z0-9.-]*(:[a-z0-9][a-z0-9.-]*)*$")


def slug(text: str) -> str:
    return _SLUG_RX.sub("-", str(text).lower()).strip("-") or "unnamed"


# --------------------------------------------------------------------------
# node types
# --------------------------------------------------------------------------
class NodeAttrs(BaseModel):
    """Base of every node type's attributes."""

    model_config = ConfigDict(extra="forbid")
    id_prefix: ClassVar[str]


class Course(NodeAttrs):
    """A course the student takes: exactly one per folder in `config/courses.yml`.

    Written by the backbone; agents never create courses.
    """

    id_prefix: ClassVar[str] = "course"
    folder: str = Field(
        description="Workspace folder name, byte for byte (e.g. 'Machine Learning')"
    )
    course_number: str | None = Field(default=None, description="e.g. '15.095'")
    canvas_id: int | None = Field(default=None, description="Canvas course id")


class Syllabus(NodeAttrs):
    """A course's syllabus: schedule, grading and policies. One per course."""

    id_prefix: ClassVar[str] = "syllabus"
    title: str | None = None
    grading: str | None = Field(default=None, description="Grade weights, one line")


class Lecture(NodeAttrs):
    """One lecture (class session) of a course. Slides, notes and lecture data hang off it."""

    id_prefix: ClassVar[str] = "lecture"
    number: int | None = Field(
        default=None, description="Lecture number, when the course numbers them"
    )
    title: str | None = None
    held_on: date | None = None


class Recitation(NodeAttrs):
    """One recitation of a course, stored in `<Course>/recitations/recitation-NN/`."""

    id_prefix: ClassVar[str] = "recitation"
    number: int | None = None
    title: str | None = None
    held_on: date | None = None
    folder: str | None = Field(default=None, description="Workspace-relative per-item folder")


class Assignment(NodeAttrs):
    """Anything graded or submitted: homework, project, exam, pre-assignment.

    Stored in `<Course>/assignments/<item>/`. The submission fields are the
    memory of the student's own work: the backbone sets them from Canvas and
    from `gradescope sync` (Gradescope wins), and the agent marks the file
    that was handed in with `file_of_assignment {role: submission}`.
    """

    id_prefix: ClassVar[str] = "assignment"
    title: str | None = None
    kind: Literal["homework", "project", "exam", "quiz", "pre_assignment", "other"] | None = None
    folder: str | None = Field(default=None, description="Workspace-relative per-item folder")
    due_at: datetime | None = None
    points_possible: float | None = None
    canvas_id: int | None = Field(default=None, description="Canvas assignment id")
    submission_status: Literal["unsubmitted", "submitted", "late", "missing", "graded"] | None = (
        None
    )
    submitted_at: datetime | None = None
    score: float | None = None
    submitted_via: Literal["canvas", "gradescope", "other"] | None = None


class _File(NodeAttrs):
    id_prefix: ClassVar[str] = "file"
    path: str = Field(
        description="Canonical workspace-relative path (the filed copy when one exists)"
    )
    mirror_path: str | None = Field(
        default=None, description="Same content in the `_canvas/` mirror"
    )
    duplicates: list[str] = Field(
        default_factory=list, description="Other course-folder paths holding identical content"
    )
    sha256: str | None = None
    title: str | None = None
    content_type: str | None = Field(default=None, description="File extension without the dot")


# A file is a document a person reads; everything else is data (datasets,
# notebooks, code, archives, plain-text outputs). The path's extension decides.
DOCUMENT_SUFFIXES = frozenset(
    {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".md", ".markdown", ".tex", ".rtf"}
)


class File(_File):
    """A document a person reads: slides, handouts, problem sets, solutions, readings,
    notes, reports, submissions. PDF, Word, PowerPoint, markdown or LaTeX source."""

    pages: int | None = None

    @model_validator(mode="after")
    def _is_a_document(self) -> File:
        if Path(self.path).suffix.lower() not in DOCUMENT_SUFFIXES:
            raise ValueError(f"{self.path!r} is not a document; it is a DataFile")
        return self


class DataFile(_File):
    """Data or code, not prose: datasets (CSV, XLSX, Parquet), notebooks, scripts,
    archives, plain-text outputs."""

    @model_validator(mode="after")
    def _is_not_a_document(self) -> DataFile:
        if Path(self.path).suffix.lower() in DOCUMENT_SUFFIXES:
            raise ValueError(f"{self.path!r} is a document; it is a File")
        return self


class Repo(NodeAttrs):
    """A code repository the student works in (e.g. `AI_Studio/nandatown`).

    Metadata only: nothing walks inside a repo (guardrail 5).
    """

    id_prefix: ClassVar[str] = "repo"
    name: str
    path: str | None = Field(default=None, description="Workspace-relative checkout path")
    remote_url: str | None = None
    description: str | None = None


class Concept(NodeAttrs):
    """An idea taught or assessed: 'logistic regression', 'simplex method', 'CART'.

    Shared across courses: reuse an existing concept id rather than minting a
    variant (`graph query --canned concepts` lists them).
    """

    id_prefix: ClassVar[str] = "concept"
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, description="One sentence")


NODE_TYPES: dict[str, type[NodeAttrs]] = {
    cls.__name__: cls
    for cls in (Course, Syllabus, Lecture, Recitation, Assignment, File, DataFile, Repo, Concept)
}
FILE_TYPES = (File, DataFile)
ITEM_TYPES = (Lecture, Recitation, Assignment)


# --------------------------------------------------------------------------
# edge types
# --------------------------------------------------------------------------
FileRole = Literal[
    "slides", "notes", "handout", "solution", "starter", "data", "submission", "reference"
]
Depth = Literal["mentioned", "taught", "applied"]


class EdgeAttrs(BaseModel):
    """Base of every edge type's attributes and endpoint rules."""

    model_config = ConfigDict(extra="forbid")
    name: ClassVar[str]
    source: ClassVar[tuple[type[NodeAttrs], ...]]
    target: ClassVar[tuple[type[NodeAttrs], ...]]


class CourseFollowsSyllabus(EdgeAttrs):
    """The course is run according to this syllabus."""

    name: ClassVar[str] = "course_follows_syllabus"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Course,)
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Syllabus,)


class LectureOfCourse(EdgeAttrs):
    """The lecture is part of the course."""

    name: ClassVar[str] = "lecture_of_course"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Lecture,)
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Course,)


class AssignmentOfCourse(EdgeAttrs):
    """The assignment is set by the course."""

    name: ClassVar[str] = "assignment_of_course"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Assignment,)
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Course,)


class RecitationOfCourse(EdgeAttrs):
    """The recitation is part of the course."""

    name: ClassVar[str] = "recitation_of_course"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Recitation,)
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Course,)


class _FileOfItem(EdgeAttrs):
    source: ClassVar[tuple[type[NodeAttrs], ...]] = FILE_TYPES
    role: FileRole | None = Field(default=None, description="What the file is for this item")


class FileOfLecture(_FileOfItem):
    """The file (slides, notes, lecture data) belongs to this lecture."""

    name: ClassVar[str] = "file_of_lecture"
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Lecture,)


class FileOfAssignment(_FileOfItem):
    """The file belongs to this assignment: its handout, data, starter code, solution,
    or, with `role: submission`, what the student turned in."""

    name: ClassVar[str] = "file_of_assignment"
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Assignment,)


class FileOfRecitation(_FileOfItem):
    """The file (slides, notebook, data, solution) belongs to this recitation."""

    name: ClassVar[str] = "file_of_recitation"
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Recitation,)


class FileOfSyllabus(EdgeAttrs):
    """The file is (part of) the syllabus: schedule, grading policy, logistics."""

    name: ClassVar[str] = "file_of_syllabus"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = FILE_TYPES
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Syllabus,)


class FileOfCourse(EdgeAttrs):
    """LAST RESORT. The file belongs to the course but to no lecture, recitation,
    assignment or syllabus (a course-wide dataset, a textbook). Say why in `reason`.
    Rationed: see `MISC_SHARE`."""

    name: ClassVar[str] = "file_of_course"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = FILE_TYPES
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Course,)
    reason: str = Field(min_length=10, description="Why no lecture/recitation/assignment owns it")


class _ConceptIn(EdgeAttrs):
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Concept,)
    depth: Depth | None = Field(
        default=None, description="mentioned in passing, taught, or applied (exercised)"
    )
    evidence: str | None = Field(default=None, description="Page, slide or a short quote")


class ConceptInLecture(_ConceptIn):
    """The concept appears in this lecture."""

    name: ClassVar[str] = "concept_in_lecture"
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Lecture,)


class ConceptInAssignment(_ConceptIn):
    """The assignment exercises or assesses the concept."""

    name: ClassVar[str] = "concept_in_assignment"
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Assignment,)


class ConceptInRecitation(_ConceptIn):
    """The concept appears in this recitation."""

    name: ClassVar[str] = "concept_in_recitation"
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Recitation,)


class RepoOfAssignment(EdgeAttrs):
    """The repository is where the student does this assignment."""

    name: ClassVar[str] = "repo_of_assignment"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Repo,)
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Assignment,)


class RepoOfCourse(EdgeAttrs):
    """LAST RESORT. The repository belongs to the course but to no single assignment.
    Say why in `reason`."""

    name: ClassVar[str] = "repo_of_course"
    source: ClassVar[tuple[type[NodeAttrs], ...]] = (Repo,)
    target: ClassVar[tuple[type[NodeAttrs], ...]] = (Course,)
    reason: str = Field(min_length=10, description="Why no assignment owns it")


EDGE_TYPES: dict[str, type[EdgeAttrs]] = {
    cls.name: cls
    for cls in (
        CourseFollowsSyllabus,
        LectureOfCourse,
        AssignmentOfCourse,
        RecitationOfCourse,
        FileOfLecture,
        FileOfAssignment,
        FileOfRecitation,
        FileOfSyllabus,
        FileOfCourse,
        ConceptInLecture,
        ConceptInAssignment,
        ConceptInRecitation,
        RepoOfAssignment,
        RepoOfCourse,
    )
}
FILE_PARENT_EDGES = frozenset(
    {
        "file_of_lecture",
        "file_of_assignment",
        "file_of_recitation",
        "file_of_syllabus",
        "file_of_course",
    }
)

# node type -> (edge types counted, direction, min, max); max None = unbounded
CARDINALITY: dict[str, tuple[frozenset[str], Literal["out", "in"], int, int | None]] = {
    "File": (FILE_PARENT_EDGES, "out", 1, 1),
    "DataFile": (FILE_PARENT_EDGES, "out", 1, 1),
    "Lecture": (frozenset({"lecture_of_course"}), "out", 1, 1),
    "Recitation": (frozenset({"recitation_of_course"}), "out", 1, 1),
    "Assignment": (frozenset({"assignment_of_course"}), "out", 1, 1),
    "Syllabus": (frozenset({"course_follows_syllabus"}), "in", 1, 1),
    "Repo": (frozenset({"repo_of_assignment", "repo_of_course"}), "out", 1, 1),
    "Concept": (
        frozenset({"concept_in_lecture", "concept_in_assignment", "concept_in_recitation"}),
        "out",
        1,
        None,
    ),
}

# filing bucket on disk -> the edge a file stored there must use
BUCKET_EDGES = {
    "lectures": "file_of_lecture",
    "recitations": "file_of_recitation",
    "assignments": "file_of_assignment",
    "syllabus": "file_of_syllabus",
    "other": "file_of_course",
    "notes": "file_of_course",
}


# --------------------------------------------------------------------------
# ids
# --------------------------------------------------------------------------
def course_id(folder: str) -> str:
    return f"course:{slug(folder)}"


def syllabus_id(folder: str) -> str:
    return f"syllabus:{slug(folder)}"


def item_id(kind: type[NodeAttrs], folder: str, key: str) -> str:
    """Id of a lecture, recitation or assignment: `<prefix>:<course-slug>:<key>`."""
    return f"{kind.id_prefix}:{slug(folder)}:{slug(key)}"


def course_slug_of(node_id: str) -> str:
    """The course segment of a course, syllabus or item id."""
    return node_id.split(":")[1]


# --------------------------------------------------------------------------
# record validation
# --------------------------------------------------------------------------
def _attr_errors(model: type[BaseModel], attrs: dict[str, Any], what: str) -> str | None:
    try:
        model.model_validate(attrs)
    except ValidationError as exc:
        extra = sorted(str(e["loc"][0]) for e in exc.errors() if e["type"] == "extra_forbidden")
        if extra:
            return f"{what} has no attribute(s) {extra} (allowed: {sorted(model.model_fields)})"
        bad = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'attrs'}: {e['msg']}" for e in exc.errors()
        )
        return f"{what} attrs invalid ({bad})"
    return None


def validate_node(node: dict[str, Any]) -> None:
    """Check a normalized node's type, id and attrs; raise `OntologyError` naming it."""
    nid = node.get("id")
    if not isinstance(nid, str) or not nid.strip():
        raise OntologyError(f"node has no id: {_show(node)}")
    ntype = node.get("type")
    model = NODE_TYPES.get(ntype) if isinstance(ntype, str) else None
    if model is None:
        raise OntologyError(
            f"unknown node type {ntype!r} (allowed: {', '.join(sorted(NODE_TYPES))}) "
            f"in {_show(node)}"
        )
    prefix, _, key = nid.partition(":")
    if prefix != model.id_prefix or not _KEY_RX.match(key):
        raise OntologyError(
            f"{ntype} id must look like '{model.id_prefix}:<lowercase-hyphenated-key>', "
            f"not {nid!r} in {_show(node)}"
        )
    attrs = node.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise OntologyError(f"node attrs must be an object in {_show(node)}")
    problem = _attr_errors(model, attrs, f"node type {ntype}")
    if problem:
        raise OntologyError(f"{problem} in {_show(node)}")


def validate_edge(edge: dict[str, Any], node_types: dict[str, str]) -> None:
    """Check an edge's type, endpoints and attrs; `node_types` maps id -> node type."""
    pred = edge.get("p")
    spec = EDGE_TYPES.get(pred) if isinstance(pred, str) else None
    if spec is None:
        raise OntologyError(
            f"unknown edge type {pred!r} (allowed: {', '.join(sorted(EDGE_TYPES))}) "
            f"in {_show(edge)}"
        )
    for role in ("s", "o"):
        nid = edge.get(role)
        if not isinstance(nid, str) or not nid.strip():
            raise OntologyError(f"edge {role} is missing in {_show(edge)}")
        if nid not in node_types:
            raise OntologyError(f"edge {role} {nid!r} is not a known node in {_show(edge)}")
    problem = _attr_errors(spec, edge.get("attrs") or {}, f"edge type {pred}")
    if problem:
        raise OntologyError(f"{problem} in {_show(edge)}")
    s_type, o_type = node_types[edge["s"]], node_types[edge["o"]]
    sources = [t.__name__ for t in spec.source]
    targets = [t.__name__ for t in spec.target]
    if s_type not in sources:
        raise OntologyError(
            f"edge '{pred}' cannot start at a {s_type} (allowed sources: {sources}) "
            f"in {_show(edge)}"
        )
    if o_type not in targets:
        raise OntologyError(
            f"edge '{pred}' cannot point at a {o_type} (allowed targets: {targets}) "
            f"in {_show(edge)}"
        )


def _show(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, default=str)[:400]


# --------------------------------------------------------------------------
# whole-graph structure
# --------------------------------------------------------------------------
class Violation(BaseModel):
    """One structural problem `graph check` reports. `code` is stable for scripts.

    `severity` tells the loop what to do: `error` the agent fixes with `graph
    add`; `human` only the student can fix (a duplicate pre-existing file may
    not be deleted unasked), so the loop escalates; `info` is reported and
    never blocks the stop condition.
    """

    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "unfiled",
        "multiple_parents",
        "no_course",
        "orphan_concept",
        "misc_overuse",
        "wrong_folder",
        "course_mismatch",
        "duplicate_content",
        "stale_edge",
    ]
    node: str
    message: str
    severity: Literal["error", "human", "info"] = "error"


def _missing_code(ntype: str) -> str:
    if ntype in ("File", "DataFile"):
        return "unfiled"
    if ntype == "Concept":
        return "orphan_concept"
    return "no_course"


def structure_violations(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]
) -> list[Violation]:
    """Every rule in `CARDINALITY`, `BUCKET_EDGES` and the misc ration, over a projected graph.

    `nodes` and `edges` are the projections `graph.load_nodes` / `load_edges`
    return; both already passed `validate_node` / `validate_edge`.
    """
    out: list[Violation] = []
    # distinct (s, p, o): the same fact from two source documents is one parent
    triples = sorted({(e["s"], e["p"], e["o"]) for e in edges})
    by_node: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for s, p, o in triples:
        by_node[(s, "out")].append((p, o))
        by_node[(o, "in")].append((p, s))

    for nid, node in nodes.items():
        rule = CARDINALITY.get(node["type"])
        if rule is None:
            continue  # Course has no parent: it is the root
        kinds, direction, low, high = rule
        found = [(p, other) for p, other in by_node[(nid, direction)] if p in kinds]
        if len(found) < low:
            out.append(
                Violation(
                    code=_missing_code(node["type"]),
                    node=nid,
                    message=f"{node['type']} {node['label']!r} has no {' / '.join(sorted(kinds))}",
                )
            )
        if high is not None and len(found) > high:
            out.append(
                Violation(
                    code="multiple_parents",
                    node=nid,
                    message=f"{node['type']} {node['label']!r} has {len(found)} parents: "
                    + ", ".join(f"{p} -> {o}" for p, o in found),
                )
            )

    files_per_course: dict[str, int] = defaultdict(int)
    misc_per_course: dict[str, list[str]] = defaultdict(list)
    for nid, node in nodes.items():
        if node["type"] not in ("File", "DataFile"):
            continue
        attrs = node["attrs"]
        if attrs.get("duplicates"):
            out.append(
                Violation(
                    code="duplicate_content",
                    severity="human",
                    node=nid,
                    message=f"{attrs['path']} has identical copies at {attrs['duplicates']}",
                )
            )
        parts = attrs["path"].split("/")
        on_disk = parts[0] != "_canvas"
        if on_disk:
            files_per_course[slug(parts[0])] += 1
        for p, target in by_node[(nid, "out")]:
            if p not in FILE_PARENT_EDGES:
                continue
            if p == "file_of_course":
                misc_per_course[course_slug_of(target)].append(nid)
            if on_disk and course_slug_of(target) != slug(parts[0]):
                out.append(
                    Violation(
                        code="course_mismatch",
                        node=nid,
                        message=f"{attrs['path']} is in {parts[0]!r} but {p} -> {target}",
                    )
                )
            bucket = parts[1] if on_disk and len(parts) > 2 else None
            want = BUCKET_EDGES.get(bucket or "")
            if want is None:
                continue  # loose or mirror-only file: the folder says nothing
            per_item = bucket in ("recitations", "assignments") and len(parts) > 3
            expected = (
                item_id(Recitation if bucket == "recitations" else Assignment, parts[0], parts[2])
                if per_item
                else None
            )
            if p != want or (expected is not None and target != expected):
                out.append(
                    Violation(
                        code="wrong_folder",
                        node=nid,
                        message=f"{attrs['path']} is stored under {bucket}/ but {p} -> {target}"
                        + (
                            f" (expected {want} -> {expected})"
                            if expected
                            else f" (expected {want})"
                        ),
                    )
                )

    for s, p, o in triples:
        if p in ("lecture_of_course", "assignment_of_course", "recitation_of_course"):
            if course_slug_of(s) != course_slug_of(o):
                out.append(
                    Violation(
                        code="course_mismatch",
                        node=s,
                        message=f"{s} is named for another course than {o}",
                    )
                )
        if p == "course_follows_syllabus" and course_slug_of(s) != course_slug_of(o):
            out.append(Violation(code="course_mismatch", node=o, message=f"{s} follows {o}"))

    for course, misc in sorted(misc_per_course.items()):
        allowed = max(MISC_FLOOR, math.floor(MISC_SHARE * files_per_course[course]))
        if len(misc) > allowed:
            out.append(
                Violation(
                    code="misc_overuse",
                    node=f"course:{course}",
                    message=f"{len(misc)} files use file_of_course (allowed {allowed} of "
                    f"{files_per_course[course]}): attach them to a lecture, recitation, "
                    "assignment or syllabus",
                )
            )
    return sorted(out, key=lambda v: (v.code, v.node))


# --------------------------------------------------------------------------
# self-description for agents
# --------------------------------------------------------------------------
def describe() -> dict[str, Any]:
    """The ontology as JSON: what `mitsync graph schema --json` prints."""
    return {
        "node_types": {
            name: {
                "doc": (model.__doc__ or "").strip(),
                "id": f"{model.id_prefix}:<key>",
                "attrs": model.model_json_schema()["properties"],
                "required": model.model_json_schema().get("required", []),
            }
            for name, model in NODE_TYPES.items()
        },
        "edge_types": {
            name: {
                "doc": (spec.__doc__ or "").strip(),
                "source": [t.__name__ for t in spec.source],
                "target": [t.__name__ for t in spec.target],
                "attrs": spec.model_json_schema()["properties"],
                "required": spec.model_json_schema().get("required", []),
            }
            for name, spec in EDGE_TYPES.items()
        },
        "cardinality": {
            ntype: {"edges": sorted(kinds), "direction": d, "min": low, "max": high}
            for ntype, (kinds, d, low, high) in CARDINALITY.items()
        },
        "bucket_edges": BUCKET_EDGES,
        "misc_share": MISC_SHARE,
        "misc_floor": MISC_FLOOR,
    }
