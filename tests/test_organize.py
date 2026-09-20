"""organize.py: plan -> apply -> undo, and the guardrails around all three."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from mitsync import organize
from mitsync.config import Settings
from mitsync.llm.base import JudgeTask
from mitsync.llm.rules_driver import RulesJudge
from mitsync.manifest import CourseRecord, FileRecord, Manifest

NAMING_MD = textwrap.dedent(
    """
    # Filing rules

    Keep the original filename. One of lectures/ recitations/ assignments/
    data/ syllabus/ notes/ other/ inside the course folder.
    """
).strip()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class RecordingJudge:
    """Wraps the rules driver so tests can inspect the payload it was handed."""

    def __init__(self, settings: Settings) -> None:
        self.inner = RulesJudge(settings)
        self.tasks: list[JudgeTask] = []

    def judge(self, task: JudgeTask) -> dict[str, Any]:
        self.tasks.append(task)
        return self.inner.judge(task)


class FixedJudge:
    """Returns a canned (schema-valid) result, whatever it is asked."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.tasks: list[JudgeTask] = []

    def judge(self, task: JudgeTask) -> dict[str, Any]:
        self.tasks.append(task)
        return self.result


def write_naming(settings: Settings, text: str = NAMING_MD) -> Path:
    path = settings.paths.config_dir / "naming.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_course_map(settings: Settings, entries: list[dict[str, Any]]) -> Path:
    path = settings.paths.config_dir / "courses.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    path.write_text(yaml.safe_dump({"courses": entries}, sort_keys=False))
    return path


def add_mirror_file(
    settings: Settings,
    *,
    uuid: str,
    canvas_id: int,
    name: str,
    body: bytes = b"canvas bytes",
    mirror_folder: str = "ML Mirror",
    course_canvas_id: int = 1,
    module_name: str | None = None,
    canvas_folder: str | None = "course files",
) -> Path:
    rel = f"_canvas/{mirror_folder}/{name}"
    path = settings.paths.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    rec = FileRecord(
        uuid=uuid,
        canvas_id=canvas_id,
        course_folder=mirror_folder,
        course_canvas_id=course_canvas_id,
        display_name=name,
        filename=name,
        content_type="application/pdf",
        size=len(body),
        canvas_folder=canvas_folder,
        module_name=module_name,
        module_position=None,
        updated_at="2026-09-01T00:00:00Z",
        sha256=organize.sha256_file(path),
        mirror_path=rel,
        filed_path=None,
        first_seen="2026-09-01T00:00:00Z",
        last_synced="2026-09-01T00:00:00Z",
    )
    with Manifest(settings.paths.manifest_db) as man:
        man.upsert_course(
            CourseRecord(
                canvas_id=course_canvas_id,
                name="Machine Learning 15.095",
                course_code="15.095",
                term_name="Fall 2026",
                term_start=None,
                term_end=None,
                folder=mirror_folder,
            )
        )
        man.upsert_file(rec)
    return path


def filed_path_of(settings: Settings, uuid: str) -> str | None:
    with Manifest(settings.paths.manifest_db) as man:
        rec = man.get_file(uuid)
    return rec.filed_path if rec else None


@pytest.fixture
def prepared(settings: Settings) -> Settings:
    write_naming(settings)
    write_course_map(
        settings,
        [{"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": []}],
    )
    return settings


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def test_plan_on_empty_manifest_is_harmless(prepared: Settings) -> None:
    judge = RecordingJudge(prepared)
    result = organize.plan(prepared, judge)
    assert result.entries == []
    assert judge.tasks == []  # nothing to judge -> no task, no pending judgment
    assert result.path is not None and result.path.exists()


def test_plan_writes_a_plan_and_moves_nothing(prepared: Settings) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    judge = RecordingJudge(prepared)

    result = organize.plan(prepared, judge)

    assert [e.destination for e in result.entries] == ["Machine Learning/lectures/Lec03_2026.pdf"]
    assert result.entries[0].uuid == "u1"
    assert source.exists()
    assert not (prepared.paths.workspace / "Machine Learning" / "lectures").exists()
    doc = json.loads(result.path.read_text())
    assert doc["plan_id"] == result.plan_id
    assert doc["naming_rules_sha"] and doc["entries"][0]["file_id"] == "101"


def test_plan_uses_the_course_map_not_the_mirror_folder(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="hw1.pdf")
    result = organize.plan(prepared, RecordingJudge(prepared))
    assert result.entries[0].destination.startswith("Machine Learning/")


def test_naming_rules_are_injected_verbatim_and_tune_the_plan(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")

    judge = RecordingJudge(prepared)
    first = organize.plan(prepared, judge)
    assert judge.tasks[0].rules == NAMING_MD  # the file's text reached the judge

    edited = NAMING_MD + "\n\nPut every lecture in `slides/` instead of `lectures/`.\n"
    write_naming(prepared, edited)
    judge2 = RecordingJudge(prepared)
    second = organize.plan(prepared, judge2)

    assert judge2.tasks[0].rules == edited
    assert "slides/" in judge2.tasks[0].rules
    assert second.naming_rules_sha != first.naming_rules_sha


def test_naming_rules_change_the_destination_for_a_rules_aware_judge(
    prepared: Settings,
) -> None:
    """Prose is the tuning surface: a judge that reads it files differently."""
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")

    class ProseFollowingJudge:
        def judge(self, task: JudgeTask) -> dict[str, Any]:
            bucket = "slides" if "slides/" in task.rules else "lectures"
            return {
                "placements": [
                    {
                        "file_id": "101",
                        "destination": f"Machine Learning/{bucket}/Lec03_2026.pdf",
                        "reason": "followed config/naming.md",
                        "confidence": 0.9,
                    }
                ]
            }

    before = organize.plan(prepared, ProseFollowingJudge())
    write_naming(prepared, NAMING_MD + "\nPut lectures in `slides/`.\n")
    after = organize.plan(prepared, ProseFollowingJudge())

    assert before.entries[0].destination.endswith("lectures/Lec03_2026.pdf")
    assert after.entries[0].destination.endswith("slides/Lec03_2026.pdf")


@pytest.mark.parametrize(
    "destination",
    [
        "../outside.pdf",
        "Machine Learning/../../escape.pdf",
        "~/secret.pdf",
        "_canvas/ML Mirror/copy.pdf",
        "_agent/mitsync/evil.py",
        "_kb/text/evil.txt",
        "AI_Studio/nandatown/x.pdf",
        "bare.pdf",
    ],
)
def test_bad_destinations_are_rejected_before_they_reach_a_plan(
    prepared: Settings, destination: str
) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="x.pdf")
    judge = FixedJudge(
        {
            "placements": [
                {
                    "file_id": "101",
                    "destination": destination,
                    "reason": "hostile",
                    "confidence": 1.0,
                }
            ]
        }
    )
    result = organize.plan(prepared, judge)
    assert result.entries == []
    assert result.rejected and "101" in {r["file_id"] for r in result.rejected}


def test_schema_itself_blocks_absolute_destinations(prepared: Settings) -> None:
    """Defence in depth: the task schema rejects `/abs` before organize sees it."""
    from mitsync.llm.base import ResultValidationError

    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="x.pdf")
    judge = FixedJudge(
        {
            "placements": [
                {
                    "file_id": "101",
                    "destination": "/etc/passwd",
                    "reason": "hostile",
                    "confidence": 1.0,
                }
            ]
        }
    )
    with pytest.raises(ResultValidationError):
        organize.plan(prepared, judge)


@pytest.mark.parametrize("bad", ["", "justafile.pdf", "../x/y.pdf", "/abs/x.pdf"])
def test_validate_destination_rejects(prepared: Settings, bad: str) -> None:
    with pytest.raises(ValueError):
        organize.validate_destination(prepared, bad)


def test_validate_destination_accepts_a_normal_path(prepared: Settings) -> None:
    assert (
        organize.validate_destination(prepared, "Machine Learning/lectures/a.pdf")
        == "Machine Learning/lectures/a.pdf"
    )


def test_nandatown_and_venvs_never_appear_in_a_plan(prepared: Settings) -> None:
    ws = prepared.paths.workspace
    for rel in (
        "AI_Studio/nandatown/src/app.py",
        "AI_Studio/nandatown/README.md",
        "AI_Studio/.venv/lib/site-packages/thing.py",
        "Machine Learning/__pycache__/x.pyc",
        "Machine Learning/notes_of_mine.pdf",
    ):
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    result = organize.plan(prepared, RecordingJudge(prepared), include_existing=True)

    planned = [e.source for e in result.entries] + [r.get("file_id", "") for r in result.rejected]
    blob = " ".join(planned)
    assert "nandatown" not in blob
    assert ".venv" not in blob
    assert "__pycache__" not in blob
    assert any(e.source == "Machine Learning/notes_of_mine.pdf" for e in result.entries)


def test_ignored_paths_cover_the_directory_itself(prepared: Settings) -> None:
    assert organize.is_ignored(prepared, "AI_Studio/nandatown")
    assert organize.is_ignored(prepared, "AI_Studio/nandatown/src/app.py")
    assert not organize.is_ignored(prepared, "AI_Studio/notes.pdf")


# --------------------------------------------------------------------------
# apply / undo
# --------------------------------------------------------------------------
def test_apply_links_from_the_mirror_and_leaves_it_intact(prepared: Settings) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    the_plan = organize.plan(prepared, RecordingJudge(prepared))

    report = organize.apply_plan(prepared, the_plan.path)

    dest = prepared.paths.workspace / "Machine Learning/lectures/Lec03_2026.pdf"
    assert dest.exists()
    assert source.exists(), "the Canvas mirror must remain the source of truth"
    assert source.read_bytes() == dest.read_bytes()
    assert report.applied[0]["mode"] in ("hardlink", "copy")
    assert filed_path_of(prepared, "u1") == "Machine Learning/lectures/Lec03_2026.pdf"
    assert report.undo_log is not None and report.undo_log.exists()


def test_plan_apply_undo_is_a_byte_identical_round_trip(prepared: Settings) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    before = {
        p.relative_to(prepared.paths.workspace).as_posix(): p.read_bytes()
        for p in prepared.paths.workspace.rglob("*")
        if p.is_file() and "_agent" not in p.parts
    }

    the_plan = organize.plan(prepared, RecordingJudge(prepared))
    report = organize.apply_plan(prepared, the_plan.path)
    assert report.applied

    undone = organize.undo(prepared, "latest")

    assert not undone.refused and not undone.errors
    after = {
        p.relative_to(prepared.paths.workspace).as_posix(): p.read_bytes()
        for p in prepared.paths.workspace.rglob("*")
        if p.is_file() and "_agent" not in p.parts
    }
    assert after == before
    assert source.exists()
    assert filed_path_of(prepared, "u1") is None


def test_hardlink_falls_back_to_copy(prepared: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    the_plan = organize.plan(prepared, RecordingJudge(prepared))

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", boom)
    report = organize.apply_plan(prepared, the_plan.path)

    assert report.applied[0]["mode"] == "copy"
    dest = prepared.paths.workspace / "Machine Learning/lectures/Lec03_2026.pdf"
    assert dest.exists() and dest.stat().st_nlink == 1


def test_symlink_mode_is_honoured(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    prepared.organize.link_mode = "symlink"
    the_plan = organize.plan(prepared, RecordingJudge(prepared))
    organize.apply_plan(prepared, the_plan.path)
    dest = prepared.paths.workspace / "Machine Learning/lectures/Lec03_2026.pdf"
    assert dest.is_symlink()
    organize.undo(prepared, None)
    assert not dest.exists() and not dest.is_symlink()


def test_identical_destination_is_skipped_not_overwritten(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf", body=b"same")
    dest = prepared.paths.workspace / "Machine Learning/lectures/Lec03_2026.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"same")

    the_plan = organize.plan(prepared, RecordingJudge(prepared))
    report = organize.apply_plan(prepared, the_plan.path)

    assert not report.applied
    assert report.skipped and "identical" in report.skipped[0]["status"]
    assert dest.read_bytes() == b"same"
    assert filed_path_of(prepared, "u1") == "Machine Learning/lectures/Lec03_2026.pdf"


def test_different_destination_is_suffixed_and_flagged(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf", body=b"new bytes")
    dest = prepared.paths.workspace / "Machine Learning/lectures/Lec03_2026.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"mine, hands off")

    the_plan = organize.plan(prepared, RecordingJudge(prepared))
    report = organize.apply_plan(prepared, the_plan.path)

    assert dest.read_bytes() == b"mine, hands off"
    suffixed = dest.with_name("Lec03_2026-2.pdf")
    assert suffixed.exists() and suffixed.read_bytes() == b"new bytes"
    assert report.flagged and "collision" in report.flagged[0]["status"]


def test_low_confidence_placements_are_held_for_review(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="mystery.pdf")
    judge = FixedJudge(
        {
            "placements": [
                {
                    "file_id": "101",
                    "destination": "Machine Learning/other/mystery.pdf",
                    "reason": "no idea",
                    "confidence": 0.2,
                }
            ]
        }
    )
    the_plan = organize.plan(prepared, judge)
    report = organize.apply_plan(prepared, the_plan.path)

    assert the_plan.entries[0].needs_review
    assert not report.applied
    assert report.skipped[0]["status"] == "low-confidence"


def test_undo_refuses_when_the_file_changed_since_apply(prepared: Settings) -> None:
    ws = prepared.paths.workspace
    loose = ws / "Machine Learning" / "HW1" / "15_095_hw1.pdf"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"original")

    the_plan = organize.plan(prepared, RecordingJudge(prepared), include_existing=True)
    organize.apply_plan(prepared, the_plan.path, yes=True)
    moved = ws / "Machine Learning/assignments/15_095_hw1.pdf"
    assert moved.exists()

    moved.write_bytes(b"edited since")
    report = organize.undo(prepared, "latest")

    assert report.refused and "changed" in report.refused[0]["reason"]
    assert moved.read_bytes() == b"edited since"
    assert not loose.exists()


# --------------------------------------------------------------------------
# pre-existing files
# --------------------------------------------------------------------------
def test_preexisting_files_are_not_planned_without_include_existing(
    prepared: Settings,
) -> None:
    loose = prepared.paths.workspace / "Machine Learning" / "HW1" / "15_095_hw1.pdf"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"mine")

    result = organize.plan(prepared, RecordingJudge(prepared))
    assert result.entries == []
    assert loose.exists()


def test_preexisting_files_are_not_moved_without_yes(
    prepared: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    loose = prepared.paths.workspace / "Machine Learning" / "HW1" / "15_095_hw1.pdf"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"mine")

    the_plan = organize.plan(prepared, RecordingJudge(prepared), include_existing=True)
    assert the_plan.entries and the_plan.entries[0].kind == "existing"

    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    report = organize.apply_plan(prepared, the_plan.path)  # no yes=True

    assert not report.applied
    assert report.skipped[0]["status"] == "needs --yes"
    assert loose.exists() and loose.read_bytes() == b"mine"


def test_preexisting_files_move_with_yes_and_undo_restores_them(prepared: Settings) -> None:
    ws = prepared.paths.workspace
    loose = ws / "Machine Learning" / "recitation 1" / "Recitation_1_Linear_Regression_v1.ipynb"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"notebook bytes")

    the_plan = organize.plan(prepared, RecordingJudge(prepared), include_existing=True)
    organize.apply_plan(prepared, the_plan.path, yes=True)

    moved = ws / "Machine Learning/recitations/Recitation_1_Linear_Regression_v1.ipynb"
    assert moved.exists() and not loose.exists()

    report = organize.undo(prepared, "latest")
    assert not report.refused
    assert loose.exists() and loose.read_bytes() == b"notebook bytes"
    assert not moved.exists()


def test_already_bucketed_files_are_left_alone(prepared: Settings) -> None:
    filed = prepared.paths.workspace / "Machine Learning" / "lectures" / "keep.pdf"
    filed.parent.mkdir(parents=True, exist_ok=True)
    filed.write_text("x")
    result = organize.plan(prepared, RecordingJudge(prepared), include_existing=True)
    assert result.entries == []


# --------------------------------------------------------------------------
# the agent driver round trip (task file -> result -> replay)
# --------------------------------------------------------------------------
def test_agent_driver_task_carries_the_rules_and_replays(prepared: Settings) -> None:
    from mitsync.cli import _PreJudged
    from mitsync.errors import PendingJudgment
    from mitsync.llm.agent_driver import AgentJudge, resolve_task

    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")

    with pytest.raises(PendingJudgment) as excinfo:
        organize.plan(prepared, AgentJudge(prepared))

    task_doc = json.loads(excinfo.value.task_path.read_text())
    assert task_doc["origin_command"] == "organize plan"
    assert task_doc["rules"] == NAMING_MD
    assert task_doc["payload"]["files"][0]["display_name"] == "Lec03_2026.pdf"
    assert task_doc["payload"]["files"][0]["course"] == "Machine Learning"

    answer = prepared.paths.state_dir / "answer.json"
    answer.write_text(
        json.dumps(
            {
                "placements": [
                    {
                        "file_id": "101",
                        "destination": "Machine Learning/lectures/03-trees.pdf",
                        "reason": "module Week 3",
                        "confidence": 0.95,
                    }
                ]
            }
        )
    )
    validated = resolve_task(excinfo.value.task_path, answer)
    replayed = organize.plan(prepared, _PreJudged(validated))

    assert [e.destination for e in replayed.entries] == ["Machine Learning/lectures/03-trees.pdf"]
