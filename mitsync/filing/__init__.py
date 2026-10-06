"""
# Filing

Putting a document where the student's own folder scheme says it belongs, and
doing so reversibly.

## 1. What This Module Does

`course_map` reads the hand-maintained `config/courses.yml` -- the Canvas
course id to folder-name mapping, which is also the list of folders that count
as courses -- and owns the workspace walk and the ignore rules.
`organize` lists what is waiting to be filed, validates a plan of
`{file_id, destination, reason}` placements that the driving agent wrote,
applies it with an undo log, and reverses it.

## 2. Why This Module Exists

Folder naming is the student's, not the tool's. **No folder name, course
number or placement rule is hardcoded**: the mapping lives in `courses.yml` and
the filing policy lives as prose in `config/naming.md`, which the agent reads
itself when it writes a plan. Changing how material is filed is an edit to
English, never a patch to Python. The code checks only the structure that
prose promises.

## 3. How It Fits in the Architecture

Filing reads `_canvas/` and writes into the human-named course folders, always
by copy or hardlink, so a mis-file can never lose the original. Nothing in
this package decides a destination; it only refuses the ones that break a
guardrail.

## 4. Key Concepts

**The agent plans, the tool applies.** A plan is data on disk written by the
agent; `organize apply` validates every placement, reports each rejection with
its reason, asks for confirmation, and writes an undo log to `state/undo/`.
Pre-existing student files are never touched without an explicit
`--include-existing`.

**One display classifier.** `classify_bucket` is the single place a path
becomes a display bucket for the knowledge base. It used to exist twice with
regexes that disagreed, which meant the same file could be shown two ways
depending on the caller.
"""
