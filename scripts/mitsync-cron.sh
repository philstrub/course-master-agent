#!/bin/bash
#
# mitsync-cron.sh -- the wrapper the scheduler actually executes.
#
#   ./scripts/mitsync-cron.sh sync     incremental Canvas sync + KB refresh
#   ./scripts/mitsync-cron.sh brief    deadlines + daily briefing
#   ./scripts/mitsync-cron.sh verify   weekly full re-check + doctor
#
# Designed for a LaunchAgent-run OpenClaw gateway, which means:
#   * a minimal PATH with NO nvm shims and NO shell-profile sourcing, so every
#     binary is resolved by absolute path or from an explicitly set PATH;
#   * no TTY, so nothing may prompt (mitsync's judgment commands never prompt;
#     `organize apply` does, which is exactly why it is NOT run here);
#   * its own macOS TCC identity -- a Calendar or Desktop grant made in
#     Terminal does not carry over. See docs/RUNBOOK.md.
#
# Exit code 20 from mitsync means "a task file awaits judgment", NOT failure.
# This script treats it as a distinct third outcome: log it, notify the user
# that something is pending, and exit 20 itself so the caller can tell the
# difference between "worked", "needs you", and "broke".
#
# Exit codes from this script:
#   0  everything completed
#  20  completed as far as it could; a judgment task is pending
#   1  a real failure
#  75  another run holds the lock (EX_TEMPFAIL); nothing was done

set -euo pipefail

# --------------------------------------------------------------------------
# Absolute paths -- assume nothing about the inherited environment.
# --------------------------------------------------------------------------
REPO="/Users/filippostrub/Desktop/MIT/courses/_agent"
WORKSPACE="/Users/filippostrub/Desktop/MIT/courses"
LOG_DIR="${REPO}/state/logs"
LOCK_DIR="${REPO}/state/mitsync-cron.lock"
ENV_FILE="${HOME}/.openclaw/.env"

# uv is normally at ~/.local/bin/uv (astral installer) or /opt/homebrew/bin/uv.
UV=""
for candidate in "${HOME}/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if [ -x "${candidate}" ]; then UV="${candidate}"; break; fi
done

# Give child processes a usable PATH anyway (uv itself shells out).
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

OSASCRIPT="/usr/bin/osascript"
TERMINAL_NOTIFIER="/opt/homebrew/bin/terminal-notifier"

# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------
usage() {
  printf 'usage: %s {sync|brief|verify}\n' "$(basename "$0")" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 1
fi

JOB="$1"
case "${JOB}" in
  sync|brief|verify) ;;
  -h|--help) usage; exit 0 ;;
  *) printf 'unknown job: %s\n' "${JOB}" >&2; usage; exit 1 ;;
esac

LOG_FILE="${LOG_DIR}/cron-${JOB}.log"
mkdir -p "${LOG_DIR}"

# --------------------------------------------------------------------------
# Logging: everything (stdout and stderr) is timestamped and appended.
# --------------------------------------------------------------------------
ts() { /bin/date "+%Y-%m-%dT%H:%M:%S%z"; }

log() {
  printf '[%s] %s\n' "$(ts)" "$*" >>"${LOG_FILE}"
}

# Run a command, appending its output to the log with a marker line.
run_logged() {
  log "\$ $*"
  # Do not let a non-zero exit kill the script here; callers inspect $?.
  set +e
  "$@" >>"${LOG_FILE}" 2>&1
  local rc=$?
  set -e
  log "-> exit ${rc}"
  return "${rc}"
}

notify() {
  # $1 = title, $2 = message. Best-effort only; never fails the run.
  local title="$1" message="$2"
  if [ -x "${TERMINAL_NOTIFIER}" ]; then
    "${TERMINAL_NOTIFIER}" -title "${title}" -message "${message}" >/dev/null 2>&1 || true
  elif [ -x "${OSASCRIPT}" ]; then
    local safe_title safe_message
    safe_title=${title//\"/\\\"}
    safe_message=${message//\"/\\\"}
    "${OSASCRIPT}" -e "display notification \"${safe_message}\" with title \"${safe_title}\"" \
      >/dev/null 2>&1 || true
  fi
}

# --------------------------------------------------------------------------
# Lock: single-user safe. mkdir is atomic, so two overlapping runs cannot both
# win. A sync and an organize plan racing over the same manifest would be a
# genuine corruption risk, so the loser does nothing rather than waiting.
# --------------------------------------------------------------------------
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  holder="unknown"
  if [ -r "${LOCK_DIR}/pid" ]; then holder=$(cat "${LOCK_DIR}/pid" 2>/dev/null || echo unknown); fi
  # A lock left behind by a crashed run would otherwise wedge every future
  # run, so drop it if its owner is gone.
  if [ "${holder}" != "unknown" ] && ! /bin/kill -0 "${holder}" 2>/dev/null; then
    log "removing stale lock from dead pid ${holder}"
    rm -rf "${LOCK_DIR}"
    mkdir "${LOCK_DIR}" 2>/dev/null || { log "lock still held; skipping ${JOB}"; exit 75; }
  else
    log "another mitsync run is active (pid ${holder}); skipping ${JOB}"
    exit 75
  fi
fi
printf '%s\n' "$$" >"${LOCK_DIR}/pid"
cleanup() { rm -rf "${LOCK_DIR}"; }
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------
# Environment: load ~/.openclaw/.env if present (CANVAS_TOKEN lives there).
# --------------------------------------------------------------------------
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090 -- path is a runtime constant, not resolvable statically
  . "${ENV_FILE}"
  set +a
  log "loaded ${ENV_FILE}"
else
  log "no ${ENV_FILE}; relying on the inherited environment"
fi

cd "${REPO}"

if [ -z "${UV}" ]; then
  log "FATAL: uv not found (looked in ~/.local/bin, /opt/homebrew/bin, /usr/local/bin)"
  notify "mitsync ${JOB} failed" "uv is not installed or not where this script looks."
  exit 1
fi

MITSYNC=("${UV}" run --project "${REPO}" mitsync)

log "==== ${JOB} starting (workspace ${WORKSPACE}) ===="

# --------------------------------------------------------------------------
# The jobs.
#
# Each is catch-up safe: mitsync derives its work from manifest state, never
# from "time since last run". A Mac that slept through four scheduled runs
# loses nothing -- the next run does all of it. Nothing below assumes it ran
# at the previous scheduled time.
#
# `organize apply` is deliberately absent from every job: filing needs human
# approval, which an unattended run cannot obtain.
# --------------------------------------------------------------------------
PENDING=0
FAILED=0
FAILED_STEP=""

step() {
  # Run one mitsync step, classifying exit 20 as pending rather than failure.
  local label="$1"; shift
  local rc=0
  run_logged "$@" || rc=$?
  case "${rc}" in
    0)
      ;;
    20)
      PENDING=1
      log "PENDING JUDGMENT from '${label}' -- see the task file printed above"
      ;;
    *)
      FAILED=1
      [ -n "${FAILED_STEP}" ] || FAILED_STEP="${label}"
      log "FAILED: '${label}' exited ${rc}"
      ;;
  esac
}

case "${JOB}" in
  sync)
    step "sync"           "${MITSYNC[@]}" sync
    step "extract"        "${MITSYNC[@]}" extract
    step "organize plan"  "${MITSYNC[@]}" organize plan
    step "due"            "${MITSYNC[@]}" due
    ;;
  brief)
    step "due"            "${MITSYNC[@]}" due
    step "brief"          "${MITSYNC[@]}" brief
    ;;
  verify)
    step "sync --full"    "${MITSYNC[@]}" sync --full
    step "extract --force" "${MITSYNC[@]}" extract --force
    step "graph extract"  "${MITSYNC[@]}" graph extract
    step "graph rebuild"  "${MITSYNC[@]}" graph rebuild
    step "kb build"       "${MITSYNC[@]}" kb build
    step "doctor"         "${MITSYNC[@]}" doctor
    ;;
esac

# --------------------------------------------------------------------------
# Outcome. Failure beats pending beats success in what the user is told.
# --------------------------------------------------------------------------
if [ "${FAILED}" -eq 1 ]; then
  log "==== ${JOB} FAILED (first failing step: ${FAILED_STEP}) ===="
  notify "mitsync ${JOB} failed" "Step '${FAILED_STEP}' failed. See state/logs/cron-${JOB}.log"
  exit 1
fi

if [ "${PENDING}" -eq 1 ]; then
  log "==== ${JOB} finished with a pending judgment task ===="
  notify "mitsync ${JOB}: judgment needed" \
    "A task in _agent/state/tasks/ needs resolving. Ask OpenClaw to resolve it."
  exit 20
fi

log "==== ${JOB} completed cleanly ===="
notify "mitsync ${JOB} done" "Completed with no errors."
exit 0
