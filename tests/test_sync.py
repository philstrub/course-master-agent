"""
# Sync Tests

Mirroring Canvas end to end against a fake Canvas, and the invariants the
mirror has to keep.

`FakeCanvas` serves the fixtures under `tests/fixtures/canvas/` through
`httpx.MockTransport`, with switches for the failure shapes that matter
(`files_403`, `pages_404`). Term dates in the fixtures are placeholders
rewritten relative to today at import, so the "current term" tests do not rot.
Nothing here touches the network or a real token.

What the file is really guarding:

* **Idempotence and catch-up.** A second run downloads nothing, a missing
  mirror file is re-fetched, `--full` re-checks everything without creating
  spurious versions, and a file whose bytes changed is re-downloaded with the
  previous version kept beside it rather than overwritten.
* **State ownership.** `first_seen` is stable across runs and a `filed_path`
  written by `organize` survives a re-sync.
* **Partial failure is not total failure.** A 403 on Files falls back to
  walking modules, a 404 from a disabled Pages tab is a notice rather than an
  error, and a download failure is reported on the run rather than raised --
  but a run that recorded errors still exits non-zero.
* **Course selection.** Explicit terms, the dateless "Default Term" rule, and
  `canvas.exclude_courses`.
* **The metadata contract.** The `_meta` files everything downstream reads,
  including announcements fetched over explicit term-spanning dates and a
  planner written once globally.

Dry-run tests assert that nothing at all is written, which is the property the
whole review-before-apply design depends on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from mitsync.canvas.client import CanvasClient
from mitsync.canvas.manifest import Manifest
from mitsync.canvas.sync import course_folder_name, run_sync
from mitsync.core.errors import CanvasAuthError

FIXTURES = Path(__file__).parent / "fixtures" / "canvas"
BASE = "https://canvas.mit.edu/api/v1"
ML = "Machine Learning"
ALGO = "Intro to Algorithms - Recitation"

_TODAY = datetime.now(UTC).date()
_TERM_START = (_TODAY - timedelta(days=30)).isoformat() + "T04:00:00Z"
_TERM_END = (_TODAY + timedelta(days=60)).isoformat() + "T04:00:00Z"


def fixture(name: str):
    raw = (FIXTURES / f"{name}.json").read_text()
    raw = raw.replace("__TERM_START__", _TERM_START).replace("__TERM_END__", _TERM_END)
    return json.loads(raw)


BODIES = {
    5001: b"%PDF-1.4 lecture one!!!\n",
    5002: b"%PDF-1.4 syllabus\n",
    5003: b"%PDF-1.4 week two notes\n",
}


class FakeCanvas:
    """A Canvas instance served from the JSON fixtures, via httpx.MockTransport."""

    def __init__(self, *, files_403: bool = False, pages_404: bool = False) -> None:
        self.files_403 = files_403
        self.pages_404 = pages_404
        self.files = {
            5001: fixture("files_page1")[0],
            5002: fixture("files_page2")[0],
            5003: fixture("file_5003"),
        }
        self.bodies = dict(BODIES)
        self.paths: list[str] = []
        self.downloaded: list[int] = []
        self.announcement_params: dict[str, str] = {}

    # -- mutation helpers used by tests --------------------------------
    def change_file(self, file_id: int, body: bytes, updated_at: str) -> None:
        self.files[file_id]["updated_at"] = updated_at
        self.bodies[file_id] = body

    # -- the transport -------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        self.paths.append(path)

        if request.url.host != "canvas.mit.edu":
            file_id = int(path.strip("/").split("/")[-1])
            self.downloaded.append(file_id)
            return httpx.Response(200, content=self.bodies[file_id])

        if path == "/api/v1/courses":
            if params.get("page") is None:
                return httpx.Response(
                    200,
                    json=fixture("courses_page1"),
                    headers={"Link": f'<{BASE}/courses?page=2&per_page=100>; rel="next"'},
                )
            return httpx.Response(200, json=fixture("courses_page2"))

        parts = path.strip("/").split("/")  # api, v1, ...
        if parts[2:4] == ["courses", "28451"]:
            tail = parts[4] if len(parts) > 4 else ""
            if tail == "folders":
                return httpx.Response(200, json=fixture("folders"))
            if tail == "files":
                if self.files_403:
                    return httpx.Response(403, json=fixture("files_403"))
                if params.get("page") is None:
                    return httpx.Response(
                        200,
                        json=[self.files[5001]],
                        headers={
                            "Link": f'<{BASE}/courses/28451/files?page=2&per_page=100>; rel="next"'
                        },
                    )
                return httpx.Response(200, json=[self.files[5002]])
            if tail == "modules":
                return httpx.Response(200, json=fixture("modules"))
            if tail == "assignments":
                return httpx.Response(200, json=fixture("assignments"))
            if tail == "pages":
                if self.pages_404:
                    # A course with the Pages feature switched off.
                    return httpx.Response(404, json=fixture("pages_disabled_404"))
                return httpx.Response(200, json=fixture("pages"))

        if parts[2] == "courses" and parts[3] == "28452":
            return httpx.Response(200, json=[])

        if parts[2] == "files":
            return httpx.Response(200, json=self.files[int(parts[3])])

        if path == "/api/v1/announcements":
            self.announcement_params = {
                "start_date": params.get("start_date", ""),
                "end_date": params.get("end_date", ""),
                "context_codes[]": ",".join(params.get_list("context_codes[]")),
            }
            return httpx.Response(200, json=fixture("announcements"))

        if path == "/api/v1/planner/items":
            return httpx.Response(200, json=fixture("planner"))

        return httpx.Response(200, json=[])


def sync(settings, fake: FakeCanvas, **kw):
    client = CanvasClient(
        settings,
        token="test-token",
        transport=httpx.MockTransport(fake.handler),
        sleep=lambda _s: None,
    )
    try:
        return run_sync(settings, client=client, **kw)
    finally:
        client.close()


def mirror(settings) -> Path:
    return settings.paths.canvas_mirror


def mirrored_files(settings) -> set[str]:
    root = mirror(settings)
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and "_meta" not in p.relative_to(root).parts
    }


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_sync_mirrors_files_and_records_them(settings):
    fake = FakeCanvas()
    report = sync(settings, fake)

    assert report.errors == []
    assert set(report.courses) == {ML, ALGO}
    assert report.new == 3
    assert report.updated == 0
    assert report.bytes_downloaded == sum(len(BODIES[i]) for i in (5001, 5002, 5003))

    assert mirrored_files(settings) == {
        f"{ML}/Lectures/lecture1.pdf",
        f"{ML}/syllabus.pdf",
        f"{ML}/Lectures/week2-notes.pdf",
    }
    assert (mirror(settings) / ML / "Lectures" / "lecture1.pdf").read_bytes() == BODIES[5001]


def test_manifest_rows_carry_module_and_folder_info(settings):
    sync(settings, FakeCanvas())
    with Manifest(settings.paths.manifest_db) as m:
        rows = {r.uuid: r for r in m.list_files()}

    assert set(rows) == {"uuid-5001", "uuid-5002", "uuid-5003"}
    lec = rows["uuid-5001"]
    assert lec.course_folder == ML
    assert lec.course_canvas_id == 28451
    assert lec.canvas_folder == "Lectures"  # "course files/" prefix stripped
    assert lec.module_name == "Week 1 - Foundations"
    assert lec.module_position == 1
    assert lec.mirror_path == f"_canvas/{ML}/Lectures/lecture1.pdf"
    assert lec.filed_path is None
    assert lec.content_type == "application/pdf"
    assert len(lec.sha256) == 64
    assert rows["uuid-5002"].canvas_folder is None  # root folder
    assert rows["uuid-5003"].module_name == "Week 2 - Linear Models"
    assert rows["uuid-5003"].module_position == 2


def test_modules_are_walked_even_when_files_succeeds(settings):
    """5003 is reachable only through a module item, and must still be mirrored."""
    fake = FakeCanvas()
    sync(settings, fake)
    assert "/api/v1/courses/28451/modules" in fake.paths
    assert 5003 in fake.downloaded


def test_courses_are_recorded(settings):
    sync(settings, FakeCanvas())
    with Manifest(settings.paths.manifest_db) as m:
        courses = {c.canvas_id: c for c in m.list_courses()}
    assert set(courses) == {28451, 28452}
    assert courses[28451].folder == ML
    assert courses[28451].course_code == "6.7900"
    assert courses[28451].term_name == "2026 Fall"


def test_run_is_recorded(settings):
    report = sync(settings, FakeCanvas())
    with Manifest(settings.paths.manifest_db) as m:
        last = m.last_run("sync")
    assert last is not None
    assert last["stats"]["new"] == report.new
    assert last["started"] <= last["finished"]


def test_report_renders(settings):
    report = sync(settings, FakeCanvas())
    table = report.render()
    assert table.row_count > 0


# --------------------------------------------------------------------------
# term filtering, course selection, naming
# --------------------------------------------------------------------------
def test_only_current_term_courses_are_synced(settings):
    """The 2024 Spring course in the fixture must be filtered out client-side."""
    sync(settings, FakeCanvas())
    assert not (mirror(settings) / "Ancient History").exists()
    with Manifest(settings.paths.manifest_db) as m:
        assert 11111 not in {c.canvas_id for c in m.list_courses()}


def test_named_term_matches_on_term_name(settings):
    settings.canvas.term = "2024 Spring"
    fake = FakeCanvas()
    report = sync(settings, fake)
    assert report.courses == ["Ancient History"]


def test_named_term_with_no_match_syncs_nothing(settings):
    settings.canvas.term = "1999 Summer"
    report = sync(settings, FakeCanvas())
    assert report.courses == []
    assert report.new == 0


@pytest.mark.parametrize("needle", [ML, "6.7900", "machine learning"])
def test_course_filter_matches_folder_name_or_code(settings, needle):
    report = sync(settings, FakeCanvas(), course=needle)
    assert report.courses == [ML]
    assert not (mirror(settings) / ALGO).exists()


def test_unknown_course_filter_reports_an_error(settings):
    report = sync(settings, FakeCanvas(), course="Underwater Basket Weaving")
    assert report.new == 0
    assert report.errors and "no current-term course matches" in report.errors[0]["message"]


def test_course_folder_name_is_filesystem_safe():
    assert course_folder_name("Intro to Algorithms / Recitation") == ALGO
    assert course_folder_name("A:B*C?D") == "A-B-C-D"
    assert course_folder_name("  spaced   out  ") == "spaced out"
    assert course_folder_name("", 99) == "course-99"


# --------------------------------------------------------------------------
# hidden Files tab
# --------------------------------------------------------------------------
def test_403_on_files_falls_back_to_modules(settings):
    fake = FakeCanvas(files_403=True)
    report = sync(settings, fake)

    # 5001 and 5003 are reachable through modules; 5002 (root folder) is not.
    assert sorted(fake.downloaded) == [5001, 5003]
    assert mirrored_files(settings) == {
        f"{ML}/Lectures/lecture1.pdf",
        f"{ML}/Lectures/week2-notes.pdf",
    }
    assert report.new == 2
    # A hidden Files tab is expected, not a failure: it is a notice, and the
    # Modules fallback covers it.
    assert report.errors == []
    stages = {n["stage"] for n in report.notices}
    assert "files" in stages
    msg = next(n["message"] for n in report.notices if n["stage"] == "files")
    assert "module-derived" in msg


# --------------------------------------------------------------------------
# disabled Pages feature (404, not 403)
# --------------------------------------------------------------------------
def test_pages_disabled_404_is_a_notice_not_an_error(settings):
    fake = FakeCanvas(pages_404=True)
    report = sync(settings, fake)

    # The whole point: a disabled feature must not pollute the error count.
    assert report.errors == []
    pages_notices = [n for n in report.notices if n["stage"] == "pages"]
    assert [n["course"] for n in pages_notices] == [ML]
    assert "disabled" in pages_notices[0]["message"]
    assert report.as_dict()["notices"] == report.notices

    # ...and the course is otherwise synced exactly as normal.
    assert report.new == 3
    assert mirrored_files(settings) == {
        f"{ML}/Lectures/lecture1.pdf",
        f"{ML}/syllabus.pdf",
        f"{ML}/Lectures/week2-notes.pdf",
    }
    pages_meta = json.loads((mirror(settings) / ML / "_meta" / "pages.json").read_text())
    assert pages_meta["items"] == []
    # Later stages still run: announcements come after pages in the fetch list.
    assert (mirror(settings) / ML / "_meta" / "announcements.json").exists()


def test_notices_render_without_counting_as_errors(settings):
    report = sync(settings, FakeCanvas(pages_404=True))
    table = report.render()
    assert table.row_count > 0
    assert len(report.notices) >= 1


# --------------------------------------------------------------------------
# incremental behaviour
# --------------------------------------------------------------------------
def test_second_run_downloads_nothing(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    fake.downloaded.clear()

    report = sync(settings, fake)
    assert fake.downloaded == []
    assert report.new == 0
    assert report.updated == 0
    assert report.unchanged == 3
    assert report.bytes_downloaded == 0


def test_missing_mirror_file_is_re_fetched(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    (mirror(settings) / ML / "syllabus.pdf").unlink()
    fake.downloaded.clear()

    report = sync(settings, fake)
    assert fake.downloaded == [5002]
    # The bytes came back identical, so it counts as unchanged -- but it is
    # back on disk, which is the point: work is derived from actual state.
    assert report.unchanged == 3
    assert (mirror(settings) / ML / "syllabus.pdf").read_bytes() == BODIES[5002]


def test_changed_file_is_redownloaded_and_old_version_kept(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    fake.downloaded.clear()
    fake.change_file(5001, b"%PDF-1.4 lecture one, revised\n", "2026-10-05T08:00:00Z")

    report = sync(settings, fake)
    assert fake.downloaded == [5001]
    assert report.updated == 1
    assert report.unchanged == 2

    lectures = mirror(settings) / ML / "Lectures"
    assert (lectures / "lecture1.pdf").read_bytes() == b"%PDF-1.4 lecture one, revised\n"
    archived = [p for p in lectures.iterdir() if p.name.startswith("lecture1.v")]
    assert len(archived) == 1, "the previous version must be preserved"
    assert archived[0].suffix == ".pdf"
    assert archived[0].read_bytes() == BODIES[5001]

    with Manifest(settings.paths.manifest_db) as m:
        row = m.get_file("uuid-5001")
    assert row.updated_at == "2026-10-05T08:00:00Z"


def test_full_rechecks_everything_without_creating_spurious_versions(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    fake.downloaded.clear()

    report = sync(settings, fake, full=True)
    assert sorted(fake.downloaded) == [5001, 5002, 5003]
    assert report.unchanged == 3
    assert not list((mirror(settings) / ML / "Lectures").glob("*.v*"))


def test_first_seen_is_stable_across_runs(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    with Manifest(settings.paths.manifest_db) as m:
        first = m.get_file("uuid-5001").first_seen

    fake.change_file(5001, b"changed\n", "2026-11-01T00:00:00Z")
    sync(settings, fake)
    with Manifest(settings.paths.manifest_db) as m:
        row = m.get_file("uuid-5001")
    assert row.first_seen == first
    assert row.last_synced >= first


def test_filed_path_set_by_organize_survives_a_resync(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    with Manifest(settings.paths.manifest_db) as m:
        m.set_filed_path("uuid-5001", "Machine Learning/lectures/Lecture 1.pdf")

    fake.change_file(5001, b"changed\n", "2026-11-02T00:00:00Z")
    sync(settings, fake)
    with Manifest(settings.paths.manifest_db) as m:
        assert m.get_file("uuid-5001").filed_path == "Machine Learning/lectures/Lecture 1.pdf"


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------
def test_dry_run_writes_nothing(settings):
    fake = FakeCanvas()
    report = sync(settings, fake, dry_run=True)

    assert report.dry_run is True
    assert report.new == 3
    assert fake.downloaded == []
    assert mirrored_files(settings) == set()
    assert not settings.paths.manifest_db.exists()
    assert list(mirror(settings).rglob("*.json")) == []


def test_dry_run_after_a_real_run_reports_only_the_delta(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    fake.change_file(5002, b"new syllabus\n", "2026-10-10T00:00:00Z")
    fake.downloaded.clear()

    report = sync(settings, fake, dry_run=True)
    assert report.new == 0
    assert report.updated == 1
    assert report.unchanged == 2
    assert fake.downloaded == []
    assert (mirror(settings) / ML / "syllabus.pdf").read_bytes() == BODIES[5002]


# --------------------------------------------------------------------------
# ignore globs
# --------------------------------------------------------------------------
def test_ignore_globs_are_honored_against_the_mirror_path(settings):
    settings.ignore_globs.append(f"_canvas/{ML}/Lectures/**")
    fake = FakeCanvas()
    report = sync(settings, fake)

    assert fake.downloaded == [5002]
    assert report.skipped == 2
    assert report.new == 1
    assert mirrored_files(settings) == {f"{ML}/syllabus.pdf"}


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------
def test_per_course_meta_files_are_written(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    meta = mirror(settings) / ML / "_meta"

    for name in ("courses", "modules", "assignments", "pages", "announcements"):
        payload = json.loads((meta / f"{name}.json").read_text())
        assert set(payload) == {"fetched_at", "course_canvas_id", "items"}
        assert payload["course_canvas_id"] == 28451
        assert isinstance(payload["items"], list)

    course = json.loads((meta / "courses.json").read_text())["items"][0]
    assert course["mirror_folder"] == ML
    assert course["mirror_path"] == f"_canvas/{ML}"

    modules = json.loads((meta / "modules.json").read_text())["items"]
    assert [m["position"] for m in modules] == [1, 2]
    assert {i["type"] for i in modules[0]["items"]} >= {"File", "Page", "SubHeader"}


def test_announcements_use_explicit_term_spanning_dates(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    params = fake.announcement_params
    assert params["start_date"] and params["end_date"]
    assert params["start_date"] <= _TERM_START[:10]
    assert params["end_date"] >= _TERM_END[:10]
    assert "course_" in params["context_codes[]"]


def test_planner_is_written_once_globally(settings):
    fake = FakeCanvas()
    sync(settings, fake)
    payload = json.loads((mirror(settings) / "_meta" / "planner.json").read_text())
    assert payload["course_canvas_id"] is None
    assert payload["items"][0]["plannable"]["title"] == "Problem Set 1"
    assert fake.paths.count("/api/v1/planner/items") == 1


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------
def test_missing_token_raises_a_typed_error_with_remediation(settings, monkeypatch):
    """`run_sync` is a library call: it raises, it never exits the process.

    `cli.run()` is the single place a `MitsyncError` becomes an exit code, so
    tests and OpenClaw skills that call `run_sync` directly can catch this.
    """
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    with pytest.raises(CanvasAuthError, match="CANVAS_TOKEN"):
        run_sync(settings, dry_run=True)


def test_download_failure_is_reported_not_raised(settings):
    fake = FakeCanvas()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "canvas.mit.edu":
            return httpx.Response(500, content=b"boom")
        return fake.handler(request)

    client = CanvasClient(
        settings,
        token="t",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    try:
        report = run_sync(settings, client=client)
    finally:
        client.close()

    assert report.new == 0
    assert {e["stage"] for e in report.errors} == {"download"}
    assert mirrored_files(settings) == set()


# --------------------------------------------------------------------------
# course selection, end to end
# --------------------------------------------------------------------------
ORIENTATION = {
    "id": 17557,
    "name": "F-1 Immigration Orientation eCourse",
    "course_code": "F1-ORIENT",
    "term": {"id": 1, "name": "Default Term", "start_at": None, "end_at": None},
}


class CanvasWithOrientation(FakeCanvas):
    """FakeCanvas plus one dateless-term administrative course."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses" and request.url.params.get("page") is not None:
            return httpx.Response(200, json=[*fixture("courses_page2"), ORIENTATION])
        return super().handler(request)


def test_dateless_term_course_is_excluded_and_reported(settings):
    report = sync(settings, CanvasWithOrientation(), dry_run=True)

    assert set(report.courses) == {ML, ALGO}
    assert ORIENTATION["name"] not in report.courses
    assert report.excluded == [
        {
            "canvas_id": "17557",
            "name": ORIENTATION["name"],
            "reason": "term has no start/end dates",
        }
    ]


def test_exclude_courses_setting_drops_a_course_before_any_work(settings):
    settings.canvas.exclude_courses = ["intro to algorithms"]
    fake = FakeCanvas()
    report = sync(settings, fake, dry_run=True)

    assert set(report.courses) == {ML}
    assert [e["reason"] for e in report.excluded] == ["excluded by config"]
    assert "/api/v1/courses/28452/modules" not in fake.paths


def test_a_sync_that_recorded_errors_exits_nonzero(monkeypatch, workspace):
    """A recorded error must not exit 0.

    scripts/mitsync-cron.sh branches on the exit code. When course discovery
    failed, run_sync filed the failure as an error row and the CLI still exited
    0, so a revoked Canvas token reported "Completed with no errors" on every
    scheduled run instead of alerting.
    """
    from typer.testing import CliRunner

    from mitsync import cli
    from mitsync.canvas.sync import SyncReport

    report = SyncReport(dry_run=True)
    report.add_error("-", "courses", "Canvas rejected the token (401)")
    monkeypatch.setattr(cli.sync_mod, "run_sync", lambda *a, **k: report)

    result = CliRunner().invoke(cli.app, ["sync", "--dry-run"])
    assert result.exit_code == 1
