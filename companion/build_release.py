#!/usr/bin/env python3
"""Build a reproducible standalone installer; no source-host access is needed on the Pi."""
import ast
import base64
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile

SOURCE = Path(__file__).resolve().parent
FILES = ("agent.py", "camera_source.py", "autodetect.py", "usb_camera.py", "config.py", "install.py", "install.sh", "control.py", "print-monitor.service", "README.md", "SECURITY.md", "network_guard.py", "transport.py", "print-monitor-network.service", "print-monitor-network.timer", "bambu_monitor.py", "bambu-printer-ca.pem", "BAMBU.md")

HEADER = '''#!/usr/bin/env bash
set -euo pipefail
work_dir="$(mktemp -d)"
trap 'rm -rf -- "$work_dir"' EXIT
python3 - "$0" "$work_dir" <<'PY'
import base64, hashlib, io, pathlib, sys, tarfile
payload=pathlib.Path(sys.argv[1]).read_bytes().split(b"\\n__PRINTER_PAL_PAYLOAD__\\n",1)[1]
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload)),mode="r:gz") as archive:
    for member in archive.getmembers():
        path=pathlib.PurePosixPath(member.name)
        if not member.isfile() or path.is_absolute() or ".." in path.parts:
            raise SystemExit("Invalid installer payload")
    # Python 3.12+ can restrict extraction to plain files; members were validated above either way.
    archive.extractall(sys.argv[2], **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
root=pathlib.Path(sys.argv[2])
for line in (root/"SHA256SUMS").read_text().splitlines():
    digest,name=line.split("  ",1)
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:
        raise SystemExit("Installer checksum mismatch")
PY
if [[ "${1:-}" == "--check" ]]; then
  echo 'Printer Pal installer payload verified.'
  exit 0
fi
bash "$work_dir/install.sh" "$@"
exit 0
__PRINTER_PAL_PAYLOAD__
'''


RELEASE_BASE = "https://github.com/meatball4801/printer-pal/releases/download/companion-v{version}"


def build(output, base_url=None):
    version = next(ast.literal_eval(node.value) for node in ast.parse((SOURCE / "agent.py").read_text()).body
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets))
    contents = {name: (SOURCE / name).read_bytes() for name in FILES}
    contents["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in contents.items()).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    compressed = gzip.compress(raw.getvalue(), mtime=0)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / f"printer-pal-install-{version}.run"
    bundle.write_bytes(HEADER.encode() + base64.encodebytes(compressed))
    bundle.chmod(0o755)
    tar = output / f"printer-pal-companion-{version}.tar.gz"
    tar.write_bytes(compressed)
    # Public one-line installer: published unversioned so releases/latest/download/install.sh always works.
    bootstrap = output / "install.sh"
    base = (base_url or RELEASE_BASE.format(version=version)).rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]_/-]+", base):
        raise ValueError("Invalid download base URL")
    bootstrap.write_text((SOURCE / "bootstrap.sh").read_text()
                         .replace("@VERSION@", version)
                         .replace("@BASE@", base)
                         # Only a development server (see serve-dev-release.sh) uses plain HTTP; the pinned checksum still applies.
                         .replace("@CURL_SECURITY@", "--proto '=https' --tlsv1.2 " if base.startswith("https://") else "")
                         .replace("@DIGEST@", hashlib.sha256(bundle.read_bytes()).hexdigest()))
    bootstrap.chmod(0o755)
    (output / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in (bundle, tar, bootstrap)))
    return bundle


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SOURCE.parent / "dist")
    parser.add_argument("--base-url", help="Development only: where install.sh downloads the installer from")
    arguments = parser.parse_args()
    print(build(arguments.output, arguments.base_url))
