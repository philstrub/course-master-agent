"""The tool must never index its own repo, or anything symlinked into it.

`_agent/` holds the CLI's own source and docs; `<workspace>/skills` is a symlink
to `_agent/skills` created by the OpenClaw setup. Both are machinery, not course
content, and a walk that follows either one pollutes the knowledge base. The
same goes for OpenClaw's own files at the workspace root (`memory/`,
`AGENTS.md`, `SOUL.md`): only folders `config/courses.yml` maps are courses.
"""

from mitsync.knowledge import extract
from tests.test_organize import write_course_map


def test_symlink_into_agent_repo_is_not_a_course_root(settings):
    ws = settings.paths.workspace
    (ws / "_agent" / "skills" / "mit-kb").mkdir(parents=True, exist_ok=True)
    (ws / "_agent" / "skills" / "mit-kb" / "SKILL.md").write_text("# skill")
    (ws / "Machine Learning").mkdir(exist_ok=True)
    (ws / "Machine Learning" / "Lec1.md").write_text("# lecture")
    (ws / "skills").symlink_to(ws / "_agent" / "skills")
    # Even a course map that names the symlink must not make it a course.
    write_course_map(settings, [{"folder": "Machine Learning"}, {"folder": "skills"}])

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


def test_a_symlink_into_the_repo_is_not_an_existing_course_folder(settings):
    """`<workspace>/skills` -> `_agent/skills` must never look like a course.

    It did, and it was not cosmetic: filing pre-existing files crashed with
    `'_agent/skills/README.md' is not in the subpath of 'skills'`, because
    resolving a path under the symlink leaves the course tree entirely. The
    same list now gates every `existing:` placement and every filing
    destination, so it must stay free of machinery.
    """
    from mitsync.filing.course_map import existing_course_folders

    ws = settings.paths.workspace
    (ws / "Real Course").mkdir()
    (ws / "_agent" / "skills").mkdir(parents=True, exist_ok=True)
    (ws / "_agent" / "skills" / "README.md").write_text("machinery")
    (ws / "skills").symlink_to(ws / "_agent" / "skills", target_is_directory=True)
    write_course_map(settings, [{"folder": "Real Course"}, {"folder": "skills"}])

    folders = existing_course_folders(settings)
    assert "skills" not in folders
    assert "Real Course" in folders


def test_openclaw_workspace_files_are_not_a_course_root(settings):
    """OpenClaw keeps `memory/` and `AGENTS.md` at the workspace root; neither is a course."""
    ws = settings.paths.workspace
    (ws / "memory").mkdir()
    (ws / "memory" / "2026-09-23.md").write_text("# what the agent remembered")
    (ws / "AGENTS.md").write_text("# openclaw agents")
    (ws / "Machine Learning" / "Lec1.md").write_text("# lecture")

    roots = [p.name for p in extract.source_roots(settings)]
    assert "memory" not in roots
    assert "Machine Learning" in roots

    sources = [p.name for p in extract.iter_sources(settings)]
    assert "2026-09-23.md" not in sources
    assert "AGENTS.md" not in sources
    assert "Lec1.md" in sources


def test_a_filing_plan_cannot_write_into_machinery(settings):
    """Even a mapped symlink into `_agent/` is not a destination an agent may file into."""
    from mitsync.filing import organize
    from tests.test_organize import add_mirror_file, place, write_plan

    ws = settings.paths.workspace
    (ws / "_agent" / "skills").mkdir(parents=True, exist_ok=True)
    (ws / "skills").symlink_to(ws / "_agent" / "skills", target_is_directory=True)
    write_course_map(
        settings, [{"canvas_id": 1, "folder": "Machine Learning"}, {"folder": "skills"}]
    )
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="x.pdf")

    plan = write_plan(settings, [place("101", "skills/lectures/x.pdf")])
    report = organize.apply_plan(settings, plan, yes=True)

    assert not report.applied
    assert "not a course folder" in report.rejected[0]["reason"]
    assert not (ws / "_agent" / "skills" / "lectures").exists()
