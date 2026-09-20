"""HTTP client for the Canvas LMS REST API.

This is the one place HTTP correctness matters, so everything awkward about
Canvas is centralized here:

* **Pagination** — Canvas defaults to 10 items per page and advertises the next
  page only in the ``Link`` header. :meth:`CanvasClient.paginate` always sends
  ``per_page`` and follows ``rel="next"`` URLs as opaque strings.
* **Throttling** — responses carry ``X-Request-Cost`` and
  ``X-Rate-Limit-Remaining``. The docs disagree on whether throttling surfaces
  as 429 or 403, so *both* are treated as throttle signals when the body or
  headers look rate-limited; a plain 403 on a resource is access-denied. The
  client also pauses proactively once the remaining budget drops below
  ``settings.canvas.min_rate_limit_remaining``.
* **Pre-signed download URLs** — a file's ``url`` field is short lived and is
  never cached. :meth:`CanvasClient.download` re-resolves it via
  ``GET /files/:id`` immediately before streaming, and retries exactly once
  through a fresh resolve if the download 403s mid-flight
  (:class:`~mitsync.errors.StalePresignedURL`).

Requests are deliberately serial: Canvas penalizes concurrency.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import CanvasAccessDenied, CanvasAuthError, CanvasRateLimited, StalePresignedURL
from .logging import get_logger

log = get_logger(__name__)

__all__ = ["CanvasClient"]

# Substrings that mark a 403 body as throttling rather than permissions.
_THROTTLE_MARKERS = re.compile(
    r"rate[\s_-]?limit|throttl|403 Forbidden \(Rate Limit Exceeded\)", re.IGNORECASE
)
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0
_DOWNLOAD_CHUNK = 1 << 16


class CanvasClient:
    """A small, retrying, paginating Canvas API client built on httpx."""

    def __init__(
        self,
        settings: Any,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ) -> None:
        self.settings = settings
        self.base_url = settings.canvas.base_url.rstrip("/")
        self._token = token or settings.canvas.token
        if not self._token:
            raise CanvasAuthError(
                f"no Canvas token: set ${settings.canvas.token_env} in your environment "
                f"(Canvas > Account > Settings > '+ New Access Token')"
            )
        self._sleep = sleep
        self._max_retries = max(0, int(settings.canvas.max_retries))
        self._min_remaining = float(settings.canvas.min_rate_limit_remaining)
        self._per_page = int(settings.canvas.per_page)
        self._owns_client = True
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        self._host = urlparse(self.base_url).netloc

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> CanvasClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- public API --------------------------------------------------------
    def get(self, path: str, **params: Any) -> Any:
        """GET a single JSON object (or the first page's list) from Canvas."""
        response = self._request("GET", self._url(path), params=_clean(params))
        return _json(response)

    def paginate(self, path: str, **params: Any) -> Iterator[dict]:
        """Yield every item across every page, following ``Link`` ``rel=next``."""
        params = _clean(params)
        params.setdefault("per_page", self._per_page)
        url = self._url(path)
        first = True
        while url:
            response = self._request("GET", url, params=params if first else None)
            payload = _json(response)
            if isinstance(payload, dict):
                # Some endpoints wrap the list (e.g. {"items": [...]}) or return one object.
                values = next(
                    (v for v in payload.values() if isinstance(v, list)),
                    None,
                )
                payload = values if values is not None else [payload]
            for item in payload or []:
                if isinstance(item, dict):
                    yield item
            url = _next_link(response.headers.get("Link") or response.headers.get("link"))
            first = False

    def download(self, file_id: int, dest: Path) -> str:
        """Re-resolve the file's URL, stream it to ``dest``, return its sha256.

        The bytes land in a sibling ``.part`` file and are atomically renamed,
        so a killed run never leaves a half-written file in the mirror.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._stream_to(self._resolve_url(file_id), dest)
        except StalePresignedURL:
            log.info("download url for file %s went stale; re-resolving once", file_id)
            return self._stream_to(self._resolve_url(file_id), dest)

    # -- internals ---------------------------------------------------------
    def _resolve_url(self, file_id: int) -> str:
        record = self.get(f"/files/{file_id}")
        url = (record or {}).get("url")
        if not url:
            raise StalePresignedURL(f"Canvas returned no download url for file {file_id}")
        return url

    def _stream_to(self, url: str, dest: Path) -> str:
        request = self._client.build_request("GET", url)
        if urlparse(url).netloc != self._host:
            # Pre-signed URLs carry their own credentials; some CDNs reject a
            # stray Authorization header outright.
            request.headers.pop("Authorization", None)
        digest = hashlib.sha256()
        tmp = dest.with_name(dest.name + ".part")
        try:
            response = self._client.send(request, stream=True)
            try:
                if response.status_code == 403:
                    response.read()
                    raise StalePresignedURL(f"403 streaming {dest.name}")
                if response.status_code == 401:
                    response.read()
                    raise CanvasAuthError("Canvas rejected the token (401) during download")
                response.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                        digest.update(chunk)
                        fh.write(chunk)
            finally:
                response.close()
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return digest.hexdigest()

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self, method: str, url: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(method, url, params=params)
            except httpx.TransportError as exc:  # network blip: retry
                last = exc
                if attempt >= self._max_retries:
                    raise
                self._backoff(attempt, None, f"{type(exc).__name__}: {exc}")
                continue

            self._log_budget(url, response)

            if response.status_code == 401:
                raise CanvasAuthError(
                    f"Canvas rejected the token (401) for {url}; regenerate it in "
                    f"Canvas > Account > Settings"
                )
            if response.status_code == 403 and not _looks_throttled(response):
                raise CanvasAccessDenied(_resource_of(url))
            if response.status_code in _RETRY_STATUS or response.status_code == 403:
                last = CanvasRateLimited(_retry_after(response))
                if attempt >= self._max_retries:
                    raise last
                self._backoff(attempt, _retry_after(response), f"HTTP {response.status_code}")
                continue

            response.raise_for_status()
            self._maybe_pause(response)
            return response

        raise last or RuntimeError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: float | None, why: str) -> None:
        delay = retry_after if retry_after else min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt))
        delay += random.uniform(0, delay * 0.25)  # noqa: S311 -- jitter, not crypto
        log.warning("canvas backoff %.1fs after %s (attempt %d)", delay, why, attempt + 1)
        self._sleep(delay)

    def _log_budget(self, url: str, response: httpx.Response) -> None:
        cost = response.headers.get("X-Request-Cost")
        remaining = response.headers.get("X-Rate-Limit-Remaining")
        if cost or remaining:
            log.debug("canvas %s cost=%s remaining=%s", url, cost, remaining)

    def _maybe_pause(self, response: httpx.Response) -> None:
        remaining = _float(response.headers.get("X-Rate-Limit-Remaining"))
        if remaining is not None and remaining < self._min_remaining:
            log.warning(
                "canvas rate budget low (%.1f < %.1f); pausing", remaining, self._min_remaining
            )
            self._sleep(min(_BACKOFF_CAP, _BACKOFF_BASE * 2))


# -- helpers ---------------------------------------------------------------
def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:  # pragma: no cover - defensive
        if _looks_throttled(response):
            raise CanvasRateLimited(None) from exc
        raise


def _looks_throttled(response: httpx.Response) -> bool:
    """True when a 403/429 is Canvas throttling rather than a permissions denial."""
    if response.status_code == 429:
        return True
    remaining = _float(response.headers.get("X-Rate-Limit-Remaining"))
    if remaining is not None and remaining <= 0:
        return True
    if response.headers.get("Retry-After"):
        return True
    try:
        body = response.text
    except Exception:  # pragma: no cover - streaming body not read
        return False
    return bool(_THROTTLE_MARKERS.search(body or ""))


def _retry_after(response: httpx.Response) -> float | None:
    return _float(response.headers.get("Retry-After"))


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _resource_of(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or url


def _next_link(header: str | None) -> str | None:
    """Extract the ``rel="next"`` URL from a ``Link`` header, or None."""
    if not header:
        return None
    for part in header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        for attr in section[1:]:
            key, _, value = attr.strip().partition("=")
            if key.strip().lower() == "rel" and value.strip().strip('"') == "next":
                return url or None
    return None
