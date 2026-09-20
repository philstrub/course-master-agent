#!/bin/bash
#
# install.sh -- prepare this machine to host mitsync under OpenClaw.
#
#   ./openclaw/install.sh
#
# Idempotent and safe to re-run. It CHECKS and REPORTS far more than it
# changes. Specifically, it will NEVER:
#   * install or upgrade Node, or run brew;
#   * install OpenClaw itself (it prints the command for you to run);
#   * edit ~/.openclaw/openclaw.json (you merge the fragment by hand);
#   * touch ~/Library/LaunchAgents;
#   * write a secret anywhere.
#
# OpenClaw is OPTIONAL. mitsync works fully from a plain terminal without any
# of this; what OpenClaw adds is scheduling and a chat surface.
#
# Every step prints PASS, SKIP, WARN, or FAIL and, except for the Node check,
# keeps going so you get the whole picture in one run.

set -euo pipefail

REPO="/Users/filippostrub/Desktop/MIT/courses/_agent"
WORKSPACE="/Users/filippostrub/Desktop/MIT/courses"
SKILLS_SRC="${REPO}/skills"
SKILLS_LINK="${WORKSPACE}/skills"
CONFIG_FRAGMENT="${REPO}/openclaw/openclaw.config.json5"
OPENCLAW_DIR="${HOME}/.openclaw"
OPENCLAW_JSON="${OPENCLAW_DIR}/openclaw.json"
ENV_FILE="${OPENCLAW_DIR}/.env"

NODE_MIN_MAJOR=24
NODE_MIN_MINOR=16

PASS_N=0; SKIP_N=0; WARN_N=0; FAIL_N=0

hr()   { printf '%s\n' "------------------------------------------------------------------"; }
head1(){ printf '\n== %s\n' "$*"; }
pass() { PASS_N=$((PASS_N + 1)); printf '  PASS  %s\n' "$*"; }
skip() { SKIP_N=$((SKIP_N + 1)); printf '  SKIP  %s\n' "$*"; }
warn() { WARN_N=$((WARN_N + 1)); printf '  WARN  %s\n' "$*"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '        %s\n' "$*"; }

printf '\n'
hr
printf 'mitsync -> OpenClaw setup\n'
printf 'repo:      %s\n' "${REPO}"
printf 'workspace: %s\n' "${WORKSPACE}"
hr

# --------------------------------------------------------------------------
head1 "1. macOS"
# --------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
  pass "macOS $(sw_vers -productVersion 2>/dev/null || echo '(version unknown)')"
else
  fail "this integration is macOS-only (Apple Calendar via EventKit, LaunchAgent gateway, TCC)."
  note "uname -s reports: $(uname -s)"
  exit 1
fi

# --------------------------------------------------------------------------
head1 "2. Node >= ${NODE_MIN_MAJOR}.${NODE_MIN_MINOR} (or >= 26.1)"
# --------------------------------------------------------------------------
# This is the one hard gate. A half-configured OpenClaw on an unsupported Node
# is worse than none: the gateway starts, then dies in ways that look like
# mitsync bugs. Refuse rather than continue.
node_ok=0
if ! command -v node >/dev/null 2>&1; then
  fail "node is not on PATH."
else
  NODE_RAW="$(node --version 2>/dev/null || echo v0.0.0)"   # e.g. v20.19.2
  NODE_V="${NODE_RAW#v}"
  N_MAJOR="${NODE_V%%.*}"
  N_REST="${NODE_V#*.}"
  N_MINOR="${N_REST%%.*}"
  case "${N_MAJOR}" in (*[!0-9]*|"") N_MAJOR=0 ;; esac
  case "${N_MINOR}" in (*[!0-9]*|"") N_MINOR=0 ;; esac

  # Two supported lines: 24.16+ (LTS) and 26.1+ (current). 25.x sits between
  # them and is NOT supported, so this cannot be a single ">=" comparison.
  if [ "${N_MAJOR}" -eq "${NODE_MIN_MAJOR}" ] && [ "${N_MINOR}" -ge "${NODE_MIN_MINOR}" ]; then
    node_ok=1
  elif [ "${N_MAJOR}" -eq 26 ] && [ "${N_MINOR}" -ge 1 ]; then
    node_ok=1
  elif [ "${N_MAJOR}" -gt 26 ]; then
    node_ok=1
  fi

  if [ "${node_ok}" -eq 1 ]; then
    pass "node ${NODE_RAW} ($(command -v node))"
  else
    fail "node ${NODE_RAW} is below OpenClaw's minimum."
  fi
fi

if [ "${node_ok}" -ne 1 ]; then
  cat <<'NODEHELP'

        OpenClaw requires Node 24.16+ or 26.1+.

        Refusing to continue: configuring skills, cron jobs, and a gateway
        against an unsupported Node leaves you with a half-installed system
        that fails at runtime in confusing ways.

        Upgrade with whichever manager you already use, then re-run this
        script. With nvm:

            nvm install 24
            nvm alias default 24
            # open a new shell, then:
            node --version

        IMPORTANT: the OpenClaw gateway runs as a LaunchAgent, which does NOT
        source your shell profile and therefore does NOT see nvm's shims. If
        you manage Node with nvm, either install Node system-wide as well, or
        make sure `openclaw gateway install` records an absolute path to a
        supported node binary. Verify after install with:

            openclaw doctor

        mitsync itself does not use Node at all. If you only want the CLI,
        stop here -- everything in docs/RUNBOOK.md "Daily operation" already
        works.

NODEHELP
  printf '\nSummary: %d pass, %d skip, %d warn, %d fail\n\n' "${PASS_N}" "${SKIP_N}" "${WARN_N}" "${FAIL_N}"
  exit 1
fi

# --------------------------------------------------------------------------
head1 "3. openclaw on PATH"
# --------------------------------------------------------------------------
if command -v openclaw >/dev/null 2>&1; then
  pass "openclaw at $(command -v openclaw)"
  if [ -f "${OPENCLAW_JSON}" ]; then
    pass "onboarded: ${OPENCLAW_JSON} exists"
  else
    warn "${OPENCLAW_JSON} not found -- you have not onboarded yet."
    note "Run:  openclaw onboard"
    note "Then: openclaw gateway install      # installs the macOS LaunchAgent, port 18789"
  fi
else
  warn "openclaw is not installed."
  note "Install it with:"
  note "    curl -fsSL https://openclaw.ai/install.sh | bash"
  note "Then:"
  note "    openclaw onboard"
  note "    openclaw gateway install        # macOS LaunchAgent, gateway on port 18789"
  note "Re-run this script afterwards. Everything below still applies."
fi

# --------------------------------------------------------------------------
head1 "4. uv"
# --------------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  pass "uv $(uv --version 2>/dev/null | awk '{print $2}') at $(command -v uv)"
else
  fail "uv is not on PATH; mitsync cannot run."
  note "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  note "Note that scripts/mitsync-cron.sh looks for uv in ~/.local/bin,"
  note "/opt/homebrew/bin, and /usr/local/bin -- a LaunchAgent has no shims."
fi

# --------------------------------------------------------------------------
head1 "5. uv sync (Python deps)"
# --------------------------------------------------------------------------
# `--extra dev` matches `make install`. A bare `uv sync` would UNINSTALL pytest
# and ruff, because they live in the `dev` optional-dependency group -- which
# silently breaks `make test` for anyone who runs this script.
if command -v uv >/dev/null 2>&1; then
  if (cd "${REPO}" && uv sync --extra dev); then
    pass "uv sync --extra dev completed in ${REPO}"
  else
    fail "uv sync failed -- see the output above."
  fi
else
  skip "uv sync (uv is missing)"
fi

# --------------------------------------------------------------------------
head1 "6. <workspace>/skills symlink"
# --------------------------------------------------------------------------
# OpenClaw discovers skills from <workspace>/skills, <workspace>/.agents/skills,
# and ~/.agents/skills. The canonical files live in _agent/skills, so link it.
if [ -L "${SKILLS_LINK}" ]; then
  CURRENT="$(readlink "${SKILLS_LINK}")"
  if [ "${CURRENT}" = "${SKILLS_SRC}" ]; then
    pass "${SKILLS_LINK} -> ${SKILLS_SRC}"
  else
    warn "${SKILLS_LINK} is a symlink pointing somewhere else: ${CURRENT}"
    note "Leaving it alone. Remove it yourself, then re-run, if that is wrong."
  fi
elif [ -e "${SKILLS_LINK}" ]; then
  fail "${SKILLS_LINK} exists and is a real directory or file, not a symlink."
  note "Refusing to clobber it -- it may contain your own skills."
  note "Move its contents into ${SKILLS_SRC}, remove it, and re-run this script."
else
  if ln -s "${SKILLS_SRC}" "${SKILLS_LINK}"; then
    pass "created ${SKILLS_LINK} -> ${SKILLS_SRC}"
  else
    fail "could not create the symlink at ${SKILLS_LINK}"
  fi
fi

if [ -d "${SKILLS_SRC}" ]; then
  FOUND="$(find "${SKILLS_SRC}" -mindepth 2 -maxdepth 2 -name SKILL.md 2>/dev/null | wc -l | tr -d ' ')"
  NAMES="$(find "${SKILLS_SRC}" -mindepth 2 -maxdepth 2 -name SKILL.md -exec dirname {} \; 2>/dev/null \
           | xargs -n1 basename 2>/dev/null | sort | tr '\n' ' ')"
  if [ "${FOUND}" -ge 1 ]; then
    pass "${FOUND} skill(s) with a SKILL.md: ${NAMES}"
  else
    fail "no SKILL.md found under ${SKILLS_SRC}"
  fi
else
  fail "${SKILLS_SRC} does not exist."
fi

# --------------------------------------------------------------------------
head1 "7. OpenClaw config fragment (MANUAL MERGE)"
# --------------------------------------------------------------------------
if [ -f "${CONFIG_FRAGMENT}" ]; then
  pass "fragment: ${CONFIG_FRAGMENT}"
else
  fail "fragment missing at ${CONFIG_FRAGMENT}"
fi
cat <<CFGHELP

        This script does NOT edit ${OPENCLAW_JSON}.
        Your config already holds onboarding keys this fragment does not
        reproduce, so an automated overwrite would break the gateway.

        Merge it yourself:

            cp ${OPENCLAW_JSON} ${OPENCLAW_JSON}.bak
            \$EDITOR ${OPENCLAW_JSON}     # merge keys from the fragment (JSON5)
            openclaw doctor

        The fragment sets: workspace, tools.fs.workspaceOnly: true,
        tools.exec.mode "allowlist" with exact argv+cwd entries for the
        mitsync commands and the calendar binary, the Anthropic model
        default, and env.file pointing at ${ENV_FILE}.

        IF THE GATEWAY WILL NOT START after an edit:

            openclaw doctor --non-interactive

        A bad key can crash the gateway, and a crashed gateway can silently
        lose Full Disk Access -- which presents as missing files, not as a
        config error.

CFGHELP

# --------------------------------------------------------------------------
head1 "8. CANVAS_TOKEN"
# --------------------------------------------------------------------------
if [ -f "${ENV_FILE}" ]; then
  if grep -q '^CANVAS_TOKEN=..*' "${ENV_FILE}" 2>/dev/null; then
    pass "CANVAS_TOKEN is set in ${ENV_FILE}"
  else
    warn "${ENV_FILE} exists but has no non-empty CANVAS_TOKEN."
  fi
  PERMS="$(stat -f '%Lp' "${ENV_FILE}" 2>/dev/null || echo '???')"
  if [ "${PERMS}" = "600" ]; then
    pass "${ENV_FILE} is mode 600"
  else
    warn "${ENV_FILE} is mode ${PERMS}; run: chmod 600 ${ENV_FILE}"
  fi
else
  warn "${ENV_FILE} does not exist."
fi
cat <<ENVHELP

        mitsync reads the Canvas token from \$CANVAS_TOKEN
        (config/settings.yml -> canvas.token_env). Put it in ${ENV_FILE},
        which both the gateway and scripts/mitsync-cron.sh load:

            mkdir -p ${OPENCLAW_DIR}
            touch ${ENV_FILE} && chmod 600 ${ENV_FILE}
            \$EDITOR ${ENV_FILE}       # add: CANVAS_TOKEN=<token>

        Get a token at Canvas -> Account -> Settings -> "+ New Access Token".
        This script will not write it for you and never prints it.

        Do NOT set ANTHROPIC_API_KEY there for mitsync's sake. With no key,
        mitsync's driver resolves to 'agent' -- the OpenClaw model judges via
        the exit-20 / \`mitsync resolve\` protocol, which is the intended setup.

ENVHELP

# --------------------------------------------------------------------------
head1 "9. Calendar CLI and the one-time TCC grant"
# --------------------------------------------------------------------------
CAL_CLI="ical-guy"
if command -v grep >/dev/null 2>&1 && [ -f "${REPO}/config/settings.yml" ]; then
  FROM_YML="$(grep -E '^[[:space:]]*cli:' "${REPO}/config/settings.yml" | head -1 \
              | sed -E 's/.*cli:[[:space:]]*([^[:space:]#]+).*/\1/')"
  [ -n "${FROM_YML}" ] && CAL_CLI="${FROM_YML}"
fi

if command -v "${CAL_CLI}" >/dev/null 2>&1; then
  pass "${CAL_CLI} at $(command -v "${CAL_CLI}")"
else
  warn "${CAL_CLI} is not on PATH; \`mitsync calendar\` and the calendar half of"
  note "\`mitsync brief\` will fail (the briefing reports this rather than guessing)."
  note "Install it (e.g. brew install ical-guy) -- this script does not run brew."
fi

cat <<TCCHELP

        macOS Calendar access is gated by TCC, and a grant binds to a code
        identity AND path. That has two consequences:

        1. ONE-TIME INTERACTIVE GRANT. The permission prompt only appears for
           an interactive run. From a Terminal window, run once:

               ${CAL_CLI} list --format json --days 1

           and approve the prompt. Check it landed in
           System Settings -> Privacy & Security -> Calendars.

        2. THE LAUNCHAGENT GATEWAY IS A DIFFERENT IDENTITY. The grant you just
           made covers your terminal, NOT the gateway that OpenClaw installs
           as a LaunchAgent. The gateway needs its own grant. After
           \`openclaw gateway install\`, ask the running gateway to read the
           calendar once (e.g. via \`openclaw agent\`) while you are at the
           machine, so the prompt can appear and you can approve it.

           Same story for ~/Desktop, which is itself TCC-gated -- and the
           whole workspace lives under it. The gateway may need Full Disk
           Access in System Settings -> Privacy & Security.

        mitsync never writes to Apple Calendar. Read-only, always.

TCCHELP

# --------------------------------------------------------------------------
head1 "10. Script permissions"
# --------------------------------------------------------------------------
for s in "${REPO}/openclaw/install.sh" "${REPO}/openclaw/cron.sh" "${REPO}/scripts/mitsync-cron.sh"; do
  if [ -f "${s}" ]; then
    chmod +x "${s}" 2>/dev/null || true
    if [ -x "${s}" ]; then pass "executable: ${s}"; else fail "not executable: ${s}"; fi
  else
    fail "missing: ${s}"
  fi
done

# --------------------------------------------------------------------------
head1 "11. mitsync doctor"
# --------------------------------------------------------------------------
DOCTOR_RC=0
if command -v uv >/dev/null 2>&1; then
  (cd "${REPO}" && uv run --project "${REPO}" mitsync doctor) || DOCTOR_RC=$?
  if [ "${DOCTOR_RC}" -eq 0 ]; then
    pass "mitsync doctor: no FAIL rows"
  else
    warn "mitsync doctor exited ${DOCTOR_RC} -- read its table above and fix the FAIL rows."
  fi
else
  skip "mitsync doctor (uv is missing)"
fi

# --------------------------------------------------------------------------
hr
printf 'Summary: %d pass, %d skip, %d warn, %d fail\n' "${PASS_N}" "${SKIP_N}" "${WARN_N}" "${FAIL_N}"
hr
cat <<'NEXT'

Next steps, in order:

  1. Merge openclaw/openclaw.config.json5 into ~/.openclaw/openclaw.json, then
     run `openclaw doctor`.
  2. Put CANVAS_TOKEN in ~/.openclaw/.env (chmod 600).
  3. Grant Calendar access interactively -- once from Terminal, once from the
     LaunchAgent gateway.
  4. Register the schedule:  ./openclaw/cron.sh
  5. Talk to it:             openclaw agent
     or the dashboard:       openclaw dashboard     (http://127.0.0.1:18789)

See docs/RUNBOOK.md -> "OpenClaw integration" for the schedule table and the
failure table.

NEXT

if [ "${FAIL_N}" -ne 0 ]; then exit 1; fi
exit 0
