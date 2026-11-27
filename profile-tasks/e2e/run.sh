#!/usr/bin/env bash
# Runs the end-to-end trials against an isolated Hermes home: fake HOME, own gateway, fake LLM providers.
# Usage: e2e/run.sh [scenario ...]   (default: all); PT_SCRIPT=tests/test_load_check.py runs that script instead.
# On Windows run it from Git Bash; the Hermes install is read from %LOCALAPPDATA%\hermes (HERMES_INSTALL_ROOT overrides).
# On every exit (success, failure, Ctrl-C, TERM from timeout) any gateway of the isolated home is stopped
# through its own control socket, then the run's directory is removed unless the run failed or PT_E2E_KEEP=1.
# Run directories older than a day under the e2e base (kept failures) are removed at start.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) WINDOWS=1 ;; *) WINDOWS=0 ;; esac
if [ "$WINDOWS" = 1 ]; then
  # Windows programs get C:/... paths; bash keeps its own /c/... form.
  native() { cygpath -m "$1"; }
  HERMES_ROOT="${HERMES_INSTALL_ROOT:-$(cygpath -u "$LOCALAPPDATA")/hermes}"
  PYTHON_IN_ENV="Scripts/python.exe"
  boot_python() { command -v python3 || command -v python; }
else
  native() { printf '%s' "$1"; }
  HERMES_ROOT="$HOME/.hermes"
  PYTHON_IN_ENV="bin/python"
  boot_python() { echo python3; }
fi
AGENT="${HERMES_AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
# The interpreter of the installed Hermes environment (read only); the checkout's venv/ may be an older Python.
PY="${HERMES_PYTHON:-$("$(boot_python)" -c 'import json,glob,sys; f=sorted(glob.glob(sys.argv[1]+"/installs/*/facts.json"))[0]; print(json.load(open(f))["packages"]["venv"]["environment"]+"/"+sys.argv[2])' "$(native "$HERMES_ROOT")" "$PYTHON_IN_ENV")}"
ROOT="${PT_E2E_BASE:-$HOME/.cache/profile-tasks-e2e}"
mkdir -p "$ROOT"
# Only directories named like a run (see BASE below), so a shared PT_E2E_BASE keeps everything else.
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-??????' \
  -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
# Its own directory per invocation, even for two started in the same second.
BASE="$(mktemp -d "$ROOT/$(date +%Y%m%d-%H%M%S)-XXXXXX")"
mkdir -p "$BASE/home" "$BASE/run"
chmod 700 "$BASE/run"

isolated() {
  if [ "$WINDOWS" = 1 ]; then
    # Everything a Windows Hermes derives its home from points into the run directory.
    local base home
    base="$(native "$BASE")"
    home="$base/home"
    mkdir -p "$BASE/home/AppData/Local" "$BASE/home/AppData/Roaming" "$BASE/tmp"
    env -i \
      PATH="$PATH" SYSTEMROOT="$SYSTEMROOT" SYSTEMDRIVE="$SYSTEMDRIVE" WINDIR="$WINDIR" COMSPEC="$COMSPEC" PATHEXT="$PATHEXT" \
      PROCESSOR_ARCHITECTURE="${PROCESSOR_ARCHITECTURE:-AMD64}" USERNAME="${USERNAME:-user}" \
      HERMES_GIT_BASH_PATH="${HERMES_GIT_BASH_PATH:-}" \
      HOME="$home" USERPROFILE="$home" HOMEDRIVE="${base%%/*}" HOMEPATH="${home#*:}" \
      APPDATA="$home/AppData/Roaming" LOCALAPPDATA="$home/AppData/Local" TEMP="$base/tmp" TMP="$base/tmp" \
      HERMES_HOME="$home/.hermes" HERMES_GATEWAY_LOCK_DIR="$home/.local/state/hermes/gateway-locks" \
      HERMES_STATE_DB_GUARD_BYPASS=1 PYTHONUTF8=1 PYTHONUNBUFFERED=1 NO_COLOR=1 TERM=dumb \
      PT_LOG_LEVEL="${PT_LOG_LEVEL:-INFO}" PYTHONPATH="$(native "$AGENT")" PT_REPO="$(native "$REPO")" \
      PT_AGENT="$(native "$AGENT")" PT_E2E_ROOT="$(native "$ROOT")" \
      PT_REAL_HOME="$(native "$HOME")" PT_REAL_HERMES="$(native "$HERMES_ROOT")" \
      "$PY" "$@"
  else
    env -i \
      PATH="/usr/local/bin:/usr/bin:/bin" LANG="${LANG:-C.UTF-8}" \
      HOME="$BASE/home" HERMES_HOME="$BASE/home/.hermes" XDG_RUNTIME_DIR="$BASE/run" \
      XDG_STATE_HOME="$BASE/home/.local/state" XDG_CONFIG_HOME="$BASE/home/.config" \
      HERMES_GATEWAY_LOCK_DIR="$BASE/home/.local/state/hermes/gateway-locks" \
      HERMES_STATE_DB_GUARD_BYPASS=1 PYTHONUNBUFFERED=1 NO_COLOR=1 TERM=dumb \
      PT_LOG_LEVEL="${PT_LOG_LEVEL:-INFO}" PYTHONPATH="$AGENT" PT_REPO="$REPO" PT_AGENT="$AGENT" PT_E2E_ROOT="$ROOT" \
      "$PY" "$@"
  fi
}

child=""
cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
  if [ -d "$BASE/home/.hermes" ]; then
    isolated "$(native "$REPO")/e2e/stop_gateway.py" "$(native "$BASE")/home/.hermes" || status=1
  fi
  if [ "$status" -eq 0 ] && [ -z "${PT_E2E_KEEP:-}" ]; then
    # Windows: a process just reaped may still hold a file for a moment ("Device or resource busy"). A leftover
    # directory must not turn a passing run into a failing one: the next run removes it once it is a day old.
    for _ in 1 2 3 4 5 6; do
      rm -rf "$BASE" 2>/dev/null && break
      sleep 2
    done
    if [ -d "$BASE" ]; then
      echo "e2e: could not remove $BASE (a file is still in use); it is removed by a later run" >&2
    fi
  else
    echo "e2e: kept $BASE" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

isolated "$(native "$REPO")/${PT_SCRIPT:-e2e/run_e2e.py}" "$@" &
child=$!
wait "$child"
