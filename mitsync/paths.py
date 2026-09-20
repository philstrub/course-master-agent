"""Filesystem layout. Every module asks Paths() rather than joining strings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parent


def _default_workspace() -> Path:
    env = os.environ.get("MITSYNC_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return _REPO_ROOT.parent


@dataclass(frozen=True)
class Paths:
    """Resolved locations. Construct with no arguments in normal use."""

    workspace: Path = None  # type: ignore[assignment]
    repo: Path = _REPO_ROOT

    def __post_init__(self) -> None:
        if self.workspace is None:
            object.__setattr__(self, "workspace", _default_workspace())
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(self, "repo", Path(self.repo).expanduser().resolve())

    # --- workspace-side (course data; lives outside the repo) ---
    @property
    def canvas_mirror(self) -> Path:
        return self.workspace / "_canvas"

    @property
    def kb(self) -> Path:
        return self.workspace / "_kb"

    @property
    def kb_text(self) -> Path:
        return self.kb / "text"

    @property
    def kb_graph(self) -> Path:
        return self.kb / "graph"

    @property
    def kb_courses(self) -> Path:
        return self.kb / "courses"

    @property
    def kb_briefings(self) -> Path:
        return self.kb / "briefings"

    # --- repo-side ---
    @property
    def config_dir(self) -> Path:
        return self.repo / "config"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.yml"

    @property
    def state_dir(self) -> Path:
        return self.repo / "state"

    @property
    def tasks_dir(self) -> Path:
        return self.state_dir / "tasks"

    @property
    def undo_dir(self) -> Path:
        return self.state_dir / "undo"

    @property
    def plans_dir(self) -> Path:
        return self.state_dir / "plans"

    @property
    def manifest_db(self) -> Path:
        return self.state_dir / "manifest.duckdb"

    @property
    def graph_db(self) -> Path:
        return self.state_dir / "graph.duckdb"

    def writable_dirs(self) -> list[Path]:
        return [
            self.canvas_mirror,
            self.kb,
            self.kb_text,
            self.kb_graph,
            self.kb_courses,
            self.kb_briefings,
            self.state_dir,
            self.tasks_dir,
            self.undo_dir,
            self.plans_dir,
        ]

    def ensure(self) -> Paths:
        """mkdir -p every directory mitsync writes to."""
        for d in self.writable_dirs():
            d.mkdir(parents=True, exist_ok=True)
        return self

    # --- guardrails ---
    def is_inside_workspace(self, p: Path | str) -> bool:
        """True iff `p` resolves to the workspace or something under it.

        Resolution follows symlinks, so a symlink pointing out of the workspace
        is correctly rejected.
        """
        try:
            candidate = Path(p).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            candidate = candidate.resolve()
        except (OSError, RuntimeError):
            return False
        return candidate == self.workspace or self.workspace in candidate.parents

    def safe_relative(self, p: Path | str) -> Path:
        """Workspace-relative form of `p`, refusing anything that escapes it."""
        if not self.is_inside_workspace(p):
            raise ValueError(f"path escapes workspace {self.workspace}: {p}")
        candidate = Path(p).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve().relative_to(self.workspace)
