"""Gradescope: read-only client, dashboard parsing, course mapping, the `due` overlay.

The HTML below mirrors the student dashboard's markup (course boxes, the
`#assignments-student-table`, `time.submissionTimeChart--*` stamps). Nothing
here reaches gradescope.com: requests go to an `httpx.MockTransport`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mitsync.core.config import Settings
from mitsync.core.errors import GradescopeAuthError, GradescopeWriteRefused, MitsyncError
from mitsync.gradescope import client as gs
from mitsync.gradescope import snapshot
from mitsync.schedule import deadlines
from tests.test_deadlines import seeded  # noqa: F401  (fixture)

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)

ACCOUNT = """
<div class="courseList">
  <div class="courseList--term">Fall 2026</div>
  <div class="courseList--coursesForTerm">
    <a class="courseBox" href="/courses/111">
      <h3 class="courseBox--shortname">15.095</h3>
      <div class="courseBox--name">Machine Learning Under a Modern Optimization Lens</div>
    </a>
    <a class="courseBox" href="/courses/222">
      <h3 class="courseBox--shortname">6.7900</h3><div class="courseBox--name">Other</div>
    </a>
  </div>
</div>
"""


def _row(title: str, status: str, due: str, link: str | None = None) -> str:
    head = (
        f'<a href="{link}">{title}</a>'
        if link
        else f'<button data-assignment-id="9">{title}</button>'
    )
    return (
        f'<tr><th class="table--primaryLink">{head}</th>'
        f'<td class="submissionStatus"><div class="submissionStatus--text">{status}</div></td>'
        '<td><time class="submissionTimeChart--releaseDate" datetime="2026-09-01 09:00:00 -0400">'
        f'</time><time class="submissionTimeChart--dueDate" datetime="{due}"></time></td></tr>'
    )  # fmt: skip


COURSE = (
    '<table id="assignments-student-table"><tbody>'
    + _row("Problem Set 1", "8.0 / 10.0", "2026-09-20 23:59:00 -0400",
           "/courses/111/assignments/501/submissions/9001")
    + _row("Problem Set 2", "Submitted", "2026-10-02 23:59:00 -0400",
           "/courses/111/assignments/502/submissions/9002")
    + _row("Problem Set 3", "No Submission", "2026-10-09 23:59:00 -0400")
    + _row("Quiz 0", "No Submission", "2026-09-10 23:59:00 -0400")
    + "</tbody></table>"
)  # fmt: skip


def _transport(pages: dict[str, httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: pages[req.url.path])


# --------------------------------------------------------------------------
# read-only and auth
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_are_refused_at_the_chokepoint(method: str) -> None:
    client = gs.GradescopeClient("c=1", transport=_transport({}))
    with pytest.raises(GradescopeWriteRefused, match="read-only"):
        client._request(method, "/courses/111")


def test_a_login_redirect_means_the_cookie_expired() -> None:
    login = httpx.Response(302, headers={"location": "https://www.gradescope.com/login"})
    client = gs.GradescopeClient("c=1", transport=_transport({"/": login}))
    with pytest.raises(GradescopeAuthError, match="expired"):
        client.page("/")


def test_no_cookie_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRADESCOPE_COOKIE", raising=False)
    with pytest.raises(GradescopeAuthError, match="GRADESCOPE_COOKIE"):
        gs.GradescopeClient()


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def test_parse_courses_reads_boxes_and_terms() -> None:
    courses = gs.parse_courses(ACCOUNT)
    assert [c["gradescope_id"] for c in courses] == ["111", "222"]
    assert courses[0]["shortname"] == "15.095"
    assert courses[0]["term"] == "Fall 2026"


def test_parse_assignments_maps_every_status() -> None:
    rows = {a.title: a for a in gs.parse_assignments(COURSE, NOW)}
    assert rows["Problem Set 1"].status == "graded"
    assert (rows["Problem Set 1"].score, rows["Problem Set 1"].points) == (8.0, 10.0)
    assert rows["Problem Set 1"].gradescope_id == "501"
    assert rows["Problem Set 2"].status == "submitted"
    assert rows["Problem Set 3"].status == "unsubmitted"  # not due yet
    assert rows["Problem Set 3"].gradescope_id == "9"  # from the submit button
    assert rows["Quiz 0"].status == "missing"  # past due, nothing handed in
    assert rows["Problem Set 2"].due_at == datetime(2026, 10, 3, 3, 59, tzinfo=UTC)


def test_a_changed_layout_raises_instead_of_reading_as_nothing_due() -> None:
    with pytest.raises(MitsyncError, match="no student assignment table"):
        gs.parse_assignments("<html><body>new layout</body></html>", NOW)
    assert gs.parse_assignments("You don't have any assignments yet", NOW) == []


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("15.C57 - HW 1: Linear Optimization - Fall 2026", "HW 1"),
        ("Homework 01", "hw-01"),
        ("Problem Set 3", "pset3"),
        ("15.C57 - Midterm 2 - Fall 2026", "Midterm 2"),
        ("Term Project: Slides - Fall 2026", "term project slides"),
    ],
)
def test_title_keys_join_canvas_gradescope_and_folders(a: str, b: str) -> None:
    assert snapshot.title_key(a) == snapshot.title_key(b)


def test_title_keys_keep_different_numbers_apart() -> None:
    assert snapshot.title_key("HW 1") != snapshot.title_key("HW 10")
    assert snapshot.title_key("Midterm 1") != snapshot.title_key("HW 1")


# --------------------------------------------------------------------------
# sync and the due overlay
# --------------------------------------------------------------------------
def test_sync_fetches_mapped_courses_only_and_writes_the_snapshot(seeded: Settings) -> None:  # noqa: F811
    pages = {
        "/": httpx.Response(200, text=ACCOUNT),
        "/courses/111": httpx.Response(200, text=COURSE),
    }
    snap = gs.sync(seeded, gs.GradescopeClient("c=1", transport=_transport(pages)))
    by_id = {c.gradescope_id: c for c in snap.courses}
    assert by_id["111"].folder == "Machine Learning"  # matched on course_number
    assert by_id["222"].folder is None and by_id["222"].assignments == []
    assert snapshot.load(seeded) == snap
    assert json.loads(snapshot.path(seeded).read_text())["courses"][0]["assignments"]


def test_gradescope_status_wins_on_the_matching_canvas_row(seeded: Settings) -> None:  # noqa: F811
    due = datetime.now(UTC) + timedelta(days=2)
    snap = snapshot.Snapshot(
        fetched_at=NOW,
        courses=[
            snapshot.GsCourse(
                gradescope_id="111", shortname="15.095", name="ML", folder="Machine Learning",
                assignments=[
                    snapshot.GsAssignment(gradescope_id="501", title="PS 1", status="graded",
                                          score=9, points=10, due_at=due),
                    snapshot.GsAssignment(gradescope_id="503", title="Recitation Quiz 7",
                                          status="submitted", due_at=due),
                ],
            )
        ],
    )  # fmt: skip
    snapshot.path(seeded).write_text(snap.model_dump_json())
    items = {i["title"]: i for i in deadlines.build_due(seeded).items}

    ps1 = items["Problem Set 1"]  # Canvas says unsubmitted
    assert ps1["status"] == "graded, 9/10"
    assert ps1["submitted"] is True
    assert ps1["source"] == "canvas/assignments+gradescope"
    assert "PS 1" not in items  # joined, not duplicated

    extra = items["Recitation Quiz 7"]  # on Gradescope only
    assert extra["source"] == "gradescope"
    assert extra["status"] == "submitted"


def test_due_warns_when_the_cookie_is_set_but_sync_never_ran(
    seeded: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRADESCOPE_COOKIE", "c=1")
    warnings = deadlines.build_due(seeded).warnings
    assert any("gradescope sync" in w for w in warnings)
