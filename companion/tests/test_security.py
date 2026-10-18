import hashlib
import http.client
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import CameraServer, FrameBuffer, PairingState, BoundedRequestInput
from network_guard import LANPolicy, ruleset, apply_guard, refresh_ufw
from transport import ensure_identity, fingerprint, server_context, local_client_context
from control import enrollment_url


class SecurityTests(unittest.TestCase):
    def test_ufw_status_failure_preserves_its_diagnostic(self):
        runner = Mock(side_effect=subprocess.CalledProcessError(1, ['ufw', 'status'], stderr='iptables: permission denied'))
        with patch('network_guard.shutil.which', return_value='/usr/sbin/ufw'):
            with self.assertRaisesRegex(RuntimeError, 'UFW status failed: iptables: permission denied'):
                refresh_ufw(32187, [], runner=runner)

    def test_ufw_upgrade_adds_only_lan_scoped_discovery_and_caches_new_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'etc/default').mkdir(parents=True)
            (root / 'etc/default/ufw').write_text('IPV6=yes\n')
            state = root / 'run/print-monitor-network/ufw-policy.json'
            state.parent.mkdir(parents=True)
            scopes = [('eth0', '192.168.1.0/24'), ('eth0', 'fd00::/64')]
            state.write_text(json.dumps({'port': 32187, 'scopes': [list(s) for s in scopes]}))
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout='Status: active'))
            with patch('network_guard.shutil.which', return_value='/usr/sbin/ufw'):
                refresh_ufw(32187, scopes, root=root, runner=runner)
                additions = [call.args[0] for call in runner.call_args_list if 'allow' in call.args[0]]
                udp = [command for command in additions if 'udp' in command]
                self.assertEqual(len(udp), 2)
                for command, port in zip(udp, ['1990', '2021']):
                    self.assertEqual(command, ['ufw', 'allow', 'in', 'on', 'eth0', 'from', '192.168.1.0/24',
                                               'to', 'any', 'port', port, 'proto', 'udp', 'comment', 'Printer Pal discovery'])
                runner.reset_mock()
                refresh_ufw(32187, scopes, root=root, runner=runner)
                self.assertEqual(runner.call_count, 1)

    def test_lan_policy_rejects_external_peers_and_missing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'scopes.json'
            policy = LANPolicy(file)
            self.assertFalse(policy.allows('192.168.1.10'))
            self.assertTrue(policy.allows('::ffff:127.0.0.1'))
            file.write_text(json.dumps([['eth0', '192.168.1.0/24'], ['eth0', '2600:1700:d840:a3d0::/64']]))
            self.assertTrue(policy.allows('192.168.1.145'))
            self.assertTrue(policy.allows('::ffff:192.168.1.145'))
            self.assertTrue(policy.allows('2600:1700:d840:a3d0::20'))
            self.assertFalse(policy.allows('8.8.8.8'))
            self.assertFalse(policy.allows('192.168.2.10'))
            self.assertFalse(policy.allows('2600:1700:d840:a3d1::20'))
            file.write_text('broken')
            policy.updated = None
            self.assertFalse(policy.allows('192.168.1.145'))

    def test_guard_replaces_only_owned_table_and_blocks_unknown_peers(self):
        text = ruleset(32187, [('eth0', '192.168.1.0/24')], existing=True)
        self.assertIn('delete table inet printer_pal', text)
        self.assertNotIn('flush ruleset', text)
        self.assertNotIn('dport 22', text)
        self.assertIn('iifname "eth0" ip saddr 192.168.1.0/24 tcp dport 32187 accept', text)
        self.assertIn('tcp dport 32187 drop', text)

    def test_guard_failure_does_not_publish_new_allow_list(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Mock(side_effect=[subprocess.CompletedProcess([], 1), subprocess.CalledProcessError(1, 'nft')])
            with self.assertRaises(subprocess.CalledProcessError):
                apply_guard({'port': 32187}, runner=runner, scopes=[('eth0', '192.168.1.0/24')], root=Path(directory))
            self.assertFalse((Path(directory) / 'run/print-monitor-network/scopes.json').exists())

    def test_token_revocation_persists_and_closes_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PairingState(Path(directory) / 'state.json')
            code = state.open_pairing()
            _, result = state.pair(code)
            header = 'Bearer ' + result['token']
            self.assertTrue(state.authorized(header))
            state.open_pairing()
            state.forget_phones()
            self.assertFalse(state.authorized(header))
            self.assertFalse(state.is_open())
            self.assertFalse(PairingState(state.path).authorized(header))

    def test_setup_link_contains_pin_and_code_only_in_fragment(self):
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(enrollment_url({'port': 32187}, '123456', 'a' * 64))
        self.assertEqual(parts.scheme, 'https')
        self.assertEqual(parts.query, '')
        self.assertEqual(parse_qs(parts.fragment), {'pin': ['a' * 64], 'code': ['123456']})

    def test_http_headers_are_bounded_before_body_parsing(self):
        data = b'GET /api/info HTTP/1.0\r\n' + b'X-Pad: ' + b'a' * 20000 + b'\r\n'
        reader = BoundedRequestInput(io.BytesIO(data))
        reader.readline()
        with self.assertRaises(ValueError):
            reader.readline()


class TLSTests(unittest.TestCase):
    def test_device_identity_is_unique_stable_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            first, key = ensure_identity(Path(directory) / 'one')
            pin = fingerprint(first)
            self.assertEqual(len(pin), 64)
            ensure_identity(Path(directory) / 'one')
            self.assertEqual(fingerprint(first), pin)
            other, _ = ensure_identity(Path(directory) / 'two')
            self.assertNotEqual(fingerprint(other), pin)
            self.assertEqual(key.stat().st_mode & 0o777, 0o640)
            self.assertEqual(key.parent.stat().st_mode & 0o777, 0o750)

    def test_https_rejects_untrusted_certificate_and_unauthorized_video(self):
        with tempfile.TemporaryDirectory() as directory:
            tls = Path(directory) / 'tls'
            ensure_identity(tls)
            state = PairingState(Path(directory) / 'state.json')
            frames = FrameBuffer()
            frames.write(b'\xff\xd8private-camera-frame\xff\xd9')
            server = CameraServer(('127.0.0.1', 0), state, frames, tls_context=server_context(tls))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                untrusted = http.client.HTTPSConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        untrusted.request('GET', '/api/info')
                finally:
                    untrusted.close()
                connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=local_client_context(tls), timeout=3)
                try:
                    connection.request('GET', '/snapshot')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    self.assertNotIn(b'private-camera-frame', response.read())
                finally:
                    connection.close()
                connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=local_client_context(tls), timeout=3)
                try:
                    code = state.open_pairing()
                    connection.request('POST', '/api/pair', json.dumps({'code': code}), {'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    token = json.loads(response.read())['token']
                finally:
                    connection.close()
                connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=local_client_context(tls), timeout=3)
                try:
                    connection.request('GET', '/snapshot', headers={'Authorization': 'Bearer ' + token})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'\xff\xd8private-camera-frame\xff\xd9')
                finally:
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
