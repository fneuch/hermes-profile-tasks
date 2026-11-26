#!/usr/bin/env bash
# Exercise install.sh with a fake `hermes` on PATH, in a throwaway Hermes root under ${TMPDIR:-/tmp}.
set -Eeuo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RUN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/usage-governor-install-test.XXXXXX")"
trap 'rm -rf "$RUN_ROOT"' EXIT
FAKEBIN="$RUN_ROOT/bin"
LOG="$RUN_ROOT/calls"
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/hermes" <<'FAKE_HERMES'
#!/bin/sh
printf '%s\n' "$*" >> "$UG_TEST_LOG"
printf '%s\n' "$HERMES_HOME" >> "$UG_TEST_LOG.homes"
case "$*" in "-p bad "*) exit 1 ;; esac
exit 0
FAKE_HERMES
chmod +x "$FAKEBIN/hermes"

failures=0
check() {  # check <label> <expected calls, one per line>
  local got
  got="$(cat "$LOG" 2>/dev/null || true)"
  if [ "$got" = "$2" ]; then echo "PASS $1"; else
    echo "FAIL $1"; printf -- '--- expected\n%s\n--- got\n%s\n' "$2" "$got"; failures=$((failures + 1)); fi
  rm -f "$LOG" "$LOG.homes"
}

check_homes() {  # check_homes <label> <root>: every hermes call ran with HERMES_HOME=<root>
  local got
  got="$(sort -u "$LOG.homes" 2>/dev/null || true)"
  if [ "$got" = "$2" ]; then echo "PASS $1"; else
    echo "FAIL $1"; printf -- '--- expected\n%s\n--- got\n%s\n' "$2" "$got"; failures=$((failures + 1)); fi
  rm -f "$LOG.homes"
}

fresh_root() {
  ROOT="$RUN_ROOT/home-$1/.hermes"
  mkdir -p "$ROOT/profiles/beta" "$ROOT/profiles/alpha"
  : > "$ROOT/profiles/not-a-dir"
}

run() {  # run <HERMES_HOME> [args...]; sets RC
  local home="$1"; shift
  RC=0
  env PATH="$FAKEBIN:$PATH" HERMES_HOME="$home" UG_TEST_LOG="$LOG" "$PLUGIN/install.sh" "$@" >/dev/null 2>&1 || RC=$?
}

S=fneuch/hermes-profile-tasks/usage-governor

fresh_root all
run "$ROOT"
check "default and every profile directory get install --enable" \
"-p default plugins install $S --enable
-p alpha plugins install $S --enable
-p beta plugins install $S --enable"

mkdir -p "$ROOT/plugins/usage-governor" "$ROOT/profiles/beta/plugins/usage-governor"
run "$ROOT"
check "a profile that has the plugin is only enabled (rerun)" \
"-p default plugins enable usage-governor
-p alpha plugins install $S --enable
-p beta plugins enable usage-governor"

fresh_root profile-home
run "$ROOT/profiles/alpha"
check "HERMES_HOME of a profile resolves to the root and finds every profile" \
"-p default plugins install $S --enable
-p alpha plugins install $S --enable
-p beta plugins install $S --enable"

# A root whose own path holds a /profiles/ part: only a direct parent named profiles marks a profile
# home (hermes_cli/profiles.py profile_root_for_env_home), so this root stays the root.
fresh_root "a/profiles/b"
run "$ROOT"
check_homes "a root under a profiles/ dir is not cut: HERMES_HOME stays that root" "$ROOT"
check "a root under a profiles/ dir installs into its own default and profiles" \
"-p default plugins install $S --enable
-p alpha plugins install $S --enable
-p beta plugins install $S --enable"

run "$ROOT/profiles/alpha"
check_homes "a profile home of that root resolves to that root" "$ROOT"
check "a profile home of that root installs into that root's default and profiles" \
"-p default plugins install $S --enable
-p alpha plugins install $S --enable
-p beta plugins install $S --enable"

fresh_root limit
run "$ROOT" beta default
check "named profiles limit the install, in the given order" \
"-p beta plugins install $S --enable
-p default plugins install $S --enable"

expect_rc() {  # expect_rc <code> <label>
  if [ "$RC" = "$1" ]; then echo "PASS $2"; else echo "FAIL $2 (exit $RC)"; failures=$((failures + 1)); fi
}

run "$ROOT" alpha nosuch
expect_rc 2 "unknown profile exits 2"
check "unknown profile: nothing runs" ""

mkdir -p "$ROOT/profiles/bad"
run "$ROOT" bad alpha
expect_rc 1 "a failing profile exits 1"
check "a failing profile does not stop the others" \
"-p bad plugins install $S --enable
-p alpha plugins install $S --enable"

echo "FAILURES $failures"
[ "$failures" = 0 ]
