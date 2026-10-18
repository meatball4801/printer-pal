#!/usr/bin/env bash
# Development: install this checkout on a Pi from your Mac: bash companion/install-on-pi.sh USER@PI
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo 'Usage: bash companion/install-on-pi.sh USER@PI [installer options]' >&2
  exit 1
fi
pi_target="$1"
shift
if [[ "$pi_target" == -* || ! "$pi_target" =~ ^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+$ ]]; then
  echo 'Use a Pi SSH destination such as noah@printer-camera.local.' >&2
  exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bundle="$(python3 "$script_dir/build_release.py")"
# Transfer the standalone file; the Pi never receives a GitHub access token.
scp "$bundle" "$pi_target:printer-pal-install.run"
remote_command='sudo bash ~/printer-pal-install.run'
for argument in "$@"; do
  printf -v quoted '%q' "$argument"
  remote_command+=" $quoted"
done
ssh -t "$pi_target" "$remote_command"
