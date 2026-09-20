"""Exception hierarchy shared by every mitsync module."""

from __future__ import annotations

from pathlib import Path

# Exit code meaning "I wrote a task file; a driving agent must judge it".
EXIT_PENDING_JUDGMENT = 20


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


class JudgeUnavailable(MitsyncError):
    """The requested judgment driver cannot run (missing SDK, key, or model)."""


class OntologyError(MitsyncError):
    """A graph record violates config/ontology.yml."""


class PendingJudgment(MitsyncError):
    """The agent driver wrote a task file and needs an agent to resolve it."""

    def __init__(self, task_path: Path, task_name: str, instructions: str) -> None:
        self.task_path = Path(task_path)
        self.task_name = task_name
        self.instructions = instructions
        super().__init__(f"Judgment '{task_name}' pending: {self.task_path}")


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
