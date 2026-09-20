"""The Judge interface, task specs, and result validation.

Every judgment step in mitsync builds a JudgeTask and hands it to a Judge. The
three drivers (api / agent / rules) are interchangeable and must produce results
that validate against the same JSON schema, so callers apply them identically.
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
    if resolved == "rules":
        from .rules_driver import RulesJudge

        return RulesJudge(settings)
    raise ConfigError(f"unknown driver {resolved!r}")
