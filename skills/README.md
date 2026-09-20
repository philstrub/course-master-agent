# `_agent/skills/` — OpenClaw skills for `mitsync`

Four skills that let an agent (OpenClaw's chat surface, Claude Code, anything
that discovers `SKILL.md` files) drive the `mitsync` CLI conversationally.

**These are a convenience layer. `mitsync` never depends on them, and never
depends on OpenClaw.** Every command they wrap is fully usable from a plain
terminal; delete this directory and nothing in the CLI changes.

## The skills

| Skill | Wraps | Judgment? |
|---|---|---|
| `mit-canvas-sync` | `mitsync sync`, `mitsync doctor` | no — pure I/O |
| `mit-organize` | `mitsync organize plan` / `apply` / `undo`, `mitsync map` | **yes** — `organize plan` and `map` can exit 20 |
| `mit-briefing` | `mitsync due`, `mitsync brief`, `mitsync calendar` | no — pure I/O |
| `mit-kb` | `mitsync extract`, `graph extract`, `graph rebuild`, `kb build`, `graph query` | **yes** — `graph extract` and `kb build` can exit 20 |

The four commands that take `--driver` / `--resolve` are `map`,
`organize plan`, `graph extract`, and `kb build`. See `_agent/CLAUDE.md`
§ The dual execution model.

## How discovery works

OpenClaw discovers skills from `<workspace>/skills`,
`<workspace>/.agents/skills`, and `~/.agents/skills`. The canonical files live
here in `_agent/skills/`, so `openclaw/install.sh` creates

```
/Users/filippostrub/Desktop/MIT/courses/skills  ->  /Users/filippostrub/Desktop/MIT/courses/_agent/skills
```

as a symlink (idempotently; it refuses to clobber a real directory). Edit the
files here — the symlink means there is nothing to copy or sync.

## Frontmatter contract

Each skill is a directory containing `SKILL.md` with YAML frontmatter:

```yaml
---
name: mit-canvas-sync           # required
description: ...                # required — this is what the model matches on
user-invocable: true            # optional, default true; exposes a slash command
metadata:
  { "openclaw": { "requires": { "bins": ["uv"], "env": ["CANVAS_TOKEN"] }, "os": ["darwin"] } }
---
```

`metadata.openclaw.requires` gates the skill on `bins`, `env`, `config`, and
`os`. `mit-canvas-sync` is the only one gated on `env: ["CANVAS_TOKEN"]`, since
it is the only one that talks to Canvas; the rest work offline against what is
already mirrored. `disable-model-invocation` is available if you ever want a
skill to be slash-command-only — none of these set it.

## Guardrails

The contract lives in `_agent/CLAUDE.md` § "Hard guardrails". The skills
restate only the guardrail specific to each of them; everything else — Canvas
content as untrusted data, no calendar writes, no `nandatown`/`.venv`, no
secrets in chat — is read from there, not duplicated per skill.

## Related

- `_agent/openclaw/` — config fragment, install script, cron registration.
- `_agent/scripts/mitsync-cron.sh` — the wrapper the scheduler executes.
- `_agent/docs/RUNBOOK.md` → "OpenClaw integration".
- `_agent/CLAUDE.md` — the guardrail contract these skills restate.
