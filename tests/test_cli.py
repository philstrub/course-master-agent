"""The data-tool surface: `--json` shapes an agent parses, and `organize apply` exit codes.

Every command here runs through Typer against the temp workspace (the
`settings` fixture is injected in place of `cli._settings`, so nothing touches
the real repo's state). The JSON shapes are asserted key by key because the
driving agent parses them; a renamed key is a breaking change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mitsync import cli
from mitsync.core.config import Settings
from tests.test_deadlines import seeded  # noqa: F401 -- fixture
from tests.test_organize import add_mirror_file, place, write_course_map, write_plan

runner = CliRunner()


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch):
    def _use(settings: Settings) -> Settings:
        monkeypatch.setattr(cli, "_settings", lambda: settings)
        return settings

    return _use


def run_json(*args: str) -> dict:
    result = runner.invoke(cli.app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_the_judgment_protocol_is_gone() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for gone in ("resolve", "brief "):
        assert gone not in result.stdout
    for name in ("due", "work", "unfiled", "calendar", "doctor", "sync", "extract"):
        assert name in result.stdout
    plan = runner.invoke(cli.app, ["organize", "plan"])
    assert plan.exit_code != 0
    extract = runner.invoke(cli.app, ["graph", "extract"])
    assert extract.exit_code != 0


def test_due_json_shape(seeded: Settings, use) -> None:  # noqa: F811 -- fixture
    use(seeded)
    doc = run_json("due", "--days", "7")

    assert set(doc) == {
        "generated_at",
        "last_sync",
        "calendar_available",
        "warnings",
        "window_days",
        "due_json",
        "items",
    }
    assert doc["window_days"] == 7
    assert Path(doc["due_json"]).exists()
    by_title = {i["title"]: i for i in doc["items"]}
    assert set(by_title) == {"Problem Set 1", "Quiz 2"}  # past and undated are outside
    ps1 = by_title["Problem Set 1"]
    assert set(ps1) == {
        "course",
        "title",
        "due_at",
        "type",
        "url",
        "source",
        "submitted",
        "status",
        "description",
    }
    assert ps1["course"] == "Machine Learning"
    assert ps1["status"] == "unsubmitted"
    assert ps1["description"].startswith("Fit a ridge model.")
    # due.json keeps everything, regardless of the printed window
    stored = json.loads(Path(doc["due_json"]).read_text())
    assert {"Problem Set 0", "Ungraded reading"} <= {i["title"] for i in stored["items"]}


def test_work_json_shape(seeded: Settings, use) -> None:  # noqa: F811 -- fixture
    use(seeded)
    hw = seeded.paths.workspace / "Machine Learning" / "assignments" / "hw-01"
    hw.mkdir(parents=True)
    (hw / "answers.tex").write_text("x")

    doc = run_json("work", "--course", "Machine Learning")

    assert set(doc) == {"generated_at", "recent_days", "files_per_folder", "tags", "courses"}
    [course] = doc["courses"]
    assert set(course) == {"course", "folders"}
    [folder] = course["folders"]
    assert set(folder) == {"folder", "files", "omitted"}
    assert folder["folder"] == "Machine Learning/assignments/hw-01"
    [f] = folder["files"]
    assert set(f) == {"path", "tag", "modified"}
    assert f == {**f, "path": "Machine Learning/assignments/hw-01/answers.tex", "tag": "yours"}


def test_work_with_an_unknown_course_fails_cleanly(seeded: Settings, use) -> None:  # noqa: F811
    use(seeded)
    result = runner.invoke(cli.app, ["work", "--course", "Nope", "--json"])
    assert result.exit_code != 0
    assert result.stdout == ""


def test_unfiled_ids_is_the_cheap_check(settings: Settings, use) -> None:
    use(settings)
    write_course_map(settings, [{"canvas_id": 1, "folder": "Machine Learning"}])
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="Lec03.pdf", module_name="Week 3")

    doc = run_json("unfiled", "--ids")

    assert set(doc) == {"file_ids"}
    assert len(doc["file_ids"]) == 1


def test_unfiled_json_shape(settings: Settings, use) -> None:
    use(settings)
    write_course_map(settings, [{"canvas_id": 1, "folder": "Machine Learning"}])
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="Lec03.pdf", module_name="Week 3")

    doc = run_json("unfiled")

    assert set(doc) == {
        "naming_rules",
        "course_folders",
        "buckets",
        "per_item_buckets",
        "existing_file_id_prefix",
        "plans_dir",
        "plan_schema",
        "files",
        "links",
    }
    [f] = doc["files"]
    assert set(f) == {
        "file_id",
        "display_name",
        "course",
        "mirror_course",
        "mirror_path",
        "canvas_folder",
        "module_name",
        "module_position",
        "content_type",
        "size",
        "module_item_title",
        "module_subheader",
    }
    assert f["file_id"] == "101" and f["course"] == "Machine Learning"


def test_organize_apply_exit_codes(settings: Settings, use) -> None:
    use(settings)
    write_course_map(settings, [{"canvas_id": 1, "folder": "Machine Learning"}])
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="a.pdf")
    add_mirror_file(settings, uuid="u2", canvas_id=102, name="b.pdf")

    bad = write_plan(settings, [place("101", "Machine Learning/data/a.pdf")], name="bad")
    result = runner.invoke(cli.app, ["organize", "apply", "--plan", str(bad), "--yes"])
    assert result.exit_code == 1
    assert "not an allowed bucket" in result.stdout

    good = write_plan(settings, [place("102", "Machine Learning/lectures/b.pdf")], name="good")
    result = runner.invoke(cli.app, ["organize", "apply", "--plan", str(good), "--yes"])
    assert result.exit_code == 0, result.output
    assert (settings.paths.workspace / "Machine Learning/lectures/b.pdf").exists()

    missing = runner.invoke(cli.app, ["organize", "apply"])
    assert missing.exit_code != 0  # --plan is required: the agent writes the plan


def test_graph_add_reports_line_numbers(settings: Settings, use, tmp_path: Path) -> None:
    use(settings)
    path = tmp_path / "facts.jsonl"
    path.write_text('{"id": "x:1", "type": "Widget", "label": "W", "src": ["a.pdf"]}\n')
    result = runner.invoke(cli.app, ["graph", "add", str(path)])
    assert result.exit_code != 0
    assert "line 1:" in str(result.exception) + result.output


# --------------------------------------------------------------------------
# graph check / schema: the loop's stopping oracle and the agents' ontology
# --------------------------------------------------------------------------
def test_graph_check_json_and_exit_code(use, settings: Settings) -> None:
    use(settings)
    ml = settings.paths.workspace / "Machine Learning"
    (ml / "Lec1.pdf").write_bytes(b"%PDF-1.4 loose")
    runner.invoke(cli.app, ["graph", "backbone"])

    result = runner.invoke(cli.app, ["graph", "check", "--json"])
    assert result.exit_code == 1  # a loose file nobody attached
    doc = json.loads(result.stdout)
    assert set(doc) == {"ok", "counts", "violations"}
    assert doc["ok"] is False and doc["counts"] == {"error": 1, "human": 0, "info": 0}
    [v] = doc["violations"]
    assert set(v) == {"code", "node", "message", "severity"} and v["code"] == "unfiled"

    (ml / "Lec1.pdf").unlink()
    runner.invoke(cli.app, ["graph", "backbone"])
    assert run_json("graph", "check")["ok"] is True


def test_graph_query_takes_params_and_prints_json(use, settings: Settings) -> None:
    from tests.test_graph import seed_graph

    use(settings)
    seed_graph(settings)
    rows = run_json("graph", "query", "--canned", "files_of", "--param", "item=lecture:ae:05")
    assert [r["path"] for r in rows] == ["Analytics Edge/lectures/trees.pdf"]
    bad = runner.invoke(cli.app, ["graph", "query", "--canned", "files_of", "--param", "item"])
    assert bad.exit_code != 0 and "NAME=VALUE" in str(bad.exception) + bad.output


def test_graph_schema_prints_the_ontology() -> None:
    result = runner.invoke(cli.app, ["graph", "schema"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert {"node_types", "edge_types", "cardinality", "bucket_edges"} <= set(doc)
    assert doc["edge_types"]["file_of_lecture"]["target"] == ["Lecture"]
