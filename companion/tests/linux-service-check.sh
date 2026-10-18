#!/usr/bin/env bash
# Ephemeral Linux CI runner only: exercise the real service sandbox with UFW.
# UFW configuration is bind-mounted from a disposable copy and all packet rules
# live in a separate network namespace; the host firewall is never changed.
set -euo pipefail
test "$(id -u)" = 0
service_work="$(mktemp -d /opt/printer-pal-service-test.XXXXXX)"
service_ns="pp-service-$$"
cleanup() {
  ip netns del "$service_ns" 2>/dev/null || true
  rm -rf -- "$service_work"
}
trap cleanup EXIT
cp -a companion/. "$service_work/"
mkdir "$service_work/ufw" "$service_work/config"
cp -a /etc/ufw/. "$service_work/ufw/"
id print-monitor >/dev/null 2>&1 || useradd --system --user-group --shell /usr/sbin/nologin print-monitor
mkdir -p /etc/print-monitor
ip netns add "$service_ns"
ip -n "$service_ns" link set lo up
ip -n "$service_ns" link add testlan0 type dummy
ip -n "$service_ns" address add 192.168.55.1/24 dev testlan0
ip -n "$service_ns" link set testlan0 up
cat > "$service_work/check.py" <<'PY'
import pathlib
import subprocess
from transport import ensure_identity
from network_guard import apply_guard, refresh_ufw

ensure_identity()
subprocess.run(['ufw', '--force', 'enable'], check=True)
scopes = [('testlan0', '192.168.55.0/24')]
apply_guard({'port': 32187}, scopes=scopes)
refresh_ufw(32187, scopes)
refresh_ufw(32187, scopes)
status = subprocess.check_output(['ufw', 'status'], text=True)
assert '32187' in status and '192.168.55.0/24' in status, status
assert '1990/udp' in status and '2021/udp' in status, status
subprocess.run(['nft', 'list', 'table', 'inet', 'printer_pal'], check=True)
assert pathlib.Path('/etc/print-monitor/tls/private-key.pem').stat().st_mode & 0o777 == 0o640
print('Real UFW status, rule installation, repeat refresh, TLS identity and nftables passed inside the service sandbox.')
PY
python3 - "$service_work" "$service_ns" <<'PY'
import configparser
import pathlib
import subprocess
import sys

work, namespace = sys.argv[1:]
unit = configparser.ConfigParser(interpolation=None)
# Preserve property spelling; systemd property names are case-sensitive.
unit.optionxform = str
unit.read(pathlib.Path(work) / 'print-monitor-network.service')
command = ['systemd-run', '--wait', '--pipe', '--collect']
for key, value in unit['Service'].items():
    if key != 'ExecStart':
        command += ['--property', f'{key}={value}']
command += ['--property', f'NetworkNamespacePath=/run/netns/{namespace}',
            '--property', f'BindPaths={work}/ufw:/etc/ufw {work}/config:/etc/print-monitor',
            '/usr/bin/python3', f'{work}/check.py']
subprocess.run(command, check=True)
PY
