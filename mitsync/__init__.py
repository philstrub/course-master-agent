"""
# mitsync

Mirror MIT Canvas course material, file it into the student's own folders, and
index the result for an agent to read.

## 1. What This Module Does

Names the package and pins `__version__`. It deliberately holds no logic:
importing `mitsync` must stay free of side effects and of heavy dependencies,
so `import mitsync.paths` never drags in httpx or duckdb.

## 2. Why This Module Exists

mitsync has three kinds of consumer and they must not pay for each other: the
Typer CLI, the pytest suite, and OpenClaw skills that import the library
directly and never touch the command line. A package root that did work on
import would impose the CLI's cost on the other two.

## 3. How It Fits in the Architecture

The tool operates on a *workspace* -- the student's course directory -- from a
repo checked out at `<workspace>/_agent`. Inside the workspace, `_canvas/` is
an immutable mirror of Canvas and the source of truth; the human-named course
folders are a curated view populated by hardlink or copy out of that mirror;
`_kb/` is entirely generated. Canvas and Apple Calendar are both read-only, by
enforcement rather than by convention.

Layering is one-directional and expressed as packages, bottom up:

* `core/` -- `paths`, `clock`, `errors`, `logging`, `env`, `config`. Imports
  nothing else in mitsync.
* `canvas/` -- `client`, `sync`, `manifest`. The only package that speaks HTTP.
* `filing/` -- `course_map`, `organize`. Mirror in, course folders out.
* `schedule/` -- `calendar`, `deadlines`. Local state and EventKit only; imports
  neither `canvas` nor `httpx`, and a test asserts it.
* `knowledge/` -- `extract`, `graph`, `kb`. Everything under `_kb/`.
* `cli` -- wires them together, and is the only place an error becomes an exit
  code.

Each package's `__init__.py` carries the constraints its modules must keep;
those docstrings, not this one, are where a future edit should look first.

## 4. Key Concepts

**Data tools only; the agent judges.** Every command reads, writes or
validates, deterministically. None of them asks for judgment: the driving
agent (OpenClaw, Claude Code) reads their JSON output and the files directly,
decides, and hands decisions back as data -- a filing plan for `organize
apply`, a JSONL of graph facts for `graph add`, a `NOTES.md` it writes itself.
mitsync never calls a model, so it needs no model credentials at all.

**Prose is configuration.** `config/naming.md` is the filing policy, and the
agent reads it itself. The code enforces only the structure that prose
promises (which buckets exist, which hold per-item folders); where a file
belongs is never decided in Python.

**Catch-up safety.** Work is always derived from state (what the manifest
holds versus what Canvas holds), never from time since the last run, so a
missed or late scheduled run costs nothing.
"""

__version__ = "0.1.0"
