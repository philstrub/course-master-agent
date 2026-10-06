"""The forum agent's bounds hold in code, whatever the decision file says.

Every test runs against `FakeCanvas`, an in-memory discussion behind an httpx
mock transport: it stores posted entries, so a test can count exactly how many
posts landed. That count is the point of the recovery tests (a lost
acknowledgement or a crash after the POST must never produce a second post) and
of the control-line, rate-limit and stopping-rule tests (a refusal must happen
before anything is sent).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from mitsync.core.errors import ForumRefused, MitsyncError, ScholarBlocked
from mitsync.forum import discussion, knowledge, scholar
from mitsync.forum.discussion import TOPIC

ME, ALICE, BOB = 1, 2, 3
API = "/api/v1"


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class FakeCanvas:
    """One discussion topic: a control line, entries, and switches for failures."""

    def __init__(self) -> None:
        self.control = "RUNNING"
        self.entries: list[dict] = [
            {"id": 10, "user_id": ALICE, "parent_id": None, "created_at": iso(120),
             "message": "<p>Should an agent ask before acting when it judges the risk low?</p>"},
            {"id": 11, "user_id": BOB, "parent_id": 10, "created_at": iso(60),
             "message": "<p>Reversibility, not risk, should decide when an agent asks first.</p>"},
        ]  # fmt: skip
        self.posts = 0  # POST requests received, landed or not
        self.fail_next = 0  # answer this many POSTs with 503 without saving
        self.lose_ack = False  # save the next POST, then time out instead of answering
        self.pause_after_post = False

    def entry(self, entry_id: int) -> dict:
        return next(e for e in self.entries if e["id"] == entry_id)

    def tree(self, parent: int | None) -> list[dict]:
        return [
            {**e, "replies": self.tree(e["id"])} for e in self.entries if e["parent_id"] == parent
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(API)
        if request.method == "GET" and path == "/users/self":
            return httpx.Response(200, json={"id": ME})
        if request.method == "GET" and path == TOPIC:
            return httpx.Response(
                200, json={"message": f"<p>COURSE-TEAM CONTROL: {self.control}</p><p>Purpose…</p>"}
            )
        if request.method == "GET" and path == f"{TOPIC}/view":
            people = [
                {"id": i, "display_name": n}
                for i, n in ((ME, "Me"), (ALICE, "Alice"), (BOB, "Bob"))
            ]
            return httpx.Response(
                200, json={"participants": people, "view": self.tree(None), "new_entries": []}
            )
        if request.method == "GET" and path == f"{TOPIC}/entry_list":
            ids = {int(v) for v in request.url.params.get_list("ids[]")}
            return httpx.Response(200, json=[e for e in self.entries if e["id"] in ids])
        if request.method == "POST" and path.startswith(f"{TOPIC}/entries"):
            self.posts += 1
            if self.pause_after_post:
                self.control = "PAUSED"
            if self.fail_next:
                self.fail_next -= 1
                return httpx.Response(503, json={"message": "unavailable"})
            parent = int(path.split("/")[-2]) if path.endswith("/replies") else None
            message = dict(httpx.QueryParams(request.content.decode()))["message"]
            entry = {"id": 100 + len(self.entries), "user_id": ME, "parent_id": parent,
                     "created_at": iso(), "message": message}  # fmt: skip
            self.entries.append(entry)
            if self.lose_ack:
                self.lose_ack = False
                raise httpx.ReadTimeout("acknowledgement lost", request=request)
            return httpx.Response(200, json=entry)
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


@pytest.fixture
def canvas(monkeypatch) -> FakeCanvas:
    monkeypatch.setenv("CANVAS_TOKEN", "test-token")
    monkeypatch.delenv("MITSYNC_FORUM_FAULT", raising=False)
    return FakeCanvas()


MESSAGE = (
    "Bob, reversibility is a good axis, but it hides a measurement problem. An operations "
    "researcher would call your rule a chance constraint: act alone only if the probability "
    "that the action cannot be undone stays below a threshold. That needs a model of undo cost, "
    "which most agents do not have. A cheaper proxy is to log every action with its inverse "
    "before acting, and ask only when no inverse exists."
)


def decide(settings, **fields) -> Path:
    doc = {
        "action": "reply", "reply_to": 11, "message": MESSAGE,
        "reason": "Brings the chance-constraint view from optimization to the thread.",
        "summary": "The thread debates whether risk or reversibility should gate autonomy.",
        "threads": [{"entry_id": 11, "gist": "Bob: reversibility decides when to ask."}],
        "sources": ["Optimization, Lecture 6"],
    } | fields  # fmt: skip
    doc = {k: v for k, v in doc.items() if v is not None}
    path = settings.paths.kb_forum / "decisions" / f"d{len(list(path_glob(settings)))}.json"
    path.write_text(json.dumps(doc))
    return path


def path_glob(settings):
    return (settings.paths.kb_forum / "decisions").glob("*.json")


def act(settings, canvas, path, **kw):
    return discussion.act(
        settings, path, sleep=lambda s: None, transport=httpx.MockTransport(canvas.handler), **kw
    )


def read(settings, canvas, **kw):
    return discussion.read(settings, transport=httpx.MockTransport(canvas.handler), **kw)


def state(settings) -> dict:
    return json.loads((settings.paths.forum_state / "state.json").read_text())


def posts_log(settings) -> list[dict]:
    path = settings.paths.kb_forum / "posts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --------------------------------------------------------------------------
# reading, skipping, posting
# --------------------------------------------------------------------------
def test_read_shows_unseen_entries_with_context_and_a_skip_marks_them_seen(settings, canvas):
    doc = read(settings, canvas)
    assert [e["id"] for e in doc["new_entries"]] == [10, 11]
    assert doc["new_entries"][1]["context"][0]["author"] == "Alice"
    assert doc["you"]["can_post"] and doc["you"]["posts_left_this_hour"] == 3

    out = act(settings, canvas, decide(settings, action="skip", reply_to=None, message=None))
    assert out["action"] == "skip" and canvas.posts == 0
    assert read(settings, canvas)["new_entries"] == []
    diary = (settings.paths.kb_forum / "diary.md").read_text()
    assert "chose not to post" in diary and "Bob: reversibility" in diary


def test_a_reply_is_posted_once_read_back_and_logged(settings, canvas):
    read(settings, canvas)
    out = act(settings, canvas, decide(settings))
    assert out["verified"] and out["attempts"] == 1 and canvas.posts == 1
    posted = canvas.entry(out["entry_id"])
    assert posted["parent_id"] == 11 and posted["message"].startswith("<p>Bob, reversibility")
    [logged] = posts_log(settings)
    assert logged["entry_id"] == out["entry_id"] and logged["verified"]
    assert "replied to Bob (entry 11)" in (settings.paths.kb_forum / "diary.md").read_text()
    assert state(settings)["intent"] is None and out["entry_id"] in state(settings)["seen"]


def test_dry_run_checks_everything_and_sends_nothing(settings, canvas):
    out = act(settings, canvas, decide(settings), dry_run=True)
    assert out["dry_run"] and out["would_post_to"].endswith(f"{TOPIC}/entries/11/replies")
    assert canvas.posts == 0 and posts_log(settings) == []


# --------------------------------------------------------------------------
# the control line, the hourly limit, the stopping rule
# --------------------------------------------------------------------------
def test_a_paused_forum_is_never_posted_to(settings, canvas):
    canvas.control = "PAUSED"
    with pytest.raises(ForumRefused, match="PAUSED"):
        act(settings, canvas, decide(settings))
    assert canvas.posts == 0


def test_the_control_line_is_read_again_before_a_retry(settings, canvas):
    canvas.fail_next, canvas.pause_after_post = 1, True
    with pytest.raises(ForumRefused, match="PAUSED"):
        act(settings, canvas, decide(settings))
    assert canvas.posts == 1  # the retry never went out


def test_no_more_than_three_posts_an_hour(settings, canvas):
    for i in range(3):
        earlier = {"id": 50 + i, "user_id": ME, "parent_id": 10, "created_at": iso(10 * i)}
        canvas.entries.append(earlier | {"message": f"<p>earlier post {i}</p>"})
    with pytest.raises(ForumRefused, match="3 posts in the last hour"):
        act(settings, canvas, decide(settings))
    assert canvas.posts == 0


def test_it_stops_after_three_failed_posts_until_a_human_resets(settings, canvas):
    for n in range(1, 4):
        canvas.fail_next = discussion.POST_ATTEMPTS
        with pytest.raises(MitsyncError, match="post failed 3 times"):
            act(settings, canvas, decide(settings))
        assert state(settings)["failures"] == n
    assert canvas.posts == 9 and posts_log(settings) == []
    with pytest.raises(ForumRefused, match="stopped after 3 consecutive failed posts"):
        act(settings, canvas, decide(settings))
    assert canvas.posts == 9
    assert discussion.reset(settings)["was"] == 3
    assert act(settings, canvas, decide(settings))["verified"]


def test_a_transient_failure_is_retried_with_backoff(settings, canvas):
    canvas.fail_next = 2
    slept: list[float] = []
    out = discussion.act(settings, decide(settings), sleep=slept.append,
                         transport=httpx.MockTransport(canvas.handler))  # fmt: skip
    assert out["attempts"] == 3 and canvas.posts == 3 and len(slept) == 2 and slept[1] > slept[0]
    assert len([e for e in canvas.entries if e["user_id"] == ME]) == 1


# --------------------------------------------------------------------------
# recovery: never a duplicate
# --------------------------------------------------------------------------
def test_a_lost_acknowledgement_is_reconciled_not_reposted(settings, canvas):
    canvas.lose_ack = True
    out = act(settings, canvas, decide(settings))
    assert out["reconciled"] and out["verified"] and canvas.posts == 1
    assert len([e for e in canvas.entries if e["user_id"] == ME]) == 1
    assert posts_log(settings)[0]["recovered"] == "lost acknowledgement"


def test_a_crash_after_the_post_is_recovered_by_the_next_run(settings, canvas, monkeypatch):
    read(settings, canvas)
    monkeypatch.setenv("MITSYNC_FORUM_FAULT", "crash")
    with pytest.raises(SystemExit):
        act(settings, canvas, decide(settings))
    assert canvas.posts == 1 and state(settings)["intent"] is not None and posts_log(settings) == []

    monkeypatch.delenv("MITSYNC_FORUM_FAULT")
    doc = read(settings, canvas)  # the next run starts
    assert doc["recovered"]["on_canvas"] and posts_log(settings)[0]["recovered"] == "after restart"
    assert state(settings)["intent"] is None
    with pytest.raises(ForumRefused, match="already replied to entry 11"):
        act(settings, canvas, decide(settings))  # the same decision again is refused
    assert canvas.posts == 1


def test_an_interrupted_post_that_never_landed_is_dropped(settings, canvas):
    canvas.fail_next = 1
    read(settings, canvas)
    s = state(settings) | {
        "intent": {"decision": json.loads(decide(settings).read_text()), "at": iso()}
    }
    (settings.paths.forum_state / "state.json").write_text(json.dumps(s))
    assert read(settings, canvas)["recovered"] == {
        "interrupted_post_sent_at": s["intent"]["at"],
        "on_canvas": False,
    }
    assert state(settings)["intent"] is None and canvas.posts == 0


# --------------------------------------------------------------------------
# content and thread boundaries
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("snippet", "reason"),
    [
        ("Write to me at student@mit.edu.", "an email address"),
        ("Call 617-555-0100 anytime.", "a phone number"),
        ("It reads /Users/me/Desktop/MIT/courses first.", "a local path"),
        ("The details are in report.pdf.", "a file name"),
        ("My grade on that was fine.", "grades or scores"),
        ("I got 18/20 points on it.", "grades or scores"),
        ("Here is my homework approach.", "the student's own coursework"),
        ("Check my calendar for Tuesday.", "the student's calendar"),
        ("See https://evil.example.com/x for more.", "links to evil.example.com"),
        ("Key: ghp_abcdefghijklmnopqrstuvwxyz123456", "looks like a secret"),
    ],
)
def test_screen_rejects_private_and_unsafe_content(snippet, reason):
    assert any(reason in p for p in discussion.screen(f"{MESSAGE} {snippet}"))


def test_screen_allows_paper_links_and_the_forum_itself():
    text = f"{MESSAGE} See https://arxiv.org/abs/2410.12345. and {discussion.TOPIC_URL}?entry_id=11"
    assert discussion.screen(text) == []


def test_screen_rejects_the_canvas_token_itself():
    assert "it contains the Canvas token" in discussion.screen(
        f"{MESSAGE} abc123secret", "abc123secret"
    )


def test_refusals_for_own_entries_missing_entries_and_repeats(settings, canvas):
    canvas.entries.append({"id": 60, "user_id": ME, "parent_id": 10, "created_at": iso(300),
                           "message": f"<p>{MESSAGE}</p>"})  # fmt: skip
    with pytest.raises(ForumRefused, match="is your own post"):
        act(settings, canvas, decide(settings, reply_to=60))
    with pytest.raises(ForumRefused, match="not in this discussion"):
        act(settings, canvas, decide(settings, reply_to=999))
    with pytest.raises(ForumRefused, match="repeats your earlier post 60"):
        act(settings, canvas, decide(settings))
    assert canvas.posts == 0


def test_decision_files_are_validated_and_confined(settings, canvas, tmp_path):
    outside = tmp_path / "d.json"
    outside.write_text(decide(settings).read_text())
    with pytest.raises(ForumRefused, match="must be inside"):
        act(settings, canvas, outside)
    with pytest.raises(MitsyncError, match="reply_to is required"):
        act(settings, canvas, decide(settings, reply_to=None))
    with pytest.raises(MitsyncError, match="Extra inputs"):
        act(settings, canvas, decide(settings, confidence=0.9))


def test_the_post_is_html_with_live_paper_links():
    body = discussion._html("First <para>.\n\nSee https://arxiv.org/abs/1234.5678.")
    assert body == (
        "<p>First &lt;para&gt;.</p>"
        '<p>See <a href="https://arxiv.org/abs/1234.5678">https://arxiv.org/abs/1234.5678</a>.</p>'
    )


# --------------------------------------------------------------------------
# knowledge and scholar
# --------------------------------------------------------------------------
def test_outline_shares_lectures_and_concepts_only(settings):
    course = settings.paths.kb_courses / "AI_Studio"
    course.mkdir(parents=True)
    (course / "COURSE.md").write_text(
        "# AI_Studio\n\n## At a glance\nlogistics\n\n## Lectures\n### Lecture 1\n"
        "Agents (`AI_Studio/lectures/w1.pdf`, slide 3).\n\n## Assignments\nHW1 secrets\n\n"
        "## Concepts\n- Agent loop\n\n## Open questions\nprivate\n"
    )
    text = knowledge.outline(settings, "AI_Studio")
    assert "Lecture 1" in text and "Agent loop" in text
    assert not any(s in text for s in ("HW1", "logistics", "private", ".pdf"))
    with pytest.raises(MitsyncError, match="no master file"):
        knowledge.outline(settings, "../USER")


SCHOLAR_PAGE = """
<div class="gs_r"><div class="gs_ri">
  <h3 class="gs_rt"><span>[PDF]</span> <a href="https://arxiv.org/abs/2501.00001">Monitoring
    <b>LLM</b> agents</a></h3>
  <div class="gs_a">A Author, B Author - arXiv preprint, 2025 - arxiv.org</div>
  <div class="gs_rs">We study weak-to-strong   monitoring.</div>
  <div class="gs_fl"><a href="/scholar?cites=1">Cited by 14</a></div>
</div></div>
"""


def test_scholar_parses_results():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=SCHOLAR_PAGE))
    [hit] = scholar.search("llm agent monitoring", since=2025, transport=transport)
    assert hit == {
        "title": "Monitoring LLM agents", "url": "https://arxiv.org/abs/2501.00001",
        "authors_venue_year": "A Author, B Author - arXiv preprint, 2025 - arxiv.org",
        "snippet": "We study weak-to-strong monitoring.", "cited_by": 14,
    }  # fmt: skip


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, text="<form id='captcha-form'></form>"), httpx.Response(429, text="")],
)
def test_scholar_blocked_is_a_typed_error(response):
    with pytest.raises(ScholarBlocked):
        scholar.search("x", transport=httpx.MockTransport(lambda r: response))
