#!/usr/bin/env python3
"""Install or update the companion, preserving configuration and paired phones."""
import argparse
import json
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape
from urllib.parse import urlsplit
from config import load_config, validate_config
from autodetect import detect_source
from network_guard import lan_scopes

SOURCE = Path(__file__).resolve().parent
# Printed before machine-readable lines so the app can find them among installer output.
DETECT_PREFIX = "PRINTER_PAL_DETECT "


def supported_system(root=Path("/")):
    """Debian-family systems with apt and systemd: Raspberry Pi OS, OctoPi, MainsailOS, Ubuntu, DietPi."""
    return ((root / "etc/debian_version").exists() and shutil.which("apt-get") is not None
            and shutil.which("systemctl") is not None)


def atomic_write(path, contents, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def available_port(port):
    try:
        dual_stack = socket.has_dualstack_ipv6()
        with socket.socket(socket.AF_INET6 if dual_stack else socket.AF_INET) as sock:
            if dual_stack:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::" if dual_stack else "0.0.0.0", port))
        return True
    except OSError:
        return False


def choose_config(previous, args, installed=False, port_check=available_port):
    config = dict(previous)
    for key in ("name", "port", "mode", "source_url", "device"):
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value
    config['tls'] = True
    if not installed and args.port is None:
        upstream_port = None
        if config.get("mode") == "mjpeg" and isinstance(config.get("source_url"), str):
            parts = urlsplit(config["source_url"])
            upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        config["port"] = next((p for p in (32187, 32188, 32189, 32190) if p != upstream_port and port_check(p)), None)
        if config["port"] is None:
            raise ValueError("No free camera port. Specify --port with an unused port.")
    if args.port is not None and (not installed or args.port != previous["port"]) and not port_check(args.port):
        raise ValueError("The selected port is already in use. Leave the existing service running and choose another port.")
    return validate_config(config)


class Installer:
    def __init__(self, root=Path("/"), runner=subprocess.run):
        self.root, self.runner = Path(root), runner

    def path(self, value):
        return self.root / value.lstrip("/")

    def command(self, *args, check=True):
        return self.runner(list(args), check=check)

    def apt(self, action, *arguments):
        deadline = time.monotonic() + 600
        command = ['apt-get', action, '-o', 'DPkg::Lock::Timeout=600', *arguments]
        # Never stop for a configuration question; there's no one to answer it.
        env = {**os.environ, 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive'}
        while True:
            returncode, message = self.run_apt(command, env)
            if returncode == 0:
                return
            if time.monotonic() >= deadline or not any(text in message for text in ('Could not get lock', 'Unable to acquire')):
                raise subprocess.CalledProcessError(returncode, command)
            print('Waiting for the package manager to finish its current update…', flush=True)
            time.sleep(2)

    def run_apt(self, command, env):
        """Runs apt, showing its progress as it happens so long installs and lock waits don't look stuck."""
        if self.root != Path('/'):
            result = self.runner(command, check=False, capture_output=True, text=True, env=env)
            return result.returncode, (result.stdout or '') + (result.stderr or '')
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, env=env)
        lines = []
        for line in process.stdout:
            print(line, end='', flush=True)
            lines.append(line)
        return process.wait(), ''.join(lines)

    def installable(self, packages):
        """The subset of packages with an install candidate in this system's repositories."""
        result = self.runner(['apt-cache', 'policy', *packages], check=False, capture_output=True, text=True,
                             env={**os.environ, 'LC_ALL': 'C'})
        available, current = set(), None
        for line in (result.stdout or '').splitlines():
            if line and not line[0].isspace() and line.endswith(':'):
                current = line[:-1]
            elif current and line.strip().startswith('Candidate:') and line.split(':', 1)[1].strip() != '(none)':
                available.add(current)
        return [name for name in packages if name in available]

    def configure_firewall(self, port):
        if not shutil.which("ufw"):
            return
        result = self.runner(["ufw", "status"], check=True, capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"})
        if "Status: active" not in result.stdout:
            return
        addresses = self.runner(["ip", "-json", "address", "show", "up"], check=True, capture_output=True, text=True)
        interfaces = json.loads(addresses.stdout)
        physical = {item["ifname"] for item in interfaces if (self.path("/sys/class/net") / item["ifname"] / "device").exists()}
        scopes = lan_scopes(interfaces, physical)
        ipv6_enabled = "IPV6=yes" in self.path("/etc/default/ufw").read_text()
        for interface, subnet in scopes:
            if ":" in subnet and not ipv6_enabled:
                continue
            self.command("ufw", "allow", "in", "on", interface, "from", subnet, "to", "any",
                         "port", str(port), "proto", "tcp", "comment", "Printer Pal")
            print(f"UFW: allowed Printer Pal port {port} from {subnet} on {interface}.", flush=True)
        if not scopes:
            print("UFW is active but no physical LAN subnet was identified. Allow the companion port from your LAN with ufw before pairing.", flush=True)

    def install(self, config, image=False):
        saved_config = self.path('/etc/print-monitor/config.json')
        legacy_transport = saved_config.exists() and json.loads(saved_config.read_text()).get('tls') is not True
        if not image:
            print('Installing companion dependencies. This can take several minutes on a fresh system, and longer if '
                  'automatic updates are running; progress is shown below.', flush=True)
            self.apt('update')
            packages = ["python3", "avahi-daemon", "iproute2", "nftables", "python3-cryptography", "python3-qrcode", "python3-paho-mqtt"]
            optional = []
            if config["mode"] == "picamera2":
                if not self.installable(["python3-picamera2"]):
                    raise ValueError("This system doesn't provide Picamera2, which a directly attached Pi camera needs "
                                     "(Raspberry Pi OS Bookworm or later). To share a camera feed that already works, "
                                     "rerun with --source-url and its MJPEG address.")
                packages.append("python3-picamera2")
            if config["mode"] == "usb":
                packages.extend(("ffmpeg", "v4l-utils"))
            if config["mode"] == "auto":
                # No camera was found yet. Prepare for one, but don't fail on systems that lack a package.
                optional = ["python3-picamera2", "ffmpeg", "v4l-utils"]
                available = self.installable(optional)
                skipped = [name for name in optional if name not in available]
                if skipped:
                    print(f"Skipping packages this system doesn't provide: {', '.join(skipped)}.", flush=True)
                packages.extend(available)
            self.apt('install', '--no-upgrade', '-y', *packages)
        account = self.command("id", "print-monitor", check=False)
        if account.returncode:
            self.command("useradd", "--system", "--user-group", "--home-dir", "/var/lib/print-monitor",
                         "--shell", "/usr/sbin/nologin", "print-monitor")
        if config["mode"] in ("picamera2", "usb", "auto"):
            self.command("usermod", "-a", "-G", "video,render", "print-monitor")
        if not image:
            self.command("systemctl", "stop", "print-monitor.service", check=False)
        state_file = self.path('/var/lib/print-monitor/state.json')
        try:
            value = json.loads(state_file.read_text()) if legacy_transport and state_file.exists() else None
        except ValueError:
            value = None  # The agent resets unreadable pairing state on start.
        if isinstance(value, dict):
            value['tokens'] = []
            atomic_write(state_file, json.dumps(value) + '\n', 0o600)
            self.command('chown', 'print-monitor:print-monitor', str(state_file))
            print('Upgraded HTTP to HTTPS. Old phone access was revoked; pair again with the setup QR.', flush=True)
        destination = self.path("/opt/print-monitor")
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("agent.py", "config.py", "camera_source.py", "autodetect.py", "usb_camera.py", "network_guard.py", "transport.py", "bambu_monitor.py", "bambu-printer-ca.pem"):
            atomic_write(destination / name, (SOURCE / name).read_text())
        atomic_write(self.path("/usr/local/bin/print-monitor"), (SOURCE / "control.py").read_text(), 0o755)
        state = self.path("/var/lib/print-monitor")
        state.mkdir(parents=True, exist_ok=True)
        os.chmod(state, 0o700)
        self.command("chown", "print-monitor:print-monitor", str(state))
        config_path = self.path("/etc/print-monitor/config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(config_path.parent, 0o750)
        atomic_write(config_path, json.dumps(config, indent=2) + "\n", 0o640)
        self.command("chown", "root:print-monitor", str(config_path.parent), str(config_path))
        for unit in ("print-monitor.service", "print-monitor-network.service", "print-monitor-network.timer"):
            atomic_write(self.path("/etc/systemd/system") / unit, (SOURCE / unit).read_text())
        advertisement = f'''<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group><name replace-wildcards="yes">{escape(config['name'])} on %h</name>
<service><type>_printmonitor._tcp</type><port>{config['port']}</port><txt-record>protocol=1</txt-record><txt-record>tls={int(config["tls"])}</txt-record></service>
</service-group>
'''
        atomic_write(self.path("/etc/avahi/services/print-monitor.service"), advertisement)
        if not image:
            self.command("systemctl", "daemon-reload")
        self.command("systemctl", "enable", "avahi-daemon.service", "print-monitor.service", "print-monitor-network.timer")
        if not image:
            self.command("systemctl", "start", "avahi-daemon.service")
            self.command("systemctl", "start", "print-monitor-network.timer")
            self.command("systemctl", "restart", "print-monitor-network.service")
            self.command("systemctl", "restart", "print-monitor.service")
            self.command("systemctl", "is-active", "--quiet", "print-monitor.service")
            self.configure_firewall(config["port"])


def report_detection(previous, installed, rescan=False, detect=detect_source):
    """Print what an install would use, as one JSON line, for the app's review step."""
    if installed and not rescan:
        report = {"installed": True, "mode": previous["mode"], "source_url": previous.get("source_url"),
                  "device": previous.get("device"), "port": previous["port"],
                  "message": "Printer Pal is already installed. Updating keeps its camera choice and pairing."}
    else:
        detected, message = detect((previous["port"],) if installed else ())
        found = detected["mode"] != "picamera2" or "detected" in message
        report = {"installed": installed, "mode": detected["mode"] if found else "auto",
                  "source_url": detected["source_url"], "device": detected["device"], "port": None,
                  "message": message}
    print(DETECT_PREFIX + json.dumps(report), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("auto", "picamera2", "mjpeg", "usb"))
    parser.add_argument("--device", help="For explicit USB mode, e.g. /dev/video0")
    parser.add_argument("--rescan", action="store_true", help="Repeat automatic detection instead of preserving the saved source")
    parser.add_argument("--source-url")
    parser.add_argument("--name")
    parser.add_argument("--port", type=int)
    parser.add_argument("--image", action="store_true", help="Image-builder use: enable offline; dependencies must already be installed")
    parser.add_argument("--detect", action="store_true", help="Report the camera source that would be used, without installing")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run the installer with sudo.")
    if not args.image and not supported_system():
        parser.error("This installer needs a Debian-based system with apt and systemd, such as Raspberry Pi OS.")
    installed = Path("/etc/systemd/system/print-monitor.service").exists()
    config_path = Path("/etc/print-monitor/config.json")
    if args.detect:
        return report_detection(load_config(config_path), installed, args.rescan)
    try:
        previous = load_config(config_path)
        if args.source_url and args.mode is None:
            args.mode = "mjpeg"
        if args.rescan or (not installed and args.mode is None):
            args.mode = "auto"
        if args.mode == "auto" and not args.image:
            print("Scanning this Pi for working camera feeds and attached cameras…", flush=True)
            excluded = (previous['port'],) if installed else ()
            detected, message = detect_source(excluded)
            print(message, flush=True)
            # Save a working source to avoid switching cameras on every reboot.
            # No source yet: retain auto so a later USB connection is also detected.
            if detected['mode'] != 'picamera2' or 'detected' in message:
                args.mode, args.source_url, args.device = detected['mode'], detected['source_url'], detected['device']
        config = choose_config(previous, args, installed=installed, port_check=(lambda _: True) if args.image else available_port)
        Installer().install(config, image=args.image)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    print(f"\nPrinter Pal installed. Camera mode: {config['mode']}; companion port: {config['port']}.")
    print("Starts automatically on boot and retries when camera video becomes unavailable.")
    if args.image:
        print("First boot will create the device identity and pairing code.")
    else:
        reachable = False
        for _ in range(40):
            result = subprocess.run(['/usr/local/bin/print-monitor', 'status'], capture_output=True, text=True)
            if result.returncode == 0:
                reachable = True
                break
            time.sleep(0.25)
        if not reachable:
            print('Service started but its API health check failed. Run: sudo print-monitor logs', file=sys.stderr)
            return 1
        print('HTTPS API health check passed.' if config['tls'] else 'Legacy HTTP API health check passed.')
        for _ in range(30):
            if Path("/run/print-monitor/pairing.json").exists():
                subprocess.run(["/usr/local/bin/print-monitor", "pair"], check=False)
                break
            time.sleep(0.2)
        if not Path('/run/print-monitor/pairing.json').exists():
            subprocess.run(['/usr/local/bin/print-monitor', 'pair', '--new'], check=True)
        print("For status: sudo print-monitor status\nFor a new pairing code: sudo print-monitor pair --new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
