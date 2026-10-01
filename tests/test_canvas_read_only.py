"""Canvas access is read-only, enforced rather than assumed.

Canvas holds graded work: a submission, a deletion, or an overwritten file is
not recoverable from this tool's side. The guarantee is therefore structural --
a chokepoint check plus a source scan -- not a matter of caller discipline.
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


#: The one file allowed an HTTP POST: the LTI launch to HBS Publishing, a login
#: rather than a write, and `hbsp._post` refuses every other host.
POST_ALLOWED = {"hbsp.py"}


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


def test_hbsp_is_the_only_module_that_posts_and_only_to_hbs():
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
