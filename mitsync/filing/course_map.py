"""
# Course Map

Which Canvas course is which of the student's own folders.

## 1. What This Module Does

Owns `config/courses.yml` -- the Canvas course id -> workspace folder mapping --
end to end: reading it (`load_course_map`, `folder_for_canvas_id`), mining the
evidence for it out of the workspace (`existing_course_folders`,
`observed_course_numbers`, `course_numbers_in`) and proposing it with a judge
(`suggest_course_map`, behind `mitsync map`).

## 2. Why This Module Exists

Canvas names a course "Fall 2026 - 15.095 Machine Learning Under a Modern
Optimization Lens"; the student calls the folder "Machine Learning". Every other
feature -- filing, deadlines, calendar tagging -- needs that translation, and
most of them need nothing else. Keeping it here means `deadlines` and
`calendar_read` can read fifteen lines of YAML without importing the plan /
apply / undo machinery in :mod:`mitsync.organize`, which is what they used to do.

## 3. How It Fits in the Architecture

The bottom of the filing stack: `organize` imports this module, never the other
way round. `deadlines` and `calendar_read` import only `load_course_map`.
`config/courses.yml` is hand-editable and hand edits always win -- a re-run of
`mitsync map` reports a conflict rather than resolving it.

## 4. Key Concepts

**Course number.** The MIT dotted form (`15.095`, `6.7900`, `15.C57`). It turns
up in filenames as `15_095`, `15095` or `15 095`, and a bare four-digit year is
not one -- `course_numbers_in` knows the difference.

**Observed numbers.** Course numbers mined from the filenames already in the
student's folders, fed to the judge as evidence for a mapping it would otherwise
have to guess from the course title alone.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from rich.console import Console
from rich.table import Table

from mitsync.core.config import read_meta
from mitsync.core.errors import ConfigError
from mitsync.core.logging import get_logger
from mitsync.llm.base import make_task, validate_result

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)
console = Console()

__all__ = [
    "MapReport",
    "course_numbers_in",
    "courses_file",
    "existing_course_folders",
    "folder_for_canvas_id",
    "load_course_map",
    "observed_course_numbers",
    "suggest_course_map",
]

# 15.095 / 15_095 / 15095 / 15 095 / 15.C57, as they turn up inside filenames.
_COURSE_NUMBER_RX = re.compile(r"\b(\d{1,2})[._\s]?([A-Za-z]?\d{2,4})\b")
#: `_`, `-` and `.` are separators everywhere a course number or a bucket name
#: has to be read out of a filename.
SEPARATORS_RX = re.compile(r"[._\-]+")
_DIGITS_RX = re.compile(r"\D")


def course_numbers_in(text: str) -> list[str]:
    """Course numbers in canonical dotted form found in `text`.

    A bare four-digit year (`Fall_2026`) is not a course number, so anything
    that looks like one and carries no separator of its own is skipped.
    """
    out: list[str] = []
    for m in _COURSE_NUMBER_RX.finditer(SEPARATORS_RX.sub(" ", text)):
        whole = m.group(0)
        digits = _DIGITS_RX.sub("", whole)
        if len(digits) == 4 and 1900 <= int(digits) <= 2099 and not re.search(r"[.\s]", whole):
            continue
        out.append(f"{m.group(1)}.{m.group(2)}")
    return out


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
        # `<workspace>/skills` is a symlink to `_agent/skills` created by the
        # OpenClaw setup, and the same shape can appear for any convenience
        # link. Resolving one leaves the course tree, so every path under it
        # comes back rooted at `_agent/` and `PurePosixPath.relative_to(folder)`
        # raises. Machinery is not a course: keep only directories that really
        # live directly in the workspace.
        if child.resolve().parent != ws.resolve():
            continue
        out.append(child.name)
    return out


def observed_course_numbers(settings: Settings) -> dict[str, list[str]]:
    """Course numbers mined out of existing filenames -> the folders they sit in."""
    found: dict[str, set[str]] = {}
    for folder in existing_course_folders(settings):
        for path in walk(settings, settings.paths.workspace / folder):
            for number in course_numbers_in(path.stem):
                found.setdefault(number, set()).add(folder)
    return {k: sorted(v) for k, v in sorted(found.items())}


def walk(settings: Settings, root: Path):
    """Yield files under ``root``, pruning every ignored directory."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = settings.paths.safe_relative(here).as_posix()
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
        from mitsync.canvas.manifest import Manifest

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
        for item in read_meta(meta):
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


def suggest_course_map(settings: Settings, judge: Any, *, apply: bool = False) -> MapReport:
    """Propose (and with ``apply``, write) the Canvas course -> folder mapping."""
    # Imported here, not at module level: `sync` pulls in `httpx`, and the whole
    # point of this module is that `deadlines` and `calendar_read` can read the
    # course map without dragging the Canvas client in. Only this one function
    # -- which is already talking to Canvas -- needs it.
    from mitsync.canvas.sync import filter_excluded_courses

    courses, excluded = filter_excluded_courses(
        _canvas_courses(settings), settings.canvas.exclude_courses
    )
    for item in excluded:
        console.print(
            f"[dim]skipping canvas {item['canvas_id']} ({item['name']}): {item['reason']}[/dim]"
        )
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
