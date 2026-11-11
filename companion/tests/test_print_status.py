import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import print_status
from print_status import (FIELDS, NotReady, PrinterMonitor, discover_local, moonraker_fields, octoprint_fields,
                          validated_printer)
from network_guard import LANPolicy
from agent import CameraServer, FrameBuffer, PairingState
from timelapse import TimelapseRecorder, printer_status, storage_paths
from transport import ensure_identity, server_context, local_client_context

KEY = 'Abc123_-Abc123_-Abc123_-Abc123_-'
BAMBU = {'host': '192.168.1.20', 'serial': 'TESTSERIAL123', 'access_code': 'aB123456'}


class FakeHTTP:
    """A local web server answering fixed routes, recording each request's path and headers."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers)))
                status, body, headers = outer.routes.get(self.path, (404, {'error': 'Not found'}, {}))
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_port
        self.url = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def route(self, path, body, status=200, headers=None):
        self.routes[path] = (status, body, headers or {})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def closed_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def moonraker(state='standby', duration=0.0, progress=0.0, layer=None, total=None, filename='', klippy='ready',
              display=None):
    return {'result': {'eventtime': 1234.5, 'status': {
        'webhooks': {'state': klippy, 'state_message': 'Printer is ready'},
        'print_stats': {'state': state, 'print_duration': duration, 'total_duration': duration + 30,
                        'filename': filename, 'filament_used': 0.0, 'message': '',
                        'info': {'current_layer': layer, 'total_layer': total}},
        'virtual_sdcard': {'progress': progress, 'is_active': state == 'printing', 'file_position': 0},
        'display_status': {'progress': progress if display is None else display, 'message': None},
        'extruder': {'temperature': 214.96, 'target': 215.0},
        'heater_bed': {'temperature': 60.04, 'target': 60.0}}}}


def octoprint_printer(text='Operational', **flags):
    values = {'cancelling': False, 'closedOrError': False, 'error': False, 'finishing': False, 'operational': True,
              'paused': False, 'pausing': False, 'printing': False, 'ready': True, 'resuming': False,
              'sdReady': False, **flags}
    return {'state': {'text': text, 'flags': values},
            'temperature': {'tool0': {'actual': 199.84, 'target': 200.0, 'offset': 0},
                            'bed': {'actual': 55.01, 'target': 55.0, 'offset': 0}}}


def octoprint_job(name='models/benchy.gcode', completion=None, left=None):
    return {'job': {'file': {'name': name.rsplit('/', 1)[-1], 'display': name.rsplit('/', 1)[-1], 'path': name,
                             'origin': 'local'}, 'estimatedPrintTime': 3600},
            'progress': {'completion': completion, 'filepos': 0, 'printTime': 100, 'printTimeLeft': left,
                         'printTimeLeftOrigin': 'estimate'},
            'state': 'Operational'}


class PrintStatusCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scopes = self.root / 'scopes.json'
        self.scopes.write_text(json.dumps([['eth0', '192.168.1.0/24']]))
        self.policy = LANPolicy(self.scopes)
        self.fake = FakeHTTP()
        self.addCleanup(self.fake.close)

    def monitor(self, **options):
        return PrinterMonitor(self.root / 'printer.json', self.policy, threading.Event(),
                              local_discovery=lambda: [], **options)


class ValidationTests(PrintStatusCase):
    def test_accepts_loopback_and_attached_private_addresses(self):
        cases = {'http://127.0.0.1:7125': 'http://127.0.0.1:7125',
                 'http://localhost:7125/': 'http://127.0.0.1:7125',
                 '  HTTP://127.0.0.1:7125  ': 'http://127.0.0.1:7125',
                 'http://[::1]:7125': 'http://[::1]:7125',
                 'https://192.168.1.30': 'https://192.168.1.30',
                 'http://192.168.1.30:80': 'http://192.168.1.30',
                 'https://192.168.1.30:7130/': 'https://192.168.1.30:7130'}
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(validated_printer({'kind': 'klipper', 'url': url}, self.policy),
                                 {'kind': 'klipper', 'url': expected})

    def test_rejects_public_unattached_and_malformed_addresses(self):
        cases = {'http://8.8.8.8:7125': 'attached private local network',
                 'http://192.168.2.5:7125': 'attached private local network',
                 'http://169.254.169.254': 'attached private local network',
                 'http://[2001:db8::1]:7125': 'attached private local network',
                 'http://user:secret@127.0.0.1:7125': 'user name and password',
                 'http://user@127.0.0.1:7125': 'user name and password',
                 'http://127.0.0.1:7125/printer': 'only the address and port',
                 'http://127.0.0.1:7125?token=1': 'only the address and port',
                 'http://127.0.0.1:7125/?': 'only the address and port',
                 'http://127.0.0.1:7125#x': 'only the address and port',
                 'ftp://127.0.0.1': 'http:// or https://',
                 '127.0.0.1:7125': 'http:// or https://',
                 'http://mainsailos.local:7125': 'IP address, or localhost',
                 'http://127.0.0.1:99999': 'port number',
                 'http://127.0.0.1:0': 'port number',
                 'http://127.0.0.1\n:7125': 'Check the address',
                 'http://' + '1' * 300: 'Check the address',
                 'http://': 'Enter the Moonraker address',
                 '': 'Enter the Moonraker address'}
        for url, message in cases.items():
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, message):
                    validated_printer({'kind': 'klipper', 'url': url}, self.policy)
        for url in (None, 7125, ['http://127.0.0.1']):
            with self.assertRaises(ValueError):
                validated_printer({'kind': 'klipper', 'url': url}, self.policy)
        with self.assertRaises(ValueError):
            validated_printer({'kind': 'klipper'}, self.policy)

    def test_api_keys(self):
        url = 'http://127.0.0.1:5000'
        self.assertEqual(validated_printer({'kind': 'octoprint', 'url': url, 'api_key': f' {KEY} '}, self.policy),
                         {'kind': 'octoprint', 'url': url, 'api_key': KEY})
        for key in (None, '', '   '):
            with self.assertRaisesRegex(ValueError, 'Enter the OctoPrint API key'):
                validated_printer({'kind': 'octoprint', 'url': url, 'api_key': key}, self.policy)
        with self.assertRaisesRegex(ValueError, 'Enter the OctoPrint API key'):
            validated_printer({'kind': 'octoprint', 'url': url}, self.policy)
        for key in ('short', 'x' * 15, 'x' * 129, KEY[:-1] + '!', 'Abc123_-Abc 123_-Abc123', 'é' * 20, 12345678901234567, ['a' * 20]):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, '16 to 128'):
                    validated_printer({'kind': 'octoprint', 'url': url, 'api_key': key}, self.policy)
        # Moonraker usually trusts local clients; a key is optional but still checked.
        self.assertEqual(validated_printer({'kind': 'klipper', 'url': url, 'api_key': ''}, self.policy),
                         {'kind': 'klipper', 'url': url})
        self.assertEqual(validated_printer({'kind': 'klipper', 'url': url, 'api_key': 'x' * 128}, self.policy)['api_key'],
                         'x' * 128)
        with self.assertRaisesRegex(ValueError, '16 to 128'):
            validated_printer({'kind': 'klipper', 'url': url, 'api_key': 'bad key'}, self.policy)

    def test_kinds_and_unexpected_fields(self):
        with self.assertRaisesRegex(ValueError, 'bambu, klipper, or octoprint'):
            validated_printer({'kind': 'prusa', 'url': 'http://127.0.0.1'}, self.policy)
        with self.assertRaisesRegex(ValueError, 'bambu, klipper, or octoprint'):
            validated_printer({'kind': None, 'url': 'http://127.0.0.1'}, self.policy)
        with self.assertRaisesRegex(ValueError, 'Send only'):
            validated_printer({'kind': 'klipper', 'url': 'http://127.0.0.1:7125', 'host': '127.0.0.1'}, self.policy)
        # No kind means Bambu, with its original checks and messages; kind "bambu" is the same.
        self.assertEqual(validated_printer(BAMBU, self.policy), BAMBU)
        self.assertEqual(validated_printer({'kind': 'bambu', **BAMBU}, self.policy), BAMBU)
        with self.assertRaisesRegex(ValueError, 'eight-character LAN access code'):
            validated_printer({**BAMBU, 'access_code': 'short'}, self.policy)
        with self.assertRaisesRegex(ValueError, 'serial number, and access code are required'):
            validated_printer({'url': 'http://127.0.0.1:7125'}, self.policy)


class MappingTests(unittest.TestCase):
    def test_moonraker_states(self):
        cases = [(moonraker('standby'), 'IDLE'), (moonraker('printing', duration=12), 'RUNNING'),
                 (moonraker('printing', duration=0), 'PREPARE'), (moonraker('paused', duration=12), 'PAUSE'),
                 (moonraker('complete', duration=12), 'FINISH'), (moonraker('cancelled'), 'FAILED'),
                 (moonraker('error'), 'FAILED'), (moonraker('printing', duration=12, klippy='shutdown'), 'FAILED'),
                 (moonraker('standby', klippy='error'), 'FAILED'), (moonraker('something-new'), None)]
        for data, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(moonraker_fields(data)['state'], expected)
        with self.assertRaises(NotReady):
            moonraker_fields(moonraker(klippy='startup'))
        for data in ({}, {'result': {}}, {'result': {'status': []}}, {'error': {'message': 'nope'}}):
            with self.assertRaises(print_status.StatusError):
                moonraker_fields(data)

    def test_moonraker_layers_progress_and_time_left(self):
        fields = moonraker_fields(moonraker('printing', duration=600, progress=0.25, layer=12, total=80,
                                            filename='parts/Benchy.gcode'))
        self.assertEqual(fields, {'state': 'RUNNING', 'progress_percent': 25, 'remaining_minutes': 30,
                                  'current_layer': 12, 'total_layers': 80, 'nozzle_temp': 215.0, 'bed_temp': 60.0,
                                  'job_name': 'Benchy', 'active_tray': None, 'ams_slots': None})
        self.assertEqual(set(fields), set(FIELDS))
        # Slicers that don't send SET_PRINT_STATS_INFO leave layers unknown.
        fields = moonraker_fields(moonraker('printing', duration=600, progress=0.5))
        self.assertEqual((fields['current_layer'], fields['total_layers'], fields['remaining_minutes']), (None, None, 10))
        # M73 progress in display_status wins over the file position.
        self.assertEqual(moonraker_fields(moonraker('printing', duration=60, progress=0.2, display=0.4))['progress_percent'], 40)
        data = moonraker('printing', duration=60, progress=0.333)
        del data['result']['status']['display_status']
        self.assertEqual(moonraker_fields(data)['progress_percent'], 33)
        # No estimate without progress; a finished print has none left; idle printers have no job.
        self.assertIsNone(moonraker_fields(moonraker('printing', duration=60, progress=0))['remaining_minutes'])
        self.assertIsNone(moonraker_fields(moonraker('printing', duration=1e8, progress=5e-324))['remaining_minutes'])
        finished = moonraker_fields(moonraker('complete', duration=900, progress=0.99, layer=80, total=80, filename='a.gcode'))
        self.assertEqual((finished['progress_percent'], finished['remaining_minutes'], finished['current_layer']), (100, 0, 80))
        idle = moonraker_fields(moonraker('standby', progress=0.5, layer=3, filename='old.gcode'))
        self.assertEqual((idle['job_name'], idle['progress_percent'], idle['current_layer'], idle['nozzle_temp']),
                         (None, None, None, 215.0))
        bad = moonraker('printing', duration=float('inf'), progress=7, layer=-1, total=True)
        bad['result']['status']['extruder']['temperature'] = 'hot'
        fields = moonraker_fields(bad)
        self.assertEqual((fields['progress_percent'], fields['current_layer'], fields['total_layers'], fields['nozzle_temp'],
                          fields['remaining_minutes']), (None, None, None, None, None))

    def test_octoprint_states(self):
        cases = [(octoprint_printer('Printing', printing=True), octoprint_job(completion=10), 'RUNNING'),
                 (octoprint_printer('Starting', printing=True), octoprint_job(completion=0), 'PREPARE'),
                 (octoprint_printer('Paused', paused=True), octoprint_job(completion=10), 'PAUSE'),
                 (octoprint_printer('Pausing', pausing=True, printing=True), octoprint_job(completion=10), 'PAUSE'),
                 (octoprint_printer('Cancelling', cancelling=True), octoprint_job(completion=10), 'FAILED'),
                 (octoprint_printer('Error', error=True, operational=False), octoprint_job(), 'FAILED'),
                 (octoprint_printer('Operational'), octoprint_job(completion=100.0), 'FINISH'),
                 (octoprint_printer('Operational'), octoprint_job(completion=42), 'IDLE'),
                 (octoprint_printer('Operational'), octoprint_job(name='', completion=100.0), 'IDLE'),
                 (octoprint_printer('Offline', operational=False, ready=False), octoprint_job(), None)]
        for printer, job, expected in cases:
            with self.subTest(text=printer['state']['text'], expected=expected):
                self.assertEqual(octoprint_fields(job, printer)['state'], expected)
        with self.assertRaises(print_status.StatusError):
            octoprint_fields(octoprint_job(), {'state': 'Printing'})

    def test_octoprint_progress_time_left_and_no_layers(self):
        fields = octoprint_fields(octoprint_job(completion=42.7, left=125), octoprint_printer('Printing', printing=True))
        self.assertEqual(fields, {'state': 'RUNNING', 'progress_percent': 42, 'remaining_minutes': 3,
                                  'current_layer': None, 'total_layers': None, 'nozzle_temp': 199.8, 'bed_temp': 55.0,
                                  'job_name': 'benchy', 'active_tray': None, 'ams_slots': None})
        finished = octoprint_fields(octoprint_job(completion=100.0, left=0), octoprint_printer())
        self.assertEqual((finished['state'], finished['progress_percent'], finished['remaining_minutes']), ('FINISH', 100, 0))
        printer = octoprint_printer('Printing', printing=True)
        del printer['temperature']['bed']
        fields = octoprint_fields(octoprint_job(completion=None, left=None), printer)
        self.assertEqual((fields['progress_percent'], fields['remaining_minutes'], fields['bed_temp']), (None, None, None))


class KlipperMonitorTests(PrintStatusCase):
    def test_polls_moonraker_and_reports_a_printer_agnostic_snapshot(self):
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker('printing', duration=600, progress=0.25, layer=12,
                                                                 total=80, filename='Benchy.gcode'))
        monitor = self.monitor()
        notified = []
        monitor.listeners.append(lambda: notified.append(True))
        result = monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.assertEqual(result, None)
        self.assertEqual(monitor.snapshot()['connection'], 'connecting')
        self.assertFalse(monitor.snapshot()['fresh'])
        self.assertEqual(monitor.poll(), 'connected')
        self.assertEqual(notified, [True])
        snapshot = monitor.snapshot()
        self.assertEqual({key: snapshot[key] for key in ('configured', 'kind', 'connection', 'fresh', 'host', 'serial')},
                         {'configured': True, 'kind': 'klipper', 'connection': 'connected', 'fresh': True,
                          'host': '127.0.0.1', 'serial': None})
        self.assertEqual(snapshot['age_seconds'], 0)
        self.assertEqual(snapshot['fields']['state'], 'RUNNING')
        self.assertEqual((snapshot['fields']['current_layer'], snapshot['fields']['remaining_minutes']), (12, 30))
        self.assertEqual(self.fake.requests[-1][0], print_status.MOONRAKER_QUERY)
        self.assertNotIn('X-Api-Key', self.fake.requests[-1][1])
        self.assertEqual(json.loads((self.root / 'printer.json').read_text()), {'kind': 'klipper', 'url': self.fake.url})

    def test_optional_moonraker_key_is_sent_stored_privately_and_never_returned(self):
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker())
        monitor = self.monitor()
        monitor.configure({'kind': 'klipper', 'url': self.fake.url, 'api_key': KEY})
        file = self.root / 'printer.json'
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(file.read_text())['api_key'], KEY)
        with self.assertLogs(level='DEBUG') as logs:
            logging.getLogger().debug('poll')
            self.assertEqual(monitor.poll(), 'connected')
        self.assertEqual(self.fake.requests[-1][1].get('X-Api-Key'), KEY)
        self.assertNotIn(KEY, json.dumps(monitor.snapshot()))
        self.assertNotIn(KEY, '\n'.join(logs.output))
        self.fake.route(print_status.MOONRAKER_QUERY, {'error': {'code': 401, 'message': 'Unauthorized'}}, 401)
        self.assertEqual(monitor.poll(), 'authentication_failed')
        self.assertNotIn(KEY, json.dumps(monitor.snapshot()))

    def test_waiting_for_klipper_and_unexpected_answers(self):
        monitor = self.monitor()
        monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.fake.route(print_status.MOONRAKER_QUERY, {'error': {'code': 503, 'message': 'Klippy Host not connected'}}, 503)
        self.assertEqual(monitor.poll(), 'waiting')
        self.assertFalse(monitor.snapshot()['fresh'])
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker(klippy='startup'))
        self.assertEqual(monitor.poll(), 'waiting')
        for status, body, headers in [(200, b'<html>not moonraker</html>', {}), (404, b'', {}),
                                      (302, b'', {'Location': 'http://8.8.8.8/'}), (200, b'x' * (300 * 1024), {}),
                                      (200, b'[]', {})]:
            with self.subTest(status=status, body=body[:20]):
                self.fake.route(print_status.MOONRAKER_QUERY, body, status, headers)
                self.assertEqual(monitor.poll(), 'unreachable')
                self.assertFalse(monitor.snapshot()['fresh'])
        self.assertEqual(len(self.fake.requests), 7, 'redirects are not followed')

    def test_unreachable_host_hides_status_immediately(self):
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker('printing', duration=5))
        monitor = self.monitor()
        monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.assertEqual(monitor.poll(), 'connected')
        self.assertTrue(monitor.snapshot()['fresh'])
        monitor.opener = print_status.make_opener()
        with monitor.lock:
            monitor.settings = {'kind': 'klipper', 'url': f'http://127.0.0.1:{closed_port()}'}
        self.assertEqual(monitor.poll(), 'unreachable')
        snapshot = monitor.snapshot()
        self.assertEqual((snapshot['connection'], snapshot['fresh']), ('unreachable', False))
        self.assertEqual(snapshot['fields'], dict.fromkeys(FIELDS))

    def test_status_expires_after_ninety_seconds(self):
        now = [1000.0]
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker('printing', duration=5))
        monitor = self.monitor(clock=lambda: now[0])
        monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        monitor.poll()
        now[0] += 89
        self.assertEqual((monitor.snapshot()['fresh'], monitor.snapshot()['age_seconds']), (True, 89))
        now[0] += 1
        snapshot = monitor.snapshot()
        self.assertEqual((snapshot['fresh'], snapshot['age_seconds'], snapshot['fields']['state']), (False, 90, None))

    def test_address_that_leaves_the_attached_network_is_not_contacted(self):
        monitor = self.monitor()
        monitor.configure({'kind': 'klipper', 'url': 'http://192.168.1.30:7125'})
        self.scopes.write_text(json.dumps([['eth0', '10.0.0.0/24']]))
        self.policy.updated = None
        with patch.object(print_status, 'poll_moonraker') as request:
            self.assertEqual(monitor.poll(), 'network_unavailable')
        request.assert_not_called()

    def test_works_without_paho(self):
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker('printing', duration=5))
        monitor = self.monitor()
        monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        with patch.dict(sys.modules, {'paho': None, 'paho.mqtt': None, 'paho.mqtt.client': None}):
            self.assertEqual(monitor.poll(), 'connected')

    def test_background_thread_polls_and_wakes_listeners(self):
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker('printing', duration=5))
        monitor = self.monitor(poll_interval=0.05)
        woken = threading.Event()
        polls = []
        monitor.listeners.append(lambda: (polls.append(1), len(polls) >= 3 and woken.set()))
        monitor.start()
        try:
            monitor.configure({'kind': 'klipper', 'url': self.fake.url})
            self.assertTrue(woken.wait(5))
            self.assertEqual(monitor.snapshot()['connection'], 'connected')
            monitor.forget()
            self.assertEqual(monitor.snapshot()['connection'], 'not_configured')
        finally:
            monitor.stop.set()
            monitor.changed.set()
            monitor.thread.join(timeout=3)
        self.assertFalse(monitor.thread.is_alive())


class OctoPrintMonitorTests(PrintStatusCase):
    def configure(self, monitor, key=KEY):
        monitor.configure({'kind': 'octoprint', 'url': self.fake.url, 'api_key': key})

    def test_polls_job_and_printer_with_the_key(self):
        self.fake.route('/api/printer', octoprint_printer('Printing', printing=True))
        self.fake.route('/api/job', octoprint_job(completion=50, left=600))
        monitor = self.monitor()
        self.configure(monitor)
        self.assertEqual(monitor.poll(), 'connected')
        snapshot = monitor.snapshot()
        self.assertEqual((snapshot['kind'], snapshot['host'], snapshot['serial']), ('octoprint', '127.0.0.1', None))
        self.assertEqual((snapshot['fields']['state'], snapshot['fields']['progress_percent'],
                          snapshot['fields']['remaining_minutes'], snapshot['fields']['current_layer']),
                         ('RUNNING', 50, 10, None))
        self.assertEqual({path for path, _ in self.fake.requests}, {'/api/printer', '/api/job'})
        self.assertTrue(all(headers.get('X-Api-Key') == KEY for _, headers in self.fake.requests))
        self.assertNotIn(KEY, json.dumps(snapshot))

    def test_rejected_key_is_an_authentication_failure(self):
        monitor = self.monitor()
        self.configure(monitor)
        for status in (401, 403):
            with self.subTest(status=status):
                self.fake.route('/api/printer', {'error': 'Forbidden'}, status)
                self.assertEqual(monitor.poll(), 'authentication_failed')
                snapshot = monitor.snapshot()
                self.assertEqual((snapshot['connection'], snapshot['fresh']), ('authentication_failed', False))
                self.assertNotIn(KEY, json.dumps(snapshot))
        self.fake.route('/api/printer', octoprint_printer())
        self.fake.route('/api/job', {'error': 'Forbidden'}, 403)
        self.assertEqual(monitor.poll(), 'authentication_failed')

    def test_printer_not_connected_to_octoprint_is_waiting(self):
        self.fake.route('/api/printer', b'Printer is not operational', 409)
        monitor = self.monitor()
        self.configure(monitor)
        self.assertEqual(monitor.poll(), 'waiting')
        self.assertFalse(monitor.snapshot()['fresh'])

    def test_unreachable_octoprint(self):
        monitor = self.monitor()
        monitor.configure({'kind': 'octoprint', 'url': f'http://127.0.0.1:{closed_port()}', 'api_key': KEY})
        self.assertEqual(monitor.poll(), 'unreachable')
        self.assertEqual(monitor.snapshot()['connection'], 'unreachable')


class SettingsTests(PrintStatusCase):
    def test_switching_kinds_replaces_the_previous_printer(self):
        file = self.root / 'printer.json'
        monitor = self.monitor()
        monitor.configure(BAMBU)
        self.assertEqual(json.loads(file.read_text()), BAMBU)
        snapshot = monitor.snapshot()
        self.assertEqual((snapshot['kind'], snapshot['host'], snapshot['serial']), ('bambu', '192.168.1.20', 'TESTSERIAL123'))
        self.assertEqual(snapshot['fields'], {'ams_slots': []})
        generation = monitor.generation

        monitor.configure({'kind': 'klipper', 'url': 'http://127.0.0.1:7125'})
        self.assertEqual(json.loads(file.read_text()), {'kind': 'klipper', 'url': 'http://127.0.0.1:7125'})
        snapshot = monitor.snapshot()
        self.assertEqual((snapshot['kind'], snapshot['host'], snapshot['serial'], snapshot['connection']),
                         ('klipper', '127.0.0.1', None, 'connecting'))
        self.assertEqual(snapshot['fields'], dict.fromkeys(FIELDS))
        self.assertGreater(monitor.generation, generation)
        self.assertNotIn('aB123456', file.read_text())

        monitor.configure({'kind': 'octoprint', 'url': 'http://127.0.0.1:5000', 'api_key': KEY})
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(monitor.snapshot()['kind'], 'octoprint')
        restarted = self.monitor()
        self.assertEqual((restarted.snapshot()['kind'], restarted.snapshot()['connection']), ('octoprint', 'connecting'))
        self.assertEqual(restarted.settings['api_key'], KEY)

        restarted.configure(BAMBU)
        self.assertEqual(json.loads(file.read_text()), BAMBU)
        restarted.forget()
        self.assertFalse(file.exists())
        snapshot = restarted.snapshot()
        self.assertEqual({key: snapshot[key] for key in ('configured', 'kind', 'connection', 'host', 'serial', 'fresh')},
                         {'configured': False, 'kind': None, 'connection': 'not_configured', 'host': None,
                          'serial': None, 'fresh': False})

    def test_unreadable_saved_settings(self):
        file = self.root / 'printer.json'
        for content in ('{"kind": "klipper", "url": "http://8.8.8.8:7125"}', '{"kind": "octoprint", "url": "http://127.0.0.1"}',
                        '{"kind": "prusa"}', '[]', 'not json'):
            with self.subTest(content=content):
                file.write_text(content)
                snapshot = self.monitor().snapshot()
                self.assertEqual((snapshot['configured'], snapshot['kind'], snapshot['connection']),
                                 (False, None, 'invalid_configuration'))

    def test_invalid_settings_leave_the_current_printer(self):
        monitor = self.monitor()
        monitor.configure(BAMBU)
        with self.assertRaises(ValueError):
            monitor.configure({'kind': 'octoprint', 'url': 'http://127.0.0.1:5000'})
        self.assertEqual(monitor.snapshot()['kind'], 'bambu')
        self.assertEqual(json.loads((self.root / 'printer.json').read_text()), BAMBU)


class DiscoveryTests(PrintStatusCase):
    def add_server(self):
        server = FakeHTTP()
        self.addCleanup(server.close)
        return server

    def test_finds_moonraker_and_octoprint_on_this_pi(self):
        self.fake.route('/server/info', {'result': {'klippy_connected': True, 'klippy_state': 'ready',
                                                    'moonraker_version': 'v0.9.3'}})
        octoprint = self.add_server()
        octoprint.route('/api/version', b'<html>Forbidden</html>', 403)
        octoprint.route('/', b'', 302, {'Location': '/login/?redirect=%2F'})
        octoprint.route('/login/', b'<!DOCTYPE html><html><head><title>OctoPrint Login</title>' + b' ' * 100000)
        # Something else answering 403 everywhere, like macOS AirPlay on port 5000, isn't OctoPrint.
        impostor = self.add_server()
        for path in ('/api/version', '/', '/login/'):
            impostor.route(path, b'Forbidden', 403)
        with patch('print_status.socket.gethostname', return_value='voron'):
            found = discover_local(moonraker_ports=(self.fake.port,),
                                   octoprint_ports=(octoprint.port, impostor.port, closed_port()))
        self.assertEqual(found, [
            {'kind': 'klipper', 'url': self.fake.url, 'name': 'Klipper on voron', 'host': None, 'serial': None,
             'mode': None, 'needs_api_key': False},
            {'kind': 'octoprint', 'url': octoprint.url, 'name': 'OctoPrint on voron', 'host': None, 'serial': None,
             'mode': None, 'needs_api_key': True}])
        self.assertTrue(all('X-Api-Key' not in headers for _, headers in self.fake.requests + octoprint.requests))

    def test_moonraker_that_requires_a_key_and_octoprint_with_open_version(self):
        self.fake.route('/server/info', {'error': {'code': 401, 'message': 'Unauthorized'}}, 401)
        octoprint = self.add_server()
        octoprint.route('/api/version', {'api': '0.1', 'server': '1.10.2', 'text': 'OctoPrint 1.10.2'})
        not_moonraker = self.add_server()
        not_moonraker.route('/server/info', {'result': {'name': 'something else'}})
        found = discover_local(moonraker_ports=(self.fake.port, not_moonraker.port), octoprint_ports=(octoprint.port,))
        self.assertEqual([(item['kind'], item['needs_api_key']) for item in found], [('klipper', True), ('octoprint', True)])
        self.assertEqual(found[1]['url'], octoprint.url)

    def test_monitor_combines_bambu_and_local_results(self):
        local = [{'kind': 'klipper', 'url': 'http://127.0.0.1:7125', 'name': 'Klipper', 'host': None, 'serial': None,
                  'mode': None, 'needs_api_key': False}]
        monitor = PrinterMonitor(self.root / 'printer.json', self.policy, threading.Event(), local_discovery=lambda: local)
        bambu = [{'host': '192.168.1.20', 'serial': 'TESTSERIAL123', 'name': 'A1', 'mode': 'lan'}]
        with patch('bambu_monitor.discover', return_value=bambu):
            found = monitor.discover()
        self.assertEqual(found, [{'kind': 'bambu', 'url': None, **bambu[0]}, local[0]])
        # Busy Bambu ports still return what was found on this Pi; with nothing found, the error is kept.
        with patch('bambu_monitor.discover', side_effect=ValueError('Printer discovery ports are busy.')):
            self.assertEqual(monitor.discover(), local)
            monitor.discover_local = lambda: []
            with self.assertRaisesRegex(ValueError, 'busy'):
                monitor.discover()
        self.assertFalse(monitor.scan_lock.locked())
        monitor.discover_local = lambda: 1 / 0
        with patch('bambu_monitor.discover', return_value=bambu), self.assertLogs(level='WARNING'):
            self.assertEqual(monitor.discover(), [{'kind': 'bambu', 'url': None, **bambu[0]}])


class APITests(PrintStatusCase):
    def setUp(self):
        super().setUp()
        tls = self.root / 'tls'
        ensure_identity(tls)
        self.client_context = local_client_context(tls)
        state = PairingState(self.root / 'phone.json')
        self.token = state.pair(state.open_pairing())[1]['token']
        self.server = CameraServer(('127.0.0.1', 0), state, FrameBuffer(), tls_context=server_context(tls))
        self.server.printer = self.monitor()
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            worker.join(timeout=3)
        self.addCleanup(stop)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_port, context=self.client_context,
                                                 timeout=15)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               {'Authorization': 'Bearer ' + self.token, **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_configure_each_kind_over_https(self):
        code, result = self.request('POST', '/api/printer', BAMBU)
        self.assertEqual(code, 200)
        self.assertEqual(set(result), {'configured', 'kind', 'connection', 'fresh', 'age_seconds', 'host', 'serial', 'fields'})
        self.assertEqual((result['kind'], result['host'], result['serial'], result['connection']),
                         ('bambu', '192.168.1.20', 'TESTSERIAL123', 'connecting'))
        self.assertNotIn('aB123456', json.dumps(result))
        self.assertEqual(self.request('POST', '/api/printer', {**BAMBU, 'kind': 'bambu'})[1]['kind'], 'bambu')

        code, result = self.request('POST', '/api/printer', {'kind': 'octoprint', 'url': 'http://127.0.0.1:5000', 'api_key': KEY})
        self.assertEqual(code, 200)
        self.assertEqual((result['kind'], result['host'], result['serial']), ('octoprint', '127.0.0.1', None))
        self.assertNotIn(KEY, json.dumps(result))
        code, result = self.request('GET', '/api/printer')
        self.assertEqual((code, result['kind']), (200, 'octoprint'))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(set(result['fields']), set(FIELDS))

        code, result = self.request('POST', '/api/printer', {'kind': 'klipper', 'url': 'http://localhost:7125'})
        self.assertEqual((code, result['kind'], result['host']), (200, 'klipper', '127.0.0.1'))
        for body, message in [({'kind': 'octoprint', 'url': 'http://127.0.0.1:5000'}, 'Enter the OctoPrint API key.'),
                              ({'kind': 'klipper', 'url': 'http://8.8.8.8:7125'}, 'The printer must be on an attached private local network.'),
                              ({'kind': 'klipper', 'url': 'http://a:b@127.0.0.1:7125'}, 'Remove the user name and password from the address.'),
                              ({**BAMBU, 'host': '8.8.8.8'}, 'The printer must be on an attached private local network.')]:
            with self.subTest(body=body):
                self.assertEqual(self.request('POST', '/api/printer', body), (400, {'error': message}))
        self.assertEqual(self.request('GET', '/api/printer')[1]['kind'], 'klipper')
        code, result = self.request('DELETE', '/api/printer')
        self.assertEqual((code, result['kind'], result['configured']), (200, None, False))

    def test_discover_lists_every_kind(self):
        local = [{'kind': 'octoprint', 'url': 'http://127.0.0.1:5000', 'name': 'OctoPrint', 'host': None,
                  'serial': None, 'mode': None, 'needs_api_key': True}]
        self.server.printer.discover_local = lambda: local
        with patch('bambu_monitor.discover', return_value=[{'host': '192.168.1.20', 'serial': 'TESTSERIAL123',
                                                             'name': 'A1', 'mode': 'lan'}]):
            code, result = self.request('GET', '/api/printers/discover', headers={'X-Printer-Pal-Discover': 'all'})
            self.assertEqual(code, 200)
            self.assertEqual([(item['kind'], item['url'], item['serial']) for item in result['printers']],
                             [('bambu', None, 'TESTSERIAL123'), ('octoprint', 'http://127.0.0.1:5000', None)])
            # Printer Pal 1.0 requires a host and serial on every item, so it only gets Bambu printers.
            code, result = self.request('GET', '/api/printers/discover')
            self.assertEqual(code, 200)
            self.assertEqual([item['serial'] for item in result['printers']], ['TESTSERIAL123'])


class TimelapseTests(PrintStatusCase):
    def test_klipper_print_start_records_a_layer_timelapse(self):
        monitor = self.monitor()
        monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        now = [1762784553.0]
        frames = []

        def frame():
            frames.append(1)
            return b'\xff\xd8frame-%04d\xff\xd9' % len(frames)

        directory, settings_path = storage_paths(self.root / 'state.json')
        recorder = TimelapseRecorder(directory, settings_path, frame, lambda: printer_status(monitor),
                                     clock=lambda: now[0], monotonic=lambda: now[0], disk_free=lambda: 10 * 1024 ** 3)
        recorder.configure({'enabled': True, 'mode': 'layer', 'interval_seconds': 10})
        monitor.listeners.append(recorder.wake)

        def report(**values):
            self.fake.route(print_status.MOONRAKER_QUERY, moonraker(**values))
            recorder.woken.clear()
            self.assertEqual(monitor.poll(), 'connected')
            self.assertTrue(recorder.woken.is_set(), 'each poll wakes the recorder')
            now[0] += 3
            recorder.poll()
            return recorder.state()['recording']

        self.assertIsNone(report(state='standby'))
        self.assertTrue(recorder.state()['layer_available'] is False)
        self.assertIsNone(report(state='printing', duration=0, filename='Benchy.gcode'), 'heating is not printing yet')
        recording = report(state='printing', duration=4, progress=0.01, layer=1, total=3, filename='Benchy.gcode')
        self.assertIsNotNone(recording)
        self.assertEqual((recording['manual'], recording['trigger'], recording['frames']), (False, 'layer', 1))
        self.assertEqual(report(state='printing', duration=10, progress=0.02, layer=1, total=3)['frames'], 1)
        self.assertEqual(report(state='printing', duration=60, progress=0.4, layer=2, total=3)['frames'], 2)
        self.assertEqual(report(state='paused', duration=60, progress=0.4, layer=2, total=3)['frames'], 2)
        self.assertEqual(report(state='printing', duration=90, progress=0.8, layer=3, total=3)['frames'], 3)
        self.assertIsNone(report(state='complete', duration=120, progress=1.0, layer=3, total=3))
        item = recorder.list()['timelapses'][0]
        # One frame per layer, plus the final frame when the print finishes.
        self.assertEqual((item['id'], item['stop_reason'], item['frames'], item['name']),
                         (recording['id'], 'finished', 4, 'Benchy'))
        recorder.close()


if __name__ == '__main__':
    unittest.main()
