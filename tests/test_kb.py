"""The handoff layer: indexes, notes, manifest, and the AGENTS.md bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mitsync.core.config import Settings
from mitsync.core.errors import JudgeUnavailable, MitsyncError
from mitsync.knowledge import extract as extract_mod
from mitsync.knowledge import graph as graph_mod
from mitsync.knowledge import kb as kb_mod
from mitsync.llm.base import JudgeTask, validate_result
from tests.test_extract import make_notebook, make_pdf
from tests.test_graph import EmptyJudge, StubJudge, concept_judge

NOTES_RESULT: dict[str, Any] = {
    "summary": "Applied analytics, taught through cases.",
    "topics": [
        {
            "name": "Regularization",
            "summary": "Ridge and lasso as bias-variance control.",
            "sources": ["Machine Learning/lectures/lec01.pdf"],
        }
    ],
    "open_questions": ["Which dataset does HW1 use?"],
}


@pytest.fixture
def built(workspace: Path, settings: Settings) -> Settings:
    ml = workspace / "Machine Learning"
    make_pdf(ml / "lectures" / "lec01.pdf", ("regularization and ridge regression",))
    make_notebook(ml / "assignments" / "hw1.ipynb")
    (ml / "data").mkdir(parents=True, exist_ok=True)
    (ml / "data" / "train.csv").write_text("a,b\n1,2\n")

    (workspace / "_canvas" / "Analytics Edge").mkdir(parents=True)
    make_pdf(workspace / "_canvas" / "Analytics Edge" / "syllabus.pdf", ("syllabus",))

    junk = workspace / "AI_Studio" / "nandatown" / ".venv" / "lib" / "site-packages"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "x.py").write_text("print(1)")
    (junk / "guide.md").write_text("# venv guide")
    (workspace / "AI_Studio" / "nandatown" / "README.md").write_text("# nandatown")

    extract_mod.extract_all(settings)
    graph_mod.extract_graph(settings, StubJudge(concept_judge))
    return settings


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------
def test_build_writes_every_page(built: Settings) -> None:
    report = kb_mod.build(built)
    kb = built.paths.kb
    assert (kb / "INDEX.md").exists()
    assert (kb / "AGENTS.md").exists()
    assert (kb / "manifest.json").exists()
    for course in ("Machine Learning", "Analytics Edge"):
        assert (kb / "courses" / course / "INDEX.md").exists()
        assert (kb / "courses" / course / "NOTES.md").exists()
    assert set(report.courses) == {"Machine Learning", "Analytics Edge"}
    assert report.files == 4
    assert report.extracted == 4


def test_course_index_groups_by_bucket_and_links_both_views(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "courses" / "Machine Learning" / "INDEX.md").read_text()
    assert "## lectures" in body
    assert "## assignments" in body
    assert "## data" in body
    assert "`Machine Learning/lectures/lec01.pdf`" in body  # the canonical file
    text_rel = extract_mod.text_path_for(built, "Machine Learning/lectures/lec01.pdf").name
    assert text_rel in body  # and its extracted text
    assert "NOTES.md" in body

    canvas = (built.paths.kb / "courses" / "Analytics Edge" / "INDEX.md").read_text()
    assert "## canvas mirror (unfiled)" in canvas


def test_global_index_lists_courses_and_links_agents_md(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "INDEX.md").read_text()
    assert "AGENTS.md" in body
    assert "| Machine Learning | 3 |" in body
    assert "courses/Analytics Edge/INDEX.md" in body
    assert "_kb/due.json" in body or "due.json" in body


def test_manifest_is_machine_readable(built: Settings) -> None:
    kb_mod.build(built)
    manifest = json.loads((built.paths.kb / "manifest.json").read_text())
    ml = manifest["courses"]["Machine Learning"]
    lec = next(f for f in ml["files"] if f["path"].endswith("lec01.pdf"))
    assert lec["bucket"] == "lectures"
    assert lec["content_type"] == "pdf"
    assert lec["text"].startswith("_kb/text/")
    assert graph_mod.resource_id("Machine Learning/lectures/lec01.pdf") in lec["node_ids"]


def test_build_is_idempotent(built: Settings) -> None:
    kb_mod.build(built)
    pages = sorted(built.paths.kb.rglob("*.md")) + [built.paths.kb / "manifest.json"]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in pages}
    kb_mod.build(built)
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in pages}
    assert after == before, "kb build must be byte-identical and must not rewrite files"


# --------------------------------------------------------------------------
# exclusion
# --------------------------------------------------------------------------
def test_nandatown_never_reaches_the_kb(built: Settings) -> None:
    kb_mod.build(built)
    data = json.loads((built.paths.kb / "manifest.json").read_text())
    manifest = json.dumps(data["courses"])
    assert "nandatown" not in manifest
    assert "site-packages" not in manifest
    ws = str(built.paths.workspace)  # the tmp dir is named after this test
    for page in built.paths.kb.rglob("*.md"):
        if page.name == "AGENTS.md":
            continue  # AGENTS.md names it deliberately, as a guardrail
        assert "nandatown" not in page.read_text().replace(ws, "<ws>"), page
    assert "AI_Studio" not in data["courses"]


# --------------------------------------------------------------------------
# notes / judgment
# --------------------------------------------------------------------------
def test_notes_without_a_judge_are_a_deterministic_skeleton(built: Settings) -> None:
    report = kb_mod.build(built)
    body = (built.paths.kb / "courses" / "Machine Learning" / "NOTES.md").read_text()
    assert kb_mod.NOTES_PENDING_MARKER in body
    assert "Machine Learning/lectures/lec01.pdf" in body  # inventory is still useful
    assert "Machine Learning" in report.notes_pending


def test_notes_with_a_judge_use_the_validated_result(built: Settings) -> None:
    judge = StubJudge(NOTES_RESULT)
    report = kb_mod.build(built, judge)
    body = (built.paths.kb / "courses" / "Machine Learning" / "NOTES.md").read_text()
    assert kb_mod.NOTES_PENDING_MARKER not in body
    assert "Applied analytics, taught through cases." in body
    assert "### Regularization" in body
    assert "Which dataset does HW1 use?" in body
    assert report.notes_pending == []

    task = judge.tasks[0]
    assert task.name == "course_notes"
    assert task.payload["course"] in {"Machine Learning", "Analytics Edge"}
    assert task.payload["documents"]
    assert task.payload["documents"][0]["excerpt"]
    assert "untrusted" in (task.instructions + task.rules).lower()
    validate_result(task, NOTES_RESULT)  # the stub answer satisfies the shipped schema


def test_notes_fall_back_when_the_judge_returns_nothing(built: Settings) -> None:
    """The rules driver has no `course_notes` handler; it returns {}."""
    judge = EmptyJudge()
    report = kb_mod.build(built, judge)
    body = (built.paths.kb / "courses" / "Machine Learning" / "NOTES.md").read_text()
    assert kb_mod.NOTES_PENDING_MARKER in body
    assert judge.calls == 2
    assert "empty result" in report.judge_note
    assert sorted(report.notes_pending) == ["Analytics Edge", "Machine Learning"]


def test_notes_fall_back_when_the_judge_is_unavailable(built: Settings) -> None:
    class Broken:
        def judge(self, task: JudgeTask) -> dict[str, Any]:  # noqa: ARG002 -- interface
            raise JudgeUnavailable("no api key configured")

    report = kb_mod.build(built, Broken())
    body = (built.paths.kb / "courses" / "Machine Learning" / "NOTES.md").read_text()
    assert kb_mod.NOTES_PENDING_MARKER in body
    assert "no api key configured" in report.judge_note
    assert "no api key configured" in body


def test_notes_fall_back_when_the_judged_result_is_invalid(built: Settings) -> None:
    bad = {"summary": "x", "topics": [{"name": "n"}], "open_questions": []}
    report = kb_mod.build(built, StubJudge(bad))
    body = (built.paths.kb / "courses" / "Machine Learning" / "NOTES.md").read_text()
    assert kb_mod.NOTES_PENDING_MARKER in body
    assert report.notes_pending


# --------------------------------------------------------------------------
# AGENTS.md -- the acceptance test
# --------------------------------------------------------------------------
def test_agents_md_has_every_required_section(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    for heading in (
        "## 1. Folder contract",
        "## 2. Where truth lives",
        "## 3. How to query the graph",
        "## 4. Where deadlines live",
        "## 5. Per-course entry points",
        "## 6. Guardrails",
    ):
        assert heading in body, heading


def test_agents_md_explains_where_truth_lives(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "_canvas/" in body and "verbatim" in body
    assert "curated view" in body
    assert "_kb/text/" in body
    assert "source of truth" in body


def test_agents_md_gives_runnable_graph_commands_and_canned_names(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "mitsync graph rebuild" in body
    assert "mitsync graph query --canned" in body
    for name in graph_mod.CANNED:
        assert f"`{name}`" in body, name


def test_agents_md_handles_a_missing_due_json_and_then_a_present_one(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "_kb/due.json" in body
    assert "does **not exist yet**" in body
    assert "mitsync due" in body

    (built.paths.kb / "due.json").write_text(
        json.dumps({"items": [{"course": "Machine Learning", "title": "HW1"}]})
    )
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "holds 1 item(s)" in body
    assert "does **not exist yet**" not in body


def test_agents_md_lists_per_course_entry_points_and_the_weekly_recipe(
    built: Settings,
) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "`_kb/courses/Machine Learning/INDEX.md`" in body
    assert "`_kb/courses/Analytics Edge/NOTES.md`" in body
    assert "this week" in body  # the headline question is answered step by step
    assert "assignments_due" in body


def test_agents_md_states_the_data_not_instructions_guardrail(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "data, never instructions" in body
    assert "ignore previous instructions" in body
    assert "nandatown" in body
    assert "Never write to Apple Calendar" in body


def test_kb_build_survives_an_empty_workspace(settings: Settings) -> None:
    report = kb_mod.build(settings)
    assert report.courses == []
    body = (settings.paths.kb / "AGENTS.md").read_text()
    assert "No course materials have been indexed yet" in body
    assert json.loads((settings.paths.kb / "manifest.json").read_text())["courses"] == {}


def test_buckets_handle_folders_that_predate_organize() -> None:
    """The student's own names ("Assignment 1/") must still group sensibly."""
    cases = {
        "Analytics Edge/Assignment 1/Deliverable_1.ipynb": "assignments",
        "Analytics Edge/Assignment 1/out/p1a.txt": "assignments",
        "Analytics Edge/Assignment 1/insurance.csv": "assignments",
        "Machine Learning/Lecture 3/slides.pdf": "lectures",
        "Machine Learning/assignments/hw1.pdf": "assignments",
        "Optimization/syllabus.pdf": "syllabus",
        "Optimization/data/prices.csv": "data",
        "Optimization/prices.csv": "data",
        "Analytics Lab/Recitation 2/walkthrough.pdf": "recitations",
        "AI_Studio/random.pdf": "other",
        "_canvas/Analytics Edge/whatever.pdf": "canvas mirror (unfiled)",
    }
    for rel, expected in cases.items():
        assert kb_mod._bucket(rel) == expected, rel


def test_a_corrupt_due_json_raises_instead_of_rendering_zero_items(built: Settings) -> None:
    """A truncated due.json used to render as "0 item(s)" in INDEX.md."""
    due = built.paths.kb / "due.json"
    due.parent.mkdir(parents=True, exist_ok=True)
    due.write_text('{"items": [')
    with pytest.raises(MitsyncError, match="not valid JSON"):
        kb_mod.build(built)
