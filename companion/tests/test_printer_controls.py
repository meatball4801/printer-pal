import http.client
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import print_status
from print_status import ControlError, PrinterMonitor
from agent import CameraServer, FrameBuffer, PairingState
from network_guard import LANPolicy
from test_print_status import BAMBU, KEY, FakeHTTP, closed_port, moonraker, octoprint_job, octoprint_printer

REQUEST_TOPIC = 'device/TESTSERIAL123/request'
REPORT_TOPIC = 'device/TESTSERIAL123/report'
LIGHT = ('{"system":{"sequence_id":"0","command":"ledctrl","led_node":"chamber_light","led_mode":"%s",'
         '"led_on_time":500,"led_off_time":500,"loop_times":0,"interval_time":0}}')
MOONRAKER_STATES = {'IDLE': 'standby', 'PREPARE': None, 'RUNNING': 'printing', 'PAUSE': 'paused',
                    'FINISH': 'complete', 'FAILED': 'cancelled'}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeMQTT:
    """Stands in for the paho client. Only the MQTT session thread may call loop and publish."""

    def __init__(self, rc=0):
        self.rc = rc
        self.published = []
        self.reports = queue.Queue()
        self.connected = False
        self.on_connect = self.on_message = self.on_disconnect = None

    def connect(self, host, port, keepalive):
        self.address = (host, port)

    def subscribe(self, topic, qos=0):
        self.topic = topic

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append({'topic': topic, 'payload': payload, 'qos': qos, 'retain': retain,
                               'thread': threading.current_thread().name})
        return SimpleNamespace(rc=self.rc)

    def loop(self, timeout=1):
        if not self.connected:
            self.connected = True
            self.on_connect(self, None, None, 0)
        try:
            report = self.reports.get(timeout=0.01)
        except queue.Empty:
            return 0
        self.on_message(self, None, SimpleNamespace(topic=REPORT_TOPIC, payload=json.dumps({'print': report})))
        return 0

    def disconnect(self):
        pass

    def commands(self):
        return [item['payload'] for item in self.published if 'pushall' not in item['payload']]


class ControlCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scopes = self.root / 'scopes.json'
        self.scopes.write_text(json.dumps([['eth0', '192.168.1.0/24']]))
        self.policy = LANPolicy(self.scopes)
        self.fake = FakeHTTP()
        self.addCleanup(self.fake.close)
        self.clock = FakeClock()
        self.client = FakeMQTT()
        self.monitor = PrinterMonitor(self.root / 'printer.json', self.policy, threading.Event(), clock=self.clock,
                                      client_factory=lambda _settings: self.client, local_discovery=lambda: [],
                                      send_timeout=1)

    def control(self, action, advance=True):
        if advance:
            self.clock.now += 1.5  # Past the rate limit.
        body = action if isinstance(action, dict) or action is None else {'action': action}
        try:
            self.monitor.control(body)
        except ControlError as error:
            return error.status, error.message
        return 200, None

    def start_thread(self):
        self.monitor.start()

        def stop():
            self.monitor.stop.set()
            self.monitor.changed.set()
            self.monitor.thread.join(timeout=3)
        self.addCleanup(stop)

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return False

    def bambu_report(self, state):
        self.client.reports.put({'gcode_state': state, 'mc_percent': 10})
        self.assertTrue(self.wait_for(lambda: self.monitor.snapshot()['fields'].get('state') == state))

    def klipper(self, state, api_key=None):
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url, **({'api_key': api_key} if api_key else {})})
        self.set_klipper_state(state)

    def set_klipper_state(self, state):
        if state == 'PREPARE':
            body = moonraker('printing', duration=0)
        else:
            body = moonraker(MOONRAKER_STATES[state], duration=10, progress=0.2)
        self.fake.route(print_status.MOONRAKER_QUERY, body)
        self.assertEqual(self.monitor.poll(), 'connected')
        self.assertEqual(self.monitor.snapshot()['fields']['state'], state)

    def octoprint(self, state):
        self.monitor.configure({'kind': 'octoprint', 'url': self.fake.url, 'api_key': KEY})
        flags = {'RUNNING': {'printing': True}, 'PAUSE': {'paused': True}, 'IDLE': {}}[state]
        self.fake.route('/api/printer', octoprint_printer('Printing' if flags else 'Operational', **flags))
        self.fake.route('/api/job', octoprint_job(completion=20, left=600))
        self.assertEqual(self.monitor.poll(), 'connected')
        self.assertEqual(self.monitor.snapshot()['fields']['state'], state)


class ControlsListTests(ControlCase):
    def test_controls_in_the_snapshot(self):
        self.assertEqual(self.monitor.snapshot()['controls'], [])
        self.monitor.configure(BAMBU)
        self.assertEqual(self.monitor.snapshot()['controls'], ['pause', 'resume', 'stop', 'light'])
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.assertEqual(self.monitor.snapshot()['controls'], ['pause', 'resume', 'stop'])
        self.monitor.configure({'kind': 'octoprint', 'url': self.fake.url, 'api_key': KEY})
        self.assertEqual(self.monitor.snapshot()['controls'], ['pause', 'resume', 'stop'])
        self.monitor.forget()
        self.assertEqual(self.monitor.snapshot()['controls'], [])


class CheckOrderTests(ControlCase):
    def test_invalid_body_or_action(self):
        self.klipper('RUNNING')
        for body in (None, {}, {'action': 'PAUSE'}, {'action': 'light'}, {'action': 'home'}, {'action': ''},
                     {'action': 1}, {'action': None}, {'action': ['pause']}, {'action': 'pause', 'extra': 1},
                     {'command': 'pause'}, {'action': 'pause '}):
            with self.subTest(body=body):
                self.assertEqual(self.control(body), (400, 'Unknown printer action.'))
        self.assertEqual(self.fake.posts, [])

    def test_unknown_action_comes_before_no_printer(self):
        self.assertEqual(self.control({'action': 'dance'}), (400, 'Unknown printer action.'))
        for action in ('pause', 'resume', 'stop', 'light_on', 'light_off'):
            self.assertEqual(self.control(action), (409, 'Connect a printer first.'))

    def test_light_is_not_supported_on_klipper_or_octoprint(self):
        self.klipper('RUNNING')
        for action in ('light_on', 'light_off'):
            self.assertEqual(self.control(action), (400, "This printer doesn't support that."))
        self.octoprint('RUNNING')
        self.assertEqual(self.control('light_on'), (400, "This printer doesn't support that."))
        # Checked before the connection: an offline Klipper printer still says it's unsupported.
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.assertEqual(self.control('light_off'), (400, "This printer doesn't support that."))
        self.assertEqual(self.fake.posts, [])

    def test_no_fresh_status(self):
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.assertEqual(self.control('pause'), (409, "The printer isn't connected right now."))
        self.set_klipper_state('RUNNING')
        self.clock.now += 100
        self.assertEqual(self.control('pause'), (409, "The printer isn't connected right now."))
        self.monitor.configure(BAMBU)
        self.assertEqual(self.control('light_on'), (409, "The printer isn't connected right now."))
        self.assertEqual(self.fake.posts, [])

    def test_state_rules(self):
        self.fake.post_route('/printer/print/pause', {'result': 'ok'})
        self.fake.post_route('/printer/print/resume', {'result': 'ok'})
        self.fake.post_route('/printer/print/cancel', {'result': 'ok'})
        expected = {
            'pause': {'RUNNING': 200, 'PREPARE': 200, 'PAUSE': "The printer isn't printing.",
                      'IDLE': "The printer isn't printing.", 'FINISH': "The printer isn't printing.",
                      'FAILED': "The printer isn't printing."},
            'resume': {'RUNNING': "The print isn't paused.", 'PREPARE': "The print isn't paused.", 'PAUSE': 200,
                       'IDLE': "The print isn't paused.", 'FINISH': "The print isn't paused.",
                       'FAILED': "The print isn't paused."},
            'stop': {'RUNNING': 200, 'PREPARE': 200, 'PAUSE': 200, 'IDLE': "The printer isn't printing.",
                     'FINISH': "The printer isn't printing.", 'FAILED': "The printer isn't printing."}}
        self.klipper('IDLE')
        for action, states in expected.items():
            for state, outcome in states.items():
                with self.subTest(action=action, state=state):
                    self.set_klipper_state(state)
                    if outcome == 200:
                        self.assertEqual(self.control(action), (200, None))
                    else:
                        self.assertEqual(self.control(action), (409, outcome))

    def test_rate_limit(self):
        self.klipper('RUNNING')
        self.fake.post_route('/printer/print/pause', {'result': 'ok'})
        self.assertEqual(self.control('pause'), (200, None))
        self.clock.now += 0.5
        self.assertEqual(self.control('pause', advance=False), (429, 'Wait a moment and try again.'))
        # Every request counts, including invalid ones, and a refused request doesn't extend the wait.
        self.clock.now += 0.6
        self.assertEqual(self.control({'action': 'nope'}, advance=False), (400, 'Unknown printer action.'))
        self.assertEqual(self.control('pause', advance=False), (429, 'Wait a moment and try again.'))
        self.clock.now += 1.0
        self.assertEqual(self.control('pause', advance=False), (200, None))
        self.assertEqual(len(self.fake.posts), 2)


class KlipperControlTests(ControlCase):
    def test_posts_each_command_with_the_key(self):
        for path in ('/printer/print/pause', '/printer/print/resume', '/printer/print/cancel'):
            self.fake.post_route(path, {'result': 'ok'})
        self.klipper('RUNNING', api_key=KEY)
        self.assertEqual(self.control('pause'), (200, None))
        self.set_klipper_state('PAUSE')
        self.assertEqual(self.control('resume'), (200, None))
        self.assertEqual(self.control('stop'), (200, None))
        self.assertEqual([path for path, _, _ in self.fake.posts],
                         ['/printer/print/pause', '/printer/print/resume', '/printer/print/cancel'])
        for _, headers, body in self.fake.posts:
            self.assertEqual(headers.get('X-Api-Key'), KEY)
            self.assertEqual(body, b'')

    def test_no_key_header_without_a_saved_key(self):
        self.fake.post_route('/printer/print/pause', {'result': 'ok'}, 200)
        self.klipper('RUNNING')
        self.assertEqual(self.control('pause'), (200, None))
        self.assertNotIn('X-Api-Key', self.fake.posts[0][1])

    def test_server_errors(self):
        self.klipper('RUNNING', api_key=KEY)
        cases = [(204, b'', {}, (200, None)),
                 (401, {'error': 'Unauthorized'}, {}, (403, 'Check the API key.')),
                 (403, {'error': 'Forbidden'}, {}, (403, 'Check the API key.')),
                 (409, {'error': 'busy'}, {}, (409, "The printer can't do that right now.")),
                 (400, {'error': 'bad'}, {}, (502, "The printer didn't accept the command.")),
                 (404, {'error': 'missing'}, {}, (502, "The printer didn't accept the command.")),
                 (500, b'oops', {}, (502, "The printer didn't accept the command.")),
                 (503, b'', {}, (502, "The printer didn't accept the command.")),
                 (302, b'', {'Location': 'http://8.8.8.8/'}, (502, "The printer didn't accept the command.")),
                 (307, b'', {'Location': '/printer/print/cancel'}, (502, "The printer didn't accept the command."))]
        for status, body, headers, expected in cases:
            with self.subTest(status=status):
                self.fake.post_route('/printer/print/pause', body, status, headers)
                self.assertEqual(self.control('pause'), expected)
        self.assertEqual({path for path, _, _ in self.fake.posts}, {'/printer/print/pause'},
                         'redirects are not followed')
        self.assertEqual(len(self.fake.posts), len(cases))

    def test_no_answer(self):
        self.klipper('RUNNING', api_key=KEY)
        with self.monitor.lock:
            self.monitor.settings = {'kind': 'klipper', 'url': f'http://127.0.0.1:{closed_port()}', 'api_key': KEY}
        self.assertEqual(self.control('pause'), (502, "The printer didn't accept the command."))

    def test_unexpected_failure_logs_no_key(self):
        self.klipper('RUNNING', api_key=KEY)

        class Broken:
            def open(self, *_args, **_kwargs):
                raise RuntimeError(KEY)
        self.monitor.opener = Broken()
        with self.assertLogs(level='WARNING') as logs:
            self.assertEqual(self.control('stop'), (502, "The printer didn't accept the command."))
        self.assertNotIn(KEY, '\n'.join(logs.output))

    def test_address_off_the_attached_network_is_not_contacted(self):
        self.klipper('RUNNING')
        with self.monitor.lock:
            self.monitor.settings = {'kind': 'klipper', 'url': 'http://192.168.9.9:7125'}
        self.assertEqual(self.control('pause'), (409, "The printer isn't connected right now."))
        self.assertEqual(self.fake.posts, [])


class OctoPrintControlTests(ControlCase):
    def test_posts_job_commands_with_the_key(self):
        self.fake.post_route('/api/job', b'', 204)
        self.octoprint('RUNNING')
        self.assertEqual(self.control('pause'), (200, None))
        self.octoprint('PAUSE')
        self.assertEqual(self.control('resume'), (200, None))
        self.assertEqual(self.control('stop'), (200, None))
        self.assertEqual([path for path, _, _ in self.fake.posts], ['/api/job'] * 3)
        self.assertEqual([json.loads(body) for _, _, body in self.fake.posts],
                         [{'command': 'pause', 'action': 'pause'}, {'command': 'pause', 'action': 'resume'},
                          {'command': 'cancel'}])
        for _, headers, _ in self.fake.posts:
            self.assertEqual(headers.get('X-Api-Key'), KEY)
            self.assertEqual(headers.get('Content-Type'), 'application/json')

    def test_server_errors(self):
        self.octoprint('RUNNING')
        for status, expected in ((403, (403, 'Check the API key.')), (401, (403, 'Check the API key.')),
                                 (409, (409, "The printer can't do that right now.")),
                                 (500, (502, "The printer didn't accept the command.")),
                                 (200, (200, None))):
            with self.subTest(status=status):
                self.fake.post_route('/api/job', {'error': 'x'}, status)
                self.assertEqual(self.control('pause'), expected)

    def test_idle_printer(self):
        self.octoprint('IDLE')
        self.assertEqual(self.control('pause'), (409, "The printer isn't printing."))
        self.assertEqual(self.control('resume'), (409, "The print isn't paused."))
        self.assertEqual(self.control('stop'), (409, "The printer isn't printing."))
        self.assertEqual(self.fake.posts, [])


class BambuControlTests(ControlCase):
    def connect(self, state='RUNNING'):
        self.monitor.configure(BAMBU)
        self.start_thread()
        self.bambu_report(state)

    def test_publishes_each_command_from_the_session_thread(self):
        self.connect('RUNNING')
        self.assertEqual(self.control('pause'), (200, None))
        self.bambu_report('PAUSE')
        self.assertEqual(self.control('resume'), (200, None))
        self.assertEqual(self.control('stop'), (200, None))
        self.bambu_report('IDLE')
        self.assertEqual(self.control('light_on'), (200, None))
        self.assertEqual(self.control('light_off'), (200, None))
        self.assertEqual(self.client.commands(), [
            '{"print":{"sequence_id":"0","command":"pause","param":""}}',
            '{"print":{"sequence_id":"0","command":"resume","param":""}}',
            '{"print":{"sequence_id":"0","command":"stop","param":""}}',
            LIGHT % 'on', LIGHT % 'off'])
        for item in self.client.published:
            self.assertEqual((item['topic'], item['qos'], item['retain'], item['thread']),
                             (REQUEST_TOPIC, 0, False, 'printer-status'))
        self.assertEqual(self.monitor.outbox, [])

    def test_state_rules(self):
        self.connect('IDLE')
        self.assertEqual(self.control('pause'), (409, "The printer isn't printing."))
        self.assertEqual(self.control('resume'), (409, "The print isn't paused."))
        self.assertEqual(self.control('stop'), (409, "The printer isn't printing."))
        self.bambu_report('PREPARE')
        self.assertEqual(self.control('resume'), (409, "The print isn't paused."))
        self.assertEqual(self.control('pause'), (200, None))
        self.bambu_report('FINISH')
        self.assertEqual(self.control('light_on'), (200, None))
        self.assertEqual(len(self.client.commands()), 2)

    def test_lights_work_while_printing_and_paused(self):
        self.connect('RUNNING')
        self.assertEqual(self.control('light_off'), (200, None))
        self.bambu_report('PAUSE')
        self.assertEqual(self.control('light_on'), (200, None))
        self.assertEqual(self.client.commands(), [LIGHT % 'off', LIGHT % 'on'])

    def test_rejected_publish_means_not_connected(self):
        self.client.rc = 4  # paho's MQTT_ERR_NO_CONN
        self.connect('RUNNING')
        self.assertEqual(self.control('pause'), (409, "The printer isn't connected right now."))

    def test_not_connected(self):
        self.monitor.configure(BAMBU)
        self.assertEqual(self.control('light_on'), (409, "The printer isn't connected right now."))
        # Fresh status but no running session to send it: the command isn't left queued to go out later.
        with self.monitor.lock:
            self.monitor.connected = True
            self.monitor.report.update(json.dumps({'print': {'gcode_state': 'RUNNING'}}), self.clock())
        self.assertTrue(self.monitor.snapshot()['fresh'])
        started = time.monotonic()
        self.assertEqual(self.control('pause'), (409, "The printer isn't connected right now."))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.monitor.outbox, [])

    def test_settings_change_drops_waiting_commands(self):
        self.monitor.configure(BAMBU)
        with self.monitor.lock:
            self.monitor.connected = True
            self.monitor.report.update(json.dumps({'print': {'gcode_state': 'RUNNING'}}), self.clock())
        self.monitor.send_timeout = 5
        result = []
        worker = threading.Thread(target=lambda: result.append(self.control('stop')))
        worker.start()
        self.assertTrue(self.wait_for(lambda: self.monitor.outbox))
        self.monitor.configure({**BAMBU, 'serial': 'OTHERSERIAL1'})
        worker.join(timeout=3)
        self.assertEqual(result, [(409, "The printer isn't connected right now.")])
        self.assertEqual(self.monitor.outbox, [])

    def test_dropped_session_fails_waiting_commands(self):
        self.monitor.configure(BAMBU)
        with self.monitor.lock:
            self.monitor.connected = True
            self.monitor.report.update(json.dumps({'print': {'gcode_state': 'RUNNING'}}), self.clock())
            generation = self.monitor.generation
        self.monitor.send_timeout = 5
        result = []
        worker = threading.Thread(target=lambda: result.append(self.control('pause')))
        worker.start()
        self.assertTrue(self.wait_for(lambda: self.monitor.outbox))
        self.monitor._drop_pending(generation)
        worker.join(timeout=3)
        self.assertEqual(result, [(409, "The printer isn't connected right now.")])


class HTTPTests(ControlCase):
    def setUp(self):
        super().setUp()
        self.state = PairingState(self.root / 'phone.json')
        self.token = self.state.pair(self.state.open_pairing())[1]['token']
        self.server = CameraServer(('127.0.0.1', 0), self.state, FrameBuffer(), demo=True)
        self.server.printer = self.monitor
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)
        self.addCleanup(stop)

    def post(self, body, token=None, raw=None):
        self.clock.now += 1.5
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if token != '':
            headers['Authorization'] = 'Bearer ' + (token or self.token)
        try:
            connection.request('POST', '/api/printer/control', raw if raw is not None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authentication_is_required(self):
        self.klipper('RUNNING')
        self.assertEqual(self.post({'action': 'pause'}, token=''), (401, {'error': 'Pair this camera before viewing.'}))
        self.assertEqual(self.post({'action': 'pause'}, token='wrong'),
                         (401, {'error': 'Pair this camera before viewing.'}))
        self.assertEqual(self.fake.posts, [])
        self.assertIsNone(self.monitor.last_control, 'unauthenticated requests do not use up the rate limit')

    def test_success_and_errors(self):
        self.assertEqual(self.post({'action': 'pause'}), (409, {'error': 'Connect a printer first.'}))
        for raw in ('not json', '[]', '"pause"', '', 'x' * 2048):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(None, raw=raw), (400, {'error': 'Unknown printer action.'}))
        self.klipper('RUNNING', api_key=KEY)
        self.fake.post_route('/printer/print/pause', {'result': 'ok'})
        self.assertEqual(self.post({'action': 'pause'}), (200, {'sent': True}))
        self.assertEqual(self.post({'action': 'light_on'}), (400, {'error': "This printer doesn't support that."}))
        self.assertEqual(self.post({'action': 'resume'}), (409, {'error': "The print isn't paused."}))
        self.fake.post_route('/printer/print/pause', {'error': 'no'}, 401)
        code, body = self.post({'action': 'pause'})
        self.assertEqual((code, body), (403, {'error': 'Check the API key.'}))
        self.assertNotIn(KEY, json.dumps(body))
        self.clock.now += 0.1
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('POST', '/api/printer/control', json.dumps({'action': 'pause'}),
                           {'Authorization': 'Bearer ' + self.token})
        response = connection.getresponse()
        self.assertEqual((response.status, json.loads(response.read())),
                         (429, {'error': 'Wait a moment and try again.'}))
        connection.close()

    def test_unavailable_without_a_monitor(self):
        self.server.printer = None
        self.assertEqual(self.post({'action': 'pause'}), (503, {'error': 'Printer monitoring is unavailable.'}))

    def test_get_printer_lists_controls(self):
        self.monitor.configure(BAMBU)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('GET', '/api/printer', headers={'Authorization': 'Bearer ' + self.token})
        response = connection.getresponse()
        self.assertEqual(json.loads(response.read())['controls'], ['pause', 'resume', 'stop', 'light'])
        connection.close()


if __name__ == '__main__':
    unittest.main()
