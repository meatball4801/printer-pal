#!/usr/bin/env bash
# Development only: serve a locally built release so the app's "Install from iPhone" flow
# can be tested before the GitHub repository is public.
#
#   bash companion/serve-dev-release.sh
#
# Then, in a Debug build of the app, open Install from iPhone and set the development
# installer URL to the address this prints. Stop with Ctrl-C.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
port="${1:-8765}"
host="$(scutil --get LocalHostName 2>/dev/null || hostname -s).local"
out="$(mktemp -d)"
trap 'rm -rf -- "$out"' EXIT
python3 "$script_dir/build_release.py" --output "$out" --base-url "http://$host:$port" >/dev/null
echo "Development installer URL: http://$host:$port/install.sh"
echo "On the Pi, the equivalent command is: curl -fsSL http://$host:$port/install.sh | bash"
python3 -m http.server --directory "$out" "$port"
