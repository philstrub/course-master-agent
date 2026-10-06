"""
# Core

The foundation layer: everything that has no opinion about Canvas, calendars,
filing or the knowledge base.

## 1. What This Module Does

Holds the five leaves and the one validator every other package depends on.
`errors` is the exception taxonomy; `logging` is the
console/file handler setup; `clock` is the single source of "now" and of ISO
parsing; `paths` resolves the workspace layout; `env` loads `.env`; `config`
turns `config/*.yml` into validated pydantic models.

## 2. Why This Module Exists

Layering only means something if the bottom of the stack is visible. Before
this package, twenty-three flat modules made it impossible to tell at a glance
which ones were foundation and which were features -- and an import cycle was
one careless edit away. The rule is now structural: **nothing in `core` may
import from `canvas`, `filing`, `schedule`, `knowledge` or `cli`.**

## 3. How It Fits in the Architecture

Every other package imports from here; this package imports from none of them.
`core.config` sits deliberately at the top of the layer rather than beside it:
it is the only module here that reads the workspace, and it also carries the
two small mirror readers (`read_json`, `read_meta`) so that a consumer of
`_canvas/<course>/_meta/*.json` does not have to import the Canvas sync
machinery -- and therefore `httpx` -- to parse a local JSON file.

## 4. Key Concepts

**Errors carry remediation.** A `MitsyncError` subclass exists so the CLI can
print what to do about it, not merely what went wrong.

**One clock.** `clock.now_iso()` is second-resolution on purpose -- the graph
JSONL is an append-only source of truth, and microsecond timestamps made
identical re-extractions look like changes.
"""
