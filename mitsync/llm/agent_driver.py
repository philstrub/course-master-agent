"""The `agent` driver: no model call at all.

Writes a self-contained task file and exits. The driving agent (Claude Code,
OpenClaw, any chatbot) reads it, reasons, writes a result JSON, and runs
`mitsync resolve <task> --result <result.json>`, which validates the result and
replays the originating command so it applies deterministically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import PendingJudgment
from ..logging import get_logger
from .base import JudgeTask, validate_result

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = get_logger(__name__)


def _how_to_resolve(task_path: Path, result_path: Path) -> str:
    return (
        f"1. Read this file: {task_path}\n"
        f"2. Read `instructions` and `rules`, then decide the answer for every item "
        f"in `payload`.\n"
        f"3. Write ONLY the JSON answer -- no prose, no markdown fence -- to:\n"
        f"     {result_path}\n"
        f"   It must validate against `result_schema` in this file.\n"
        f"4. Run:\n"
        f"     mitsync resolve {task_path} --result {result_path}\n"
        f"   That validates your answer and replays the original command "
        f"(`origin_command`) with it, applying the decision deterministically.\n"
        f"If your JSON fails validation, `resolve` prints the exact failing path; "
        f"fix that path and run it again."
    )


class AgentJudge:
    """Judge that delegates to whatever agent is driving the CLI."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def judge(self, task: JudgeTask) -> dict[str, Any]:
        task_path = write_task(task, self.settings)
        result_path = task_path.with_suffix(".result.json")
        raise PendingJudgment(task_path, task.name, _how_to_resolve(task_path, result_path))


def write_task(task: JudgeTask, settings: Settings) -> Path:
    """Serialize a JudgeTask to state/tasks/<name>-<ts>-<hash>.json."""
    tasks_dir = settings.paths.tasks_dir
    tasks_dir.mkdir(parents=True, exist_ok=True)

    body = task.model_dump(by_alias=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:8]
    task_path = tasks_dir / f"{task.name}-{stamp}-{digest}.json"
    result_path = task_path.with_suffix(".result.json")

    document = {
        "task": task.name,
        "version": task.version,
        "created_at": datetime.now(UTC).isoformat(),
        "origin_command": task.origin_command,
        "origin_args": task.origin_args,
        "instructions": task.instructions,
        "rules": task.rules,
        "payload": task.payload,
        "result_schema": task.schema_,
        "result_path": str(result_path),
        "how_to_resolve": _how_to_resolve(task_path, result_path),
    }
    task_path.write_text(json.dumps(document, indent=2, default=str) + "\n")
    log.info("wrote judgment task %s", task_path)
    return task_path


def load_task(task_path: Path | str) -> JudgeTask:
    """Rebuild a JudgeTask from a written task file."""
    doc = json.loads(Path(task_path).read_text())
    return JudgeTask(
        name=doc["task"],
        version=doc.get("version", 1),
        schema=doc["result_schema"],
        payload=doc.get("payload", {}),
        rules=doc.get("rules", ""),
        instructions=doc.get("instructions", ""),
        origin_command=doc.get("origin_command"),
        origin_args=doc.get("origin_args", {}),
    )


def resolve_task(task_path: Path | str, result_path: Path | str) -> dict[str, Any]:
    """Validate an agent-produced result and store it beside the task file."""
    task_path = Path(task_path)
    result_path = Path(result_path)
    task = load_task(task_path)
    raw = json.loads(result_path.read_text())
    validated = validate_result(task, raw)
    out = task_path.with_suffix(".result.json")
    out.write_text(json.dumps(validated, indent=2) + "\n")
    log.info("validated result for task '%s' -> %s", task.name, out)
    return validated
