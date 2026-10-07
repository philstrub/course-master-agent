"""
# Errors

The typed failures mitsync is allowed to raise, and what each one means.

## 1. What This Module Does

Defines `MitsyncError` and one subclass per *expected* failure mode.

## 2. Why This Module Exists

This repo forbids defensive code: library functions raise rather than
returning an empty default, and exception handling is confined to two
boundaries -- the CLI entry point, and genuinely external failure. That rule
only works if there is a single vocabulary of failures to catch. `cli.run()`
can turn any escaped error into a one-line message and exit 1 precisely
because everything that escapes is a `MitsyncError`; a raw
`httpx.HTTPStatusError` or `yaml.YAMLError` reaching the user is a bug in
whichever boundary let it through.

Each class also carries its own remediation text. An operator holding a failed
run should not need the source to know what to do next -- hence "regenerate it
in Canvas > Account > Settings" rather than "401".

## 3. How It Fits in the Architecture

A leaf: it imports nothing from mitsync and nearly everything imports it.
`canvas_client` translates HTTP into the `Canvas*` family, `calendar_read`
raises `CalendarAccessDenied`, `config` raises `ConfigError`, `manifest` raises
`ManifestBusy` when a sync holds its lock too long, `graph` raises
`OntologyError`, `gradescope.client` raises `GradescopeAuthError`.

## 4. Key Concepts

**Not every error is a failure.** `CanvasAccessDenied` (a course with its
Files tab hidden) and `CanvasFeatureDisabled` (a 404 that only means the course
turned Pages off) are expected: the caller records a notice and keeps walking.
They are deliberately separate from `CanvasNotFound`, which is a genuine miss
worth reporting -- one noisy class would drown the real errors.

**`CanvasWriteRefused` is a guardrail, not a diagnosis.** It is raised before
the request leaves the process. Canvas holds graded work, so read-only is
enforced at the client chokepoint rather than trusted to caller discipline.
"""

from __future__ import annotations


class MitsyncError(Exception):
    """Base class for every expected mitsync failure."""


class ConfigError(MitsyncError):
    """settings.yml / courses.yml is missing, malformed, or inconsistent."""


class CanvasAuthError(MitsyncError):
    """Canvas rejected the token (401)."""


class CanvasRateLimited(MitsyncError):
    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        wait = f" retry after {retry_after:.0f}s" if retry_after else ""
        super().__init__(f"Canvas rate limit hit;{wait or ' back off and retry'}")


class CanvasAccessDenied(MitsyncError):
    """403 on a resource — usually a course with the Files tab hidden.

    Expected and recoverable: skip the resource and keep going.
    """

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(f"Canvas denied access to {resource} (hidden or restricted)")


class CanvasFeatureDisabled(MitsyncError):
    """404 on an optional course feature whose tab the course turned off.

    Canvas answers ``GET /courses/:id/pages`` with **404** and the body
    ``{"message": "That page has been disabled for this course"}`` when the
    course has the Pages feature disabled. This is the 404-shaped sibling of
    :class:`CanvasAccessDenied`: the course simply has no such content.

    Expected, not a failure -- skip the stage, record a notice, keep going.
    Never report it as an error, or real errors drown in the noise.
    """

    def __init__(self, resource: str, message: str = "") -> None:
        self.resource = resource
        self.canvas_message = message
        detail = f": {message}" if message else ""
        super().__init__(f"Canvas has {resource} disabled for this course{detail}")


class CanvasNotFound(MitsyncError):
    """404 on a resource that genuinely does not exist.

    Distinct from :class:`CanvasFeatureDisabled`, which is a 404 that only
    means "this course turned that tab off". This one is a real miss --
    a deleted file, a bad id, a wrong path -- and is worth reporting.
    """

    def __init__(self, resource: str, message: str = "") -> None:
        self.resource = resource
        self.canvas_message = message
        detail = f": {message}" if message else ""
        super().__init__(f"Canvas has no {resource} (404){detail}")


class CanvasHTTPError(MitsyncError):
    """Any other non-2xx Canvas response, translated at the client boundary.

    No raw ``httpx.HTTPStatusError`` may escape :mod:`mitsync.canvas_client`;
    every status the client does not handle specifically lands here so callers
    only ever catch :class:`MitsyncError`.
    """

    def __init__(self, status_code: int, resource: str, message: str = "") -> None:
        self.status_code = status_code
        self.resource = resource
        self.canvas_message = message
        detail = f": {message}" if message else ""
        super().__init__(f"Canvas returned HTTP {status_code} for {resource}{detail}")


class StalePresignedURL(MitsyncError):
    """A Canvas file download URL expired; re-fetch the file record."""


class CalendarAccessDenied(MitsyncError):
    """macOS TCC denied calendar access. Grant it in System Settings > Privacy."""


class OntologyError(MitsyncError):
    """A graph record violates the ontology in mitsync/knowledge/ontology.py."""


class CanvasWriteRefused(MitsyncError):
    """A non-read HTTP method was attempted against Canvas.

    mitsync never writes to Canvas -- it holds graded work. This is raised at the
    client chokepoint rather than trusted to caller discipline.
    """

    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url
        super().__init__(
            f"refusing {method.upper()} to Canvas ({url}): mitsync is read-only. "
            "Canvas holds graded work; no command may modify it."
        )


class GradescopeAuthError(MitsyncError):
    """GRADESCOPE_COOKIE is missing or expired (Gradescope redirected to /login)."""


class GradescopeWriteRefused(MitsyncError):
    """A non-read HTTP method was attempted against Gradescope, which holds graded work."""

    def __init__(self, method: str, url: str) -> None:
        super().__init__(
            f"refusing {method.upper()} to Gradescope ({url}): mitsync is read-only. "
            "Gradescope holds graded work; no command may modify it."
        )


class ForumRefused(MitsyncError):
    """`forum act` will not post: the course team paused the forum, the hourly
    budget is spent, the agent stopped after repeated failures, or the message
    broke a boundary. The message names which, and nothing was sent."""


class ScholarBlocked(MitsyncError):
    """Google Scholar answered with a CAPTCHA or an error instead of results.
    Not retried: the agent posts from the course knowledge alone this run."""


class ForumOutcomeUnknown(MitsyncError):
    """A forum post was sent but neither answered nor visible on Canvas yet
    (Canvas's discussion view is cached). It is never retried blindly: the
    next run reconciles it once Canvas shows the forum as it is."""


class ManifestBusy(MitsyncError):
    """Another mitsync process (almost always a `sync`) holds the manifest's
    lock, and it was not released within the wait. Expected, not a failure:
    run the command again once that sync finishes."""
