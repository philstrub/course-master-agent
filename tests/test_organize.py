"""organize.py: unfiled -> agent-written plan -> validate/apply -> undo, and the guardrails."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from mitsync.canvas.manifest import CourseRecord, FileRecord, Manifest
from mitsync.core.config import Settings
from mitsync.core.errors import MitsyncError
from mitsync.filing import organize


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def write_plan(settings: Settings, placements: list[dict[str, Any]], name: str = "plan") -> Path:
    """What the driving agent writes: `{"placements": [...]}`."""
    path = settings.paths.plans_dir / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"placements": placements}))
    return path


def place(file_id: str, destination: str, reason: str = "test") -> dict[str, str]:
    return {"file_id": file_id, "destination": destination, "reason": reason}


LEC = "Machine Learning/lectures/Lec03_2026.pdf"


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
    write_course_map(
        settings,
        [{"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": []}],
    )
    return settings


# --------------------------------------------------------------------------
# unfiled
# --------------------------------------------------------------------------
def test_unfiled_on_an_empty_manifest_is_harmless(prepared: Settings) -> None:
    doc = organize.unfiled(prepared)
    assert doc["files"] == []
    assert doc["course_folders"] == ["Machine Learning"]


def test_unfiled_lists_mirror_files_with_their_context_and_the_rules(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf", module_name="Week 3")
    doc = organize.unfiled(prepared)

    assert doc["naming_rules"].endswith("config/naming.md")
    assert doc["buckets"] == list(organize.FILING_BUCKETS)
    assert "data" not in doc["buckets"]  # naming.md: no course-wide data folder
    assert doc["per_item_buckets"] == ["assignments", "recitations"]
    assert doc["plan_schema"] == organize.PLAN_SCHEMA
    [f] = doc["files"]
    assert f["file_id"] == "101"
    assert f["course"] == "Machine Learning"  # the course map, not the mirror folder
    assert f["mirror_course"] == "ML Mirror"
    assert f["mirror_path"] == "_canvas/ML Mirror/Lec03_2026.pdf"
    assert f["display_name"] == "Lec03_2026.pdf"
    assert f["module_name"] == "Week 3"
    assert "module_position" in f


def test_unfiled_reports_an_unmapped_course_as_null(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u9", canvas_id=909, name="x.pdf", course_canvas_id=77)
    [f] = organize.unfiled(prepared)["files"]
    assert f["course"] is None


def test_filed_files_leave_the_unfiled_list(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)
    assert organize.unfiled(prepared)["files"] == []


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "justafile.pdf", "../x/y.pdf", "/abs/x.pdf"])
def test_validate_destination_rejects(prepared: Settings, bad: str) -> None:
    with pytest.raises(ValueError):
        organize.validate_destination(prepared, bad)


def test_validate_destination_accepts_a_normal_path(prepared: Settings) -> None:
    assert organize.validate_destination(prepared, LEC) == LEC


@pytest.mark.parametrize(
    ("destination", "why"),
    [
        ("../outside.pdf", "traverse"),
        ("Machine Learning/../../escape.pdf", "traverse"),
        ("~/secret.pdf", "absolute"),
        ("/etc/passwd", "absolute"),
        ("_canvas/ML Mirror/copy.pdf", "reserved"),
        ("_agent/mitsync/evil.py", "reserved"),
        ("_kb/text/evil.txt", "reserved"),
        ("AI_Studio/nandatown/x.pdf", "ignore_globs"),
        ("bare.pdf", "course folder"),
        ("Optimization/lectures/x.pdf", "courses.yml"),
        ("Machine Learning/x.pdf", "bucket"),
        ("Machine Learning/data/x.pdf", "not an allowed bucket"),
        ("Machine Learning/slides/x.pdf", "not an allowed bucket"),
        ("Machine Learning/assignments/x.pdf", "per-item"),
        ("Machine Learning/recitations/x.pdf", "per-item"),
        ("Machine Learning/assignments/hw-01/sub/x.pdf", "never nest"),
        ("Machine Learning/lectures/week-1/x.pdf", "flat"),
    ],
)
def test_bad_destinations_are_rejected_with_a_reason(
    prepared: Settings, destination: str, why: str
) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="x.pdf")
    report = organize.apply_plan(
        prepared, write_plan(prepared, [place("101", destination)]), yes=True
    )
    assert not report.applied
    [rejected] = report.rejected
    assert rejected["file_id"] == "101"
    assert rejected["destination"] == destination
    assert why in rejected["reason"]
    assert source.exists()
    assert not (prepared.paths.workspace / "Machine Learning" / "assignments").exists()


def test_per_item_folders_are_accepted(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="hw1.pdf")
    dest = "Machine Learning/assignments/hw-01/hw1.pdf"
    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", dest)]), yes=True)
    assert [r["destination"] for r in report.applied] == [dest]
    assert (prepared.paths.workspace / dest).exists()


@pytest.mark.parametrize(
    ("doc", "where"),
    [
        ({"placements": [{"file_id": "101", "destination": LEC}]}, "$.placements[0]"),
        (
            {"placements": [{**place("101", LEC), "confidence": 0.9}]},
            "$.placements[0]",
        ),
        ({"placements": "all of them"}, "$.placements"),
        ({"plan": []}, "$"),
    ],
)
def test_a_plan_that_breaks_the_schema_is_refused_whole(
    prepared: Settings, doc: dict[str, Any], where: str
) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    path = prepared.paths.plans_dir / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(MitsyncError, match="plan schema") as excinfo:
        organize.apply_plan(prepared, path, yes=True)
    assert f"at {where}:" in str(excinfo.value)
    assert not (prepared.paths.workspace / LEC).exists()


def test_a_missing_plan_file_is_an_error(prepared: Settings) -> None:
    with pytest.raises(MitsyncError, match="not found"):
        organize.apply_plan(prepared, prepared.paths.plans_dir / "nope.json", yes=True)


def test_unknown_and_duplicate_file_ids_are_rejected(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    plan = write_plan(
        prepared,
        [
            place("101", LEC),
            place("101", "Machine Learning/other/Lec03_2026.pdf"),
            place("999", "Machine Learning/other/ghost.pdf"),
        ],
    )
    report = organize.apply_plan(prepared, plan, yes=True)
    assert [r["destination"] for r in report.applied] == [LEC]
    reasons = {r["file_id"] + ":" + r["destination"]: r["reason"] for r in report.rejected}
    assert "duplicate" in reasons["101:Machine Learning/other/Lec03_2026.pdf"]
    assert "no mirrored file" in reasons["999:Machine Learning/other/ghost.pdf"]


def test_two_placements_may_not_share_a_destination(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="a.pdf")
    add_mirror_file(prepared, uuid="u2", canvas_id=102, name="b.pdf")
    dest = "Machine Learning/other/same.pdf"
    report = organize.apply_plan(
        prepared, write_plan(prepared, [place("101", dest), place("102", dest)]), yes=True
    )
    assert len(report.applied) == 1
    assert "same destination" in report.rejected[0]["reason"]


def test_a_source_missing_from_the_mirror_is_rejected(prepared: Settings) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    source.unlink()
    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)
    assert not report.applied
    assert "missing from the mirror" in report.rejected[0]["reason"]


def test_an_already_filed_file_is_rejected(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)
    again = write_plan(prepared, [place("101", "Machine Learning/other/x.pdf")], name="again")
    report = organize.apply_plan(prepared, again, yes=True)
    assert "already filed" in report.rejected[0]["reason"]


def test_a_different_file_at_the_destination_is_never_overwritten(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf", body=b"new bytes")
    dest = prepared.paths.workspace / LEC
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"mine, hands off")

    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)

    assert dest.read_bytes() == b"mine, hands off"
    assert not report.applied
    assert "different file already exists" in report.rejected[0]["reason"]
    assert filed_path_of(prepared, "u1") is None


def test_an_identical_file_at_the_destination_is_skipped_and_recorded(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf", body=b"same")
    dest = prepared.paths.workspace / LEC
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"same")

    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)

    assert not report.applied and not report.rejected
    assert "identical" in report.skipped[0]["status"]
    assert dest.read_bytes() == b"same"
    assert filed_path_of(prepared, "u1") == LEC


# --------------------------------------------------------------------------
# apply / undo
# --------------------------------------------------------------------------
def test_a_good_plan_links_from_the_mirror_and_leaves_it_intact(prepared: Settings) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")

    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)

    dest = prepared.paths.workspace / LEC
    assert dest.exists()
    assert source.exists(), "the Canvas mirror must remain the source of truth"
    assert source.read_bytes() == dest.read_bytes()
    assert report.confirmed and not report.rejected
    assert report.applied[0]["mode"] in ("hardlink", "copy")
    assert report.applied[0]["reason"] == "test"
    assert filed_path_of(prepared, "u1") == LEC
    assert report.undo_log is not None and report.undo_log.exists()
    assert json.loads(report.undo_log.read_text())["plan_id"] == "plan"


def test_nothing_is_applied_without_confirmation(
    prepared: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())

    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]))

    assert not report.confirmed
    assert not report.applied
    assert report.skipped[0]["status"] == "not confirmed"
    assert not (prepared.paths.workspace / LEC).exists()


def test_apply_undo_is_a_byte_identical_round_trip(prepared: Settings) -> None:
    source = add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    before = {
        p.relative_to(prepared.paths.workspace).as_posix(): p.read_bytes()
        for p in prepared.paths.workspace.rglob("*")
        if p.is_file() and "_agent" not in p.parts
    }

    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)
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

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", boom)
    report = organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)

    assert report.applied[0]["mode"] == "copy"
    dest = prepared.paths.workspace / LEC
    assert dest.exists() and dest.stat().st_nlink == 1


def test_symlink_mode_is_honoured(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    prepared.organize.link_mode = "symlink"
    organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)]), yes=True)
    dest = prepared.paths.workspace / LEC
    assert dest.is_symlink()
    organize.undo(prepared, None)
    assert not dest.exists() and not dest.is_symlink()


# --------------------------------------------------------------------------
# pre-existing files
# --------------------------------------------------------------------------
def _loose(settings: Settings, rel: str, body: bytes = b"mine") -> Path:
    path = settings.paths.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


HW = "Machine Learning/assignments/hw-01/15_095_hw1.pdf"


def test_preexisting_files_are_refused_without_include_existing(prepared: Settings) -> None:
    loose = _loose(prepared, "Machine Learning/HW1/15_095_hw1.pdf")
    plan = write_plan(prepared, [place("existing:Machine Learning/HW1/15_095_hw1.pdf", HW)])

    report = organize.apply_plan(prepared, plan, yes=True)

    assert not report.applied
    assert "--include-existing" in report.rejected[0]["reason"]
    assert loose.exists() and loose.read_bytes() == b"mine"


def test_preexisting_files_move_with_the_flag_and_undo_restores_them(prepared: Settings) -> None:
    loose = _loose(prepared, "Machine Learning/HW1/15_095_hw1.pdf", b"notebook bytes")
    plan = write_plan(prepared, [place("existing:Machine Learning/HW1/15_095_hw1.pdf", HW)])

    report = organize.apply_plan(prepared, plan, yes=True, include_existing=True)

    moved = prepared.paths.workspace / HW
    assert report.applied[0]["mode"] == "move"
    assert moved.exists() and not loose.exists()

    undone = organize.undo(prepared, "latest")
    assert not undone.refused
    assert loose.exists() and loose.read_bytes() == b"notebook bytes"
    assert not moved.exists()


@pytest.mark.parametrize(
    ("file_id", "why"),
    [
        ("existing:AI_Studio/nandatown/README.md", "ignore_globs"),
        ("existing:Machine Learning/nope.pdf", "does not exist"),
        ("existing:../outside.pdf", "workspace-relative"),
        ("existing:Optimization/notes.pdf", "mapped course folder"),
    ],
)
def test_bad_existing_sources_are_rejected(prepared: Settings, file_id: str, why: str) -> None:
    _loose(prepared, "AI_Studio/nandatown/README.md")
    report = organize.apply_plan(
        prepared,
        write_plan(prepared, [place(file_id, "Machine Learning/other/x.pdf")]),
        yes=True,
        include_existing=True,
    )
    assert not report.applied
    assert why in report.rejected[0]["reason"]


def test_undo_refuses_when_the_file_changed_since_apply(prepared: Settings) -> None:
    loose = _loose(prepared, "Machine Learning/HW1/15_095_hw1.pdf", b"original")
    plan = write_plan(prepared, [place("existing:Machine Learning/HW1/15_095_hw1.pdf", HW)])
    organize.apply_plan(prepared, plan, yes=True, include_existing=True)
    moved = prepared.paths.workspace / HW
    assert moved.exists()

    moved.write_bytes(b"edited since")
    report = organize.undo(prepared, "latest")

    assert report.refused and "changed" in report.refused[0]["reason"]
    assert moved.read_bytes() == b"edited since"
    assert not loose.exists()


def test_ignored_paths_cover_the_directory_itself(prepared: Settings) -> None:
    assert organize.is_ignored(prepared, "AI_Studio/nandatown")
    assert organize.is_ignored(prepared, "AI_Studio/nandatown/src/app.py")
    assert not organize.is_ignored(prepared, "AI_Studio/notes.pdf")


# --------------------------------------------------------------------------
# skips, replacing a filed copy, refiling under a new name
# --------------------------------------------------------------------------
def write_plan_doc(settings: Settings, doc: dict[str, Any], name: str = "plan") -> Path:
    path = settings.paths.plans_dir / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


def skip_reason_of(settings: Settings, uuid: str) -> str | None:
    with Manifest(settings.paths.manifest_db) as man:
        rec = man.get_file(uuid)
    return rec.skip_reason if rec else None


REG = "Machine Learning/lectures/Regression.pdf"


def test_a_skipped_file_is_not_offered_again_and_undo_brings_it_back(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="PreClass_Regression.pdf")
    plan = write_plan_doc(
        prepared, {"placements": [], "skips": [{"file_id": "101", "reason": "pre-class deck"}]}
    )

    report = organize.apply_plan(prepared, plan, yes=True)

    assert not report.rejected
    assert skip_reason_of(prepared, "u1") == "pre-class deck"
    assert organize.unfiled(prepared)["files"] == []

    organize.undo(prepared, "latest")
    assert skip_reason_of(prepared, "u1") is None
    assert [f["file_id"] for f in organize.unfiled(prepared)["files"]] == ["101"]


def test_skipping_a_filed_file_removes_its_copy_only_with_include_existing(
    prepared: Settings,
) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="a.pdf")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", REG)], "p1"), yes=True)
    plan = write_plan_doc(
        prepared, {"placements": [], "skips": [{"file_id": "101", "reason": "x"}]}, "p2"
    )

    refused = organize.validate_plan(prepared, plan)
    assert not refused.skips and refused.rejected[0]["destination"] == "-"
    assert "needs --include-existing" in refused.rejected[0]["reason"]

    organize.apply_plan(prepared, plan, yes=True, include_existing=True)
    dest = prepared.paths.workspace / REG
    assert not dest.exists()
    assert filed_path_of(prepared, "u1") is None and skip_reason_of(prepared, "u1") == "x"

    undone = organize.undo(prepared, "latest")
    assert not undone.refused and not undone.errors
    assert dest.exists()
    assert filed_path_of(prepared, "u1") == REG and skip_reason_of(prepared, "u1") is None


def test_a_post_class_deck_replaces_the_filed_pre_class_copy(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="pre", canvas_id=101, name="Pre.pdf", body=b"pre deck")
    add_mirror_file(prepared, uuid="post", canvas_id=102, name="Post.pdf", body=b"post deck")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", REG)], "p1"), yes=True)

    report = organize.apply_plan(
        prepared, write_plan(prepared, [place("102", REG)], "p2"), yes=True
    )

    dest = prepared.paths.workspace / REG
    assert not report.rejected and dest.read_bytes() == b"post deck"
    assert filed_path_of(prepared, "post") == REG
    assert filed_path_of(prepared, "pre") is None
    assert skip_reason_of(prepared, "pre") == f"replaced by 102 at {REG}"
    assert organize.unfiled(prepared)["files"] == []

    undone = organize.undo(prepared, "latest")

    assert not undone.refused and not undone.errors
    assert dest.read_bytes() == b"pre deck"
    assert filed_path_of(prepared, "pre") == REG and skip_reason_of(prepared, "pre") is None
    assert filed_path_of(prepared, "post") is None


def test_a_filed_copy_the_student_changed_is_never_replaced(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="pre", canvas_id=101, name="Pre.pdf", body=b"pre deck")
    add_mirror_file(prepared, uuid="post", canvas_id=102, name="Post.pdf", body=b"post deck")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", REG)], "p1"), yes=True)
    (prepared.paths.workspace / REG).write_bytes(b"pre deck + my annotations")

    report = organize.validate_plan(prepared, write_plan(prepared, [place("102", REG)], "p2"))

    assert not report.entries
    assert "changed since it was filed" in report.rejected[0]["reason"]


def test_renaming_a_filed_copy_needs_include_existing_and_undoes(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)], "p1"), yes=True)
    plan = write_plan(prepared, [place("101", REG)], "p2")

    refused = organize.validate_plan(prepared, plan)
    assert "needs --include-existing" in refused.rejected[0]["reason"]

    report = organize.apply_plan(prepared, plan, yes=True, include_existing=True)

    ws = prepared.paths.workspace
    assert report.applied[0]["mode"] == "refile"
    assert (ws / REG).exists() and not (ws / LEC).exists()
    assert filed_path_of(prepared, "u1") == REG

    organize.undo(prepared, "latest")
    assert (ws / LEC).exists() and not (ws / REG).exists()
    assert filed_path_of(prepared, "u1") == LEC


def test_a_filed_copy_cannot_be_moved_as_an_existing_file(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="Lec03_2026.pdf")
    organize.apply_plan(prepared, write_plan(prepared, [place("101", LEC)], "p1"), yes=True)

    plan = write_plan(prepared, [place(f"existing:{LEC}", REG)], "p2")
    report = organize.validate_plan(prepared, plan, include_existing=True)

    assert "place it by that file_id" in report.rejected[0]["reason"]


def test_unfiled_carries_module_item_titles_and_case_links(prepared: Settings) -> None:
    add_mirror_file(prepared, uuid="u1", canvas_id=101, name="InClass_Trees.pdf")
    meta = prepared.paths.canvas_mirror / "ML Mirror" / "_meta" / "modules.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    items = [
        {"id": 1, "type": "SubHeader", "title": "Lecture 5 - Regression Trees"},
        {"id": 2, "type": "File", "content_id": 101, "title": "PostClass CART Regression Slides"},
        {"id": 3, "type": "ExternalTool", "title": "OCP Group", "html_url": "https://c/items/3"},
    ]
    meta.write_text(
        json.dumps(
            {
                "fetched_at": "x",
                "course_canvas_id": 1,
                "items": [{"name": "Lectures 5 and 6", "items": items}],
            }
        )
    )

    doc = organize.unfiled(prepared)

    [f] = doc["files"]
    assert f["module_item_title"] == "PostClass CART Regression Slides"
    assert f["module_subheader"] == "Lecture 5 - Regression Trees"
    assert doc["links"] == [
        {
            "course": "Machine Learning",
            "title": "OCP Group",
            "module_name": "Lectures 5 and 6",
            "canvas_url": "https://c/items/3",
        }
    ]
