import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bambu_monitor import PrinterMonitor, PrinterTLSContext, ReportState, validated_settings, parse_announcement, make_client
from network_guard import LANPolicy
from agent import CameraServer, FrameBuffer, PairingState
from transport import ensure_identity, server_context, local_client_context


class BambuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scopes = self.root / 'scopes.json'
        scopes.write_text(json.dumps([['eth0', '192.168.1.0/24']]))
        self.policy = LANPolicy(scopes)
        self.settings = {'host': '192.168.1.20', 'serial': 'TESTSERIAL123', 'access_code': 'aB123456'}

    def test_only_attached_private_literal_addresses_are_accepted(self):
        self.assertEqual(validated_settings(self.settings, self.policy), self.settings)
        for host in ['127.0.0.1', '8.8.8.8', '169.254.169.254', '192.168.2.20', 'printer.local', 'https://192.168.1.20']:
            with self.assertRaises(ValueError):
                validated_settings({**self.settings, 'host': host}, self.policy)
        for changes in [{'serial': 'bad/#topic'}, {'access_code': 'short'}, {'port': 443}]:
            with self.assertRaises(ValueError):
                validated_settings({**self.settings, **changes}, self.policy)

    def test_access_code_is_private_persists_and_is_never_returned(self):
        file = self.root / 'printer.json'
        monitor = PrinterMonitor(file, self.policy, threading.Event())
        monitor.configure(self.settings)
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(file.read_text()), self.settings)
        self.assertNotIn(self.settings['access_code'], json.dumps(monitor.snapshot()))
        restarted = PrinterMonitor(file, self.policy, threading.Event())
        self.assertTrue(restarted.snapshot()['configured'])
        restarted.forget()
        self.assertFalse(file.exists())
        self.assertFalse(restarted.snapshot()['configured'])

    def test_partial_reports_merge_without_turning_unknown_fields_into_zero(self):
        state = ReportState()
        state.update(json.dumps({'print': {'gcode_state': 'RUNNING', 'mc_percent': 25, 'layer_num': 10}}), 100)
        state.update(json.dumps({'print': {'nozzle_temper': 210}}), 101)
        result = state.snapshot(102, True)
        self.assertTrue(result['fresh'])
        self.assertEqual(result['fields']['progress_percent'], 25)
        self.assertEqual(result['fields']['nozzle_temp'], 210)
        self.assertNotIn('remaining_minutes', result['fields'])

    def test_each_metric_expires_independently_and_disconnect_hides_data(self):
        state = ReportState()
        state.update(json.dumps({'print': {'mc_percent': 25}}), 100)
        state.update(json.dumps({'print': {'nozzle_temper': 210}}), 189)
        result = state.snapshot(191, True)
        self.assertTrue(result['fresh'])
        self.assertIsNone(result['fields']['progress_percent'])
        self.assertEqual(result['fields']['nozzle_temp'], 210)
        self.assertFalse(state.snapshot(191, False)['fresh'])
        self.assertIsNone(state.snapshot(191, False)['fields']['nozzle_temp'])
        self.assertFalse(state.snapshot(300, True)['fresh'])

    def test_new_job_does_not_inherit_previous_completion_or_time(self):
        state = ReportState()
        state.update(json.dumps({'print': {'subtask_id': 'first', 'gcode_state': 'FINISH', 'mc_percent': 100, 'mc_remaining_time': 0}}), 100)
        state.update(json.dumps({'print': {'subtask_id': 'second', 'gcode_state': 'RUNNING'}}), 101)
        result = state.snapshot(102, True)['fields']
        self.assertNotIn('progress_percent', result)
        self.assertNotIn('remaining_minutes', result)
        state.update(json.dumps({'print': {'gcode_state': 'FINISH', 'mc_percent': 100}}), 103)
        state.update(json.dumps({'print': {'gcode_state': 'PREPARE'}}), 104)
        self.assertNotIn('progress_percent', state.snapshot(104, True)['fields'])

    def test_invalid_and_oversized_reports_are_not_forwarded(self):
        state = ReportState()
        for payload in ['{}', '[]', '<html>', 'x' * (256 * 1024 + 1), json.dumps({'print': {'command': 'gcode_line', 'mc_percent': 20}})]:
            state.update(payload, 100)
        self.assertFalse(state.snapshot(100, True)['fresh'])
        state.update(json.dumps({'print': {'mc_percent': 999, 'nozzle_temper': float('nan'), 'mc_remaining_time': True}}), 101)
        self.assertFalse(state.snapshot(101, True)['fresh'])

    def test_ams_tray_deltas_merge_and_do_not_refresh_other_materials(self):
        state = ReportState()
        state.update(json.dumps({'print': {'ams': {'tray_now': '1', 'ams': [{'id': '0', 'tray': [
            {'id': '0', 'tray_type': 'PLA', 'tray_color': 'FF0000FF'}, {'id': '1', 'tray_type': 'PETG'}]}]}}}), 100)
        state.update(json.dumps({'print': {'ams': {'ams': [{'id': '0', 'tray': [{'id': '1', 'tray_color': '00FF00FF'}]}]}}}), 189)
        fields = state.snapshot(191, True)['fields']
        self.assertEqual(fields['ams_slots'][1]['color'], '00FF00FF')
        self.assertIsNone(fields['ams_slots'][1]['material'])
        self.assertIsNone(fields['ams_slots'][0]['color'])
        self.assertIsNone(fields['active_tray'])

    def test_discovery_does_not_trust_external_or_mismatched_locations(self):
        payload = b'NOTIFY * HTTP/1.1\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nLocation: 192.168.1.20\r\nUSN: TESTSERIAL123\r\nDevConnect.bambu.com: cloud\r\n\r\n'
        result = parse_announcement(payload, '192.168.1.20', self.policy)
        self.assertEqual(result['mode'], 'cloud')
        self.assertEqual(result['serial'], 'TESTSERIAL123')
        self.assertIsNone(parse_announcement(payload, '8.8.8.8', self.policy))
        self.assertIsNone(parse_announcement(payload, '192.168.1.21', self.policy))

    def test_discovery_uses_both_ports_joins_physical_lan_and_closes_sockets(self):
        monitor = PrinterMonitor(self.root / 'printer.json', self.policy, threading.Event())
        first, second = Mock(), Mock()
        first.recvfrom.return_value = (b'NOTIFY * HTTP/1.1\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nLocation: 192.168.1.20\r\nUSN: TESTSERIAL123\r\n\r\n', ('192.168.1.20', 1990))
        second.recvfrom.return_value = first.recvfrom.return_value
        def receive(*args):
            monitor.stop.set()
            return [first, second], [], []
        with patch('bambu_monitor.socket.socket', side_effect=[first, second]), \
             patch('bambu_monitor.socket.if_nametoindex', return_value=2), \
             patch('bambu_monitor.select.select', side_effect=receive):
            found = monitor.discover()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['serial'], 'TESTSERIAL123')
        first.bind.assert_called_once_with(('', 1990))
        second.bind.assert_called_once_with(('', 2021))
        for listener in (first, second):
            self.assertTrue(any(call.args[:2] == (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP)
                                for call in listener.setsockopt.call_args_list))
            listener.close.assert_called_once()
        self.assertFalse(monitor.scan_lock.locked())

    def test_discovery_reports_busy_ports_and_releases_scan_lock(self):
        monitor = PrinterMonitor(self.root / 'printer.json', self.policy, threading.Event())
        first, second = Mock(), Mock()
        first.bind.side_effect = OSError('busy')
        second.bind.side_effect = OSError('busy')
        with patch('bambu_monitor.socket.socket', side_effect=[first, second]):
            with self.assertRaisesRegex(ValueError, 'ports are busy'):
                monitor.discover()
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertFalse(monitor.scan_lock.locked())

    def test_setup_requires_authentication_and_https_and_supports_revocation(self):
        tls = self.root / 'tls'
        ensure_identity(tls)
        state = PairingState(self.root / 'phone.json')
        token = state.pair(state.open_pairing())[1]['token']
        monitor = PrinterMonitor(self.root / 'printer.json', self.policy, threading.Event())
        server = CameraServer(('127.0.0.1', 0), state, FrameBuffer(), tls_context=server_context(tls))
        server.printer = monitor
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(method, path, body=None, authenticated=True):
            conn = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=local_client_context(tls), timeout=3)
            try:
                headers = {'Authorization': 'Bearer ' + token} if authenticated else {}
                conn.request(method, path, json.dumps(body) if body else None, headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        try:
            self.assertEqual(request('POST', '/api/printer', self.settings, False)[0], 401)
            self.assertEqual(request('GET', '/api/printer', authenticated=False)[0], 401)
            self.assertFalse(monitor.path.exists())
            code, result = request('POST', '/api/printer', self.settings)
            self.assertEqual(code, 200)
            self.assertNotIn('access_code', json.dumps(result))
            self.assertEqual(request('GET', '/api/printer')[1]['connection'], 'connecting')
            self.assertEqual(request('DELETE', '/api/printer')[0], 200)
            self.assertFalse(monitor.path.exists())
            self.assertEqual(request('POST', '/api/printer', {**self.settings, 'host': '8.8.8.8'})[0], 400)
            connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=local_client_context(tls), timeout=3)
            connection.connect()
            server.tls_context = None # Already-handshaken fixture connection; handler must fail closed.
            try:
                connection.request('POST', '/api/printer', json.dumps(self.settings), {'Authorization': 'Bearer ' + token})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=3)


def fixture_identity(directory, serial):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .sign(key, hashes.SHA256()))
    certificate, private = directory / 'cert.pem', directory / 'key.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return certificate, private


class MQTTTransportTests(unittest.TestCase):
    def test_real_paho_connection_verifies_serial_and_sends_only_status_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = fixture_identity(Path(directory), 'TESTSERIAL123')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
            listener.settimeout(5)
            stop = threading.Event()
            packets = []
            failures = []
            def packet(connection):
                kind = connection.recv(1)
                if not kind:
                    raise EOFError()
                size, multiplier = 0, 1
                while True:
                    digit = connection.recv(1)[0]
                    size += (digit & 127) * multiplier
                    if not digit & 128:
                        break
                    multiplier *= 128
                body = b''
                while len(body) < size:
                    part = connection.recv(size - len(body))
                    if not part:
                        raise EOFError()
                    body += part
                return kind[0] >> 4, body
            def serve():
                try:
                    raw, _ = listener.accept()
                    with context.wrap_socket(raw, server_side=True) as connection:
                        connection.settimeout(5)
                        packets.append(packet(connection))
                        connection.sendall(b'\x20\x02\x00\x00')
                        subscribed = packet(connection)
                        packets.append(subscribed)
                        connection.sendall(b'\x90\x03' + subscribed[1][:2] + b'\x00')
                        packets.append(packet(connection))
                        stop.wait(3)
                except Exception as error:
                    failures.append(type(error).__name__)
            thread = threading.Thread(target=serve, daemon=True); thread.start()
            client = make_client({'serial': 'TESTSERIAL123', 'access_code': 'aB123456'}, cert)
            def connected(current, _userdata, _flags, reason, _properties=None):
                current.subscribe('device/TESTSERIAL123/report')
                current.publish('device/TESTSERIAL123/request', '{"pushing":{"sequence_id":"0","command":"pushall"}}')
            client.on_connect = connected
            try:
                client.connect('127.0.0.1', listener.getsockname()[1], keepalive=5)
                deadline = time.monotonic() + 4
                while len(packets) < 3 and time.monotonic() < deadline:
                    client.loop(timeout=0.1)
                self.assertFalse(failures)
                self.assertEqual([item[0] for item in packets], [1, 8, 3])
                self.assertIn(b'aB123456', packets[0][1])
                self.assertIn(b'/report', packets[1][1])
                self.assertIn(b'pushall', packets[2][1])
            finally:
                stop.set(); client.disconnect(); listener.close(); thread.join(timeout=5)

    def test_wrong_serial_is_rejected_before_mqtt_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = fixture_identity(Path(directory), 'WRONGSERIAL123')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
            listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(5)
            received = []
            def serve():
                raw, _ = listener.accept()
                try:
                    with context.wrap_socket(raw, server_side=True) as connection:
                        received.append(connection.recv(1024))
                except ssl.SSLError:
                    raw.close()
            thread = threading.Thread(target=serve, daemon=True); thread.start()
            try:
                with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        PrinterTLSContext('EXPECTED12345', cert).wrap_socket(raw, server_hostname='127.0.0.1')
                self.assertEqual(received, [])
            finally:
                listener.close(); thread.join(timeout=5)
