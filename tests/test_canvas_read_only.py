"""Canvas access is read-only, enforced rather than assumed.

Canvas holds graded work: a submission, a deletion, or an overwritten file is
not recoverable from this tool's side. The guarantee is therefore structural --
a chokepoint check plus a source scan -- not a matter of caller discipline.

There is one write, and it is pinned: `forum/discussion.py` may POST an entry
to the Homework 3 agent forum, and only there (`_post` refuses every other URL).
"""

import ast
from pathlib import Path

import httpx
import pytest

from mitsync.canvas.client import READ_ONLY_METHODS, CanvasClient
from mitsync.core.errors import CanvasWriteRefused

SRC = Path(__file__).resolve().parents[1] / "mitsync"
WRITE_VERBS = {"post", "put", "patch", "delete"}


def _client(settings) -> CanvasClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    return CanvasClient(settings, token="test-token", transport=transport)  # noqa: S106


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_write_methods_are_refused_at_the_chokepoint(settings, method):
    client = _client(settings)
    with pytest.raises(CanvasWriteRefused) as excinfo:
        client._request(method, "https://canvas.mit.edu/api/v1/courses")
    assert "read-only" in str(excinfo.value)


def test_read_methods_are_allowed(settings):
    client = _client(settings)
    assert client._request("GET", "https://canvas.mit.edu/api/v1/courses").status_code == 200
    assert READ_ONLY_METHODS == {"GET", "HEAD"}


#: The files allowed an HTTP POST. `hbsp.py`: the LTI launch to HBS Publishing, a
#: login rather than a write; `hbsp._post` refuses every other host.
#: `discussion.py`: an entry in the Homework 3 agent forum; `discussion._post`
#: refuses every URL but that topic's entries and replies.
POST_ALLOWED = {"hbsp.py", "discussion.py"}


def test_no_module_issues_an_http_write_call():
    """No `.post(`/`.put(`/`.patch(`/`.delete(` on any client object, anywhere."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in POST_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_VERBS
            ):
                target = node.func.value
                name = getattr(target, "id", "") or getattr(target, "attr", "")
                # Path.unlink/dict.pop style calls are fine; httpx/session ones are not.
                if any(tok in name.lower() for tok in ("client", "session", "httpx", "requests")):
                    offenders.append(f"{path.name}:{node.lineno} {name}.{node.func.attr}")
    assert not offenders, f"HTTP write calls found: {offenders}"


def test_only_the_pinned_modules_post_and_only_where_pinned():
    posting = {
        path.name
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WRITE_VERBS
        and getattr(node.func.value, "id", "") not in ("os", "shutil")
    }
    assert posting <= POST_ALLOWED

    from mitsync.canvas import hbsp
    from mitsync.core.errors import MitsyncError

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as web:
        with pytest.raises(MitsyncError, match="refusing to POST"):
            hbsp._post(web, {"action": "https://canvas.mit.edu/api/v1/x", "data": {}})

    from mitsync.forum import discussion

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as web:
        for url in (
            "https://canvas.mit.edu/api/v1/courses/40577/assignments/1/submissions",
            "https://canvas.mit.edu/api/v1/courses/40577/discussion_topics/1/entries",
            "https://canvas.mit.edu/api/v1/courses/1/discussion_topics/448963/entries",
            "https://canvas.mit.edu/api/v1/courses/40577/discussion_topics/448963/entries/5",
        ):
            with pytest.raises(MitsyncError, match="refusing to POST"):
                discussion._post(web, url, "<p>x</p>")
