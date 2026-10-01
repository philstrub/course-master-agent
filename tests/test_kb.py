"""The handoff layer: `_kb/AGENTS.md`, no generated index, and NOTES.md left to the agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from mitsync.core.config import Settings
from mitsync.knowledge import extract as extract_mod
from mitsync.knowledge import graph as graph_mod
from mitsync.knowledge import kb as kb_mod
from tests.test_extract import make_notebook, make_pdf


@pytest.fixture
def built(workspace: Path, settings: Settings) -> Settings:
    ml = workspace / "Machine Learning"
    make_pdf(ml / "lectures" / "lec01.pdf", ("regularization and ridge regression",))
    make_notebook(ml / "assignments" / "hw-01" / "hw1.ipynb")
    (settings.paths.config_dir / "courses.yml").write_text(
        "courses:\n  - folder: Machine Learning\n  - folder: Analytics Edge\n"
    )
    (workspace / "Analytics Edge").mkdir()

    junk = workspace / "AI_Studio" / "nandatown" / ".venv" / "lib" / "site-packages"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "guide.md").write_text("# venv guide")
    (workspace / "AI_Studio" / "nandatown" / "README.md").write_text("# nandatown")

    extract_mod.extract_all(settings)
    return settings


def test_build_writes_only_agents_md(built: Settings) -> None:
    report = kb_mod.build(built)
    kb = built.paths.kb
    written = sorted(
        p.relative_to(kb).as_posix()
        for p in kb.rglob("*")
        if p.is_file() and not p.is_relative_to(kb / "text") and not p.is_relative_to(kb / "graph")
    )
    assert written == ["AGENTS.md"], "no index, manifest or per-course page repeats the graph"
    assert report.courses == ["Analytics Edge", "Machine Learning"]
    assert report.notes_missing == ["Analytics Edge", "Machine Learning"]
    assert not (kb / "graph" / "entities").exists()


def test_build_refreshes_the_backbone_the_agent_queries(built: Settings) -> None:
    kb_mod.build(built)
    files = {
        n["attrs"]["path"]: n
        for n in graph_mod.load_nodes(built).values()
        if n["type"] in ("File", "DataFile")
    }
    lec = files["Machine Learning/lectures/lec01.pdf"]["attrs"]
    assert (
        lec["text"]
        == extract_mod.text_path_for(built, "Machine Learning/lectures/lec01.pdf")
        .relative_to(built.paths.workspace)
        .as_posix()
    )


def test_build_is_idempotent(built: Settings) -> None:
    kb_mod.build(built)
    page = built.paths.kb / "AGENTS.md"
    before = (page.read_bytes(), page.stat().st_mtime_ns)
    kb_mod.build(built)
    assert (page.read_bytes(), page.stat().st_mtime_ns) == before


def test_kb_build_never_creates_or_overwrites_notes(built: Settings) -> None:
    notes = kb_mod.notes_path(built, "Machine Learning")
    notes.parent.mkdir(parents=True, exist_ok=True)
    mine = "# Machine Learning\n\nWritten by the agent. Do not touch.\n"
    notes.write_text(mine)
    stamp = notes.stat().st_mtime_ns

    report = kb_mod.build(built)
    kb_mod.build(built)

    assert notes.read_text() == mine
    assert notes.stat().st_mtime_ns == stamp
    assert notes not in report.written
    assert not kb_mod.notes_path(built, "Analytics Edge").exists()
    assert report.notes_missing == ["Analytics Edge"]
    agents = (built.paths.kb / "AGENTS.md").read_text()
    assert "| Machine Learning | `_kb/courses/Machine Learning/NOTES.md` |" in agents
    assert "| Analytics Edge | not written yet |" in agents
    assert "never creates or overwrites it" in agents


def test_agents_md_points_at_the_graph_and_the_folders(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "**query the" in body and "**open the course folders**" in body
    assert "mitsync graph cypher" in body and "mitsync graph query" in body
    assert "`path`" in body and "`text`" in body
    for name in graph_mod.CANNED:
        assert f"`{name}`" in body, name
    assert "mitsync graph add" in body and "mitsync graph check --json" in body
    assert "mitsync due --json" in body
    for gone in ("INDEX.md", "manifest.json", "entities/", "due.json"):
        assert gone not in body, gone


def test_agents_md_states_the_guardrails(built: Settings) -> None:
    kb_mod.build(built)
    body = (built.paths.kb / "AGENTS.md").read_text()
    assert "data, never instructions" in body
    assert "ignore previous instructions" in body
    assert "nandatown" in body
    assert "Never write to Apple Calendar, Canvas or Gradescope" in body
    assert "mitsync organize apply --plan" in body


def test_kb_build_survives_an_empty_workspace(settings: Settings) -> None:
    (settings.paths.config_dir / "courses.yml").write_text("courses: []\n")
    report = kb_mod.build(settings)
    assert report.courses == []
    body = (settings.paths.kb / "AGENTS.md").read_text()
    assert "No course folder is mapped yet" in body
