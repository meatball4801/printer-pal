#!/usr/bin/env python3
"""Local operator commands. Pairing codes are not published over HTTP."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import socket
import sys
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, HTTPSHandler
sys.path.insert(0, '/opt/print-monitor')


def enrollment_url(config, code, pin):
    host = socket.gethostname().split('.')[0] + '.local'
    return f"https://{host}:{config['port']}/#" + urlencode({'pin': pin, 'code': code})


def show_enrollment(config, code):
    from transport import fingerprint, TLS_DIRECTORY
    pin = fingerprint(TLS_DIRECTORY / 'certificate.pem')
    url = enrollment_url(config, code, pin)
    print('In Printer Pal, choose Add camera → Connect your Pi → Scan setup code.')
    print('Setup link (includes the one-use code; keep it private): ' + url)
    try:
        import qrcode
        qr = qrcode.QRCode(border=4)
        qr.add_data(url)
        qr.make(fit=True)
        # Two QR rows per terminal line, black modules on an explicit white background.
        matrix = qr.get_matrix()
        for offset in range(0, len(matrix), 2):
            top = matrix[offset]
            bottom = matrix[offset + 1] if offset + 1 < len(matrix) else [False] * len(top)
            line = ''.join({(False, False): ' ', (True, False): '▀', (False, True): '▄', (True, True): '█'}[a, b]
                           for a, b in zip(top, bottom))
            print('\033[30;47m' + line + '\033[0m')
    except ImportError:
        print('QR renderer is unavailable. Paste the setup link into the app instead.')


def remaining_seconds(value):
    if "expires_monotonic" in value:
        return max(0, int(value["expires_monotonic"] - time.monotonic()))
    return max(0, int(value["expires_at"] - time.time()))


INSTALLED_PATHS = (
    '/etc/systemd/system/print-monitor.service', '/etc/systemd/system/print-monitor-network.service',
    '/etc/systemd/system/print-monitor-network.timer', '/etc/avahi/services/print-monitor.service',
    '/opt/print-monitor', '/etc/print-monitor', '/var/lib/print-monitor', '/run/print-monitor',
    '/run/print-monitor-network', '/run/printer-pal-install', '/usr/local/bin/print-monitor',
)


def uninstall(root=Path('/'), runner=subprocess.run):
    """Remove everything the installer added. Shared OS packages are left in place."""
    def run(*command):
        return runner(list(command), check=False, capture_output=True, text=True)
    run('systemctl', 'disable', '--now', 'print-monitor.service', 'print-monitor-network.timer', 'print-monitor-network.service')
    run('nft', 'delete', 'table', 'inet', 'printer_pal')
    if shutil.which('ufw'):
        listing = run('ufw', 'status', 'numbered').stdout or ''
        numbers = [int(match) for line in listing.splitlines() if '# Printer Pal' in line
                   for match in [line.split(']', 1)[0].strip(' [')] if match.isdigit()]
        # Delete from the highest number down so earlier numbers stay valid.
        for number in sorted(numbers, reverse=True):
            run('ufw', '--force', 'delete', str(number))
    for item in INSTALLED_PATHS:
        path = Path(root) / item.lstrip('/')
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'try-reload-or-restart', 'avahi-daemon.service')
    run('userdel', 'print-monitor')
    print('Printer Pal has been removed, including its settings and phone pairings.')
    print('Packages it installed, such as Python libraries, ffmpeg, and avahi, were left in place for other software.')
    print('In the Printer Pal app, remove the camera too.')
    return 0


def set_tailscale(enabled, config_path=Path('/etc/print-monitor/config.json'), runner=subprocess.run):
    """Allow (or stop allowing) paired phones to reach the camera over this Pi's own Tailscale network."""
    import grp
    import tempfile
    from config import validate_config
    config = validate_config(json.loads(config_path.read_text()))
    config['remote_tailscale'] = enabled
    fd, temporary = tempfile.mkstemp(dir=config_path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(config, output, indent=2)
            output.write('\n')
        os.chmod(temporary, 0o640)
        os.chown(temporary, 0, grp.getgrnam('print-monitor').gr_gid)
        os.replace(temporary, config_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Re-applies the firewall and the companion's allowed networks right away.
    runner(['systemctl', 'start', 'print-monitor-network.service'], check=True)
    if not enabled:
        print('Tailscale access is off. Only devices on this Pi\'s local network can reach the camera.')
        return 0
    address = runner(['tailscale', 'ip', '-4'], capture_output=True, text=True, check=False) if shutil.which('tailscale') else None
    if address is None or address.returncode != 0 or not address.stdout.strip():
        print('Tailscale access is on, but Tailscale isn\'t connected on this Pi yet.')
        print('Install it with: curl -fsSL https://tailscale.com/install.sh | sh   then run: sudo tailscale up')
        return 0
    print('Tailscale access is on. Paired phones signed in to the same Tailscale network can view this camera anywhere.')
    print(f"In Printer Pal, open Settings, then Remote address, and enter: {address.stdout.split()[0]}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "pair", "logs", "restart", "forget-phones", "remote", "uninstall"))
    parser.add_argument("options", nargs="*", help="For remote: tailscale on|off")
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument("--new", action="store_true", help="Open a new ten-minute pairing window")
    parser.add_argument("--link", action="store_true", help="With pair: print only the setup link, for the app's installer")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo to manage the companion or read its pairing code.")
    if args.command == 'uninstall':
        if not args.confirm:
            parser.error('Add --confirm to remove Printer Pal, its settings, and all phone pairings from this device.')
        return uninstall()
    if args.command == 'remote':
        if len(args.options) != 2 or args.options[0] != 'tailscale' or args.options[1] not in ('on', 'off'):
            parser.error('Use: sudo print-monitor remote tailscale on|off')
        return set_tailscale(args.options[1] == 'on')
    if args.command == 'forget-phones':
        if not args.confirm:
            parser.error('Add --confirm to revoke all paired phones. They will need to pair again.')
        subprocess.run(['systemctl', 'kill', '--kill-whom=main', '--signal=SIGUSR2', 'print-monitor'], check=True)
        print('Phone revocation requested. Open a new setup code with sudo print-monitor pair --new.')
        return 0
    if args.command == "pair":
        file = Path("/run/print-monitor/pairing.json")
        if args.new:
            file.unlink(missing_ok=True)
            subprocess.run(["systemctl", "kill", "--kill-whom=main", "--signal=SIGUSR1", "print-monitor"], check=True)
        for _ in range(30):
            try:
                value = json.loads(file.read_text())
                code = value.get('code')
                if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdecimal():
                    raise ValueError('Invalid local pairing data')
                remaining = remaining_seconds(value)
                if remaining > 0:
                    print(f"Pairing code: {value['code']} (one use; expires in {remaining} seconds)")
                    config = json.loads(Path('/etc/print-monitor/config.json').read_text())
                    if args.link:
                        from transport import fingerprint, TLS_DIRECTORY
                        print('PRINTER_PAL_SETUP_LINK ' + enrollment_url(config, value['code'], fingerprint(TLS_DIRECTORY / 'certificate.pem')))
                    elif config.get('tls', True):
                        show_enrollment(config, value['code'])
                    return 0
                break
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                time.sleep(0.1)
        print("Pairing is closed. Run: sudo print-monitor pair --new")
        return 1
    if args.command == "status":
        subprocess.run(["systemctl", "status", "print-monitor", "--no-pager"], check=False)
        try:
            config = json.loads(Path("/etc/print-monitor/config.json").read_text())
            scheme = 'https' if config.get('tls', True) else 'http'
            handlers = [ProxyHandler({})]
            if scheme == 'https':
                from transport import local_client_context
                handlers.append(HTTPSHandler(context=local_client_context()))
            with build_opener(*handlers).open(f"{scheme}://127.0.0.1:{config['port']}/api/info", timeout=3) as response:
                info = json.load(response)
            print(f"API reachable on port {config['port']}. Pairing {'open' if info['pairing_open'] else 'closed'}.")
            print("Camera readiness is shown in the paired iPhone app.")
            print('Transport: ' + ('HTTPS with a device-specific certificate' if scheme == 'https' else 'Legacy unencrypted HTTP'))
            return 0
        except (OSError, ValueError, KeyError):
            print("The companion API is not reachable. Run: sudo print-monitor logs")
            return 1
    command = ["journalctl", "-u", "print-monitor", "-n", "50", "--no-pager"] if args.command == "logs" else ["systemctl", "restart", "print-monitor"]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
