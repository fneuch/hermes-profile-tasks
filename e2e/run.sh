#!/usr/bin/env bash
# Runs the end-to-end trials against an isolated Hermes home: fake HOME, own gateway, fake LLM providers.
# Usage: e2e/run.sh [scenario ...]   (default: all); PT_SCRIPT=tests/test_load_check.py runs that script instead.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENT="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
# The interpreter of the installed Hermes environment (read only); the checkout's venv/ may be an older Python.
PY="${HERMES_PYTHON:-$(python3 -c 'import json,glob,sys; f=sorted(glob.glob(sys.argv[1]+"/installs/*/facts.json"))[0]; print(json.load(open(f))["packages"]["venv"]["environment"]+"/bin/python")' "$HOME/.hermes")}"
BASE="${PT_E2E_BASE:-$HOME/.cache/profile-tasks-e2e}/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BASE/home" "$BASE/run"
chmod 700 "$BASE/run"
exec env -i \
  PATH="/usr/local/bin:/usr/bin:/bin" LANG="${LANG:-C.UTF-8}" \
  HOME="$BASE/home" HERMES_HOME="$BASE/home/.hermes" XDG_RUNTIME_DIR="$BASE/run" \
  XDG_STATE_HOME="$BASE/home/.local/state" XDG_CONFIG_HOME="$BASE/home/.config" \
  HERMES_GATEWAY_LOCK_DIR="$BASE/home/.local/state/hermes/gateway-locks" \
  HERMES_STATE_DB_GUARD_BYPASS=1 PYTHONUNBUFFERED=1 NO_COLOR=1 TERM=dumb \
  PT_LOG_LEVEL="${PT_LOG_LEVEL:-INFO}" PYTHONPATH="$AGENT" PT_REPO="$REPO" PT_AGENT="$AGENT" \
  "$PY" "$REPO/${PT_SCRIPT:-e2e/run_e2e.py}" "$@"
