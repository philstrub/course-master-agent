"""
# Paths

The workspace layout, resolved once, so no other module joins path strings.

## 1. What This Module Does

Resolves every location mitsync reads or writes -- the Canvas mirror, the
generated knowledge base, the repo-side config and state directories -- from a
single workspace root, and enforces the boundary around that root
(`is_inside_workspace`, `safe_relative`). `unique_path` is the one place a new
name is chosen when the intended one is already taken.

## 2. Why This Module Exists

Two trees are in play and they are easy to confuse: the *workspace* (the
student's course directory, which holds the course data the tool operates on)
and the *repo* (`<workspace>/_agent`, which holds config, state and the tool
itself). Spelling `workspace / "_kb" / "text"` in a dozen modules made that
distinction implicit and the layout impossible to change.

The guardrail half exists because filing destinations are written by the
driving agent -- a language model -- and are therefore untrusted input. `safe_relative` is where an
absolute path, a `..` traversal, or a symlink pointing out of the workspace is
refused, before anything is written.

## 3. How It Fits in the Architecture

A leaf: it imports nothing from mitsync, and everything that touches the
filesystem either constructs `Paths()` or receives one as `Settings.paths`.
`MITSYNC_WORKSPACE` overrides the default root, which is what lets the entire
test suite run against a temp directory and never see the real workspace.

## 4. Key Concepts

**Workspace-relative POSIX strings.** Every path stored in the manifest, in a
plan, in an undo log, or in extracted frontmatter is relative to the workspace
and POSIX-spelled, so state survives the workspace being moved or read on
another machine.

**Resolution follows symlinks.** `is_inside_workspace` answers about the real
target, not the spelling, so a symlink pointing outside is correctly rejected.
A symlink *loop* raises instead of answering `False`: a broken tree and a path
outside the workspace are different problems and must not report identically.

**Collisions never overwrite.** Every file mitsync creates whose name it does
not control -- an archived previous version, a colliding filing destination, a
plan, an undo log -- goes through `unique_path`.

**`ensure()` is the one place mitsync's own directories are created.**
Downstream code may assume they exist; a missing one means `ensure()` was not
called, which is a bug worth seeing rather than papering over.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Anchored on this file rather than on the cwd: `mitsync` must resolve its own
# repo identically whether it was launched from the workspace, from `_agent`, or
# by a LaunchAgent with no meaningful working directory. `parents[2]` is the repo
# root because this module sits at `<repo>/mitsync/core/paths.py`.
PKG_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_DIR.parent


def unique_path(base: Path) -> Path:
    """`base` if it is free, else `<stem>-<n><suffix>` for the first free n >= 2.

    Every file mitsync creates whose name it does not control (an archived
    previous version, a collided filing destination, a plan, an undo log) goes
    through here, so a collision never overwrites what is already on disk.
    """
    candidate = base
    n = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
    return candidate


def _default_workspace() -> Path:
    env = os.environ.get("MITSYNC_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return REPO_ROOT.parent


@dataclass(frozen=True)
class Paths:
    """Resolved locations. Construct with no arguments in normal use."""

    workspace: Path = None  # type: ignore[assignment]
    repo: Path = REPO_ROOT

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

    @property
    def kb_forum(self) -> Path:
        """The forum agent's workspace: its diary, post log and decision files."""
        return self.kb / "forum"

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

    @property
    def due_json(self) -> Path:
        return self.state_dir / "due.json"

    @property
    def forum_state(self) -> Path:
        """Seen entries, the failure count and an in-flight post: code-owned, so it
        sits outside the forum agent's workspace, where its file tools cannot reach."""
        return self.state_dir / "forum"

    def writable_dirs(self) -> list[Path]:
        return [
            self.canvas_mirror,
            self.kb,
            self.kb_text,
            self.kb_graph,
            self.kb_courses,
            self.kb_briefings,
            self.kb_forum,
            self.kb_forum / "decisions",
            self.state_dir,
            self.forum_state,
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
        is correctly rejected. A symlink *loop* raises rather than answering
        False: it is a broken tree, not a path outside the workspace, and the
        two must not be reported with the same message.
        """
        candidate = Path(p).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.resolve()
        return candidate == self.workspace or self.workspace in candidate.parents

    def safe_relative(self, p: Path | str) -> Path:
        """Workspace-relative form of `p`, refusing anything that escapes it."""
        if not self.is_inside_workspace(p):
            raise ValueError(f"path escapes workspace {self.workspace}: {p}")
        candidate = Path(p).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve().relative_to(self.workspace)
