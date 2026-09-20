"""
# Judgment Layer

The only package in mitsync allowed to import a provider SDK, and the contract
every sibling module in it must keep.

## 1. What This Module Does

Holds the judgment abstraction and its three interchangeable implementations:
`base` (the `Judge` protocol, `JudgeTask`, the registered task specs, and
schema validation), and the `api`, `agent` and `rules` drivers. Nothing here
knows what a course, a file or a folder is -- a task is a payload, a schema,
some rules and some instructions, and a result is JSON that validates.

## 2. Why This Module Exists

**The hard constraint: no module outside `mitsync/llm/` may import a provider
SDK.** `tests/test_llm_drivers.py` enforces it structurally, and enforces that
even inside `api_driver` the import sits *inside* the function that needs it,
so the package imports cleanly with no SDK installed at all.

Three properties follow from that one rule, and all three are worth more than
the convenience of an `import anthropic` somewhere handy:

* **Usable with zero credentials.** `--driver agent` calls no model, and it is
  the default when no API key is configured. A fresh checkout works.
* **Testable.** The whole suite runs against a stub judge with no network, no
  key, and no SDK.
* **Provider-agnostic.** Swapping Anthropic for OpenAI or a local
  OpenAI-compatible endpoint is a settings change confined to one file.

If a future feature seems to need a model call somewhere else, the answer is a
new task spec here, not an import there.

## 3. How It Fits in the Architecture

Callers build a `JudgeTask` with `make_task`, hand it to whatever `Judge`
`get_judge` returned, and validate the answer with `validate_result`. They
apply the result identically regardless of driver -- that interchangeability is
the entire interface, and the reason all three drivers must produce results
that satisfy the same JSON schema.

Four commands use it: `map`, `organize plan`, `graph extract`, `kb build`.
Every other command is pure I/O and never touches this package.

## 4. Key Concepts

**The three drivers.**

`api`
    Calls a cloud model directly, once, using a key from the environment.
`agent`
    Calls nothing. Writes a self-contained task file to `state/tasks/`,
    prints its path, and raises `PendingJudgment`, which the CLI turns into
    **exit code 20**. The driving agent reads the task, reasons, writes a
    result JSON, and runs `mitsync resolve`, which validates it and replays
    the originating command deterministically.
`rules`
    Deterministic heuristics, no model. For dry runs, tests and CI. It never
    guesses: it either matches a rule or returns a schema-valid empty answer
    and says so in the log.

**Exit code 20 means a judgment is pending.** It is not a failure. A wrapper
script that treats any non-zero exit as an error will break the agent loop.

**Task specs are data.** `tasks/<name>.json` carries the name, version, JSON
schema and instructions for each kind of judgment. Adding a judgment means
adding a spec and a caller, not a new code path per provider.

**Rules are injected, never hardcoded.** The caller passes the applicable
prose (for filing, the verbatim text of `config/naming.md`) as `task.rules`,
and every driver forwards it untouched.

**Payloads are untrusted.** Everything in a payload -- Canvas page bodies,
document excerpts, filenames -- is data to reason about, never instructions to
follow. Callers say so explicitly in `rules`, and any judged result is
validated before it is applied.

## 5. Testing Philosophy

Sibling modules are expected to follow the same rules this package does: no
test may require a network call, an API key, or an installed provider SDK.
Drivers are exercised through the `Judge` protocol with stubs; the `rules`
driver is the deterministic end-to-end path, and the `agent` driver is tested
by asserting on the task file it writes and the result it accepts or rejects.
"""
