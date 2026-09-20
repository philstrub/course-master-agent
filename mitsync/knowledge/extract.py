"""
# Extract

Course documents to plain markdown in `_kb/text/`, deterministically.

## 1. What This Module Does

Walks the Canvas mirror and the student's own course folders, converts every
supported document (PDF, notebook, markdown, text, CSV) into markdown with
YAML frontmatter recording where it came from, and writes it under
`_kb/text/`. No judgment, no model, no network.

## 2. Why This Module Exists

Everything downstream that reasons about course content -- graph extraction,
course notes, search -- needs the text, not the container. Doing that
conversion once, deterministically, into a stable location means the expensive
and non-deterministic parts of the tool never have to open a PDF, and a
re-run costs nothing.

## 3. How It Fits in the Architecture

Between filing and knowledge: `sync` and `organize` put documents on disk,
`extract` turns them into text, `graph` and `kb` read that text. It is pure
I/O, so `mitsync extract` can never produce a pending judgment.

## 4. Key Concepts

**One source, one text file, forever.** The output filename is the SHA-1 of
the workspace-relative source path, so a document always maps to the same
text file and frontmatter always says which source it came from.

**Idempotent.** A second run over unchanged sources rewrites nothing; the
frontmatter's `source_sha256` (or size and mtime for files too large to hash)
decides. `EXTRACTOR_VERSION` forces a re-extraction when the output shape
changes. `force=True` re-extracts regardless.

**The tool must never index itself.** `source_roots` judges each candidate
root by its *resolved* target, not its name, because the OpenClaw setup
symlinks `<workspace>/skills` at `_agent/skills`. `_agent/`, `_kb/` and every
ignored subtree are pruned, and `tests/test_workspace_boundaries.py` enforces
it. Repo files appearing in `_kb/` is a bug to report, not content to process.

**Large documents are truncated, large datasets are sampled.** Text is capped
at `MAX_TEXT_CHARS` with the truncation recorded in frontmatter and in the
report; a CSV contributes its header and the first `CSV_SAMPLE_ROWS` rows,
never the whole dataset. Notebook outputs and raw cells are dropped.

**Why exceptions are caught here.** The parser boundary, and nothing else.
PyMuPDF raises arbitrary types on a malformed PDF, and the stdlib readers
raise `OSError`, `json.JSONDecodeError` and `csv.Error` on files the tool did
not write; all of them are translated into `ExtractionFailed`, naming the file
and, for a PDF, the page. `extract_all` records that in the report and keeps
going -- one corrupt file must not end the run. A document that cannot be read
is *failed*, never partially emitted: a placeholder page would otherwise reach
the knowledge base and the judgment payloads as if it were the document's real
content.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from mitsync.core.clock import now_iso
from mitsync.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

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
    paths = settings.paths
    for root in source_roots(settings):
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not settings.should_ignore(paths.safe_relative(here / d).as_posix())
                and not d.startswith(".")
            )
            for name in sorted(filenames):
                path = here / name
                rel = paths.safe_relative(path).as_posix()
                if settings.should_ignore(rel) or name.startswith("."):
                    continue
                if path.suffix.lower() not in SUPPORTED and path.suffix.lower() not in UNSUPPORTED:
                    continue
                yield path


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
            except Exception as exc:  # noqa: BLE001 -- mupdf raises many types
                # Fail the whole document. A placeholder here would land in the
                # KB and in judgment payloads as if it were the page's content.
                raise ExtractionFailed(f"page {i} is unreadable: {exc}") from exc
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
    """Digest of `path`, or None when it is too large to be worth hashing."""
    if path.stat().st_size > HASH_MAX_BYTES:
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def extract_file(settings: Settings, src: Path, *, force: bool = False) -> Path | None:
    """Extract one document. Returns the text path, or None if it was skipped.

    Raises ExtractionFailed for a source that cannot be converted; callers in
    `extract_all` record that in the report rather than aborting the run.
    """
    src = Path(src)
    rel = settings.paths.safe_relative(src).as_posix()
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
    if not force and out.exists():
        # Already current? `digest` is None only above HASH_MAX_BYTES; those, and
        # only those, fall back to comparing size and mtime.
        prior = frontmatter.load(out).metadata
        current = str(prior.get("extractor_version")) == EXTRACTOR_VERSION and (
            str(prior.get("source_sha256")) == digest
            if digest is not None
            else prior.get("source_bytes") == stat.st_size
            and prior.get("source_mtime") == int(stat.st_mtime)
        )
        if current:
            return None

    body, counts = _CONVERTERS[kind](src)
    truncated = False
    if len(body) > MAX_TEXT_CHARS:
        body = body[:MAX_TEXT_CHARS] + "\n\n[truncated by mitsync extract]"
        truncated = True

    meta: dict[str, object] = {
        "source": rel,
        "course": course_of(rel),
        "title": " ".join(src.stem.replace("_", " ").replace("-", " ").split()) or src.name,
        "content_type": kind,
        "extracted_at": now_iso(),
        "extractor_version": EXTRACTOR_VERSION,
        "source_bytes": stat.st_size,
        "source_mtime": int(stat.st_mtime),
        "truncated": truncated,
    }
    if digest:
        meta["source_sha256"] = digest
    meta.update(counts)

    post = frontmatter.Post(body.strip() + "\n", **meta)
    out.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    log.debug("extracted %s -> %s", rel, out.name)
    return out


def extract_all(settings: Settings, *, force: bool = False) -> ExtractReport:
    """Extract every supported document under the mirror and course folders."""
    report = ExtractReport()
    for src in iter_sources(settings):
        report.scanned += 1
        rel = settings.paths.safe_relative(src).as_posix()
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
        if frontmatter.load(out).metadata.get("truncated"):
            report.truncated.append(rel)
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
        meta = frontmatter.load(path).metadata
        src = meta.get("source")
        if isinstance(src, str) and src:
            index[src] = path
    return index
