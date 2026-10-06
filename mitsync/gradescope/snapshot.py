"""
# Gradescope Snapshot

The shape of what `gradescope sync` saw, and the two derivations other
modules need from it.

## 1. What This Module Does

Declares `Snapshot` / `GsCourse` / `GsAssignment` (pydantic, `extra="forbid"`),
reads and writes `state/gradescope.json`, and renders an assignment's status
the way `deadlines.submission_status` renders Canvas's (`status_text`).
`title_key` is re-exported from the ontology, which owns naming.

## 2. Why This Module Exists

Canvas calls it "15.C57 - HW 1: Linear Optimization - Fall 2026", Gradescope
calls it "HW 1", and the student's folder is `hw-01`. Joining the three is the
whole point of reading Gradescope, and doing it in one function keeps `due`
and the graph from disagreeing about which assignment is which.

## 3. How It Fits in the Architecture

A leaf: no HTTP, no parsing. `client` writes the file, `deadlines` and
`graph` read it.

## 4. Key Concepts

**Title keys.** `ontology.title_key` reduces "HW 1", "Homework 01" and
`hw-01` to `("hw", "1")`. Two titles match when their keys are equal.

**Status.** One of the ontology's `submission_status` values. A score on the
dashboard means `graded`, and "No Submission" after the due date means `missing`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from mitsync.knowledge.ontology import title_key

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from mitsync.core.config import Settings

__all__ = ["GsAssignment", "GsCourse", "Snapshot", "load", "path", "status_text", "title_key"]

Status = Literal["unsubmitted", "submitted", "late", "missing", "graded"]


class GsAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gradescope_id: str | None
    title: str
    url: str | None = None
    released_at: datetime | None = None
    due_at: datetime | None = None
    late_due_at: datetime | None = None
    status: Status
    score: float | None = None
    points: float | None = None


class GsCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gradescope_id: str
    shortname: str
    name: str
    term: str | None = None
    folder: str | None  # the student's folder per courses.yml; None when unmapped
    assignments: list[GsAssignment] = []


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetched_at: datetime
    courses: list[GsCourse]


def path(settings: Settings) -> Path:
    return settings.paths.state_dir / "gradescope.json"


def load(settings: Settings) -> Snapshot | None:
    """The last snapshot, or None when `gradescope sync` has never run."""
    p = path(settings)
    return Snapshot.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else None


def status_text(a: GsAssignment) -> str:
    """`deadlines.submission_status`'s spelling: `graded, 8/10`, `submitted (late)`."""
    text = {"late": "submitted (late)"}.get(a.status, a.status)
    if a.score is not None and a.points:
        text += f", {a.score:g}/{a.points:g}"
    return text
