# Filing rules

> **EDIT THIS FILE to change filing behavior — no code change needed.**
> The driving agent reads this file itself before writing a filing plan
> (`mitsync unfiled --json` points at it). Write these rules as instructions to
> a careful assistant, not as code. `mitsync organize apply` enforces only the
> structure in §2 — course folders from `courses.yml`, the bucket names, and
> per-item folders under `assignments/` and `recitations/`. If you change that
> structure, `FILING_BUCKETS` / `PER_ITEM_BUCKETS` in `mitsync/filing/organize.py`
> must change with it.

## 1. Course folders

One folder per course, at the top level of the workspace, using **my existing
human names** — never the Canvas course code, never a course number:

- `Machine Learning`
- `Analytics Edge`
- `Optimization`
- `Analytics Tools`
- `Analytics Lab`
- `AI_Studio`
- `From Anaytics to Action`  ← keep this spelling, it is the folder that exists

Never create a new top-level course folder. If a file cannot be attributed to
one of the folders above, leave it in the Canvas mirror and flag it.

## 2. Subfolders

Inside a course folder, every filed item goes in exactly one of:

| subfolder | contents |
|---|---|
| `lectures/` | lecture slides and notes, plus any data or code belonging to a lecture |
| `case studies/` | `From Anaytics to Action` only: the cases and readings assigned for each class |
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
  calls it (Homework, Assignment, Problem Set, Deliverable, Lab), so filing does
  not depend on one instructor's vocabulary.
- A handout for a graded item is part of that item, not `other/`:
  `A2A_Lab1_Student_Handout.pdf` goes in `assignments/hw-01/`.
- Items with no number get a short lowercase slug instead: `midterm`, `final`,
  `final-project`, `pre-assignment`.
- Solutions stay in the folder of the item they solve. `Recitation2_Soln.pdf`
  goes in `recitations/recitation-02/`, not anywhere else.
- Never nest below the per-item folder.

**Every `assignments/<item>/` folder is also my working folder.** I add my own
files and subfolders there, and it may be a git repository. File Canvas
material only at its top level, never into a subfolder I made. A file of mine
already at the destination is never replaced: `organize apply` rejects the
placement, and you pick another name or leave the file out.

## 3. Filenames

Canvas upload names are not kept for lectures. Each course below says what the
filed name is. Anything a course section does not cover keeps its original
filename, and the general rules at the end of this section apply.

The module item title (`module_item_title`) and the subheader above it
(`module_subheader`) in `unfiled --json` are the course's own labels. They
often say more than the filename, and they win when the two disagree.

### AI_Studio

Lecture decks are Google Slides links. `mitsync sync` mirrors each one as a PDF
named after its title, e.g. `Slides: 2026-09-10 - Week 1 - 3 - Course Logistics`.
File it in `lectures/`. Drop the `Slides: ` prefix and the leading date, and keep
the rest of the title exactly:

- `lectures/Week 1 - 3 - Course Logistics.pdf`
- `lectures/Week 3 - 2 - Agentic Engineering.pdf`

Drop any character a filename cannot hold (`? : / \ * " < > |`):
`What Is an AI Agent?` becomes `Week 1 - 2 - What Is an AI Agent.pdf`.

### Analytics Edge

**One deck per lecture topic, and only the post-class version.**

- If a PostClass deck exists for a topic, file it. Put the PreClass deck (and
  any InClass deck) for that topic in the plan's `skips`, with the reason.
- If there is no PostClass deck yet, file the PreClass deck. When the PostClass
  deck arrives later, place it at the **same destination**. `organize apply`
  replaces the filed PreClass copy (only if I have not changed it) and marks it
  skipped.
- Decide Pre/Post from the module item title and subheader, not the filename.
  `InClass-RM_RegressionTree_2026.pdf` is the item "PostClass CART Regression
  Slides", so it is the post-class deck.

The filename is the topic title only. Remove the lecture number, Pre/Post/In
class, the instructor's initials, the year and words like `CombinedSlides`.
Join words with `_`, capitalise the first word, keep acronyms upper case and
put everything else in lower case:

- `lectures/Regression.pdf`
- `lectures/Classification.pdf`
- `lectures/CART_regression.pdf`
- `lectures/CART_classification.pdf`
- `lectures/Intro.pdf` (the course introduction, `Intro_Lecture1_2026_RM.pdf`)

Recitations and assignments keep their original names.

### From Anaytics to Action

- **Lectures:** `lectures/Class_<N>.pdf`. So `A2A_2026_Class_1.pdf` becomes
  `lectures/Class_1.pdf`.
- **Handouts are assignments.** A lab handout goes in the lab's
  `assignments/hw-NN/` folder under its original name (Lab 1 is `hw-01`).
- **Case studies and readings** go in `case studies/`. Name each one with a
  short lowercase form of its title, words joined by `_`, the way the ones
  already there are named:

  | title in Canvas | file |
  |---|---|
  | Neflix in 2011 | `netflix.pdf` |
  | The 4 Tiers of Digital Transformation | `tiers.pdf` |
  | OCP Group | `OCP_group.pdf` |
  | Strategy and the New Economics of Information | `strategy_in_the_age_of_information.pdf` |

  `mitsync sync` downloads each HBS Publishing case and article in the
  modules (it performs the same launch as clicking the link in Canvas), and
  they arrive in `unfiled` like any file, titled by their module item. A
  reading Canvas hosts as a file (e.g. `Toward_Global_Food_Security.pdf`) is
  filed the same way. If `case studies/` already has a file for that case
  (I downloaded some by hand), put the new one in `skips` with that reason
  instead of filing a second copy. `unfiled --json` lists under `links` only
  cases sync could not download. Report those to me with their `canvas_url`.

### Analytics Lab, Analytics Tools

Keep the original filenames.

### Machine Learning

**Lectures:** `lectures/<N>_<title>.pdf`, where `<N>` is the lecture number
without zero padding. `<title>` is the title on the deck's first slide, in
lower case, with words joined by `_`:

- `lectures/7_holistic_regression.pdf`

Take `<N>` from the filename (`Lec03_2026.pdf`, `Lecture-04.pdf` and
`Lecture05.pdf` are all standard numbers). If you cannot read the title slide,
leave the file out rather than guess.

### Optimization

**Lectures:** `lectures/L<N>.pdf`, e.g. `L1.pdf`, `L2.pdf`. Take `<N>` from the
module item title (`Lecture 5 slides`), not the filename.
`Fall_2026_15_C57-5.pdf` and `Lecture+2.pdf` are both real Canvas names.

### General rules (everything not covered above)

- Keep the original filename whenever it is informative.
- Rename only when the original is uninformative — e.g. `download.pdf`,
  `Untitled.pdf`, `slides.pdf`, `document(3).pdf`, a bare hash or number.
  Then use: `<NN>-<short-topic>.<ext>`, lowercase, hyphen-separated.
- Never change the extension. Never strip a meaningful version suffix
  (`-v2`, `-solutions`, `-final`).

## 4. Never touch pre-existing files

Files I put in a course folder myself are **never renamed and never moved** by
a routine run. They may be read and referenced. Only newly mirrored Canvas
files are filed.

Two things need `--include-existing`, which only I run, from a terminal, after
reviewing the plan:

- moving one of my own files (`file_id` `existing:<path>`)
- renaming a copy mitsync filed earlier, when these rules change. Place it
  again **by its `file_id`** with the new destination. The move is refused if
  I changed the copy since it was filed.

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

## 6. When unsure

Leave a file out of the plan rather than guessing when the course is ambiguous
or the file could plausibly sit in two subfolders, and say why in your reply to
the student. A file left out stays in the Canvas mirror and is listed again by
`mitsync unfiled` next time.

`skips` is only for files these rules say I do not want (a PreClass deck once
the PostClass one exists). A skipped file is not offered again. A file you are
unsure about goes in neither list.
