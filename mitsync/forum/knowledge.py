"""
# Forum knowledge

What the forum agent may know about the courses: what was taught, never what
the student did.

## 1. What This Module Does

* `outline(course)` -- the `Lectures` and `Concepts` sections of a course's
  master file (`_kb/courses/<Course>/COURSE.md`), with file paths removed.
* `search(term)` -- the graph's concepts whose name, aliases or description
  match `term`, where each is taught (course and lecture), and short passages
  around the term from lecture, recitation and syllabus text.

## 2. Why This Module Exists

The forum agent is useful to other students because it can bring in the
student's organized course knowledge: a concept from Optimization or Machine
Learning that bears on an agent-design question. It must not leak the rest of
the workspace. Its file tools are confined to `_kb/forum/`, so this module is
its only window onto the knowledge base, and it shows lecture material only.

## 3. How It Fits in the Architecture

Read-only over the graph's DuckDB projection (`knowledge.graph`) and the
extracted text pages. Called by `mitsync forum knowledge`, which the forum
wrapper exposes.

## 4. Key Concepts

**What is withheld.** Assignment items and their files (homework, the
student's reports and submissions), anything under an `assignments/` or
`notes/` folder, the master files' `Assignments`, `Schedule` and `Open questions`
sections, and every path. Submission status, scores, briefs and the calendar
are not reachable from here at all.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from mitsync.core.config import Settings
from mitsync.core.errors import MitsyncError
from mitsync.knowledge import graph as graph_mod

__all__ = ["outline", "search"]

SHARED_SECTIONS = ("Lectures", "Concepts")
SNIPPET = 260
MAX_PASSAGES = 6
PER_COURSE = 2  # so passages span fields, not one course's slides
_WITHHELD_PATH = re.compile(r"/(?:assignments|notes)/")

_CONCEPTS = """
    SELECT DISTINCT k.label AS concept,
           json_extract_string(k.attrs, '$.description') AS description,
           c.label AS course, i.label AS item, json_extract_string(i.attrs, '$.title') AS title
    FROM nodes k
    JOIN edges ce ON ce.s = k.id AND ce.p IN ('concept_in_lecture', 'concept_in_recitation')
    JOIN nodes i ON i.id = ce.o
    JOIN edges pe ON pe.s = i.id AND pe.p IN ('lecture_of_course', 'recitation_of_course')
    JOIN nodes c ON c.id = pe.o
    WHERE k.type = 'Concept'
      AND (lower(k.label) LIKE $pattern OR lower(k.attrs) LIKE $pattern)
    ORDER BY concept, course, item
"""

_TEXTS = """
    SELECT DISTINCT c.label AS course, i.label AS item,
           json_extract_string(i.attrs, '$.title') AS title,
           json_extract_string(f.attrs, '$.path') AS path,
           json_extract_string(f.attrs, '$.text') AS text
    FROM nodes f
    JOIN edges fe ON fe.s = f.id
         AND fe.p IN ('file_of_lecture', 'file_of_recitation', 'file_of_syllabus')
    JOIN nodes i ON i.id = fe.o
    JOIN edges pe ON pe.s = i.id
         AND pe.p IN ('lecture_of_course', 'recitation_of_course', 'syllabus_of_course')
    JOIN nodes c ON c.id = pe.o
    WHERE json_extract_string(f.attrs, '$.text') IS NOT NULL
    ORDER BY course, item
"""


def outline(settings: Settings, course: str) -> str:
    """The shareable sections of a course's master file."""
    courses = sorted(
        p.name for p in settings.paths.kb_courses.iterdir() if (p / "COURSE.md").exists()
    )
    if course not in courses:
        raise MitsyncError(f"no master file for {course!r}; courses: {', '.join(courses)}")
    text = (settings.paths.kb_courses / course / "COURSE.md").read_text()
    sections = re.split(r"(?m)^## ", text)[1:]
    kept = ["## " + s for s in sections if s.split("\n", 1)[0].strip() in SHARED_SECTIONS]
    # Paths and slide references in backticks point into the student's folders.
    return re.sub(r"`[^`]*(?:/|\.pdf|\.ipynb|\.md)[^`]*`", "", "\n".join(kept)).strip()


def search(settings: Settings, term: str) -> dict[str, Any]:
    """Concepts matching `term`, where they are taught, and passages that mention it."""
    if len(term.strip()) < 3:
        raise MitsyncError("search for at least three characters")
    concepts: dict[str, dict] = {}
    rows = graph_mod.query(
        settings, sql=_CONCEPTS, params={"pattern": f"%{term.lower()}%"}, show=False
    )
    for row in rows:
        c = concepts.setdefault(
            row["concept"],
            {"concept": row["concept"], "description": row["description"], "taught_in": []},
        )
        c["taught_in"].append(
            f"{row['course']}, {row['item']}" + (f" ({row['title']})" if row["title"] else "")
        )

    passages: list[dict] = []
    per_course: Counter[str] = Counter()
    needle = re.compile(re.escape(term.strip()), re.IGNORECASE)
    for row in graph_mod.query(settings, sql=_TEXTS, show=False):
        if _WITHHELD_PATH.search("/" + row["path"]) or per_course[row["course"]] >= PER_COURSE:
            continue  # withheld by design (module docstring), or this course has given enough
        body = (
            (settings.paths.workspace / row["text"])
            .read_text(encoding="utf-8")
            .split("\n---\n", 1)[-1]
        )
        hit = needle.search(body)
        if hit:
            start = max(0, hit.start() - SNIPPET // 2)
            per_course[row["course"]] += 1
            passages.append({
                "course": row["course"],
                "where": row["item"] + (f" ({row['title']})" if row["title"] else ""),
                "passage": " ".join(body[start : start + SNIPPET].split()),
            })  # fmt: skip
    passages = passages[:MAX_PASSAGES]
    return {"term": term, "concepts": list(concepts.values()), "passages": passages}
