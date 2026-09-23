"""
# Organize

File the Canvas mirror into the student's own course folders -- proposed
first, applied only on review, and always undoable.

## 1. What This Module Does

Two stages, deliberately separated so nothing moves without review:

`plan`
    Ask a `Judge` where every unfiled file should go, validate each proposed
    destination against the workspace guardrails, and write
    `state/plans/plan-<ts>.json`. **This never touches the filesystem.**
`apply_plan` / `undo`
    Execute a saved plan and reverse it. Canvas-mirrored files are *linked or
    copied* into the curated folder; `_canvas/` stays the source of truth and
    is never emptied. Only pre-existing student files are really moved, and
    only those need -- and get -- an undo entry that restores them
    byte-for-byte.

## 2. Why This Module Exists

The student's course folders are their own. A tool that reorganises them on a
model's say-so, with no preview and no way back, is not one anybody should
run. Hence the plan/apply split, the confidence threshold below which a
placement is shown rather than applied, the explicit confirmation before any
pre-existing file is moved, and the undo log that refuses to reverse anything
whose bytes changed in the meantime.

## 3. How It Fits in the Architecture

Sits above `manifest` (which says what is mirrored and not yet filed) and
`course_map` (which says which Canvas course is which folder), and below
`kb`, which reuses `BUCKETS` and `classify_bucket` for its display order.
`organize` imports `course_map`, never the reverse.

## 4. Key Concepts

**The filing rules are prose, not code.** `config/naming.md` is read fresh on
every run and injected verbatim into the judgment payload, so editing that
file changes the next plan with no code change. Filing rules must never be
hardcoded in Python. `classify_bucket` is the deterministic fallback used by
the rules driver, not the policy itself.

**Buckets.** `lectures`, `recitations`, `assignments`, `data`, `syllabus`,
`notes`, `other` -- the vocabulary `naming.md` allows inside a course folder.
Grouping context (a Canvas module name, a Canvas folder, the student's own
subfolder) always beats the filename: a file inside `Recitation 2/` is a
recitation even when it is called `walkthrough.pdf`.

**Destinations are untrusted input.** They come from a language model.
`validate_destination` rejects absolute paths, `..` traversal, anything
outside the workspace, the reserved `_canvas`/`_agent`/`_kb` trees, ignored
paths, and directory-only destinations -- at plan time *and* again at apply
time, because a plan file can be edited between the two.

**A name collision never overwrites.** An identical file already at the
destination is a skip; a different one gets a suffixed name and is flagged for
review.

**Why exceptions are caught here.** Three, each at a real boundary:

1. `ValueError` from `validate_destination` -- this is untrusted judge output
   being validated. A bad destination is recorded in `plan.rejected` (or the
   apply report's errors) with the reason, and the run continues; one
   hallucinated path must not cost the other forty placements.
2. `OSError` around an individual file operation -- a hardlink across
   filesystems falls back to a copy, and a failed move or unlink is recorded
   per file so the undo log still describes everything that *did* happen. An
   apply that aborted midway with no log would be the worst possible outcome.
3. `OSError` in `_prune_empty`, narrowed to `ENOTEMPTY`/`EEXIST` -- on macOS
   Finder can write a `.DS_Store` between the emptiness check and the `rmdir`.
   Losing that race means the directory is no longer ours to remove, so the
   climb stops. Every other `OSError` raises: a directory this tool can
   neither read nor remove inside its own workspace is a state worth failing
   on, not one to pass over in silence.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from mitsync.core.clock import now_iso
from mitsync.core.config import read_json
from mitsync.core.errors import MitsyncError
from mitsync.core.logging import get_logger
from mitsync.core.paths import unique_path
from mitsync.filing.course_map import (
    SEPARATORS_RX,
    existing_course_folders,
    folder_for_canvas_id,
    is_ignored,
    naming_rules,
    walk,
)
from mitsync.llm.base import make_task, validate_result

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)
console = Console()

__all__ = [
    "ApplyReport",
    "Plan",
    "PlanEntry",
    "UndoReport",
    "apply_plan",
    "plan",
    "undo",
]

#: Subfolders `config/naming.md` allows inside a course folder. Canonical: the
#: knowledge base derives its own display order from this tuple.
BUCKETS = ("lectures", "recitations", "assignments", "data", "syllabus", "notes", "other")

#: Top-level names that are mitsync's own, never a filing destination.
RESERVED_TOP_LEVEL = ("_canvas", "_agent", "_kb")

#: Placements below this are shown for review instead of applied (naming.md §6).
REVIEW_THRESHOLD = 0.5

#: Bucket classification, ordered: first match wins. Patterns run against a
#: *normalised* string where `_`, `-` and `.` become spaces, so real filenames
#: like `15_095_hw1.pdf`, `Lec03_2026.pdf` and `deliv_1_15072_Fall2026.pdf`
#: tokenise the way a reader would expect rather than hiding the signal inside
#: one long word.
_BUCKET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "syllabus",
        re.compile(r"\bsyllab|\bcourse info|\blogistics?\b|\bgrading\b|\bschedule\b", re.I),
    ),
    (
        "recitations",
        re.compile(r"\brecit\w*|\brec\b|\brec\s?\d|\bsection\b|\bsection\s?\d|\btutorial", re.I),
    ),
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
            r"\blec\b|\blec\s?\d|lecture|\bslides?\b|\bunit\b|\bunit\s?\d|\bmodule\b"
            r"|\bsession\b|\bsession\s?\d|\bclass\s?\d|\bweek\s?\d",
            re.I,
        ),
    ),
    ("notes", re.compile(r"\bnotes?\b", re.I)),
)

#: Extensions that make a file `data/` regardless of what it is called.
_DATA_EXTS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet"}


def classify_bucket(filename: str, *grouping: str | None) -> str:
    """The `config/naming.md` bucket for one file.

    `grouping` is the context the file was found in -- a Canvas module name, a
    Canvas folder, the student's own subfolders -- in priority order. Grouping
    always wins over the filename (naming.md: a file inside "Recitation 2/" is a
    recitation even when it is called `walkthrough.pdf`). Only when no grouping
    text matches does the data extension, and then the filename itself, decide.
    """
    for text in grouping:
        if not text:
            continue
        for bucket, rx in _BUCKET_PATTERNS:
            if rx.search(SEPARATORS_RX.sub(" ", text)):
                return bucket
    if PurePosixPath(filename).suffix.lower() in _DATA_EXTS:
        return "data"
    stem = SEPARATORS_RX.sub(" ", PurePosixPath(filename).stem)
    for bucket, rx in _BUCKET_PATTERNS:
        if rx.search(stem):
            return bucket
    return "other"


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
@dataclass
class PlanEntry:
    """One proposed placement. ``uuid`` is empty for pre-existing files."""

    file_id: str
    uuid: str
    source: str
    destination: str
    reason: str
    confidence: float
    kind: str = "canvas"  # canvas | existing

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD


@dataclass
class Plan:
    plan_id: str
    created_at: str
    naming_rules_sha: str
    entries: list[PlanEntry] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "naming_rules_sha": self.naming_rules_sha,
            "entries": [asdict(e) for e in self.entries],
            "rejected": list(self.rejected),
        }


@dataclass
class ApplyReport:
    plan_id: str
    link_mode: str = "hardlink"
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    flagged: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    undo_log: Path | None = None


@dataclass
class UndoReport:
    log_id: str
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# destination guardrails
# --------------------------------------------------------------------------
def validate_destination(settings: Settings, destination: str) -> str:
    """Return the cleaned workspace-relative destination, or raise ``ValueError``.

    Rejects absolute paths, `..` traversal, anything outside the workspace, the
    reserved `_canvas`/`_agent`/`_kb` trees, ignored paths, and directory-only
    destinations.
    """
    raw = str(destination or "").strip()
    if not raw:
        raise ValueError("destination is empty")
    if raw.startswith("/") or raw.startswith("~") or PurePosixPath(raw).is_absolute():
        raise ValueError("destination must be workspace-relative, not absolute")
    if "\x00" in raw:
        raise ValueError("destination contains a NUL byte")
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("destination must be workspace-relative, not absolute")
    parts = [p for p in PurePosixPath(raw.replace("\\", "/")).parts if p not in (".",)]
    if any(p == ".." for p in parts):
        raise ValueError("destination may not traverse upward with '..'")
    if len(parts) < 2:
        raise ValueError("destination must be <course folder>/<subpath>/<filename>")
    if parts[0] in RESERVED_TOP_LEVEL:
        raise ValueError(f"destination may not target the reserved '{parts[0]}/' tree")
    rel = PurePosixPath(*parts).as_posix()
    if is_ignored(settings, rel):
        raise ValueError(f"destination '{rel}' is excluded by ignore_globs")
    target = settings.paths.workspace / rel
    if not settings.paths.is_inside_workspace(target.parent):
        raise ValueError("destination escapes the workspace")
    if target.is_dir():
        raise ValueError("destination is an existing directory, not a file path")
    return rel


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def _unfiled_canvas_files(settings: Settings) -> list[dict[str, Any]]:
    db = settings.paths.manifest_db
    if not db.exists():
        return []
    from mitsync.canvas.manifest import Manifest

    out: list[dict[str, Any]] = []
    with Manifest(db) as man:
        for rec in man.list_files(unfiled_only=True):
            if not rec.mirror_path or is_ignored(settings, rec.mirror_path):
                continue
            course = folder_for_canvas_id(settings, rec.course_canvas_id) or rec.course_folder
            out.append(
                {
                    "file_id": str(rec.canvas_id or rec.uuid),
                    "uuid": rec.uuid,
                    "source": rec.mirror_path,
                    "display_name": rec.display_name or rec.filename,
                    "canvas_folder": rec.canvas_folder,
                    "module_name": rec.module_name,
                    "module_position": rec.module_position,
                    "content_type": rec.content_type,
                    "size": rec.size,
                    "course": course,
                    "kind": "canvas",
                }
            )
    return out


def _preexisting_files(settings: Settings) -> list[dict[str, Any]]:
    """Student files already in a course folder that are not yet in a bucket."""
    out: list[dict[str, Any]] = []
    for folder in existing_course_folders(settings):
        root = settings.paths.workspace / folder
        for path in walk(settings, root):
            rel = settings.paths.safe_relative(path).as_posix()
            inner = PurePosixPath(rel).relative_to(folder).parts
            # Anything already under a bucket is filed. The depth is deliberately
            # not pinned: naming.md may give a bucket per-item subfolders
            # (`assignments/hw-01/profit.csv`) and did, at which point a
            # `len(inner) == 2` test silently re-proposed every filed file as
            # unfiled -- and `--include-existing` moves, so that would have
            # churned the whole tree.
            if len(inner) >= 2 and inner[0] in BUCKETS:
                continue  # already filed the way naming.md wants
            out.append(
                {
                    "file_id": f"existing:{rel}",
                    "uuid": "",
                    "source": rel,
                    "display_name": path.name,
                    "canvas_folder": PurePosixPath(rel).parent.as_posix(),
                    "module_name": None,
                    "module_position": None,
                    "content_type": None,
                    "size": path.stat().st_size,
                    "course": folder,
                    "kind": "existing",
                }
            )
    return out


def plan(settings: Settings, judge: Any, *, include_existing: bool = False) -> Plan:
    """Propose destinations for unfiled files. Writes a plan; moves nothing."""
    rules = naming_rules(settings)
    candidates = _unfiled_canvas_files(settings)
    if include_existing:
        candidates += _preexisting_files(settings)

    the_plan = Plan(
        plan_id=f"plan-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        created_at=now_iso(),
        naming_rules_sha=hashlib.sha256(rules.encode("utf-8")).hexdigest()[:16],
    )

    if not candidates:
        console.print("[green]Nothing to file — every known file already has a home.[/green]")
        the_plan.path = _write_plan(settings, the_plan)
        console.print(f"[dim]empty plan written to {the_plan.path}[/dim]")
        return the_plan

    by_id = {c["file_id"]: c for c in candidates}
    payload = {
        "files": [
            {
                "file_id": c["file_id"],
                "display_name": c["display_name"],
                "canvas_folder": c["canvas_folder"],
                "module_name": c["module_name"],
                "module_position": c["module_position"],
                "content_type": c["content_type"],
                "size": c["size"],
                "course": c["course"],
            }
            for c in candidates
        ],
        "existing_folders": {
            folder: sorted(
                c.name
                for c in (settings.paths.workspace / folder).iterdir()
                if c.is_dir()
                and not c.name.startswith(".")
                and not is_ignored(settings, f"{folder}/{c.name}")
            )
            for folder in existing_course_folders(settings)
        },
    }
    task = make_task(
        "organize_plan",
        payload,
        rules=rules,  # verbatim config/naming.md -- prose is the tuning surface
        origin_command="organize plan",
        origin_args={"include_existing": include_existing},
    )
    result = validate_result(task, judge.judge(task))

    seen: set[str] = set()
    for placement in result.get("placements", []):
        file_id = str(placement.get("file_id"))
        candidate = by_id.get(file_id)
        if candidate is None:
            the_plan.rejected.append(
                {"file_id": file_id, "reason": "judgment named a file that was not in the payload"}
            )
            continue
        if file_id in seen:
            the_plan.rejected.append({"file_id": file_id, "reason": "duplicate placement"})
            continue
        seen.add(file_id)
        try:
            dest = validate_destination(settings, placement.get("destination", ""))
        except ValueError as exc:
            the_plan.rejected.append(
                {
                    "file_id": file_id,
                    "destination": str(placement.get("destination", "")),
                    "reason": str(exc),
                }
            )
            continue
        if dest == candidate["source"]:
            the_plan.rejected.append(
                {"file_id": file_id, "destination": dest, "reason": "already at its destination"}
            )
            continue
        the_plan.entries.append(
            PlanEntry(
                file_id=file_id,
                uuid=candidate["uuid"],
                source=candidate["source"],
                destination=dest,
                reason=str(placement.get("reason") or ""),
                confidence=float(placement.get("confidence") or 0.0),
                kind=candidate["kind"],
            )
        )
    for file_id in by_id:
        if file_id not in seen:
            the_plan.rejected.append({"file_id": file_id, "reason": "no placement was returned"})

    the_plan.path = _write_plan(settings, the_plan)
    console.print(_plan_table(the_plan))
    for bad in the_plan.rejected:
        console.print(f"[yellow]rejected[/yellow] {bad['file_id']}: {bad['reason']}")
    console.print(f"plan written to [bold]{the_plan.path}[/bold] (nothing has moved)")
    return the_plan


def _write_plan(settings: Settings, the_plan: Plan) -> Path:
    path = unique_path(settings.paths.plans_dir / f"{the_plan.plan_id}.json")
    path.write_text(json.dumps(the_plan.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def _plan_table(the_plan: Plan) -> Table:
    table = Table(title=f"organize plan {the_plan.plan_id}")
    for col in ("source", "destination", "conf", "why"):
        table.add_column(col, overflow="fold")
    for entry in the_plan.entries:
        flag = " [yellow]review[/yellow]" if entry.needs_review else ""
        conf = f"{entry.confidence:.2f}{flag}"
        table.add_row(entry.source, entry.destination, conf, entry.reason)
    return table


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def _newest(directory: Path, pattern: str) -> Path | None:
    candidates = sorted(directory.glob(pattern))
    return candidates[-1] if candidates else None


def load_plan(settings: Settings, plan_path: Path | str | None) -> Plan:
    if plan_path is None:
        found = _newest(settings.paths.plans_dir, "plan-*.json")
        if found is None:
            raise MitsyncError(
                f"no plan found in {settings.paths.plans_dir}; run `mitsync organize plan` first"
            )
        plan_path = found
    path = Path(plan_path)
    if not path.exists():
        raise MitsyncError(f"plan file not found: {path}")
    doc = read_json(path)
    return Plan(
        plan_id=doc["plan_id"],
        created_at=doc["created_at"],
        naming_rules_sha=doc["naming_rules_sha"],
        entries=[PlanEntry(**e) for e in doc["entries"]],
        rejected=list(doc["rejected"]),
        path=path,
    )


def _confirm_existing(count: int, *, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        console.print(
            f"[yellow]{count} pre-existing file(s) would be MOVED. Refusing without --yes "
            f"(no terminal to confirm on).[/yellow]"
        )
        return False
    answer = input(f"Move {count} pre-existing file(s) out of their current place? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _place(source: Path, dest: Path, mode: str) -> str:
    """Materialise ``dest`` from ``source``; returns the mode actually used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "symlink":
        dest.symlink_to(source)
        return "symlink"
    if mode == "hardlink":
        try:
            os.link(source, dest)
            return "hardlink"
        except OSError as exc:  # different filesystem, or a FS with no hard links
            log.info("hardlink %s -> %s failed (%s); copying instead", source, dest, exc)
    shutil.copy2(source, dest)
    return "copy"


def apply_plan(
    settings: Settings, plan_path: Path | str | None = None, *, yes: bool = False
) -> ApplyReport:
    """Execute a saved plan, writing an undo log for everything it touches."""
    the_plan = load_plan(settings, plan_path)
    mode = settings.organize.link_mode
    report = ApplyReport(plan_id=the_plan.plan_id, link_mode=mode)
    ws = settings.paths.workspace

    movers = [e for e in the_plan.entries if e.kind == "existing" and not e.needs_review]
    allow_moves = True
    if movers:
        allow_moves = _confirm_existing(len(movers), yes=yes)

    operations: list[dict[str, Any]] = []
    filed: list[tuple[str, str]] = []

    for entry in the_plan.entries:
        source = ws / entry.source
        dest = ws / entry.destination
        if entry.needs_review:
            report.skipped.append({**asdict(entry), "status": "low-confidence"})
            continue
        if entry.kind == "existing" and not allow_moves:
            report.skipped.append({**asdict(entry), "status": "needs --yes"})
            continue
        if not source.exists():
            report.errors.append({"file_id": entry.file_id, "error": f"missing source {source}"})
            continue
        try:
            dest_rel = validate_destination(settings, entry.destination)
        except ValueError as exc:
            report.errors.append({"file_id": entry.file_id, "error": str(exc)})
            continue
        dest = ws / dest_rel

        flagged = False
        if dest.exists():
            if sha256_file(dest) == sha256_file(source):
                report.skipped.append({**asdict(entry), "status": "identical file already there"})
                if entry.uuid:
                    filed.append((entry.uuid, dest_rel))
                continue
            dest = unique_path(dest)
            dest_rel = settings.paths.safe_relative(dest).as_posix()
            flagged = True

        src_sha = sha256_file(source)
        try:
            if entry.kind == "existing":
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                used = "move"
            else:
                used = _place(source, dest, mode)
        except OSError as exc:
            report.errors.append(
                {"file_id": entry.file_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue

        op = {
            "file_id": entry.file_id,
            "uuid": entry.uuid,
            "kind": entry.kind,
            "mode": used,
            "source": entry.source,
            "destination": dest_rel,
            "source_sha256": src_sha,
            "destination_sha256": src_sha if used != "symlink" else None,
        }
        operations.append(op)
        record = {**asdict(entry), "destination": dest_rel, "mode": used}
        report.applied.append(record)
        if flagged:
            report.flagged.append({**record, "status": "name collision: suffixed"})
        if entry.uuid:
            filed.append((entry.uuid, dest_rel))

    report.undo_log = _write_undo_log(settings, the_plan, mode, operations)
    _record_filed(settings, filed)
    console.print(_apply_table(report))
    if report.undo_log is not None:
        console.print(f"undo log: [bold]{report.undo_log}[/bold]")
    return report


def _record_filed(settings: Settings, filed: list[tuple[str, str]]) -> None:
    if not filed:
        return
    from mitsync.canvas.manifest import Manifest

    with Manifest(settings.paths.manifest_db) as man:
        for uuid, dest in filed:
            man.set_filed_path(uuid, dest)


def _write_undo_log(
    settings: Settings, the_plan: Plan, mode: str, operations: list[dict[str, Any]]
) -> Path:
    path = unique_path(settings.paths.undo_dir / f"undo-{the_plan.plan_id}.json")
    doc = {
        "log_id": path.stem,
        "plan_id": the_plan.plan_id,
        "plan_path": str(the_plan.path) if the_plan.path else None,
        "applied_at": now_iso(),
        "link_mode": mode,
        "operations": operations,
        "undone_at": None,
    }
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _apply_table(report: ApplyReport) -> Table:
    table = Table(title=f"organize apply {report.plan_id} ({report.link_mode})")
    for col in ("destination", "mode", "status"):
        table.add_column(col, overflow="fold")
    for row in report.applied:
        table.add_row(row["destination"], row["mode"], "applied")
    for row in report.skipped:
        table.add_row(row["destination"], "-", f"skipped: {row['status']}")
    for row in report.errors:
        table.add_row(row["file_id"], "-", f"[red]error: {row['error']}[/red]")
    return table


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------
def _find_undo_log(settings: Settings, log_id: str | None) -> Path:
    undo_dir = settings.paths.undo_dir
    if log_id in (None, "", "latest"):
        found = _newest(undo_dir, "undo-*.json")
        if found is None:
            raise MitsyncError(f"no undo log in {undo_dir}; nothing to undo")
        return found
    assert log_id is not None
    for candidate in (
        Path(log_id),
        undo_dir / log_id,
        undo_dir / f"{log_id}.json",
        undo_dir / f"undo-{log_id}.json",
    ):
        if candidate.exists():
            return candidate
    raise MitsyncError(f"no undo log matching '{log_id}' in {undo_dir}")


def undo(settings: Settings, log_id: str | None = None) -> UndoReport:
    """Reverse an applied plan, refusing anything whose bytes changed since."""
    path = _find_undo_log(settings, log_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    report = UndoReport(log_id=doc.get("log_id") or path.stem)
    ws = settings.paths.workspace

    if doc.get("undone_at"):
        console.print(f"[yellow]{path.name} was already undone at {doc['undone_at']}.[/yellow]")
        return report

    cleared: list[str] = []
    for op in reversed(doc.get("operations", [])):
        dest = ws / op["destination"]
        source = ws / op["source"]
        if not dest.exists() and not dest.is_symlink():
            report.refused.append(
                {"path": op["destination"], "reason": "already gone; leaving the rest alone"}
            )
            continue
        expected = op.get("destination_sha256")
        if expected and not dest.is_symlink():
            if sha256_file(dest) != expected:
                report.refused.append(
                    {
                        "path": op["destination"],
                        "reason": "changed since the plan was applied; refusing to undo it",
                    }
                )
                continue
        try:
            if op["mode"] == "move":
                if source.exists():
                    report.refused.append(
                        {"path": op["source"], "reason": "original path is occupied again"}
                    )
                    continue
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(source))
                report.restored.append(op["source"])
            else:
                dest.unlink()
                report.removed.append(op["destination"])
        except OSError as exc:
            report.errors.append({"path": op["destination"], "error": str(exc)})
            continue
        _prune_empty(dest.parent, ws)
        if op.get("uuid"):
            cleared.append(op["uuid"])

    if cleared:
        from mitsync.canvas.manifest import Manifest

        with Manifest(settings.paths.manifest_db) as man:
            for uuid in cleared:
                man.set_filed_path(uuid, None)

    if not report.errors and not report.refused:
        doc["undone_at"] = now_iso()
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    console.print(
        f"undo {report.log_id}: restored {len(report.restored)}, removed {len(report.removed)}, "
        f"refused {len(report.refused)}, errors {len(report.errors)}"
    )
    for row in report.refused:
        console.print(f"[yellow]refused[/yellow] {row['path']}: {row['reason']}")
    for row in report.errors:
        console.print(f"[red]error[/red] {row['path']}: {row['error']}")
    return report


def _prune_empty(directory: Path, workspace: Path) -> None:
    """Remove directories this tool emptied, never climbing past the workspace.

    Both failure modes used to return silently, which gave "I cannot read this
    directory" (a real problem, usually permissions) and "something appeared in
    it while I looked" (a benign macOS race -- Finder writing .DS_Store between
    the emptiness check and the rmdir) the same answer: none.

    Only the race is tolerated, and only by stopping the climb. Anything else
    raises, because a directory this tool cannot read or remove inside its own
    workspace is a state worth failing on.
    """
    current = directory
    while current != workspace and workspace in current.parents:
        if any(current.iterdir()):
            return
        try:
            current.rmdir()
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return  # lost the race; the directory is no longer ours to remove
            raise
        current = current.parent
