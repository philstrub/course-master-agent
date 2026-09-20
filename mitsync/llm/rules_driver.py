"""The `rules` driver: deterministic heuristics, no model.

Used for dry runs, tests and CI. Results are intentionally low-confidence (0.5)
so downstream review treats them as provisional. Unknown tasks never raise --
they return a well-formed empty result and log a warning.
"""

from __future__ import annotations

import difflib
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from .base import JudgeTask

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = get_logger(__name__)

CONFIDENCE = 0.5

# Ordered: first match wins. Patterns run against a *normalised* string where
# `_`, `-` and `.` become spaces, so real filenames like `15_095_hw1.pdf`,
# `Lec03_2026.pdf` and `deliv_1_15072_Fall2026.pdf` tokenise the way a reader
# would expect rather than hiding the signal inside one long word.
_BUCKET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("syllabus", re.compile(r"\bsyllab|\bcourse info|\blogistics\b|\bgrading\b", re.I)),
    ("recitations", re.compile(r"\brecit\w*|\brec\s?\d|\bsection\s?\d|\btutorial", re.I)),
    (
        "assignments",
        re.compile(
            r"\bhw\b|\bhw\s?\d|homework|assign\w*|deliv\w*|\bpset|problem\s?set"
            r"|\bps\s?\d|\bexam\b|midterm|\bquiz|\bproject\b|solution",
            re.I,
        ),
    ),
    (
        "lectures",
        re.compile(
            r"\blec\b|\blec\s?\d|lecture|\bslides?\b|\bunit\s?\d|\bsession\s?\d"
            r"|\bclass\s?\d|\bweek\s?\d",
            re.I,
        ),
    ),
    ("notes", re.compile(r"\bnotes?\b", re.I)),
]

_SEPARATORS = re.compile(r"[._\-]+")


def _normalise(text: str) -> str:
    """`Lec03_2026.pdf` -> `Lec03 2026 pdf`, so word boundaries actually land."""
    return _SEPARATORS.sub(" ", text)


_DATA_EXTS = {".csv", ".xlsx", ".xls", ".json", ".parquet", ".tsv"}

# 15.095 / 15_095 / 15095 / 15.C57 / 6.7900
_COURSE_NUMBER = re.compile(r"\b(\d{1,2})[._]?([A-Z]?\d{2,4})\b")
_EMPTY: dict[str, str] = {"organize_plan": "placements", "course_map": "mappings"}


def _bucket(display_name: str, module_name: str | None, canvas_folder: str | None) -> str:
    """Module grouping wins over the filename (see config/naming.md)."""
    for text in (module_name, canvas_folder):
        if not text:
            continue
        for bucket, rx in _BUCKET_PATTERNS:
            if rx.search(_normalise(text)):
                return bucket
    if PurePosixPath(display_name).suffix.lower() in _DATA_EXTS:
        return "data"
    stem = _normalise(PurePosixPath(display_name).stem)
    for bucket, rx in _BUCKET_PATTERNS:
        if rx.search(stem):
            return bucket
    return "other"


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def _score(course_name: str, folder: str) -> float:
    a, b = _tokens(course_name), _tokens(folder)
    overlap = len(a & b) / len(a | b) if (a | b) else 0.0
    ratio = difflib.SequenceMatcher(None, course_name.lower(), folder.lower()).ratio()
    return 0.6 * overlap + 0.4 * ratio


def _course_number(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        m = _COURSE_NUMBER.search(text)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
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
            bucket = _bucket(name, f.get("module_name"), f.get("canvas_folder"))
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
