# AGENTS.md

See [CLAUDE.md](CLAUDE.md).

`mitsync` supports two execution modes — a cloud-API driver and an
agent-driven driver (task file + JSON result + `resolve`, exit code 20 when
a judgment is pending) — plus a deterministic rules-only driver for CI.
Whatever agent is reading this file (Claude Code, OpenClaw, or another
chatbot) should follow the contract and guardrails in `CLAUDE.md`; this
file exists only so the convention of looking for `AGENTS.md` also works.
