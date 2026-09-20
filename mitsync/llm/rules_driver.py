"""
# Rules Driver

Deterministic heuristics, no model, for dry runs, tests and CI.

## 1. What This Module Does

Answers the judgment tasks that can honestly be answered by pattern matching:
`organize_plan` (bucket a file from its module name, Canvas folder and
filename) and `course_map` (match a Canvas course to an existing folder by
token overlap and string similarity). For `graph_extract` it emits nothing, and
for `course_notes` it assembles a file inventory rather than a summary.

## 2. Why This Module Exists

The test suite and CI must exercise the full command paths -- plan, apply,
undo, build -- end to end, with no network, no API key, and no installed
provider SDK. This driver is what makes that possible, and it doubles as the
`--driver rules` dry run for a user who wants to see what the deterministic
part of the system would do.

## 3. How It Fits in the Architecture

Selected by `base.get_judge` for driver `rules`. It is the one driver that
imports from elsewhere in mitsync (`organize.classify_bucket`,
`course_map.course_numbers_in`), because its whole job is to reuse the
deterministic logic those modules already own rather than restate it.

## 4. Key Concepts

**It never guesses, and it says when it cannot answer.** Results are
deliberately stamped at confidence 0.5 -- exactly `organize.REVIEW_THRESHOLD`
-- so downstream review treats every one of them as provisional rather than
applying them unattended.

**Empty is a real answer, and an honest one.** No heuristic can mine concept
triples out of prose, so `graph_extract` returns a schema-valid empty result
and logs a warning naming the document. That keeps `--driver rules` working end
to end while making it obvious in the log that no extraction happened. An
unknown task name does the same rather than raising, because a missing
heuristic is a known limitation of this driver, not a bug in the caller.

**Course notes are an inventory, not a summary.** The generated text says so in
its own body and in its open questions, so a reader can never mistake a list of
filenames for an understanding of the material.

**Why no exception is caught here.** It calls nothing external. Every input is
a payload already validated upstream, and every output is checked against the
task schema by the caller.
"""

from __future__ import annotations

import difflib
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from ..course_map import course_numbers_in
from ..logging import get_logger
from ..organize import classify_bucket
from .base import JudgeTask

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = get_logger(__name__)

CONFIDENCE = 0.5

_EMPTY: dict[str, str] = {"organize_plan": "placements", "course_map": "mappings"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def _score(course_name: str, folder: str) -> float:
    a, b = _tokens(course_name), _tokens(folder)
    overlap = len(a & b) / len(a | b) if (a | b) else 0.0
    ratio = difflib.SequenceMatcher(None, course_name.lower(), folder.lower()).ratio()
    return 0.6 * overlap + 0.4 * ratio


def _course_number(*texts: str | None) -> str | None:
    for text in texts:
        numbers = course_numbers_in(text or "")
        if numbers:
            return numbers[0]
    return None


class RulesJudge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def judge(self, task: JudgeTask) -> dict[str, Any]:
        handler = getattr(self, f"_task_{task.name}", None)
        if handler is None:
            key = _EMPTY.get(task.name)
            log.warning("rules driver has no heuristic for task '%s'; returning empty", task.name)
            return {key: []} if key else {}
        return handler(task.payload)

    def _task_organize_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        placements = []
        for f in payload.get("files", []):
            name = str(f.get("display_name") or "file")
            course = str(f.get("course") or "").strip("/")
            bucket = classify_bucket(name, f.get("module_name"), f.get("canvas_folder"))
            dest = PurePosixPath(course) / bucket / name if course else PurePosixPath(bucket) / name
            placements.append(
                {
                    "file_id": str(f.get("file_id")),
                    "destination": dest.as_posix(),
                    "reason": f"heuristic: filed under {bucket}/ from module and filename signals",
                    "confidence": CONFIDENCE,
                }
            )
        return {"placements": placements}

    def _task_course_map(self, payload: dict[str, Any]) -> dict[str, Any]:
        folders = [str(x) for x in payload.get("existing_folders", [])]
        mappings = []
        for course in payload.get("courses", []):
            name = str(course.get("name") or "")
            code = str(course.get("course_code") or "")
            best, best_score = "", 0.0
            for folder in folders:
                s = max(_score(name, folder), _score(code, folder))
                if s > best_score:
                    best, best_score = folder, s
            number = _course_number(code, name)
            aliases = sorted({a for a in (code, number) if a and a != best})
            mappings.append(
                {
                    "canvas_id": int(course.get("id", 0)),
                    "folder": best or name or code or "unmapped",
                    "course_number": number,
                    "aliases": aliases,
                    "confidence": round(min(best_score, CONFIDENCE), 3),
                }
            )
        return {"mappings": mappings}

    # -- knowledge-base tasks ------------------------------------------------
    def _task_graph_extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """No heuristic can honestly mine triples from prose; emit nothing.

        Schema-valid and empty is the correct deterministic answer -- it keeps
        `--driver rules` working end to end while making it obvious in the log
        that no extraction happened.
        """
        log.warning(
            "rules driver cannot extract graph facts from %s (chunk %s); returning no nodes/edges",
            payload.get("source") or "<unknown source>",
            payload.get("chunk_index", 0),
        )
        return {"nodes": [], "edges": []}

    def _task_course_notes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Assemble notes from the file inventory alone -- no model, no guessing."""
        course = str(payload.get("course") or "this course")
        documents = [d for d in payload.get("documents", []) if isinstance(d, dict)]
        by_bucket: dict[str, list[dict[str, Any]]] = {}
        for doc in documents:
            bucket = str(doc.get("bucket") or "other")
            by_bucket.setdefault(bucket, []).append(doc)

        topics = []
        for bucket in sorted(by_bucket):
            docs = by_bucket[bucket]
            titles = [str(d.get("title") or d.get("source") or "") for d in docs if d]
            shown = ", ".join(t for t in titles[:5] if t)
            more = f" (+{len(titles) - 5} more)" if len(titles) > 5 else ""
            topics.append(
                {
                    "name": bucket.replace("_", " ").title(),
                    "summary": (
                        f"{len(docs)} item(s) filed under {bucket}/"
                        + (f": {shown}{more}." if shown else ".")
                    ),
                    "sources": [str(d.get("source")) for d in docs if d.get("source")],
                }
            )

        concepts = [str(c) for c in payload.get("concepts", []) if str(c)]
        summary = (
            f"{course}: {len(documents)} extracted document(s) across "
            f"{len(by_bucket)} bucket(s) ({', '.join(sorted(by_bucket)) or 'none'}). "
            "This inventory was assembled by the deterministic rules driver, not a model, "
            "so it lists what exists rather than what it means."
        )
        if concepts:
            summary += f" Known graph concepts: {', '.join(concepts[:10])}."

        open_questions = [
            "These notes are a file inventory only; re-run with --driver agent or "
            "--driver api for a real summary of the material.",
        ]
        if not documents:
            open_questions.append(
                f"No extracted text was available for {course}; run `mitsync extract` first."
            )
        return {"summary": summary, "topics": topics, "open_questions": open_questions}
