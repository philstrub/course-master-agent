"""
# Judgment Driver Tests

The three drivers, the task specs, and the structural rule that keeps provider
SDKs out of the rest of the codebase.

Three groups. The `rules` driver is checked for producing schema-valid results
for every task it claims to handle, and for returning a well-formed empty
answer rather than raising on one it does not. The `agent` driver is checked by
running it and asserting on the task file it wrote and the `PendingJudgment` it
raised, then feeding results back through `resolve_task` -- valid ones accepted,
malformed ones rejected with the failing JSON path named. Every registered spec
under `mitsync/llm/tasks/` is checked for the four required keys.

The last group is the important one and is enforced with `ast`, not mocks:
no module outside `mitsync/llm/` imports a provider SDK, the provider imports
inside `api_driver` sit within functions rather than at module scope, and the
package therefore imports cleanly with none of them installed. That invariant
is what makes mitsync usable with zero credentials and testable without a
network, so these tests fail loudly by design.

The payload constants at the top of the file are realistic but fictitious
course material; no test calls a model, reads a key, or touches the network.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from mitsync.core.config import Settings
from mitsync.core.errors import PendingJudgment
from mitsync.llm import base
from mitsync.llm.agent_driver import AgentJudge, resolve_task
from mitsync.llm.base import ResultValidationError, get_judge, make_task, validate_result
from mitsync.llm.rules_driver import RulesJudge

FILES_PAYLOAD = {
    "files": [
        {
            "file_id": "101",
            "display_name": "Lecture 3 - Trees.pdf",
            "canvas_folder": "course files/slides",
            "module_name": "Week 3",
            "module_position": 3,
            "content_type": "application/pdf",
            "size": 12345,
            "course": "Machine Learning",
        },
        {
            "file_id": "102",
            "display_name": "pset2.ipynb",
            "canvas_folder": "course files",
            "module_name": None,
            "module_position": None,
            "content_type": "application/json",
            "size": 999,
            "course": "Optimization",
        },
        {
            "file_id": "103",
            "display_name": "lecture_notes.pdf",
            "canvas_folder": "course files",
            "module_name": "Recitation 4",
            "module_position": 4,
            "content_type": "application/pdf",
            "size": 42,
            "course": "Analytics Edge",
        },
        {
            "file_id": "104",
            "display_name": "airfares.csv",
            "canvas_folder": "course files/data",
            "module_name": None,
            "module_position": None,
            "content_type": "text/csv",
            "size": 7,
            "course": "Analytics Edge",
        },
        {
            "file_id": "105",
            "display_name": "Syllabus_Fall.pdf",
            "canvas_folder": "course files",
            "module_name": None,
            "module_position": None,
            "content_type": "application/pdf",
            "size": 8,
            "course": "Analytics Tools",
        },
    ],
    "existing_folders": {"Machine Learning": ["lectures", "assignments"]},
}

COURSES_PAYLOAD = {
    "courses": [
        {"id": 1, "name": "The Analytics Edge", "course_code": "15.071", "term": "Fall 2026"},
        {"id": 2, "name": "Machine Learning", "course_code": "6.7900", "term": "Fall 2026"},
        {"id": 3, "name": "Optimization Methods", "course_code": "15.093", "term": "Fall 2026"},
    ],
    "existing_folders": ["Analytics Edge", "Machine Learning", "Optimization", "AI_Studio"],
    "observed_course_numbers": {"15.071": ["Analytics Edge"]},
}


# --- rules driver -----------------------------------------------------------
def test_rules_organize_plan_is_schema_valid(settings: Settings) -> None:
    task = make_task("organize_plan", FILES_PAYLOAD, rules="keep original names")
    result = RulesJudge(settings).judge(task)
    validate_result(task, result)
    dests = {p["file_id"]: p["destination"] for p in result["placements"]}
    assert len(dests) == 5
    assert dests["101"] == "Machine Learning/lectures/Lecture 3 - Trees.pdf"
    assert dests["102"] == "Optimization/assignments/pset2.ipynb"
    # module grouping beats the filename
    assert dests["103"] == "Analytics Edge/recitations/lecture_notes.pdf"
    assert dests["104"] == "Analytics Edge/data/airfares.csv"
    assert dests["105"] == "Analytics Tools/syllabus/Syllabus_Fall.pdf"


def test_rules_course_map_is_schema_valid(settings: Settings) -> None:
    task = make_task("course_map", COURSES_PAYLOAD)
    result = RulesJudge(settings).judge(task)
    validate_result(task, result)
    by_id = {m["canvas_id"]: m for m in result["mappings"]}
    assert by_id[1]["folder"] == "Analytics Edge"
    assert by_id[2]["folder"] == "Machine Learning"
    assert by_id[3]["folder"] == "Optimization"
    assert by_id[1]["course_number"] == "15.071"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("15.095", "15.095"), ("15_095", "15.095"), ("15095", "15.095"), ("15.C57", "15.C57")],
)
def test_rules_course_number_forms(settings: Settings, code: str, expected: str) -> None:
    payload = {
        "courses": [{"id": 9, "name": f"Some Course {code}", "course_code": code, "term": "F26"}],
        "existing_folders": ["Machine Learning"],
        "observed_course_numbers": {},
    }
    result = RulesJudge(settings).judge(make_task("course_map", payload))
    assert result["mappings"][0]["course_number"] == expected


def test_rules_unknown_task_never_raises(settings: Settings) -> None:
    task = base.JudgeTask(name="not_a_task", schema={"type": "object"}, payload={})
    assert RulesJudge(settings).judge(task) == {}


def test_get_judge_dispatches_to_rules(settings: Settings) -> None:
    assert isinstance(get_judge(settings, "rules"), RulesJudge)
    assert isinstance(get_judge(settings, "agent"), AgentJudge)


# --- agent driver -----------------------------------------------------------
def test_agent_driver_writes_task_and_raises(settings: Settings) -> None:
    task = make_task(
        "organize_plan",
        FILES_PAYLOAD,
        rules="KEEP ORIGINAL NAMES",
        origin_command="organize plan",
        origin_args={"include_existing": False},
    )
    with pytest.raises(PendingJudgment) as excinfo:
        AgentJudge(settings).judge(task)

    exc = excinfo.value
    assert exc.task_path.exists()
    assert exc.task_path.parent == settings.paths.tasks_dir
    doc = json.loads(exc.task_path.read_text())
    assert doc["task"] == "organize_plan"
    assert doc["rules"] == "KEEP ORIGINAL NAMES"
    assert doc["payload"] == FILES_PAYLOAD
    assert doc["result_schema"] == task.schema_
    assert doc["origin_command"] == "organize plan"
    assert "mitsync resolve" in doc["how_to_resolve"]
    assert "mitsync resolve" in exc.instructions


def test_resolve_task_accepts_valid_result(settings: Settings, tmp_path: Path) -> None:
    task = make_task("organize_plan", FILES_PAYLOAD, origin_command="organize plan")
    with pytest.raises(PendingJudgment) as excinfo:
        AgentJudge(settings).judge(task)
    task_path = excinfo.value.task_path

    good = {
        "placements": [
            {
                "file_id": "101",
                "destination": "Machine Learning/lectures/Lecture 3 - Trees.pdf",
                "reason": "module Week 3",
                "confidence": 0.9,
            }
        ]
    }
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(good))
    out = resolve_task(task_path, result_path)
    assert out == good
    assert task_path.with_suffix(".result.json").exists()


@pytest.mark.parametrize(
    "bad",
    [
        {"placements": [{"file_id": "101", "destination": "x", "reason": "y"}]},  # missing field
        {"placements": [{"file_id": "101", "destination": "/abs", "reason": "y", "confidence": 1}]},
        {"placements": [{"file_id": 101, "destination": "x", "reason": "y", "confidence": 1}]},
        {"placements": [], "extra": True},  # additionalProperties: false
        {"wrong_key": []},
    ],
)
def test_resolve_task_rejects_bad_result(settings: Settings, tmp_path: Path, bad: dict) -> None:
    task = make_task("organize_plan", FILES_PAYLOAD)
    with pytest.raises(PendingJudgment) as excinfo:
        AgentJudge(settings).judge(task)
    result_path = tmp_path / "bad.json"
    result_path.write_text(json.dumps(bad))
    with pytest.raises(ResultValidationError):
        resolve_task(excinfo.value.task_path, result_path)


def test_task_specs_are_well_formed() -> None:
    import jsonschema

    for name in ("organize_plan", "course_map"):
        spec = base.load_task_spec(name)
        assert spec["name"] == name
        assert spec["instructions"].strip()
        jsonschema.Draft202012Validator.check_schema(spec["schema"])


# --- structural invariant ---------------------------------------------------
FORBIDDEN = {"anthropic", "openai", "google.generativeai", "google", "litellm"}
PKG_ROOT = Path(__file__).resolve().parents[1] / "mitsync"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_no_provider_sdk_imported_outside_llm_package() -> None:
    """Structural invariant: only mitsync/llm/ may touch a provider SDK."""
    offenders: list[str] = []
    for path in PKG_ROOT.rglob("*.py"):
        if (PKG_ROOT / "llm") in path.parents or path.parent == PKG_ROOT / "llm":
            continue
        for mod in _imported_modules(path):
            root = mod.split(".")[0]
            if root in FORBIDDEN or mod in FORBIDDEN:
                offenders.append(f"{path.relative_to(PKG_ROOT.parent)} imports {mod}")
    assert not offenders, "provider SDK imported outside mitsync/llm/:\n" + "\n".join(offenders)


def test_provider_imports_in_api_driver_are_lazy() -> None:
    """Provider imports must sit inside functions, so the package imports with no SDK."""
    tree = ast.parse((PKG_ROOT / "llm" / "api_driver.py").read_text())
    module_scope: set[str] = set()
    for node in tree.body:  # top level only
        if isinstance(node, ast.Import):
            module_scope.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_scope.add(node.module.split(".")[0])
    assert not (module_scope & FORBIDDEN)


def test_package_imports_cleanly_without_sdks() -> None:
    import importlib

    for mod in ("mitsync.cli", "mitsync.llm", "mitsync.llm.api_driver"):
        importlib.import_module(mod)
