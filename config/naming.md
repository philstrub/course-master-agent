# Filing rules

> **EDIT THIS FILE to change filing behavior — no code change needed.**
> These rules are injected verbatim into every judgment prompt (`organize plan`,
> `map`) and are also the spec the deterministic `rules` driver approximates.
> Write them as instructions to a careful assistant, not as code.

## 1. Course folders

One folder per course, at the top level of the workspace, using **my existing
human names** — never the Canvas course code, never a course number:

- `Machine Learning`
- `Analytics Edge`
- `Optimization`
- `Analytics Tools`
- `Analytics Lab`
- `AI_Studio`
- `From Anaytics to Action`  ← keep this spelling; it is the folder that exists

Never create a new top-level course folder. If a file cannot be attributed to
one of the folders above, leave it in the Canvas mirror and flag it.

## 2. Subfolders

Inside a course folder, every filed item goes in exactly one of:

| subfolder | contents |
|---|---|
| `lectures/` | lecture slides, lecture notes, lecture recordings/transcripts |
| `recitations/` | recitation and section materials, TA walkthroughs |
| `assignments/` | problem sets, homework, deliverables, projects, exams, solutions |
| `data/` | datasets and code fixtures: `.csv`, `.xlsx`, `.json`, `.parquet`, `.zip` of data |
| `syllabus/` | syllabus, schedule, grading policy, course logistics |
| `notes/` | my own notes and anything I authored |
| `other/` | anything that clearly belongs to the course but fits none of the above |

Do not invent further subfolders. Do not nest below these.

## 3. Filenames

- **Keep the original filename** whenever it is informative.
- Rename only when the original is uninformative — e.g. `download.pdf`,
  `Untitled.pdf`, `slides.pdf`, `document(3).pdf`, a bare hash or number.
  Then use: `<NN>-<short-topic>.<ext>`, lowercase, hyphen-separated.
- Preserve any leading lecture/session number the original already has, zero
  padded to two digits: `Lecture 3 - Trees.pdf` → `03-trees.pdf` only if a
  rename was needed; otherwise leave it exactly as is.
- If the material is dated and the date matters more than the sequence
  (guest lectures, one-off workshops), prefix `YYYY-MM-DD-`.
- Never change the extension. Never strip a meaningful version suffix
  (`-v2`, `-solutions`, `-final`).

## 4. Never touch pre-existing files

Files that were already in a course folder before mitsync ran are **never
renamed and never moved**. They may be read and referenced. Only newly mirrored
Canvas files are filed. (`--include-existing` opts into filing them, and even
then the original name is kept.)

## 5. When signals conflict

- **Canvas module grouping wins over the filename.** A file in the module
  "Recitation 4" goes to `recitations/` even if it is named `lecture_notes.pdf`.
- Canvas folder path is the next strongest signal, then the filename, then the
  content type.
- Solutions follow their assignment: a solution to pset 3 goes to
  `assignments/`, not `notes/`.
- Data files referenced by an assignment still go to `data/`.

## 6. Confidence

Report low confidence (< 0.5) rather than guessing when the course is ambiguous
or the file could plausibly sit in two subfolders. Low-confidence placements are
shown for review instead of being applied silently.
