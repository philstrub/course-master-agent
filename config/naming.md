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
| `lectures/` | lecture slides and notes, plus any data or code belonging to a lecture |
| `recitations/<item>/` | one folder per recitation — see "per-item folders" below |
| `assignments/<item>/` | one folder per assignment — see "per-item folders" below |
| `syllabus/` | syllabus, schedule, grading policy, course logistics |
| `notes/` | my own notes and anything I authored |
| `other/` | anything that clearly belongs to the course but fits none of the above |

### Material and its data live together

**There is no course-wide `data/` folder.** A dataset, starter notebook, code
bundle or fixture goes in the *same folder as the document it belongs to*:

- HW1's PDF, its starter notebook and its CSVs all land in `assignments/hw-01/`.
- Recitation 2's slides, its solutions and its code zip all land in
  `recitations/recitation-02/`.
- A dataset belonging to a lecture goes straight into `lectures/`, beside the
  slides — lectures do not get per-item folders.

Only a dataset that belongs to the course as a whole rather than to one lecture,
recitation or assignment goes to `other/`.

### Per-item folders

`recitations/` and `assignments/` contain **only folders**, never loose files.
Name each folder lowercase, hyphen-separated, with the number zero-padded to two
digits:

- Recitations: `recitation-01`, `recitation-02`.
- Anything graded or submitted: `hw-01`, `hw-02` — use `hw-` whatever the course
  calls it (Homework, Assignment, Problem Set, Deliverable), so filing does not
  depend on one instructor's vocabulary.
- Items with no number get a short lowercase slug instead: `midterm`, `final`,
  `final-project`, `pre-assignment`.
- Solutions stay in the folder of the item they solve. `Recitation2_Soln.pdf`
  goes in `recitations/recitation-02/`, not anywhere else.
- Never nest below the per-item folder.

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
  "Recitation 4" goes to `recitations/recitation-04/` even if it is named
  `lecture_notes.pdf`.
- Canvas folder path is the next strongest signal, then the filename, then the
  content type.
- **A data file never travels alone.** Work out which lecture, recitation or
  assignment it belongs to and put it in that item's folder. Canvas usually says
  so in the folder path — `Homeworks/HW1/HW1_data/profit.csv` is HW1's data, so
  it goes to `assignments/hw-01/profit.csv`. Only when no item owns it does it go
  to `other/`.
- A code bundle or notebook belongs to its item too: `julia-rec1.zip` under
  `Recitations/Recitation 1` goes to `recitations/recitation-01/`.

## 6. Confidence

Report low confidence (< 0.5) rather than guessing when the course is ambiguous
or the file could plausibly sit in two subfolders. Low-confidence placements are
shown for review instead of being applied silently.
