import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DEFAULT_CONFIG, validate_config
from network_guard import TAILSCALE_SCOPES, LANPolicy, discover_scopes, refresh_ufw, ruleset
import control


def addresses(*entries):
    return json.dumps([{'ifname': name, 'addr_info': [{'local': local, 'prefixlen': prefix}]} for name, local, prefix in entries])


class TailscaleAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'sys/class/net/eth0/device').mkdir(parents=True)
        (self.root / 'sys/class/net/tailscale0').mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def scopes(self, tailscale, interfaces):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=interfaces))
        return discover_scopes(self.root, runner, tailscale=tailscale)

    def test_tailscale_is_allowed_only_when_turned_on_and_connected(self):
        both = addresses(('eth0', '192.168.1.5', 24), ('tailscale0', '100.101.102.103', 32))
        self.assertEqual(self.scopes(False, both), [('eth0', '192.168.1.0/24')])
        self.assertEqual(self.scopes(True, both), sorted([('eth0', '192.168.1.0/24')] + TAILSCALE_SCOPES))
        self.assertEqual(self.scopes(True, addresses(('eth0', '192.168.1.5', 24))), [('eth0', '192.168.1.0/24')])
        rules = ruleset(32187, self.scopes(True, both))
        self.assertIn('iifname "tailscale0" ip saddr 100.64.0.0/10 tcp dport 32187 accept', rules)
        self.assertTrue(rules.rstrip().endswith('tcp dport 32187 drop\n }\n}'.strip()))

    def test_companion_accepts_tailnet_peers_but_never_searches_for_printers_there(self):
        file = self.root / 'scopes.json'
        file.write_text(json.dumps([['eth0', '192.168.1.0/24']] + [list(scope) for scope in TAILSCALE_SCOPES]))
        policy = LANPolicy(file)
        self.assertTrue(policy.allows('100.101.102.103'))
        self.assertTrue(policy.allows('fd7a:115c:a1e0::1'))
        self.assertFalse(policy.allows('8.8.8.8'))
        self.assertEqual(policy.discovery_interfaces(), ['eth0'])

    def test_ufw_opens_only_the_camera_port_to_the_tailnet(self):
        (self.root / 'etc/default').mkdir(parents=True)
        (self.root / 'etc/default/ufw').write_text('IPV6=no\n')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout='Status: active'))
        with patch('network_guard.shutil.which', return_value='/usr/sbin/ufw'):
            refresh_ufw(32187, TAILSCALE_SCOPES, root=self.root, runner=runner)
        allowed = [call.args[0] for call in runner.call_args_list if 'allow' in call.args[0]]
        self.assertEqual(allowed, [['ufw', 'allow', 'in', 'on', 'tailscale0', 'from', '100.64.0.0/10', 'to', 'any',
                                    'port', '32187', 'proto', 'tcp', 'comment', 'Printer Pal']])

    def test_setting_must_be_a_boolean(self):
        self.assertFalse(validate_config({})['remote_tailscale'])
        with self.assertRaises(ValueError):
            validate_config({'remote_tailscale': 'yes'})

    def test_remote_command_saves_the_setting_and_reapplies_the_firewall(self):
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(DEFAULT_CONFIG))
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout='100.101.102.103\n'))
        group = Mock(gr_gid=1234)
        with patch('control.os.chown') as chown, patch('grp.getgrnam', return_value=group), \
                patch('control.shutil.which', return_value='/usr/bin/tailscale'), patch('builtins.print') as output:
            self.assertEqual(control.set_tailscale(True, config_path, runner), 0)
        self.assertTrue(json.loads(config_path.read_text())['remote_tailscale'])
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(chown.call_args.args[1:], (0, 1234))
        self.assertIn(['systemctl', 'start', 'print-monitor-network.service'], [call.args[0] for call in runner.call_args_list])
        self.assertIn('100.101.102.103', ' '.join(str(call.args[0]) for call in output.call_args_list))
        with patch('control.os.chown'), patch('grp.getgrnam', return_value=group), patch('builtins.print'):
            control.set_tailscale(False, config_path, runner)
        self.assertFalse(json.loads(config_path.read_text())['remote_tailscale'])


if __name__ == '__main__':
    unittest.main()
