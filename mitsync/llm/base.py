"""
# Judge Interface

The task specification, the `Judge` protocol, and the result validation every
driver is held to.

## 1. What This Module Does

Defines `JudgeTask` (what to decide, the data, the rules, and the output
shape), the `Judge` protocol, `make_task` for building a task from a
registered spec, `validate_result` for checking an answer against that
task's JSON schema, and `get_judge` for resolving the configured driver.

## 2. Why This Module Exists

The three drivers are only interchangeable if "a correct answer" means the
same thing for all of them. That definition is a JSON schema, it lives in the
task spec, and it is enforced here -- on the model's reply in `api_driver`, on
the agent's file in `resolve`, and on the heuristic's output in tests. A
caller can therefore apply any result without knowing where it came from.

Validation failures name the failing JSON path rather than saying "invalid",
because the consumer of that message is often an agent that has to correct its
own answer and try again.

## 3. How It Fits in the Architecture

The seam between the callers (`course_map`, `organize`, `graph`, `kb`) and the
drivers. Callers import `make_task`, `validate_result` and `get_judge` and
nothing else; the drivers are imported lazily inside `get_judge` so that
choosing one never imports the others.

## 4. Key Concepts

**Task specs are data, not code.** `tasks/<name>.json` holds the name,
version, schema and instructions. Registering a new kind of judgment is a new
spec file plus a caller, and an unknown task name fails loudly with the list of
available ones.

**`origin_command` and `origin_args` make replay possible.** They are stored
on the task and written into the task file so `mitsync resolve` can re-run the
command that asked the question, with the answer substituted in.

**Rules travel with the task.** `task.rules` is the applicable prose supplied
by the caller -- for filing, the verbatim current text of `config/naming.md`.
It is passed through untouched, which is what makes that file the tuning
surface.

**Why an exception is raised, not caught, here.** `ResultValidationError` is
this module's product: it carries the failing paths so the caller (or the
agent) can fix the answer. Nothing in this file catches anything; a missing or
malformed task spec is a `ConfigError` naming the file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ConfigError, MitsyncError

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


class JudgeTask(BaseModel):
    """A single unit of judgment: what to decide, the data, and the output shape."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    version: int = 1
    schema_: dict[str, Any] = Field(alias="schema")
    payload: dict[str, Any] = Field(default_factory=dict)
    rules: str = ""
    instructions: str = ""
    # Command that produced this task, so `mitsync resolve` can replay it.
    origin_command: str | None = None
    origin_args: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Judge(Protocol):
    def judge(self, task: JudgeTask) -> dict[str, Any]: ...


class ResultValidationError(MitsyncError):
    """A judgment result did not match its task schema."""


@lru_cache(maxsize=32)
def load_task_spec(name: str) -> dict[str, Any]:
    """Load `tasks/<name>.json`: {name, version, schema, instructions}."""
    path = TASKS_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in TASKS_DIR.glob("*.json"))
        raise ConfigError(f"unknown judge task {name!r}; available: {', '.join(available)}")
    spec = json.loads(path.read_text())
    missing = {"name", "version", "schema", "instructions"} - set(spec)
    if missing:
        raise ConfigError(f"task spec {path} is missing {sorted(missing)}")
    return spec


def make_task(
    name: str,
    payload: dict[str, Any],
    rules: str = "",
    origin_command: str | None = None,
    origin_args: dict[str, Any] | None = None,
) -> JudgeTask:
    """Build a JudgeTask from a registered task spec plus caller data."""
    spec = load_task_spec(name)
    return JudgeTask(
        name=spec["name"],
        version=spec["version"],
        schema=spec["schema"],
        payload=payload,
        rules=rules,
        instructions=spec["instructions"],
        origin_command=origin_command,
        origin_args=origin_args or {},
    )


def validate_result(task: JudgeTask, result: Any) -> dict[str, Any]:
    """Validate `result` against the task schema, raising with the failing path."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(task.schema_)
    errors = sorted(validator.iter_errors(result), key=lambda e: list(e.absolute_path))
    if errors:
        lines = []
        for err in errors[:10]:
            where = "/".join(str(p) for p in err.absolute_path) or "<root>"
            lines.append(f"  at {where}: {err.message}")
        raise ResultValidationError(
            f"result for task '{task.name}' failed validation:\n" + "\n".join(lines)
        )
    return result


def get_judge(settings: Settings, driver: str | None = None) -> Judge:
    """Return the Judge for the resolved driver (explicit flag > settings > auto)."""
    resolved = settings.resolve_driver(driver)
    if resolved == "api":
        from .api_driver import ApiJudge

        return ApiJudge(settings)
    if resolved == "agent":
        from .agent_driver import AgentJudge

        return AgentJudge(settings)
    from .rules_driver import RulesJudge

    return RulesJudge(settings)
