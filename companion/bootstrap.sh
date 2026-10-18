#!/usr/bin/env bash
# Printer Pal companion installer, published as the release asset install.sh.
# build_release.py fills in the version, download location, and installer checksum.
#
#   curl -fsSL https://github.com/meatball4801/printer-pal/releases/latest/download/install.sh | bash
#
# Run the same command again to update; pairing and settings are kept.
# Installer options go after `bash -s --`, for example: ... | bash -s -- --rescan
# `--check` downloads and verifies the installer without installing.
# `--detect` reports which camera source an install would use, without installing.
set -euo pipefail

# Everything runs inside a function so a partially downloaded script does nothing.
printer_pal_install() {
  local version='@VERSION@'
  local digest='@DIGEST@'
  local url="@BASE@/printer-pal-install-$version.run"
  local check=0
  if [[ "${1:-}" == "--check" ]]; then check=1; fi

  if [[ "$check" == 0 ]] && { [[ ! -f /etc/debian_version ]] || ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; }; then
    echo 'Printer Pal needs a Debian-based system such as Raspberry Pi OS. Run this command on the Pi, for example over SSH.' >&2
    return 1
  fi
  local tool
  for tool in curl python3 sha256sum; do
    if ! command -v "$tool" >/dev/null; then
      echo "Printer Pal needs $tool. Install it with: sudo apt install $tool" >&2
      return 1
    fi
  done
  local as_root=()
  if [[ $EUID -ne 0 ]]; then as_root=(sudo); fi

  printer_pal_work="$(mktemp -d)"
  trap 'rm -rf -- "$printer_pal_work"' EXIT
  echo "Downloading Printer Pal companion $version…"
  curl -fsSL @CURL_SECURITY@--retry 3 -o "$printer_pal_work/installer.run" "$url"
  if ! printf '%s  %s\n' "$digest" "$printer_pal_work/installer.run" | sha256sum --strict --quiet -c - >/dev/null 2>&1; then
    echo 'The downloaded installer failed its checksum. Nothing was installed; try again later.' >&2
    return 1
  fi
  if [[ "$check" == 1 ]]; then
    bash "$printer_pal_work/installer.run" --check
    return
  fi
  # The installer never reads stdin; keep it away from the piped script.
  ${as_root[@]+"${as_root[@]}"} bash "$printer_pal_work/installer.run" "$@" </dev/null
}

printer_pal_install "$@"
