"""
# mitsync

Mirror MIT Canvas course material, file it into the student's own folders, and
index the result for an agent to read.

## 1. What This Module Does

Names the package and pins `__version__`. It deliberately holds no logic:
importing `mitsync` must stay free of side effects and of heavy dependencies,
so `import mitsync.paths` never drags in httpx, duckdb, or a provider SDK.

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

Layering, bottom up: `paths`, `clock`, `errors`, `logging` and `env` are
leaves; `config` validates settings; `canvas_client`, `manifest` and `sync`
mirror Canvas; `course_map` and `organize` file it; `extract`, `graph` and `kb`
index it; `deadlines` and `calendar_read` report on it; `llm/` is the only
package allowed to talk to a model; `cli` wires them together and is the only
place an error becomes an exit code.

## 4. Key Concepts

**Judgment is a driver, not a dependency.** Exactly four commands need a model
-- `map`, `organize plan`, `graph extract`, `kb build` -- and each runs three
ways (`api`, `agent`, `rules`), so the tool is fully usable with no
credentials at all.

**Prose is configuration.** `config/naming.md` is the filing policy. It is
injected verbatim into judgment payloads, so editing that prose changes the
next plan with no code change, and filing rules are never hardcoded in Python.

**Catch-up safety.** Work is always derived from state (what the manifest
holds versus what Canvas holds), never from time since the last run, so a
missed or late scheduled run costs nothing.
"""

__version__ = "0.1.0"
