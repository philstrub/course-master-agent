"""The tool must never index its own repo, or anything symlinked into it.

`_agent/` holds the CLI's own source and docs; `<workspace>/skills` is a symlink
to `_agent/skills` created by the OpenClaw setup. Both are machinery, not course
content, and a walk that follows either one pollutes the knowledge base.
"""

from mitsync.knowledge import extract


def test_symlink_into_agent_repo_is_not_a_course_root(settings):
    ws = settings.paths.workspace
    (ws / "_agent" / "skills" / "mit-kb").mkdir(parents=True, exist_ok=True)
    (ws / "_agent" / "skills" / "mit-kb" / "SKILL.md").write_text("# skill")
    (ws / "Machine Learning").mkdir(exist_ok=True)
    (ws / "Machine Learning" / "Lec1.md").write_text("# lecture")
    (ws / "skills").symlink_to(ws / "_agent" / "skills")

    roots = [p.name for p in extract.source_roots(settings)]
    assert "skills" not in roots
    assert "Machine Learning" in roots

    sources = [p.name for p in extract.iter_sources(settings)]
    assert "SKILL.md" not in sources
    assert "Lec1.md" in sources


def test_machinery_paths_are_not_attributed_to_a_course():
    assert extract.course_of("_agent/skills/mit-kb/SKILL.md") == ""
    assert extract.course_of("_kb/INDEX.md") == ""
    assert extract.course_of("Machine Learning/Lec1.pdf") == "Machine Learning"
    assert extract.course_of("_canvas/Optimization/HW1.pdf") == "Optimization"
