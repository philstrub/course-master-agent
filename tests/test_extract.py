"""Extraction: format coverage, idempotence, exclusion, and failure tolerance."""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from mitsync import extract as extract_mod
from mitsync.config import Settings


def make_pdf(path: Path, pages: tuple[str, ...] = ("page one text", "page two text")) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for body in pages:
        page = doc.new_page()
        page.insert_text((72, 72), body)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def make_notebook(path: Path) -> Path:
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Regression notes\n", "Fit a line.\n"]},
            {
                "cell_type": "code",
                "source": ["import numpy as np\n", "print(np.mean([1,2]))\n"],
                "outputs": [{"output_type": "stream", "text": ["SECRET OUTPUT\n"]}],
            },
            {"cell_type": "raw", "source": ["ignored"]},
        ],
        "metadata": {"kernelspec": {"language": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb))
    return path


@pytest.fixture
def seeded(workspace: Path, settings: Settings) -> Settings:
    """A workspace with one of every supported format plus excluded junk."""
    ml = workspace / "Machine Learning"
    make_pdf(ml / "lectures" / "lec01.pdf")
    make_notebook(ml / "assignments" / "hw1.ipynb")
    (ml / "data").mkdir(parents=True, exist_ok=True)
    rows = ["a,b,c"] + [f"{i},{i * 2},{i * 3}" for i in range(200)]
    (ml / "data" / "train.csv").write_text("\n".join(rows))
    (ml / "notes").mkdir(parents=True, exist_ok=True)
    (ml / "notes" / "my-notes.md").write_text("# My notes\n\nGradient descent.\n")
    (ml / "other").mkdir(parents=True, exist_ok=True)
    (ml / "other" / "handout.docx").write_bytes(b"PK\x03\x04 not really a docx")

    mirror = workspace / "_canvas" / "Analytics Edge"
    make_pdf(mirror / "syllabus.pdf", ("Analytics Edge syllabus",))

    junk = workspace / "AI_Studio" / "nandatown" / ".venv" / "lib" / "python3.12" / "site-packages"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "x.py").write_text("print('venv')")
    (junk / "readme.md").write_text("# venv readme")
    (workspace / "AI_Studio" / "nandatown" / "main.py").write_text("print('repo')")
    (workspace / "AI_Studio" / "nandatown" / "NOTES.md").write_text("# nandatown notes")
    return settings


def test_extracts_every_supported_format(seeded: Settings) -> None:
    report = extract_mod.extract_all(seeded)
    assert report.extracted == 5, report.summary()
    assert report.failed == []
    assert any(p.endswith("handout.docx") for p in report.unsupported)

    by_source = {
        frontmatter.load(p).metadata["source"]: frontmatter.load(p)
        for p in seeded.paths.kb_text.glob("*.md")
    }
    pdf = by_source["Machine Learning/lectures/lec01.pdf"]
    assert pdf.metadata["pages"] == 2
    assert pdf.metadata["course"] == "Machine Learning"
    assert pdf.metadata["content_type"] == "pdf"
    assert pdf.metadata["source_sha256"]
    assert "## page 1" in pdf.content and "## page 2" in pdf.content
    assert "page two text" in pdf.content

    nb = by_source["Machine Learning/assignments/hw1.ipynb"]
    assert nb.metadata["cells"] == 2
    assert "# Regression notes" in nb.content
    assert "```python" in nb.content
    assert "SECRET OUTPUT" not in nb.content  # outputs are dropped
    assert "ignored" not in nb.content  # raw cells are dropped

    csv_post = by_source["Machine Learning/data/train.csv"]
    assert csv_post.metadata["sampled_rows"] == extract_mod.CSV_SAMPLE_ROWS
    assert "199,398,597" not in csv_post.content  # never the whole file

    assert by_source["_canvas/Analytics Edge/syllabus.pdf"].metadata["course"] == "Analytics Edge"


def test_extraction_is_idempotent_and_force_redoes_it(seeded: Settings) -> None:
    first = extract_mod.extract_all(seeded)
    stamps = {p: p.stat().st_mtime_ns for p in seeded.paths.kb_text.glob("*.md")}

    second = extract_mod.extract_all(seeded)
    assert second.extracted == 0
    assert second.skipped == first.extracted
    assert {p: p.stat().st_mtime_ns for p in seeded.paths.kb_text.glob("*.md")} == stamps

    forced = extract_mod.extract_all(seeded, force=True)
    assert forced.extracted == first.extracted


def test_changed_source_is_re_extracted(seeded: Settings) -> None:
    extract_mod.extract_all(seeded)
    note = seeded.paths.workspace / "Machine Learning" / "notes" / "my-notes.md"
    note.write_text("# My notes\n\nNow about regularization.\n")
    report = extract_mod.extract_all(seeded)
    assert report.extracted == 1
    out = extract_mod.text_path_for(seeded, "Machine Learning/notes/my-notes.md")
    assert "regularization" in frontmatter.load(out).content


def test_nandatown_and_venv_are_never_extracted(seeded: Settings) -> None:
    extract_mod.extract_all(seeded)
    sources = [frontmatter.load(p).metadata["source"] for p in seeded.paths.kb_text.glob("*.md")]
    assert sources, "fixture produced nothing"
    assert not [s for s in sources if "nandatown" in s]
    assert not [s for s in sources if "site-packages" in s or ".venv" in s]
    ws = seeded.paths.workspace
    walked = [p.resolve().relative_to(ws).as_posix() for p in extract_mod.iter_sources(seeded)]
    assert walked
    assert not [p for p in walked if "nandatown" in p]


def test_corrupt_and_encrypted_pdfs_are_reported_not_fatal(seeded: Settings) -> None:
    import pymupdf

    bad = seeded.paths.workspace / "Machine Learning" / "lectures" / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not actually a pdf at all")

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    locked = seeded.paths.workspace / "Machine Learning" / "lectures" / "locked.pdf"
    doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    doc.close()

    report = extract_mod.extract_all(seeded)
    failed = dict(report.failed)
    assert "Machine Learning/lectures/corrupt.pdf" in failed
    assert "Machine Learning/lectures/locked.pdf" in failed
    assert "encrypted" in failed["Machine Learning/lectures/locked.pdf"]
    # the rest of the run still completed
    assert report.extracted >= 5


def test_large_extraction_is_truncated_and_flagged(seeded: Settings, monkeypatch) -> None:
    monkeypatch.setattr(extract_mod, "MAX_TEXT_CHARS", 200)
    big = seeded.paths.workspace / "Machine Learning" / "notes" / "big.txt"
    big.write_text("x" * 5000)
    extract_mod.extract_all(seeded)
    out = extract_mod.text_path_for(seeded, "Machine Learning/notes/big.txt")
    post = frontmatter.load(out)
    assert post.metadata["truncated"] is True
    assert "[truncated by mitsync extract]" in post.content
    assert len(post.content) < 400


def test_text_path_is_stable_and_derived_from_the_source_path(settings: Settings) -> None:
    a = extract_mod.text_path_for(settings, "Machine Learning/lectures/lec01.pdf")
    b = extract_mod.text_path_for(settings, "Machine Learning/lectures/lec01.pdf")
    c = extract_mod.text_path_for(settings, "Optimization/lectures/lec01.pdf")
    assert a == b != c
    assert a.parent == settings.paths.kb_text


def test_extracted_index_maps_sources_to_text(seeded: Settings) -> None:
    extract_mod.extract_all(seeded)
    index = extract_mod.extracted_index(seeded)
    assert "Machine Learning/lectures/lec01.pdf" in index
    assert index["Machine Learning/lectures/lec01.pdf"].exists()
