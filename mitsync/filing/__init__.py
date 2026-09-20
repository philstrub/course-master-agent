"""
# Filing

Deciding where a document belongs in the student's own folder scheme, and
moving it there reversibly.

## 1. What This Module Does

`course_map` bootstraps and reads `config/courses.yml` -- the Canvas course id
to folder-name mapping -- and owns the workspace walk and the ignore rules.
`organize` produces a plan of `{source, destination, reason}`, applies it with
an undo log, and reverses it.

## 2. Why This Module Exists

Folder naming is the student's, not the tool's. **No folder name, course
number or bucket rule is hardcoded**: the mapping lives in `courses.yml` and
the filing policy lives as prose in `config/naming.md`, which is injected
verbatim into the judgment payload. Changing how material is filed is an edit
to English, never a patch to Python.

## 3. How It Fits in the Architecture

Filing reads `_canvas/` and writes into the human-named course folders, always
by copy or hardlink, so a mis-file can never lose the original. Both judgment
entry points here -- `map` and `organize plan` -- go through `llm.base`, so
all three drivers produce the same plan shape.

## 4. Key Concepts

**Plan and apply are separate commands.** A plan is data on disk; applying it
writes an undo log to `state/undo/` first. Existing files are never touched
without an explicit `--include-existing`.

**One bucket classifier.** `classify_bucket` is the single place a filename
becomes a bucket. It used to exist twice with regexes that disagreed, which
meant the same file could be filed two ways depending on the caller.
"""
