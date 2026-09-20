"""Curate the Canvas mirror into the student's own course folders.

Three stages, deliberately separated so nothing moves without review:

``suggest_course_map``
    Propose ``config/courses.yml`` (Canvas course -> workspace folder). Prints a
    YAML draft; writes only with ``apply=True``, and never clobbers hand-edited
    entries -- conflicts are reported, not resolved.
``plan``
    Ask the :class:`~mitsync.llm.base.Judge` where every unfiled file should go,
    validate each destination against the workspace guardrails, and write
    ``state/plans/plan-<ts>.json``. **This never touches the filesystem.**
``apply_plan`` / ``undo``
    Execute a saved plan and reverse it. Canvas-mirrored files are *linked or
    copied* into the curated folder -- ``_canvas/`` stays the source of truth and
    is never emptied. Only pre-existing student files are really moved, and only
    those need (and get) an undo log entry that restores them byte-for-byte.

The filing rules themselves live in ``config/naming.md`` and are injected
verbatim into every judgment payload, so editing that prose changes the next
plan with no code change.
"""

from __future__ import annotations

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

import yaml
from rich.console import Console
from rich.table import Table

from .errors import ConfigError, MitsyncError
from .llm.base import make_task, validate_result
from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

log = get_logger(__name__)
console = Console()

__all__ = [
    "ApplyReport",
    "MapReport",
    "Plan",
    "PlanEntry",
    "UndoReport",
    "apply_plan",
    "load_course_map",
    "plan",
    "suggest_course_map",
    "undo",
]

#: Subfolders `config/naming.md` allows inside a course folder.
BUCKETS = ("lectures", "recitations", "assignments", "data", "syllabus", "notes", "other")

#: Top-level names that are mitsync's own, never a filing destination.
RESERVED_TOP_LEVEL = ("_canvas", "_agent", "_kb")

#: Placements below this are shown for review instead of applied (naming.md §6).
REVIEW_THRESHOLD = 0.5

# 15.095 / 15_095 / 15095 / 15 095 / 15.C57, as they turn up inside filenames.
_COURSE_NUMBER_RX = re.compile(r"\b(\d{1,2})[._\s]?([A-Za-z]?\d{2,4})\b")
_SEPARATORS_RX = re.compile(r"[._\-]+")
_DIGITS_RX = re.compile(r"\D")


def course_numbers_in(text: str) -> list[str]:
    """Course numbers in canonical dotted form found in `text`.

    A bare four-digit year (`Fall_2026`) is not a course number, so anything
    that looks like one and carries no separator of its own is skipped.
    """
    out: list[str] = []
    for m in _COURSE_NUMBER_RX.finditer(_SEPARATORS_RX.sub(" ", text)):
        whole = m.group(0)
        digits = _DIGITS_RX.sub("", whole)
        if len(digits) == 4 and 1900 <= int(digits) <= 2099 and not re.search(r"[.\s]", whole):
            continue
        out.append(f"{m.group(1)}.{m.group(2)}")
    return out


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
@dataclass
class MapReport:
    """What `mitsync map` proposed, kept, and (optionally) wrote."""

    mappings: list[dict[str, Any]] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    preserved: list[int] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None
    written: bool = False
    yaml_text: str = ""


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
def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(settings: Settings, path: Path) -> str:
    return path.resolve().relative_to(settings.paths.workspace).as_posix()


def is_ignored(settings: Settings, relpath: str) -> bool:
    """True if a workspace-relative path (file or directory) is excluded.

    Directory globs such as ``AI_Studio/nandatown/**`` only match *contents*, so
    a directory is also probed with a trailing slash. This is what keeps the
    9,400-file `nandatown` repo out of every walk, plan and report.
    """
    rel = str(relpath).strip("/")
    if not rel:
        return False
    return settings.should_ignore(rel) or settings.should_ignore(rel + "/")


def naming_rules(settings: Settings) -> str:
    """The verbatim prose from ``config/naming.md`` (empty + warning if absent)."""
    path = settings.paths.config_dir / "naming.md"
    if not path.exists():
        log.warning("config/naming.md is missing (%s); judging with no filing rules", path)
        return ""
    return path.read_text(encoding="utf-8")


def _rules_sha(rules: str) -> str:
    return hashlib.sha256(rules.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# course map
# --------------------------------------------------------------------------
def courses_file(settings: Settings) -> Path:
    return settings.paths.config_dir / "courses.yml"


def load_course_map(settings: Settings) -> list[dict[str, Any]]:
    """Entries from ``config/courses.yml``; ``[]`` when unset or absent."""
    path = courses_file(settings)
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    entries = raw.get("courses") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: `courses` must be a list")
    return [dict(e) for e in entries if isinstance(e, dict)]


def folder_for_canvas_id(settings: Settings, canvas_id: Any) -> str | None:
    """The student's folder for a Canvas course id, per ``config/courses.yml``."""
    if canvas_id in (None, ""):
        return None
    for entry in load_course_map(settings):
        if str(entry.get("canvas_id")) == str(canvas_id):
            folder = entry.get("folder")
            return str(folder) if folder else None
    return None


def existing_course_folders(settings: Settings) -> list[str]:
    """Top-level workspace folders that look like the student's own courses."""
    ws = settings.paths.workspace
    out: list[str] = []
    if not ws.exists():
        return out
    for child in sorted(ws.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if is_ignored(settings, child.name):
            continue
        out.append(child.name)
    return out


def _subfolders(settings: Settings, folder: str) -> list[str]:
    root = settings.paths.workspace / folder
    if not root.is_dir():
        return []
    return sorted(
        c.name
        for c in root.iterdir()
        if c.is_dir()
        and not c.name.startswith(".")
        and not is_ignored(settings, f"{folder}/{c.name}")
    )


def observed_course_numbers(settings: Settings) -> dict[str, list[str]]:
    """Course numbers mined out of existing filenames -> the folders they sit in."""
    found: dict[str, set[str]] = {}
    for folder in existing_course_folders(settings):
        for path in _walk(settings, settings.paths.workspace / folder):
            for number in course_numbers_in(path.stem):
                found.setdefault(number, set()).add(folder)
    return {k: sorted(v) for k, v in sorted(found.items())}


def _walk(settings: Settings, root: Path):
    """Yield files under ``root``, pruning every ignored directory."""
    if not root.is_dir():
        return
    ws = settings.paths.workspace
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.resolve().relative_to(ws).as_posix()
        dirnames[:] = [
            d
            for d in sorted(dirnames)
            if not d.startswith(".") and not is_ignored(settings, f"{rel_dir}/{d}")
        ]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if is_ignored(settings, f"{rel_dir}/{name}"):
                continue
            yield here / name


def _canvas_courses(settings: Settings) -> list[dict[str, Any]]:
    """Canvas courses from the manifest, falling back to `_canvas/*/_meta/`."""
    courses: list[dict[str, Any]] = []
    db = settings.paths.manifest_db
    if db.exists():
        from .manifest import Manifest

        with Manifest(db) as man:
            for rec in man.list_courses():
                courses.append(
                    {
                        "id": rec.canvas_id,
                        "name": rec.name,
                        "course_code": rec.course_code,
                        "term": rec.term_name,
                        "mirror_folder": rec.folder,
                    }
                )
    if courses:
        return courses
    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/courses.json")):
        for item in _meta_items(meta):
            courses.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name") or "",
                    "course_code": item.get("course_code"),
                    "term": (item.get("term") or {}).get("name"),
                    "mirror_folder": item.get("mirror_folder") or meta.parent.parent.name,
                }
            )
    return courses


def _meta_items(path: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read %s (%s)", path, exc)
        return []
    items = doc.get("items") if isinstance(doc, dict) else doc
    return [i for i in items or [] if isinstance(i, dict)]


def suggest_course_map(settings: Settings, judge: Any, *, apply: bool = False) -> MapReport:
    """Propose (and with ``apply``, write) the Canvas course -> folder mapping."""
    courses = _canvas_courses(settings)
    folders = existing_course_folders(settings)
    report = MapReport(path=courses_file(settings))

    if not courses:
        console.print("[yellow]No Canvas courses known yet — run `mitsync sync` first.[/yellow]")
        report.yaml_text = _dump_courses_yaml([])
        return report

    payload = {
        "courses": [
            {
                "id": int(c["id"]) if str(c.get("id") or "").isdigit() else 0,
                "name": str(c.get("name") or ""),
                "course_code": c.get("course_code"),
                "term": c.get("term"),
            }
            for c in courses
        ],
        "existing_folders": folders,
        "observed_course_numbers": observed_course_numbers(settings),
    }
    task = make_task(
        "course_map",
        payload,
        rules=naming_rules(settings),
        origin_command="map",
        origin_args={"apply": apply},
    )
    result = validate_result(task, judge.judge(task))
    proposed = {int(m["canvas_id"]): m for m in result.get("mappings", [])}

    existing = {int(e["canvas_id"]): e for e in load_course_map(settings) if "canvas_id" in e}
    merged: dict[int, dict[str, Any]] = {}
    for cid, entry in existing.items():
        merged[cid] = dict(entry)
        report.preserved.append(cid)
        prop = proposed.get(cid)
        if prop and str(prop.get("folder")) != str(entry.get("folder")):
            report.conflicts.append(
                {
                    "canvas_id": cid,
                    "existing_folder": entry.get("folder"),
                    "proposed_folder": prop.get("folder"),
                }
            )
    for cid, prop in proposed.items():
        if cid in merged:
            continue
        merged[cid] = {
            "canvas_id": cid,
            "folder": prop["folder"],
            "course_number": prop.get("course_number"),
            "aliases": list(prop.get("aliases") or []),
        }
        report.added.append(cid)

    report.mappings = [merged[k] for k in sorted(merged)]
    report.yaml_text = _dump_courses_yaml(report.mappings)

    console.print(_map_table(report, proposed))
    console.print("[bold]config/courses.yml draft:[/bold]")
    console.print(report.yaml_text)
    for conflict in report.conflicts:
        console.print(
            f"[yellow]conflict:[/yellow] canvas {conflict['canvas_id']} is mapped to "
            f"'{conflict['existing_folder']}' but '{conflict['proposed_folder']}' was proposed; "
            f"your edit was kept."
        )

    if apply:
        path = courses_file(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.yaml_text, encoding="utf-8")
        report.written = True
        console.print(f"[green]wrote[/green] {path}")
    else:
        console.print("[dim]nothing written; re-run with --apply to save.[/dim]")
    return report


def _dump_courses_yaml(mappings: list[dict[str, Any]]) -> str:
    header = (
        "# Canvas course -> workspace folder mapping.\n"
        "# Generated by `mitsync map --apply`; hand edits are preserved on re-run.\n\n"
    )
    body = yaml.safe_dump({"courses": mappings}, sort_keys=False, allow_unicode=True)
    return header + body


def _map_table(report: MapReport, proposed: dict[int, dict[str, Any]]) -> Table:
    table = Table(title="course map")
    for col in ("canvas_id", "folder", "number", "aliases", "confidence", "status"):
        table.add_column(col)
    for entry in report.mappings:
        cid = int(entry["canvas_id"])
        prop = proposed.get(cid, {})
        status = "new" if cid in report.added else "kept (yours)"
        table.add_row(
            str(cid),
            str(entry.get("folder") or ""),
            str(entry.get("course_number") or ""),
            ", ".join(entry.get("aliases") or []),
            f"{prop.get('confidence', ''):.2f}" if prop.get("confidence") is not None else "",
            status,
        )
    return table


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
    from .manifest import Manifest

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
        for path in _walk(settings, root):
            rel = _rel(settings, path)
            inner = PurePosixPath(rel).relative_to(folder).parts
            if len(inner) == 2 and inner[0] in BUCKETS:
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
        plan_id=f"plan-{_stamp()}",
        created_at=_now(),
        naming_rules_sha=_rules_sha(rules),
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
            f: _subfolders(settings, f) for f in existing_course_folders(settings)
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
    plans = settings.paths.plans_dir
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"{the_plan.plan_id}.json"
    n = 2
    while path.exists():
        path = plans / f"{the_plan.plan_id}-{n}.json"
        n += 1
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
    doc = json.loads(path.read_text(encoding="utf-8"))
    return Plan(
        plan_id=doc.get("plan_id") or path.stem,
        created_at=doc.get("created_at") or "",
        naming_rules_sha=doc.get("naming_rules_sha") or "",
        entries=[PlanEntry(**e) for e in doc.get("entries", [])],
        rejected=list(doc.get("rejected", [])),
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


def _suffixed(dest: Path) -> Path:
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        candidate = dest.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


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
            try:
                same = sha256_file(dest) == sha256_file(source)
            except OSError as exc:  # pragma: no cover - defensive
                report.errors.append({"file_id": entry.file_id, "error": str(exc)})
                continue
            if same:
                report.skipped.append({**asdict(entry), "status": "identical file already there"})
                if entry.uuid:
                    filed.append((entry.uuid, dest_rel))
                continue
            dest = _suffixed(dest)
            dest_rel = _rel_of(ws, dest)
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


def _rel_of(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def _record_filed(settings: Settings, filed: list[tuple[str, str]]) -> None:
    if not filed:
        return
    from .manifest import Manifest

    with Manifest(settings.paths.manifest_db) as man:
        for uuid, dest in filed:
            man.set_filed_path(uuid, dest)


def _write_undo_log(
    settings: Settings, the_plan: Plan, mode: str, operations: list[dict[str, Any]]
) -> Path:
    undo_dir = settings.paths.undo_dir
    undo_dir.mkdir(parents=True, exist_ok=True)
    path = undo_dir / f"undo-{the_plan.plan_id}.json"
    n = 2
    while path.exists():
        path = undo_dir / f"undo-{the_plan.plan_id}-{n}.json"
        n += 1
    doc = {
        "log_id": path.stem,
        "plan_id": the_plan.plan_id,
        "plan_path": str(the_plan.path) if the_plan.path else None,
        "applied_at": _now(),
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
            try:
                actual = sha256_file(dest)
            except OSError as exc:  # pragma: no cover - defensive
                report.errors.append({"path": op["destination"], "error": str(exc)})
                continue
            if actual != expected:
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
        from .manifest import Manifest

        with Manifest(settings.paths.manifest_db) as man:
            for uuid in cleared:
                man.set_filed_path(uuid, None)

    if not report.errors and not report.refused:
        doc["undone_at"] = _now()
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
    """Remove directories this tool emptied, never climbing past the workspace."""
    current = directory
    while current != workspace and workspace in current.parents:
        try:
            next(current.iterdir())
            return
        except StopIteration:
            pass
        except OSError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
