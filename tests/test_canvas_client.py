from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from mitsync.canvas_client import CanvasClient, _next_link
from mitsync.errors import (
    CanvasAccessDenied,
    CanvasAuthError,
    CanvasRateLimited,
    StalePresignedURL,
)

FIXTURES = Path(__file__).parent / "fixtures" / "canvas"
BASE = "https://canvas.mit.edu/api/v1"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Recorder:
    """Collects the sleeps a client would have taken, instead of taking them."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def build(settings, handler, **kw) -> tuple[CanvasClient, Recorder]:
    sleeper = Recorder()
    client = CanvasClient(
        settings,
        token="test-token",
        transport=httpx.MockTransport(handler),
        sleep=sleeper,
        **kw,
    )
    return client, sleeper


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------
def test_missing_token_raises_auth_error(settings, monkeypatch):
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    with pytest.raises(CanvasAuthError) as exc:
        CanvasClient(settings)
    assert "CANVAS_TOKEN" in str(exc.value)


def test_sends_bearer_token_and_returns_object(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=fixture("file_5003"))

    client, _ = build(settings, handler)
    with client:
        got = client.get("/files/5003")
    assert seen["auth"] == "Bearer test-token"
    assert seen["url"] == f"{BASE}/files/5003"
    assert got["uuid"] == "uuid-5003"


def test_get_drops_none_params(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "skip" not in request.url.params
        assert request.url.params["order_by"] == "due_at"
        return httpx.Response(200, json=[])

    client, _ = build(settings, handler)
    with client:
        client.get("/courses/1/assignments", order_by="due_at", skip=None)


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------
def test_paginate_follows_link_header(settings):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        page = request.url.params.get("page")
        if page is None:
            return httpx.Response(
                200,
                json=fixture("courses_page1"),
                headers={
                    "Link": f'<{BASE}/courses?page=1&per_page=100>; rel="current",'
                    f'<{BASE}/courses?page=2&per_page=100>; rel="next",'
                    f'<{BASE}/courses?page=2&per_page=100>; rel="last"'
                },
            )
        return httpx.Response(
            200,
            json=fixture("courses_page2"),
            headers={"Link": f'<{BASE}/courses?page=1&per_page=100>; rel="prev"'},
        )

    client, _ = build(settings, handler)
    with client:
        items = list(client.paginate("/courses", enrollment_state="active"))

    assert [i["id"] for i in items] == [28451, 11111, 28452]
    assert len(calls) == 2
    assert "per_page=100" in calls[0]
    assert "enrollment_state=active" in calls[0]


def test_paginate_always_sends_per_page(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, json=[])

    client, _ = build(settings, handler)
    with client:
        assert list(client.paginate("/courses")) == []


def test_paginate_encodes_bracket_params(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("include[]") == ["term"]
        return httpx.Response(200, json=[])

    client, _ = build(settings, handler)
    with client:
        list(client.paginate("/courses", **{"include[]": ["term"]}))


def test_next_link_parsing():
    assert _next_link(None) is None
    assert _next_link('<https://x/a>; rel="current"') is None
    assert _next_link('<https://x/a>; rel="next"') == "https://x/a"
    assert _next_link('<https://x/a>; rel="last", <https://x/b>; rel="next"') == "https://x/b"


# --------------------------------------------------------------------------
# errors and throttling
# --------------------------------------------------------------------------
def test_401_raises_auth_error(settings):
    client, _ = build(settings, lambda r: httpx.Response(401, json={"errors": ["bad token"]}))
    with client, pytest.raises(CanvasAuthError):
        client.get("/courses")


def test_genuine_403_raises_access_denied_without_retrying(settings):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(403, json=fixture("files_403"))

    client, sleeper = build(settings, handler)
    with client, pytest.raises(CanvasAccessDenied) as exc:
        list(client.paginate("/courses/28451/files"))
    assert len(calls) == 1, "a permissions 403 must not be retried"
    assert sleeper.slept == []
    assert "/api/v1/courses/28451/files" in str(exc.value)


def test_429_is_retried_then_succeeds(settings):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] <= 2:
            return httpx.Response(429, json=fixture("throttled_429"))
        return httpx.Response(200, json=fixture("files_page1"))

    client, sleeper = build(settings, handler)
    with client:
        items = list(client.paginate("/courses/28451/files"))
    assert state["n"] == 3
    assert len(sleeper.slept) == 2
    assert sleeper.slept[0] < sleeper.slept[1], "backoff must grow"
    assert [i["id"] for i in items] == [5001]


def test_rate_limited_403_is_retried_not_denied(settings):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                403,
                json=fixture("throttled_403"),
                headers={"X-Rate-Limit-Remaining": "0.0", "X-Request-Cost": "0.9"},
            )
        return httpx.Response(200, json=fixture("files_page1"))

    client, sleeper = build(settings, handler)
    with client:
        items = list(client.paginate("/courses/28451/files"))
    assert state["n"] == 2
    assert len(sleeper.slept) == 1
    assert [i["id"] for i in items] == [5001]


def test_throttle_retries_are_capped(settings):
    settings.canvas.max_retries = 2
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json=fixture("throttled_429"))

    client, sleeper = build(settings, handler)
    with client, pytest.raises(CanvasRateLimited):
        client.get("/courses")
    assert calls["n"] == 3  # initial + 2 retries
    assert len(sleeper.slept) == 2


def test_retry_after_header_drives_the_wait(settings):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={}, headers={"Retry-After": "4"})
        return httpx.Response(200, json={"ok": True})

    client, sleeper = build(settings, handler)
    with client:
        client.get("/courses")
    assert 4.0 <= sleeper.slept[0] <= 5.0


def test_low_remaining_budget_pauses_proactively(settings):
    settings.canvas.min_rate_limit_remaining = 100.0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"X-Rate-Limit-Remaining": "12.5", "X-Request-Cost": "0.4"},
        )

    client, sleeper = build(settings, handler)
    with client:
        client.get("/courses")
    assert sleeper.slept, "a low rate-limit budget should trigger a pause"


def test_5xx_is_retried(settings):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(503 if state["n"] == 1 else 200, json={"ok": True})

    client, _ = build(settings, handler)
    with client:
        assert client.get("/courses") == {"ok": True}
    assert state["n"] == 2


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------
PAYLOAD = b"%PDF-1.4 lecture one\n"


def test_download_reresolves_url_and_returns_sha256(settings, tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v1/files/5001":
            return httpx.Response(200, json=fixture("files_page1")[0])
        assert request.headers.get("Authorization") is None, (
            "a pre-signed URL must not carry the Canvas bearer token"
        )
        return httpx.Response(200, content=PAYLOAD)

    client, _ = build(settings, handler)
    dest = tmp_path / "out" / "lecture1.pdf"
    with client:
        sha = client.download(5001, dest)

    assert calls[0] == "/api/v1/files/5001", "the url must be re-resolved before downloading"
    assert dest.read_bytes() == PAYLOAD
    assert sha == hashlib.sha256(PAYLOAD).hexdigest()
    assert list(dest.parent.iterdir()) == [dest], "no .part file may be left behind"


def test_download_403_reresolves_once_and_retries(settings, tmp_path: Path):
    state = {"resolves": 0, "gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/5001":
            state["resolves"] += 1
            return httpx.Response(200, json=fixture("files_page1")[0])
        state["gets"] += 1
        if state["gets"] == 1:
            return httpx.Response(403, content=b"<Error>AccessDenied</Error>")
        return httpx.Response(200, content=PAYLOAD)

    client, _ = build(settings, handler)
    dest = tmp_path / "lecture1.pdf"
    with client:
        sha = client.download(5001, dest)
    assert state["resolves"] == 2
    assert sha == hashlib.sha256(PAYLOAD).hexdigest()


def test_download_403_twice_raises_stale_presigned_url(settings, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/5001":
            return httpx.Response(200, json=fixture("files_page1")[0])
        return httpx.Response(403, content=b"<Error>AccessDenied</Error>")

    client, _ = build(settings, handler)
    dest = tmp_path / "lecture1.pdf"
    with client, pytest.raises(StalePresignedURL):
        client.download(5001, dest)
    assert not dest.exists()
    assert not (tmp_path / "lecture1.pdf.part").exists()


def test_download_without_url_raises_stale(settings, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 5001, "uuid": "u"})

    client, _ = build(settings, handler)
    with client, pytest.raises(StalePresignedURL):
        client.download(5001, tmp_path / "x.pdf")
