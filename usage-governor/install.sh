#!/usr/bin/env bash
# Install and enable usage-governor in the default profile and every profile found, or only the named ones.
# Usage: install.sh [PROFILE...]   ("default" names the default profile)
# Run it again at any time: a profile that has the plugin is only enabled, never reinstalled.
set -Eeuo pipefail

SOURCE="fneuch/hermes-profile-tasks/usage-governor"
NAME="usage-governor"

usage() {
  sed -n '2,4s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
esac

# The Hermes root as Hermes resolves it (hermes_cli/profiles.py profile_root_for_env_home): HERMES_HOME,
# or its grandparent when its direct parent is named profiles (a profile's home), or ~/.hermes.
resolve_hermes_root() {
  local home_dir="${HERMES_HOME:-$HOME/.hermes}" parent
  parent="$(dirname "$home_dir")"
  if [ "$(basename "$parent")" = profiles ]; then home_dir="$(dirname "$parent")"; fi
  printf '%s\n' "$home_dir"
}

HOME_DIR="$(resolve_hermes_root)"
export HERMES_HOME="$HOME_DIR"

if ! command -v hermes >/dev/null 2>&1; then
  echo "Cannot install: 'hermes' is not on PATH." >&2
  exit 2
fi

profile_root() {
  if [ "$1" = default ]; then printf '%s\n' "$HOME_DIR"; else printf '%s\n' "$HOME_DIR/profiles/$1"; fi
}

profiles=()
if [ "$#" -gt 0 ]; then
  for profile in "$@"; do
    if [ "$profile" != default ] && [ ! -d "$HOME_DIR/profiles/$profile" ]; then
      echo "No profile '$profile' in $HOME_DIR/profiles; nothing was installed." >&2
      exit 2
    fi
    profiles+=("$profile")
  done
else
  profiles=(default)
  if [ -d "$HOME_DIR/profiles" ]; then
    for profile_dir in "$HOME_DIR"/profiles/*; do
      [ -d "$profile_dir" ] || continue
      profiles+=("$(basename "$profile_dir")")
    done
  fi
fi

# Always pass -p: without it the sticky active profile (hermes profile use) would be the one changed.
failed=()
for profile in "${profiles[@]}"; do
  if [ -e "$(profile_root "$profile")/plugins/$NAME" ]; then
    echo "== $profile: installed already, enabling"
    hermes -p "$profile" plugins enable "$NAME" || failed+=("$profile")
  else
    echo "== $profile: installing"
    hermes -p "$profile" plugins install "$SOURCE" --enable || failed+=("$profile")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "Failed in: ${failed[*]}. Fix the error above and run install.sh again." >&2
  exit 1
fi
printf '\n%s enabled in: %s\n' "$NAME" "${profiles[*]}"
printf '%s\n' 'The usage tick runs from the default profile only.'
