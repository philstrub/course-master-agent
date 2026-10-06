"""
# Forum discussion

Read the Homework 3 agent forum on Canvas, and post to it inside fixed bounds.

## 1. What This Module Does

Three commands' worth of logic for one Canvas discussion topic, pinned in
code (`COURSE_ID`, `TOPIC_ID`):

* `pending` -- the trigger's cheap check: the course team's control line, and
  how many entries by others the agent has not seen yet.
* `read` -- what the agent decides from: the newest unseen entries with their
  thread context, replies to its own posts, its own recent posts, its hourly
  budget and the tail of its diary. `read(thread=...)` returns one whole thread.
* `act` -- applies the agent's decision file (`reply`, `new_thread` or
  `skip`). A post is screened, posted, read back from Canvas, and logged; every
  decision, a skip included, is appended to the diary and marks what the agent
  read as seen.

## 2. Why This Module Exists

The rest of mitsync is read-only on Canvas, and that is enforced
(`CanvasClient._request`). Homework 3 needs one write: an autonomous agent
taking part in one discussion. Rather than relax the client, the write lives
here, pinned to that one topic, and every boundary the assignment and the
student set is checked in code, before anything leaves the process:

* **The control line.** Before every POST (each retry included) the topic is
  fetched again, and its first line must read `COURSE-TEAM CONTROL: RUNNING`.
  `PAUSED`, or no control line at all, refuses.
* **Rate.** At most `MAX_POSTS_PER_HOUR` posts in any 60 minutes, counted from
  the agent's own entries on Canvas, so a lost local log cannot reset it.
* **Stopping rule.** After `MAX_FAILURES` consecutive failed posts, `act`
  refuses to post until a human runs `mitsync forum reset`, which the
  agent's wrapper (`bin/mitsync-forum`) refuses.
* **Content.** `screen` rejects email addresses, phone numbers, secrets, local
  paths, file names, grades, the student's own coursework or calendar, and links
  outside scholarly hosts. It cannot judge everything, so the agent's
  instructions carry the rest. The two together are the boundary.
* **No repeats.** No reply to the agent's own entries, no second reply to the
  same entry, and nothing close to an earlier post.

## 3. How It Fits in the Architecture

Reads go through `CanvasClient` (GET only). The single POST goes through
`_post`, which opens its own `httpx.Client` over the same transport and
refuses any URL but this topic's `entries` and `entries/<id>/replies`.
`tests/test_canvas_read_only.py` allows a POST in this file and in `hbsp.py`
only. It is driven by the separate OpenClaw `forum` agent (see
`openclaw/forum/AGENTS.md`), whose exec allowlist holds only
`bin/mitsync-forum`.

Memory is split by who may write it:

* `_kb/forum/diary.md` -- one entry per decision: what the forum discussed,
  the threads followed, what was posted or why not, the sources used. The
  agent reads it back through `read` (`diary_tail`). Separate from the graph.
* `_kb/forum/posts.jsonl` -- every post: id, parent, text, link, attempts,
  verification.
* `state/forum/state.json` -- seen entry ids, the failure count, and the
  in-flight post. Code-owned, outside the agent's workspace.

## 4. Key Concepts

**Write-ahead intent, then reconcile; never retry an unknown outcome.** The
decision is saved as `intent` before the POST. If the acknowledgement is lost
(a timeout, a malformed answer, a 5xx other than 503), or the process dies
before logging, Canvas may still have the post. The tool then re-reads the
forum (`RECONCILE_WAITS`) for its own entry with the same text under the same
parent. If none shows, it does **not** retry: it raises
`ForumOutcomeUnknown` and leaves the intent in place. Canvas caches the
discussion view, and on 2026-10-06 a retry made after one stale read
duplicated a live post. While the intent is unresolved, `act` refuses new
posts. The next run's `read`/`act`/`pending` records the entry once the view
shows it, or drops the intent after `UNRESOLVED_FOR`. Retries with backoff
happen only when Canvas provably did not create the entry: a connection
failure, or a 429/503 answer. The one-reply-per-entry rule and the hourly count
also read `posts.jsonl`, for the same caching reason.

**Seen means read by the agent.** `read` remembers the ids it showed (all
entries by others, the newest `SHOWN_NEW` in full). Only `act` marks them seen,
so a run that crashes before deciding sees the same entries again next time.
On the first run the backlog beyond `SHOWN_NEW` is counted, not shown, and is
marked seen with the first decision.

**The signature.** `act` appends `SIGNATURE` to every post, and the read-back
and reconciliation compare the signed text. The model never writes it, so no
post can go out unsigned.

**Fault injection.** `MITSYNC_FORUM_FAULT=lost-ack` makes the first attempt
raise a timeout after Canvas accepted the post. `=crash` exits right after it,
before anything is logged. Both exist to demonstrate the recovery above on a
real post. Unset, they do nothing.

**Why exceptions are caught here.** Two boundaries. A POST's transport,
status or JSON failure is caught to reconcile and retry with backoff, because
whether the post landed is unknown until Canvas is read again. Pydantic's
`ValidationError` on the agent's decision file becomes a `MitsyncError` naming
the field, because that file is untrusted model output.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mitsync.canvas.client import CanvasClient
from mitsync.core.clock import now_iso, parse_iso
from mitsync.core.config import Settings
from mitsync.core.errors import ForumOutcomeUnknown, ForumRefused, MitsyncError
from mitsync.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["Decision", "act", "pending", "read", "reset", "screen"]

#: Homework 3: Agent Discussion Forum, MAS.665. The only topic this module reads or writes.
COURSE_ID = 40577
TOPIC_ID = 448963
TOPIC = f"/courses/{COURSE_ID}/discussion_topics/{TOPIC_ID}"
TOPIC_URL = f"https://canvas.mit.edu{TOPIC}"

MAX_POSTS_PER_HOUR = 3
MAX_FAILURES = 3
POST_ATTEMPTS = 3
#: Waits (seconds) between re-reads of the forum after an ambiguous failure.
#: Canvas caches the discussion view, so a post that landed can be missing
#: from it for a while (seen live: under a minute).
RECONCILE_WAITS = (5.0, 15.0, 30.0)
#: An unresolved post younger than this blocks new posts; older, it is dropped
#: as never landed (by then the cached view has caught up).
UNRESOLVED_FOR = timedelta(minutes=10)
#: Statuses that mean Canvas did not create the entry, so a retry is safe.
_NOT_CREATED = frozenset({429, 503})
SHOWN_NEW = 10
ENTRY_CHARS = 1200  # `read` must stay under the ~30 KB a tool result may carry inline
THREADS_SHOWN = 12
SIMILAR = 0.5  # Jaccard overlap of word trigrams that counts as a repeat
#: Appended by `act` to every post, so readers always know an agent wrote it.
SIGNATURE = "— Filippo's Forum Agent"

_CONTROL = re.compile(r"COURSE-TEAM CONTROL:\s*(RUNNING|PAUSED)")
_URL = re.compile(r"https?://[^\s<>()\"']+")
_TRAILING = ".,;:!?"

#: Hosts a post may link to: papers, never arbitrary sites. Subdomains count.
LINK_HOSTS = (
    "arxiv.org", "doi.org", "scholar.google.com", "openreview.net", "aclanthology.org",
    "dl.acm.org", "ieeexplore.ieee.org", "proceedings.neurips.cc", "papers.nips.cc",
    "proceedings.mlr.press", "jmlr.org", "link.springer.com", "nature.com", "science.org",
    "pnas.org", "ssrn.com", "semanticscholar.org", "ojs.aaai.org", "ijcai.org",
    "sciencedirect.com", "onlinelibrary.wiley.com", "aclweb.org", "proceedings.iclr.cc",
    "icml.cc", "aaai.org", "acm.org", "anthropic.com", "openai.com",
)  # fmt: skip

#: What a post must never contain, by the name a refusal reports.
FORBIDDEN = {
    "an email address": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    "a phone number": r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)",
    "something that looks like a secret": (
        r"(?i)\b(?:sk|ghp|gho|xox[abp]|akia)[-_a-z0-9]{10,}|bearer\s+\S{16,}"
        r"|\b[A-Za-z0-9_~-]{40,}\b"
    ),
    "a local path": r"/Users/|~/|\b[A-Za-z]:\\|\b_(?:kb|canvas|agent)\b",
    "a file name": r"\b[\w-]+\.(?:pdf|ipynb|docx?|pptx?|xlsx?|csv|tex|jsonl?|md|py|env)\b",
    "grades or scores": (
        r"(?i)\b(?:my|our)\s+(?:grades?|scores?|gpa|marks?)\b"
        r"|\b\d+(?:\.\d+)?\s*/\s*\d+\s*(?:points|pts)\b"
    ),
    "the student's own coursework": (
        r"(?i)\bmy\s+(?:homework|pset|problem\s+set|submissions?|answers?|solutions?"
        r"|exams?|midterm|final|grade)\b"
    ),
    "the student's calendar": r"(?i)\bmy\s+(?:calendar|schedule|meetings?)\b",
}


# --------------------------------------------------------------------------
# the agent's decision, and the code-owned state
# --------------------------------------------------------------------------
class Thread(BaseModel):
    """A conversation the agent followed this run, for the diary."""

    model_config = ConfigDict(extra="forbid")
    entry_id: int
    gist: str = Field(min_length=10, max_length=400)


class Decision(BaseModel):
    """The agent's decision file, `_kb/forum/decisions/<stamp>.json`."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["reply", "new_thread", "skip"]
    reply_to: int | None = None
    message: str | None = Field(default=None, min_length=200, max_length=2200)
    reason: str = Field(min_length=20, max_length=800)
    summary: str = Field(min_length=40, max_length=2000)
    threads: list[Thread] = Field(default_factory=list, max_length=10)
    sources: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _shape(self) -> Decision:
        if self.action == "skip" and (self.message or self.reply_to):
            raise ValueError("a skip has no message and no reply_to")
        if self.action != "skip" and not self.message:
            raise ValueError(f"a {self.action} needs a message")
        if (self.action == "reply") != (self.reply_to is not None):
            raise ValueError("reply_to is required for a reply, and only for a reply")
        return self


class Intent(BaseModel):
    """A post sent (or about to be) whose outcome is not logged yet."""

    decision: Decision
    at: str


class ForumState(BaseModel):
    seen: list[int] = Field(default_factory=list)
    last_read: list[int] = Field(default_factory=list)
    failures: int = 0
    intent: Intent | None = None


def _state_file(settings: Settings) -> Path:
    return settings.paths.forum_state / "state.json"


def _load(settings: Settings) -> ForumState:
    path = _state_file(settings)
    # No file is the first run, not an error.
    return ForumState.model_validate_json(path.read_text()) if path.exists() else ForumState()


def _save(settings: Settings, state: ForumState) -> None:
    path = _state_file(settings)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    os.replace(tmp, path)


def load_decision(settings: Settings, path: Path) -> Decision:
    decisions = (settings.paths.kb_forum / "decisions").resolve()
    if decisions not in Path(path).resolve().parents:
        raise ForumRefused(f"a decision file must be inside {decisions}, not {path}")
    try:
        return Decision.model_validate_json(Path(path).read_text())
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'decision'}: {e['msg']}" for e in exc.errors()
        )
        raise MitsyncError(f"decision {path} is invalid: {problems}") from exc


# --------------------------------------------------------------------------
# reading the forum
# --------------------------------------------------------------------------
def plain(message: str) -> str:
    """Canvas HTML as text: one line per block, whitespace collapsed."""
    text = BeautifulSoup(message or "", "html.parser").get_text("\n")
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def control(topic: dict) -> str:
    """`RUNNING`, `PAUSED`, or `MISSING` when the first line is not a control line."""
    first = next(iter(plain(topic.get("message") or "").splitlines()), "")
    match = _CONTROL.fullmatch(first.strip())
    return match.group(1) if match else "MISSING"


def _signed(decision: Decision) -> str:
    """The text as posted: the agent's message plus the signature."""
    return f"{(decision.message or '').strip()}\n\n{SIGNATURE}"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _entry_url(entry_id: int) -> str:
    return f"{TOPIC_URL}?entry_id={entry_id}"


def _snapshot(client: CanvasClient) -> dict[str, Any]:
    """Who the agent is, the control line, and every entry, flattened."""
    me = client.get("/users/self")["id"]
    topic = client.get(TOPIC)
    view = client.get(f"{TOPIC}/view")
    names = {p["id"]: p.get("display_name") or f"user {p['id']}" for p in view["participants"]}
    entries: dict[int, dict] = {}

    def walk(items: list[dict], parent: int | None, root: int | None, depth: int) -> None:
        for e in items:
            entries[e["id"]] = _entry(e, parent, root or e["id"], depth, names)
            walk(e.get("replies") or [], e["id"], root or e["id"], depth + 1)

    walk(view["view"], None, None, 0)
    # Entries newer than Canvas's cached view arrive flat, with their parent id.
    for e in sorted(view.get("new_entries") or [], key=lambda e: e["created_at"]):
        if e["id"] in entries:
            continue  # already in the cached view
        parent = entries.get(e.get("parent_id"))
        entries[e["id"]] = _entry(
            e,
            e.get("parent_id"),
            parent["root_id"] if parent else e["id"],
            parent["depth"] + 1 if parent else 0,
            names,
        )
    return {"me": me, "control": control(topic), "entries": entries}


def _entry(e: dict, parent: int | None, root: int, depth: int, names: dict) -> dict:
    return {
        "id": e["id"],
        "parent_id": parent,
        "root_id": root,
        "depth": depth,
        "user_id": e.get("user_id"),
        "author": names.get(e.get("user_id"), f"user {e.get('user_id')}"),
        "created_at": e.get("created_at"),
        "deleted": bool(e.get("deleted")),
        "text": plain(e.get("message") or ""),
        "url": _entry_url(e["id"]),
    }


def _mine(snap: dict) -> list[dict]:
    return [e for e in snap["entries"].values() if e["user_id"] == snap["me"]]


def _posts_last_hour(snap: dict, now: datetime) -> int:
    return sum(parse_iso(e["created_at"]) > now - timedelta(hours=1) for e in _mine(snap))


def _landed(snap: dict, decision: Decision) -> dict | None:
    """The agent's own entry carrying this decision's message, if Canvas has it."""
    want = _norm(_signed(decision))[:400]
    for e in _mine(snap):
        if e["parent_id"] == decision.reply_to and _norm(e["text"])[:400] == want:
            return e
    return None


def _logged_posts(settings: Settings) -> list[dict]:
    """The local post log. Canvas's cached view can lag a fresh post, so the
    hourly count and the one-reply-per-entry rule consult both."""
    path = settings.paths.kb_forum / "posts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _budget(settings: Settings, state: ForumState, snap: dict) -> dict[str, Any]:
    now = datetime.now(UTC)
    logged = sum(
        bool(p["posted_at"]) and parse_iso(p["posted_at"]) > now - timedelta(hours=1)
        for p in _logged_posts(settings)
    )
    used = max(_posts_last_hour(snap, now), logged)
    why = None
    if snap["control"] != "RUNNING":
        why = f"the course team's control line is {snap['control']}"
    elif state.failures >= MAX_FAILURES:
        why = (
            f"stopped after {state.failures} consecutive failed posts; "
            "a human runs `mitsync forum reset`"
        )
    elif used >= MAX_POSTS_PER_HOUR:
        why = f"{used} posts in the last hour (limit {MAX_POSTS_PER_HOUR})"
    return {
        "control": snap["control"],
        "can_post": why is None,
        "why_not": why,
        "posts_last_hour": used,
        "posts_left_this_hour": max(0, MAX_POSTS_PER_HOUR - used),
        "consecutive_failures": state.failures,
        "stopped": state.failures >= MAX_FAILURES,
    }


def _short(e: dict, width: int = 200) -> dict:
    text = e["text"] if len(e["text"]) <= width else e["text"][: width - 1] + "…"
    return {"id": e["id"], "author": e["author"], "text": text}


def pending(settings: Settings, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """What the trigger needs: should a model be woken at all?"""
    with CanvasClient(settings, transport=transport) as client:
        state = _load(settings)
        snap = _snapshot(client)
        recovered = _reconcile(settings, state, snap)
        mine = {e["id"] for e in _mine(snap)}
        seen = set(state.seen)
        unseen = [
            e for e in snap["entries"].values()
            if e["user_id"] != snap["me"] and not e["deleted"] and e["id"] not in seen
        ]  # fmt: skip
        budget = _budget(settings, state, snap)
    return {
        "control": snap["control"],
        "stopped": budget["stopped"],
        "new": len(unseen),
        "replies_to_me": sum(e["parent_id"] in mine for e in unseen),
        "newest": max((e["id"] for e in unseen), default=None),
        "recovered": recovered,
        "fire": bool(unseen) and snap["control"] == "RUNNING" and not budget["stopped"],
    }


def read(
    settings: Settings,
    thread: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """The forum as the agent decides from it; `thread` returns one whole thread instead."""
    with CanvasClient(settings, transport=transport) as client:
        state = _load(settings)
        snap = _snapshot(client)
        recovered = _reconcile(settings, state, snap)
    entries = snap["entries"]
    if thread is not None:
        root = entries[thread]["root_id"]
        return {
            "topic_url": TOPIC_URL,
            "thread": [
                _short(e, 2000) | {k: e[k] for k in ("parent_id", "depth", "created_at", "url")}
                | {"mine": e["user_id"] == snap["me"]}
                for e in sorted(entries.values(), key=lambda e: e["created_at"])
                if e["root_id"] == root and not e["deleted"]
            ],
        }  # fmt: skip

    seen = set(state.seen)
    others = [e for e in entries.values() if e["user_id"] != snap["me"] and not e["deleted"]]
    unseen = sorted((e for e in others if e["id"] not in seen), key=lambda e: e["created_at"])
    mine = _mine(snap)
    mine_ids = {e["id"] for e in mine}

    def context(e: dict) -> list[dict]:
        chain, parent = [], e["parent_id"]
        while parent is not None and parent in entries:
            chain.append(_short(entries[parent]))
            parent = entries[parent]["parent_id"]
        chain.reverse()
        # The thread's opening entry and the three nearest parents; deep threads run to 20 levels.
        return chain if len(chain) <= 4 else [chain[0], *chain[-3:]]

    roots: dict[int, list[dict]] = {}
    for e in entries.values():
        roots.setdefault(e["root_id"], []).append(e)
    state.last_read = [e["id"] for e in others]
    _save(settings, state)
    diary = settings.paths.kb_forum / "diary.md"
    return {
        "topic_url": TOPIC_URL,
        "now": datetime.now().astimezone().isoformat(timespec="minutes"),
        "you": {"user_id": snap["me"]} | _budget(settings, state, snap),
        "recovered": recovered,
        "replies_to_you": [
            _short(e, ENTRY_CHARS) | {"reply_to": e["parent_id"], "url": e["url"]}
            for e in unseen if e["parent_id"] in mine_ids
        ],
        "new_entries": [
            _short(e, ENTRY_CHARS)
            | {k: e[k] for k in ("parent_id", "root_id", "depth", "created_at", "url")}
            | {"context": context(e)}
            for e in unseen[-SHOWN_NEW:]
        ],
        "older_unseen_not_shown": max(0, len(unseen) - SHOWN_NEW),
        "your_posts": [
            _short(e) | {"reply_to": e["parent_id"], "created_at": e["created_at"], "url": e["url"]}
            for e in sorted(mine, key=lambda e: e["created_at"])
        ],
        "threads": sorted(
            (
                {"root_id": r, "started_by": entries[r]["author"],
                 "opening": _short(entries[r], 120)["text"], "entries": len(group),
                 "last_activity": max(e["created_at"] for e in group),
                 "you_took_part": any(e["user_id"] == snap["me"] for e in group)}
                for r, group in roots.items() if r in entries
            ),
            key=lambda t: t["last_activity"], reverse=True,
        )[:THREADS_SHOWN],
        "diary_tail": diary.read_text()[-3000:] if diary.exists() else "",
    }  # fmt: skip


# --------------------------------------------------------------------------
# the boundary
# --------------------------------------------------------------------------
def _trigrams(text: str) -> set[tuple[str, ...]]:
    words = _norm(text).split()
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def screen(message: str, token: str | None = None) -> list[str]:
    """Every reason `message` may not be posted; empty means it may."""
    problems = [f"it contains {what}" for what, rx in FORBIDDEN.items() if re.search(rx, message)]
    if token and token in message:
        problems.append("it contains the Canvas token")
    for url in _URL.findall(message):
        host = urlparse(url.rstrip(_TRAILING)).netloc.lower()
        if host == "canvas.mit.edu" and urlparse(url).path.startswith(TOPIC):
            continue
        if not any(host == h or host.endswith("." + h) for h in LINK_HOSTS):
            problems.append(
                f"it links to {host}: only paper links are allowed ({', '.join(LINK_HOSTS[:6])}, …)"
            )
    return problems


def _check(settings: Settings, decision: Decision, snap: dict, token: str | None) -> list[str]:
    """Screen the message, and refuse replies that would ignore the thread's state."""
    problems = screen(decision.message or "", token)
    mine = _mine(snap)
    # The post log too: Canvas's cached view can lag a reply posted minutes ago.
    replied = {e["parent_id"] for e in mine} | {p["reply_to"] for p in _logged_posts(settings)}
    if decision.reply_to is not None:
        target = snap["entries"].get(decision.reply_to)
        if target is None:
            problems.append(f"entry {decision.reply_to} is not in this discussion")
        elif target["deleted"]:
            problems.append(f"entry {decision.reply_to} was deleted")
        elif target["user_id"] == snap["me"]:
            problems.append(f"entry {decision.reply_to} is your own post")
        elif decision.reply_to in replied:
            problems.append(f"you already replied to entry {decision.reply_to}")
    new = _trigrams(decision.message or "")
    for e in mine:
        old = _trigrams(e["text"])
        if new and old and len(new & old) / len(new | old) >= SIMILAR:
            problems.append(f"it repeats your earlier post {e['id']}")
    return problems


# --------------------------------------------------------------------------
# posting
# --------------------------------------------------------------------------
def _html(message: str) -> str:
    """Plain text with blank-line paragraphs, as Canvas HTML with live links."""

    def link(m: re.Match) -> str:
        url = m.group(0).rstrip(_TRAILING)
        return f'<a href="{url}">{url}</a>{m.group(0)[len(url) :]}'

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    return "".join(
        "<p>" + _URL.sub(link, html.escape(p, quote=False)).replace("\n", "<br>") + "</p>"
        for p in paragraphs
    )


def _endpoint(base_url: str, reply_to: int | None) -> str:
    return f"{base_url}{TOPIC}/entries" + (f"/{reply_to}/replies" if reply_to else "")


_ALLOWED_POST = re.compile(rf"^https://[^/]+/api/v1{re.escape(TOPIC)}/entries(?:/\d+/replies)?$")


def _post(web: httpx.Client, url: str, body: str) -> dict:
    """The only Canvas write in mitsync: one entry in this one topic."""
    if not _ALLOWED_POST.match(url):
        raise MitsyncError(f"refusing to POST anywhere but discussion topic {TOPIC_ID}: {url}")
    response = web.post(url, data={"message": body})
    response.raise_for_status()
    entry = response.json()
    if not isinstance(entry, dict) or "id" not in entry:
        raise ValueError(f"Canvas answered the post without an entry id: {str(entry)[:200]}")
    return entry


def _fault(attempt: int) -> None:
    """Test seam for the recovery demo; see the module docstring."""
    fault = os.environ.get("MITSYNC_FORUM_FAULT")
    if fault == "lost-ack" and attempt == 0:
        raise httpx.ReadTimeout("injected fault: acknowledgement lost after Canvas saved the post")
    if fault == "crash":
        raise SystemExit("injected fault: died after Canvas saved the post, before logging it")


def _send(
    settings: Settings,
    client: CanvasClient,
    decision: Decision,
    snap: dict,
    sleep: Callable[[float], None],
) -> tuple[dict, int, bool]:
    """POST with backoff, re-reading the control line before each attempt.

    Returns the entry as Canvas has it, the attempts used, and whether it was
    found by reconciling rather than from the POST's own answer.
    """
    url = _endpoint(client.base_url, decision.reply_to)
    body = _html(_signed(decision))
    headers = {"Authorization": f"Bearer {settings.canvas.token}", "Accept": "application/json"}
    with httpx.Client(transport=client.transport, timeout=30, headers=headers) as web:
        for attempt in range(POST_ATTEMPTS):
            status = control(client.get(TOPIC))
            if status != "RUNNING":
                raise ForumRefused(
                    f"the course team's control line is {status}; nothing was posted"
                )
            try:
                entry = _post(web, url, body)
                _fault(attempt)
                return entry, attempt + 1, False
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
                log.warning("post attempt %d failed: %s: %s", attempt + 1, type(exc).__name__, exc)
                code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                # Safe to retry only when Canvas provably did not create the entry.
                not_created = code in _NOT_CREATED or isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                )
                found = _landed(_snapshot(client), decision)
                for wait in () if not_created else RECONCILE_WAITS:
                    if found:
                        break
                    sleep(wait)
                    found = _landed(_snapshot(client), decision)
                if found:
                    log.info("entry %s is on Canvas after all; not posting again", found["id"])
                    return found, attempt + 1, True
                if code is not None and code < 500 and code != 429:
                    raise MitsyncError(f"Canvas refused the post: HTTP {code}") from exc
                if not not_created:
                    raise ForumOutcomeUnknown(
                        f"the post may be on Canvas ({type(exc).__name__}: {exc}) but no read "
                        "shows it yet; not retrying. The next run reconciles it."
                    ) from exc
                if attempt + 1 == POST_ATTEMPTS:
                    raise MitsyncError(f"post failed {POST_ATTEMPTS} times: {exc}") from exc
                delay = 2.0 * 2**attempt
                sleep(delay + random.uniform(0, delay / 4))  # noqa: S311 -- jitter
    raise AssertionError("unreachable")  # pragma: no cover


def _verify(client: CanvasClient, entry_id: int, me: int, decision: Decision) -> bool:
    """Read the entry back from Canvas: it exists, it is ours, it says what we sent."""
    rows = client.get(f"{TOPIC}/entry_list", **{"ids[]": entry_id}) or []
    want = _norm(_signed(decision))[:400]
    return any(
        r["id"] == entry_id
        and r.get("user_id") == me
        and _norm(plain(r.get("message") or ""))[:400] == want
        for r in rows
    )  # fmt: skip


def _record(
    settings: Settings,
    state: ForumState,
    decision: Decision,
    snap: dict,
    entry: dict | None,
    note: str,
    log_post: dict | None = None,
) -> None:
    """Log the post (if any), append the diary entry, mark what was read as seen."""
    if log_post is not None:
        with (settings.paths.kb_forum / "posts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(log_post, ensure_ascii=False) + "\n")
    _diary(settings, decision, snap, entry, note)
    state.seen = sorted(
        set(state.seen) | set(state.last_read) | ({entry["id"]} if entry else set())
    )
    state.intent = None
    _save(settings, state)


def _reconcile(settings: Settings, state: ForumState, snap: dict) -> dict | None:
    """Settle a post an earlier run sent but never logged (a crash, a kill, a lost answer)."""
    if state.intent is None:
        return None
    decision, sent = state.intent.decision, state.intent.at
    found = _landed(snap, decision)
    if found is None and datetime.now(UTC) - parse_iso(sent) < UNRESOLVED_FOR:
        log.warning("the post sent at %s is not visible yet; leaving it unresolved", sent)
        return {"unresolved_post_sent_at": sent, "on_canvas": "not visible yet"}
    if found is None:
        log.warning("the interrupted post from %s never reached Canvas; dropping it", sent)
        state.intent = None
        _save(settings, state)
        return {"interrupted_post_sent_at": sent, "on_canvas": False}
    state.failures = 0
    _record(
        settings, state, decision, snap, found,
        f"recovered: an earlier run posted this at {sent} but stopped before logging it",
        {"entry_id": found["id"], "reply_to": decision.reply_to, "url": found["url"],
         "posted_at": found["created_at"], "verified": True, "attempts": None,
         "recovered": "after restart", "message": decision.message},
    )  # fmt: skip
    return {"entry_id": found["id"], "on_canvas": True, "url": found["url"]}


def act(
    settings: Settings,
    decision_path: Path,
    *,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Apply the agent's decision: post it (inside every bound) or record the skip."""
    decision = load_decision(settings, decision_path)
    with CanvasClient(settings, transport=transport, sleep=sleep) as client:
        state = _load(settings)
        snap = _snapshot(client)
        recovered = _reconcile(settings, state, snap)
        if decision.action == "skip":
            if not dry_run:
                _record(settings, state, decision, snap, None, "skipped")
            return {"action": "skip", "dry_run": dry_run, "recovered": recovered}

        if state.intent is not None:
            raise ForumRefused(
                f"a post sent at {state.intent.at} is not resolved yet; record a skip, "
                "the next run settles it"
            )
        problems = _check(settings, decision, snap, settings.canvas.token)
        if problems:
            raise ForumRefused("not posted, because " + "; ".join(problems) + ". Revise and retry.")
        budget = _budget(settings, state, snap)
        if not budget["can_post"]:
            raise ForumRefused(f"not posted: {budget['why_not']}. Record a skip instead.")
        if dry_run:
            return {"action": decision.action, "dry_run": True, "reply_to": decision.reply_to,
                    "would_post_to": _endpoint(client.base_url, decision.reply_to),
                    "html": _html(_signed(decision)), "budget": budget}  # fmt: skip

        state.intent = Intent(decision=decision, at=now_iso())
        _save(settings, state)
        try:
            entry, attempts, reconciled = _send(settings, client, decision, snap, sleep)
        except ForumRefused:
            state.intent = None
            _save(settings, state)
            raise
        except ForumOutcomeUnknown as exc:
            state.failures += 1  # the intent stays: the next run reconciles it
            _save(settings, state)
            _diary(settings, decision, snap, None, f"OUTCOME UNKNOWN, left to reconcile: {exc}")
            raise
        except MitsyncError as exc:
            state.failures += 1
            state.intent = None
            _save(settings, state)
            _diary(settings, decision, snap, None, f"FAILED ({state.failures} in a row): {exc}")
            raise
        verified = _verify(client, entry["id"], snap["me"], decision)
        url = _entry_url(entry["id"])
        state.failures = 0 if verified else state.failures + 1
        note = (
            f"posted [entry {entry['id']}]({url}), "
            + ("read back from Canvas and verified" if verified else "NOT verified on read-back")
            + f" (attempt {attempts}"
            + (", found by reconciling after a lost acknowledgement)" if reconciled else ")")
        )
        _record(
            settings, state, decision, snap, {"id": entry["id"], "url": url}, note,
            {"entry_id": entry["id"], "reply_to": decision.reply_to, "url": url,
             "posted_at": entry.get("created_at"), "verified": verified, "attempts": attempts,
             "recovered": "lost acknowledgement" if reconciled else None,
             "message": decision.message},
        )  # fmt: skip
    if not verified:
        raise MitsyncError(f"entry {entry['id']} was posted but did not read back as sent: {url}")
    return {"action": decision.action, "entry_id": entry["id"], "url": url, "verified": True,
            "attempts": attempts, "reconciled": reconciled, "recovered": recovered}  # fmt: skip


def reset(settings: Settings) -> dict[str, Any]:
    """A human clears the failure count after looking at why posts failed."""
    state = _load(settings)
    before = state.failures
    state.failures = 0
    _save(settings, state)
    return {"consecutive_failures": 0, "was": before}


# --------------------------------------------------------------------------
# the diary
# --------------------------------------------------------------------------
_DIARY_HEAD = f"""# Forum diary

What the forum agent read and did in the [Homework 3: Agent Discussion Forum]({TOPIC_URL}),
one entry per decision, newest last. `mitsync forum act` writes it from the agent's decision
file. It is the agent's memory of the conversation, and the knowledge graph never reads it.
"""


def _diary(
    settings: Settings, decision: Decision, snap: dict, entry: dict | None, note: str
) -> None:
    entries = snap["entries"]
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    if decision.action == "reply":
        target = entries.get(decision.reply_to) or {"author": "?"}
        what = f"replied to {target['author']} (entry {decision.reply_to})"
    else:
        what = {"new_thread": "started a thread", "skip": "chose not to post"}[decision.action]
    lines = [
        f"\n## {stamp} · {what}\n",
        f"- **Outcome:** {note}",
        f"- **Forum since last run:** {decision.summary}",
    ]
    if decision.threads:
        lines.append("- **Threads followed:**")
        lines += [
            f"  - [{t.entry_id}]({_entry_url(t.entry_id)}) "
            f"{entries[t.entry_id]['author'] + ': ' if t.entry_id in entries else ''}{t.gist}"
            for t in decision.threads
        ]
    lines.append(f"- **Why:** {decision.reason}")
    if decision.sources:
        lines.append("- **Sources:** " + "; ".join(decision.sources))
    if decision.message:
        lines.append("")
        lines += ["> " + line if line else ">" for line in decision.message.strip().splitlines()]
    path = settings.paths.kb_forum / "diary.md"
    with path.open("a", encoding="utf-8") as fh:
        if fh.tell() == 0:
            fh.write(_DIARY_HEAD)
        fh.write("\n".join(lines) + "\n")
