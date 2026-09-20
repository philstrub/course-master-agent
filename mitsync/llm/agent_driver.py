"""
# Agent Driver

No model call at all: write the question to a file and let the driving agent
answer it.

## 1. What This Module Does

`AgentJudge.judge` serialises the whole task -- payload, rules, instructions,
result schema, the originating command, and step-by-step resolution
instructions -- into `state/tasks/<name>-<UTC stamp>-<hash>.json`, then raises
`PendingJudgment`. `resolve_task` reads an agent's answer back, validates it
against the task's schema, and stores it beside the task file.

## 2. Why This Module Exists

mitsync is usually run *by* an agent that is already a capable model. Paying a
second model to make the same judgment is wasteful and adds a credential
requirement for no benefit, which is why this is the default driver when no API
key is configured.

The task file is deliberately self-contained: an agent that has never seen this
repo can read one file and know what to decide, what rules apply, what shape
the answer must take, and exactly which command to run next. That is what makes
the loop work across Claude Code, OpenClaw, or any other chatbot.

## 3. How It Fits in the Architecture

Selected by `base.get_judge` for driver `agent`. The `PendingJudgment` it
raises propagates untouched through every caller -- `graph` and `kb` both
re-raise it ahead of their broader handlers -- and becomes **exit code 20** in
the CLI. `mitsync resolve` then calls `resolve_task` and hands the validated
answer to the replay table in `cli`, which re-runs the originating command so
the decision is applied by exactly the same code path `--driver api` would have
used.

## 4. Key Concepts

**Exit code 20 means pending, not broken.** Anything wrapping mitsync must
treat 20 as "answer the task and re-run", not as a failure.

**Nothing is applied by the driver.** Judgment and effect stay separate: this
module writes a question, and `resolve` -- after validation -- produces the
same file moves, writes and log entries the API driver would have.

**The task filename carries a timestamp and a content hash**, so concurrent or
repeated runs never collide and an answer can always be matched to the exact
question it answers.

**One round trip per judgment.** `kb build` deliberately judges one course per
task file, so a resolve keeps the courses already written and stops at the next
unresolved one with a fresh task.

**Why no exception is caught here.** Nothing external is called. A malformed
result file raises from `json` or from `validate_result`, which names the
failing JSON path; the agent fixes that path and runs `resolve` again rather
than forcing a bad answer through.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..clock import now_iso
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
        """Write the task file to state/tasks/<name>-<ts>-<hash>.json and stop."""
        body = task.model_dump(by_alias=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[
            :8
        ]
        task_path = self.settings.paths.tasks_dir / f"{task.name}-{stamp}-{digest}.json"
        result_path = task_path.with_suffix(".result.json")

        document = {
            "task": task.name,
            "version": task.version,
            "created_at": now_iso(),
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
        raise PendingJudgment(task_path, task.name, _how_to_resolve(task_path, result_path))


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
