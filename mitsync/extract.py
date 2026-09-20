"""Deterministic text extraction: course documents -> `_kb/text/*.md`.

No judgment, no model, no network. Every supported document in the Canvas
mirror and in the student's own course folders is converted to markdown with
YAML frontmatter recording where it came from. The output filename is the
SHA-1 of the workspace-relative source path, so a source maps to exactly one
text file forever.

Extraction is idempotent: a second run over unchanged sources rewrites
nothing. `force=True` re-extracts regardless.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

log = get_logger(__name__)

# Bump when the extraction output shape changes; stale outputs are re-extracted.
EXTRACTOR_VERSION = "1"

MAX_TEXT_CHARS = 300_000
CSV_SAMPLE_ROWS = 20
CSV_SAMPLE_BYTES = 512 * 1024
HASH_MAX_BYTES = 64 * 1024 * 1024

# extension -> content_type recorded in frontmatter
SUPPORTED: dict[str, str] = {
    ".pdf": "pdf",
    ".ipynb": "notebook",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".csv": "csv",
}
# Recognised but not convertible without a new dependency.
UNSUPPORTED: dict[str, str] = {
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".xlsx": "xlsx",
    ".xls": "xls",
}


@dataclass
class ExtractReport:
    """What one `extract_all` run did."""

    scanned: int = 0
    extracted: int = 0
    skipped: int = 0
    truncated: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.scanned} scanned, {self.extracted} extracted, {self.skipped} unchanged, "
            f"{len(self.unsupported)} unsupported, {len(self.failed)} failed"
        )


# --------------------------------------------------------------------------
# walking
# --------------------------------------------------------------------------
def source_roots(settings: Settings) -> list[Path]:
    """The Canvas mirror plus the student's own top-level course folders."""
    ws = settings.paths.workspace
    roots: list[Path] = []
    mirror = settings.paths.canvas_mirror
    if mirror.is_dir():
        roots.append(mirror)
    if not ws.is_dir():
        return roots
    for child in sorted(ws.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if settings.should_ignore(child.name):
            continue
        # A symlink can point back into workspace machinery -- the OpenClaw setup
        # links `<workspace>/skills` -> `_agent/skills`. Judge the target, not the name,
        # so the tool never indexes its own repo as a course.
        resolved = child.resolve()
        try:
            parts = resolved.relative_to(ws).parts
        except ValueError:
            continue  # points outside the workspace entirely
        if not parts or parts[0].startswith((".", "_")):
            continue
        roots.append(child)
    return roots


def iter_sources(settings: Settings) -> Iterator[Path]:
    """Yield every candidate document, with ignored subtrees pruned."""
    ws = settings.paths.workspace
    for root in source_roots(settings):
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not settings.should_ignore(_rel(ws, here / d)) and not d.startswith(".")
            )
            for name in sorted(filenames):
                path = here / name
                rel = _rel(ws, path)
                if settings.should_ignore(rel) or name.startswith("."):
                    continue
                if path.suffix.lower() not in SUPPORTED and path.suffix.lower() not in UNSUPPORTED:
                    continue
                yield path


def _rel(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def course_of(rel: str) -> str:
    """Course folder a workspace-relative path belongs to ('' if none)."""
    parts = Path(rel).parts
    if not parts:
        return ""
    if parts[0] == "_canvas":
        return parts[1] if len(parts) > 1 else ""
    if parts[0].startswith((".", "_")):
        return ""  # `_agent`, `_kb`, ... are machinery, not courses
    return parts[0] if len(parts) > 1 else ""


def text_path_for(settings: Settings, rel: str) -> Path:
    digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()  # noqa: S324 -- naming, not security
    return settings.paths.kb_text / f"{digest}.md"


# --------------------------------------------------------------------------
# converters
# --------------------------------------------------------------------------
class ExtractionFailed(Exception):
    """A source could not be converted; recorded in the report, never fatal."""


def _pdf_to_text(path: Path) -> tuple[str, dict[str, int]]:
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ExtractionFailed(f"pymupdf unavailable: {exc}") from exc
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 -- mupdf raises many types
        raise ExtractionFailed(f"unreadable pdf: {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise ExtractionFailed("encrypted pdf (password required)")
        chunks: list[str] = []
        for i, page in enumerate(doc, start=1):
            try:
                body = page.get_text("text")
            except Exception as exc:  # noqa: BLE001
                body = f"[page {i} could not be read: {exc}]"
            chunks.append(f"## page {i}\n\n{body.strip()}")
        pages = doc.page_count
    return "\n\n".join(chunks), {"pages": pages}


def _notebook_to_text(path: Path) -> tuple[str, dict[str, int]]:
    try:
        nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ExtractionFailed(f"unreadable notebook: {exc}") from exc
    cells = nb.get("cells")
    if not isinstance(cells, list):
        raise ExtractionFailed("notebook has no cells array")
    lang = str(((nb.get("metadata") or {}).get("kernelspec") or {}).get("language") or "python")
    out: list[str] = []
    counted = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        src = cell.get("source") or ""
        body = ("".join(src) if isinstance(src, list) else str(src)).strip()
        if not body:
            continue
        kind = cell.get("cell_type")
        if kind == "markdown":
            out.append(body)
            counted += 1
        elif kind == "code":
            out.append(f"```{lang}\n{body}\n```")
            counted += 1
        # raw cells and all outputs are dropped on purpose
    return "\n\n".join(out), {"cells": counted}


def _plain_to_text(path: Path) -> tuple[str, dict[str, int]]:
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionFailed(f"unreadable text file: {exc}") from exc
    return body, {}


def _csv_to_text(path: Path) -> tuple[str, dict[str, int]]:
    """Header plus the first CSV_SAMPLE_ROWS rows -- never the whole dataset."""
    try:
        head = path.read_bytes()[:CSV_SAMPLE_BYTES].decode("utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionFailed(f"unreadable csv: {exc}") from exc
    reader = csv.reader(io.StringIO(head))
    rows: list[list[str]] = []
    try:
        for row in reader:
            rows.append(row)
            if len(rows) > CSV_SAMPLE_ROWS:
                break
    except csv.Error as exc:
        raise ExtractionFailed(f"malformed csv: {exc}") from exc
    if not rows:
        return "", {"columns": 0, "sampled_rows": 0}
    header, body = rows[0], rows[1 : CSV_SAMPLE_ROWS + 1]
    lines = [
        f"Columns ({len(header)}): " + ", ".join(header),
        "",
        f"First {len(body)} data rows (sample only; the full file is not indexed):",
        "",
        "```csv",
        ",".join(header),
    ]
    lines += [",".join(r) for r in body]
    lines.append("```")
    return "\n".join(lines), {"columns": len(header), "sampled_rows": len(body)}


_CONVERTERS = {
    "pdf": _pdf_to_text,
    "notebook": _notebook_to_text,
    "markdown": _plain_to_text,
    "text": _plain_to_text,
    "csv": _csv_to_text,
}


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def _sha256(path: Path) -> str | None:
    try:
        if path.stat().st_size > HASH_MAX_BYTES:
            return None
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _title(path: Path) -> str:
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return " ".join(stem.split()) or path.name


def _is_current(out: Path, digest: str | None, size: int, mtime: int) -> bool:
    """True if `out` was produced from this exact source by this extractor."""
    try:
        post = frontmatter.load(out)
    except (OSError, ValueError):
        return False
    meta = post.metadata
    if str(meta.get("extractor_version")) != EXTRACTOR_VERSION:
        return False
    if digest and meta.get("source_sha256"):
        return str(meta["source_sha256"]) == digest
    return meta.get("source_bytes") == size and meta.get("source_mtime") == mtime


def extract_file(settings: Settings, src: Path, *, force: bool = False) -> Path | None:
    """Extract one document. Returns the text path, or None if it was skipped.

    Raises ExtractionFailed for a source that cannot be converted; callers in
    `extract_all` record that in the report rather than aborting the run.
    """
    src = Path(src)
    ws = settings.paths.workspace
    rel = _rel(ws, src)
    if settings.should_ignore(rel):
        return None
    kind = SUPPORTED.get(src.suffix.lower())
    if kind is None:
        if src.suffix.lower() in UNSUPPORTED:
            raise ExtractionFailed(
                f"unsupported format {src.suffix.lower()} "
                f"(no parser available; install one and add a converter)"
            )
        return None

    try:
        stat = src.stat()
    except OSError as exc:
        raise ExtractionFailed(f"cannot stat source: {exc}") from exc

    out = text_path_for(settings, rel)
    digest = _sha256(src)
    if not force and out.exists() and _is_current(out, digest, stat.st_size, int(stat.st_mtime)):
        return None

    body, counts = _CONVERTERS[kind](src)
    truncated = False
    if len(body) > MAX_TEXT_CHARS:
        body = body[:MAX_TEXT_CHARS] + "\n\n[truncated by mitsync extract]"
        truncated = True

    meta: dict[str, object] = {
        "source": rel,
        "course": course_of(rel),
        "title": _title(src),
        "content_type": kind,
        "extracted_at": datetime.now(UTC).isoformat(),
        "extractor_version": EXTRACTOR_VERSION,
        "source_bytes": stat.st_size,
        "source_mtime": int(stat.st_mtime),
        "truncated": truncated,
    }
    if digest:
        meta["source_sha256"] = digest
    meta.update(counts)

    out.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body.strip() + "\n", **meta)
    out.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    log.debug("extracted %s -> %s", rel, out.name)
    return out


def extract_all(settings: Settings, *, force: bool = False) -> ExtractReport:
    """Extract every supported document under the mirror and course folders."""
    report = ExtractReport()
    settings.paths.kb_text.mkdir(parents=True, exist_ok=True)
    ws = settings.paths.workspace
    for src in iter_sources(settings):
        report.scanned += 1
        rel = _rel(ws, src)
        try:
            out = extract_file(settings, src, force=force)
        except ExtractionFailed as exc:
            if src.suffix.lower() in UNSUPPORTED:
                report.unsupported.append(rel)
            else:
                report.failed.append((rel, str(exc)))
            log.warning("extract failed for %s: %s", rel, exc)
            continue
        if out is None:
            report.skipped += 1
            continue
        report.extracted += 1
        report.outputs.append(out)
        try:
            if frontmatter.load(out).metadata.get("truncated"):
                report.truncated.append(rel)
        except (OSError, ValueError):  # pragma: no cover - just written
            pass
    log.info("extract: %s", report.summary())
    print(f"extract: {report.summary()}")
    for rel, why in report.failed:
        print(f"  failed: {rel} -- {why}")
    for rel in report.unsupported:
        print(f"  unsupported: {rel}")
    return report


def extracted_index(settings: Settings) -> dict[str, Path]:
    """Map workspace-relative source path -> its `_kb/text` file."""
    index: dict[str, Path] = {}
    text_dir = settings.paths.kb_text
    if not text_dir.is_dir():
        return index
    for path in sorted(text_dir.glob("*.md")):
        try:
            meta = frontmatter.load(path).metadata
        except (OSError, ValueError):
            continue
        src = meta.get("source")
        if isinstance(src, str) and src:
            index[src] = path
    return index
