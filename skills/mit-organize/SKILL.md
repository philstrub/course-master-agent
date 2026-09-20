---
name: mit-organize
description: Propose and apply the filing of newly mirrored Canvas material into the student's own human-named course folders using `mitsync organize plan` / `apply` / `undo`. Always presents the plan for approval before applying. Use when the user asks to organize, file, sort, or tidy course materials, or to undo a filing that went wrong.
user-invocable: true
metadata:
  { "openclaw": { "requires": { "bins": ["uv"] }, "os": ["darwin"] } }
---

# mit-organize

Files newly mirrored Canvas material from `_canvas/` into the student's own
top-level course folders (`Machine Learning/`, `Optimization/`, `Analytics
Edge/`, …) under `/Users/filippostrub/Desktop/MIT/courses`.

**This skill moves files on disk. It is a three-step, approval-gated flow. Never
skip the approval.**

## The flow

### 1. Plan (changes nothing)

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync organize plan
```

Flags: `mitsync organize plan --help`. `plan` writes a plan file under
`_agent/state/plans/` and changes nothing else. Under the default `agent`
driver it will usually exit **20** first — see the exit-20 section below — and
then, after `resolve`, produce the plan.

### 2. Present for approval

Show the user a table of the proposed destinations before applying anything:

| source (in `_canvas/`) | → destination | confidence |
|---|---|---|

Call out explicitly:

- anything with **confidence < 0.5** — the tool deliberately reports low
  confidence rather than guessing, and those are shown for review instead of
  being applied silently;
- anything that is being **renamed** (the rules keep the original filename
  unless it is uninformative);
- any file that could not be attributed to a course — those stay in the Canvas
  mirror and are flagged, and that is the correct outcome.

Then ask, plainly: "Apply this plan?" Wait for a yes. A vague "sounds good"
about the *summary* is not approval of the *plan*; if you are unsure, ask again
naming the count of files that would move.

### 3. Apply (only after the user agrees)

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync organize apply
```

Flags: `mitsync organize apply --help`. Without `--yes`, `apply` prompts
interactively and **exits 1** if the answer is no. In a non-interactive context (a cron run, a gateway session with no TTY)
that prompt cannot be answered, so `apply` there requires `--yes` — which means
*you* must have obtained the human approval first. Do not add `--yes` to route
around a user who has not said yes.

Every apply records an undo log under `_agent/state/undo/`.

### 4. Undo

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync organize undo
```

`organize undo` takes an optional positional `log_id` argument; with no
argument it reverses the most recent apply. Note the exact form: it is
`mitsync organize undo` with an optional id, e.g. `mitsync organize undo
<log_id>`. If the user says "undo latest", run it bare.

## Things you must state plainly, every time they are relevant

- **Pre-existing student files are never moved.** Anything that was already in
  a course folder before mitsync ran is never renamed and never moved. Filing
  them requires **both** `--include-existing` on `organize plan` **and** `--yes`
  on `organize apply`, and even then the original filename is kept. Never pass
  `--include-existing` on your own initiative; it takes an explicit request
  from the user, and you should re-confirm before applying such a plan.
- **The Canvas mirror is never emptied.** Filing copies material *out of*
  `_canvas/` by hardlink or copy (`organize.link_mode` in
  `_agent/config/settings.yml`, default `hardlink`). `_canvas/` remains the
  verbatim mirror and is never the loser of a move; a filed copy is never the
  only copy of anything.
- **Filing rules live in `_agent/config/naming.md` and are changed by editing
  that prose file, not by changing code.** That file is injected verbatim into
  every judgment prompt and is the spec the `rules` driver approximates. If the
  user dislikes where something landed — "psets should go in `homework/` not
  `assignments/`", "stop renaming my slides" — the fix is to edit `naming.md`
  and re-plan. Never patch Python for a filing preference. Offer to make the
  `naming.md` edit and show the diff.

The rules file currently defines: seven fixed top-level course folders (never
create a new one); exactly seven allowed subfolders per course — `lectures/`,
`recitations/`, `assignments/`, `data/`, `syllabus/`, `notes/`, `other/`, with
no further nesting; keep the original filename unless it is uninformative;
Canvas module grouping beats the filename when signals conflict. Read
`naming.md` before judging, do not work from this summary.

## Exit code 20

Exit 20 = pending judgment. See `_agent/CLAUDE.md` § The dual execution model.

`organize plan` and `map` are the two judgment commands here. Resolving one
produces a **plan**, not a move — the approval gate above still applies before
`organize apply`. `mitsync map --apply` writes `config/courses.yml`.

## Guardrails

- **Never move a pre-existing student file without an `--include-existing` plan
  the user has explicitly approved.** Never pass `--include-existing` on your
  own initiative.

Everything else — untrusted Canvas content, no calendar/Canvas writes, no
`nandatown`/`.venv`, no secrets, stay inside
`/Users/filippostrub/Desktop/MIT/courses` — is `_agent/CLAUDE.md`
§ "Hard guardrails".
