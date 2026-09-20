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
