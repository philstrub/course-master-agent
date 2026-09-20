"""
# Clock

The one place mitsync turns wall-clock time into text and text back into time.

## 1. What This Module Does

Provides `now_iso()` -- the single spelling of "now" that every stamp mitsync
writes uses -- and `parse_iso()`, which reads an ISO 8601 string back, normalising
the `Z` suffix Canvas and Apple Calendar emit to the explicit `+00:00` offset.

## 2. Why This Module Exists

Timestamps are compared as strings across module boundaries: `graph.extract`
skips documents whose `extracted_at` sorts below `--since`, `kb build` must
produce byte-identical output over unchanged inputs, and the append-only
`_kb/graph/*.jsonl` files are the declared source of truth. Six modules used to
each spell "now" for themselves and one of them kept microseconds, so the same
event recorded in two places did not compare equal. One function removes the
whole class of bug.

## 3. How It Fits in the Architecture

Leaf module: it imports nothing from mitsync, and everything that records or
reads a timestamp imports it. `sync`, `organize`, `graph`, `extract`,
`deadlines` and the agent driver all stamp with `now_iso()`.

## 4. Key Concepts

**Second precision.** Every stamp mitsync writes is UTC to the second. Sub-second
resolution is noise for a tool whose fastest loop is a Canvas HTTP request, and
mixed precision breaks string comparison.

**Parsing raises.** `parse_iso` does not absorb a bad value into `None`. Callers
reading a genuinely external system (a Canvas term bound, an `ical-guy` payload)
translate the `ValueError` into their own typed error at that boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """The current UTC time, ISO 8601, to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, accepting the `Z` suffix as `+00:00`.

    Raises `ValueError` on anything else.
    """
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
