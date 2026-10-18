"""Protect only the companion TCP port; preserve the host's other firewall tables."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import shutil
import time
from config import load_config

SCOPES_FILE = Path('/run/print-monitor-network/scopes.json')
# Opt-in remote viewing over the user's own Tailscale network: its interface and address ranges only.
TAILSCALE_INTERFACE = 'tailscale0'
TAILSCALE_SCOPES = [(TAILSCALE_INTERFACE, '100.64.0.0/10'), (TAILSCALE_INTERFACE, 'fd7a:115c:a1e0::/48')]


def lan_scopes(interfaces, physical_names):
    scopes = set()
    private_v4 = [ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    for interface in interfaces:
        name = interface.get('ifname')
        if name not in physical_names:
            continue
        for entry in interface.get('addr_info', []):
            try:
                address = ipaddress.ip_address(entry['local'])
                network = ipaddress.ip_network(f"{address}/{entry['prefixlen']}", strict=False)
            except (KeyError, ValueError, TypeError):
                continue
            if address.version == 4:
                if not any(address in private and network.subnet_of(private) for private in private_v4):
                    continue
            elif network.prefixlen < 64 or address.is_loopback or address.is_multicast or address.is_unspecified:
                continue
            scopes.add((name, str(network)))
    return sorted(scopes)


def discover_scopes(root=Path('/'), runner=subprocess.run, tailscale=False):
    result = runner(['ip', '-json', 'address', 'show', 'up'], check=True, capture_output=True, text=True)
    interfaces = json.loads(result.stdout)
    net = Path(root) / 'sys/class/net'
    physical = set()
    for item in interfaces:
        name = item['ifname']
        if (net / name / 'device').exists() or any((port / 'device').exists() for port in (net / name / 'brif').glob('*')):
            physical.add(name)
    scopes = lan_scopes(interfaces, physical)
    if tailscale and any(item['ifname'] == TAILSCALE_INTERFACE for item in interfaces):
        scopes = sorted(set(scopes) | set(TAILSCALE_SCOPES))
    return scopes


def ruleset(port, scopes, existing=False):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid companion port')
    lines = ['delete table inet printer_pal'] if existing else []
    lines += ['table inet printer_pal {', ' chain input {',
              '  type filter hook input priority -20; policy accept;',
              f'  iifname "lo" tcp dport {port} accept']
    for interface, subnet in scopes:
        network = ipaddress.ip_network(subnet)
        family = 'ip6' if network.version == 6 else 'ip'
        lines.append(f'  iifname {json.dumps(interface)} {family} saddr {network} tcp dport {port} accept')
    lines += [f'  tcp dport {port} drop', ' }', '}']
    return '\n'.join(lines) + '\n'


def apply_guard(config, runner=subprocess.run, scopes=None, root=Path('/')):
    scopes = discover_scopes(root, runner, config.get('remote_tailscale', False)) if scopes is None else scopes
    check = runner(['nft', 'list', 'table', 'inet', 'printer_pal'], check=False, capture_output=True, text=True)
    # One nft batch replaces only this application's table atomically.
    runner(['nft', '-f', '-'], input=ruleset(config['port'], scopes, check.returncode == 0), text=True, check=True)
    file = Path(root) / str(SCOPES_FILE).lstrip('/')
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = file.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, 'w') as output:
        json.dump(scopes, output)
    os.replace(temporary, file)
    os.chmod(file, 0o644)
    return scopes


def refresh_ufw(port, scopes, root=Path('/'), runner=subprocess.run):
    if not shutil.which('ufw'):
        return
    try:
        result = runner(['ufw', 'status'], check=True, capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'})
    except subprocess.CalledProcessError as error:
        # Preserve UFW's diagnostic in journald instead of hiding it in a traceback.
        detail = (error.stderr or error.stdout or 'No diagnostic returned').strip()
        raise RuntimeError(f'UFW status failed: {detail}') from error
    if 'Status: active' not in result.stdout:
        return
    state = Path(root) / 'run/print-monitor-network/ufw-policy.json'
    desired = {'port': port, 'scopes': [list(scope) for scope in scopes], 'discovery_ports': [1990, 2021]}
    try:
        if json.loads(state.read_text()) == desired:
            return
    except (OSError, ValueError):
        pass
    ipv6 = 'IPV6=yes' in (Path(root) / 'etc/default/ufw').read_text()
    for interface, subnet in scopes:
        if ':' in subnet and not ipv6:
            continue
        runner(['ufw', 'allow', 'in', 'on', interface, 'from', subnet, 'to', 'any', 'port', str(port),
                'proto', 'tcp', 'comment', 'Printer Pal'], check=True)
        if ':' not in subnet and interface != TAILSCALE_INTERFACE:
            for discovery_port in (1990, 2021):
                runner(['ufw', 'allow', 'in', 'on', interface, 'from', subnet, 'to', 'any',
                        'port', str(discovery_port), 'proto', 'udp', 'comment', 'Printer Pal discovery'], check=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(desired, output)


class LANPolicy:
    def __init__(self, file=SCOPES_FILE):
        self.file = Path(file)
        self.updated = None
        self.networks = []

    def discovery_interfaces(self):
        # Root-owned guard state contains only physically attached LANs.
        try:
            return sorted({scope[0] for scope in json.loads(self.file.read_text())
                           if ipaddress.ip_network(scope[1]).version == 4 and scope[0] != TAILSCALE_INTERFACE})
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return []

    def allows(self, address):
        try:
            peer = ipaddress.ip_address(address.split('%', 1)[0])
            if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
                peer = peer.ipv4_mapped
            if peer.is_loopback:
                return True
            if self.updated is None or time.monotonic() - self.updated > 5:
                # Missing or malformed root-owned state fails closed.
                self.networks = [ipaddress.ip_network(scope[1]) for scope in json.loads(self.file.read_text())]
                self.updated = time.monotonic()
            return any(peer.version == network.version and peer in network for network in self.networks)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/print-monitor/config.json'))
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('The network guard requires root.')
    config = load_config(args.config)
    if args.prepare and config['tls']:
        from transport import ensure_identity
        ensure_identity()
    scopes = apply_guard(config)
    refresh_ufw(config['port'], scopes)


if __name__ == '__main__':
    main()
