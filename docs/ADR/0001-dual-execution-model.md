# ADR 0001: Dual execution model (api / agent / rules drivers)

## Status

Accepted

## Context

`mitsync` needs judgment for several steps — classifying which folder a
newly synced file belongs in, naming it consistently with the student's
own scheme, writing per-course notes, extracting knowledge-graph triples,
and synthesizing a "what's due / what to read" briefing. The tool must
work in at least two very different situations: (a) the student has
configured a cloud LLM API key and wants everything automated, and (b) the
student (or another agent, e.g. Claude Code or OpenClaw) is sitting right
there in a terminal or chat session and can do the reasoning itself,
without the tool needing any credentials at all. The secondary user this
project optimizes for is explicitly a downstream agent, so "an agent is
already driving this" had to be a first-class mode, not an afterthought
bolted onto an API-only design.

## Decision

Every judgment-requiring command takes a `Judge` interface
(`judge(self, task: JudgeTask) -> dict`, in `mitsync/llm/base.py`) rather
than a model client. Three
implementations exist:

- `api` — calls a configured cloud provider directly and returns its
  (schema-validated) answer.
- `agent` (the default when no API key is configured) — writes a
  self-contained task file (payload + JSON schema + rules + instructions)
  to `state/tasks/`, raises `PendingJudgment`, which the CLI turns into a
  printed task path and exit code 20. The driving agent reasons about the
  task itself and calls `mitsync resolve <task_file> --result <result.json>`,
  which validates the result against the schema and applies it through the
  exact same deterministic apply path the `api` driver would have used.
- `rules` — deterministic heuristics only, no judgment, for CI/dry-runs.

No module outside `mitsync/llm/` may import a provider SDK, and every
command's core function takes a `Judge`, never a model client — enforced
by a grep-based guardrail test.

## Consequences

- The tool works end-to-end with zero credentials, which is the
  common case for a student not wanting to pay for/manage an API key, and
  is exactly the mode the "downstream agent" secondary user exercises.
- `--driver api` and `--driver agent` are required to produce identical
  on-disk results for the same input (exercised by
  `tests/test_llm_drivers.py`), which constrains task-file schemas
  to be precise enough that either a model or a human/agent can satisfy
  them unambiguously.
- Extra implementation cost: every judgment call site needs a defined
  JSON schema and instructions up front, rather than an ad hoc prompt.
- The exit-code-20 / task-file / `resolve` cycle adds a round trip
  (command exits, agent resolves, command is re-run) compared to a single
  blocking API call — acceptable because the agent-driven case is
  inherently interactive already.

## Alternatives considered

- **API-only, with agent-driving as a documented "call the CLI with fake
  responses" pattern.** Rejected: this leaves zero-credential operation as
  an unsupported workaround rather than a real mode, and provides no
  schema/contract for what an agent's answer should look like, which the
  headline "downstream agent" use case needs to be reliable.
- **A single blocking prompt to stdin/stdout for judgment ("please answer:
  ...") instead of a task file.** Rejected: doesn't compose with
  non-interactive automation (OpenClaw cron), and doesn't give the
  resolving agent a machine-checkable schema to satisfy, so `resolve`
  couldn't validate before applying.
- **Always requiring an API key, treating agent-driving as out of scope.**
  Rejected outright by the project's own headline requirement that the
  whole tool work with zero credentials.
