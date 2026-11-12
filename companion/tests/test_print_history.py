import http.client
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import print_history
import print_status
from print_history import MAX_PRINTS, STALE_SECONDS, PrintHistory, storage_path
from agent import CameraServer, FrameBuffer, PairingState
from test_print_status import FakeHTTP, moonraker
from network_guard import LANPolicy

START = 1762784553  # 2025-11-10 14:22:33 UTC


class FakeClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class FakeStatus:
    """What PrinterMonitor.snapshot() returns, reduced to what the history reads."""

    def __init__(self):
        self.value = {'fresh': False, 'kind': None, 'fields': {}}

    def set(self, state, fresh=True, kind='bambu', **fields):
        self.value = {'fresh': fresh, 'kind': kind, 'fields': {'state': state, **fields}}

    def __call__(self):
        return json.loads(json.dumps(self.value))


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = storage_path(self.root / 'state.json')
        self.clock = FakeClock()
        self.status = FakeStatus()
        self.history = self.make()

    def make(self):
        return PrintHistory(self.path, self.status, clock=self.clock)

    def report(self, state, seconds=10, **values):
        self.status.set(state, **values)
        self.history.observe()
        self.clock.now += seconds

    def prints(self):
        return self.history.list()['prints']

    def saved(self):
        return json.loads(self.path.read_text())['prints']


class RecordingTests(HistoryCase):
    def test_storage_path_is_next_to_the_state_file(self):
        self.assertEqual(storage_path('/var/lib/print-monitor/state.json'), Path('/var/lib/print-monitor/prints.json'))

    def test_finished_print(self):
        self.report('IDLE')
        self.report('FINISH', progress_percent=100, job_name='Old')
        self.assertEqual(self.history.list(), {'prints': [], 'stats': {'count': 0, 'finished': 0, 'failed': 0,
                                                                        'stopped': 0, 'interrupted': 0,
                                                                        'print_seconds': 0}})
        started = int(self.clock.now)
        self.report('PREPARE', job_name=None, progress_percent=None)
        self.assertEqual(self.prints(), [{'id': '20251110-142253', 'name': None, 'kind': 'bambu', 'started': started,
                                          'ended': None, 'result': None, 'duration_seconds': None, 'progress': None,
                                          'layers': None}])
        self.report('RUNNING', job_name='Benchy', progress_percent=1, total_layers=212)
        self.report('RUNNING', job_name='Something else', progress_percent=40, total_layers=212)
        self.report('PAUSE', job_name='Benchy', progress_percent=41)
        self.report('RUNNING', progress_percent=99, total_layers=None)
        ended = int(self.clock.now)
        self.report('FINISH', progress_percent=None)
        self.report('FINISH', progress_percent=100)
        self.report('IDLE')
        result = self.history.list()
        self.assertEqual(result['prints'], [{'id': '20251110-142253', 'name': 'Benchy', 'kind': 'bambu',
                                             'started': started, 'ended': ended, 'result': 'finished',
                                             'duration_seconds': ended - started, 'progress': 100, 'layers': 212}])
        self.assertEqual(result['stats'], {'count': 1, 'finished': 1, 'failed': 0, 'stopped': 0, 'interrupted': 0,
                                           'print_seconds': ended - started})

    def test_failed_and_stopped_prints(self):
        self.report('RUNNING', kind='klipper', progress_percent=30)
        self.report('FAILED', kind='klipper', progress_percent=31)
        self.report('PAUSE', kind='octoprint', job_name='Calibration cube')
        self.report('IDLE', kind='octoprint')
        self.report('IDLE', kind='octoprint')
        prints = self.prints()
        self.assertEqual([(item['kind'], item['result'], item['progress'], item['duration_seconds'], item['name'])
                          for item in prints],
                         [('octoprint', 'stopped', None, 10, 'Calibration cube'), ('klipper', 'failed', 31, 10, None)])
        stats = self.history.list()['stats']
        self.assertEqual(stats, {'count': 2, 'finished': 0, 'failed': 1, 'stopped': 1, 'interrupted': 0,
                                 'print_seconds': 20})

    def test_only_fresh_snapshots_count(self):
        self.status.set('RUNNING', fresh=False)
        self.history.observe()
        self.assertEqual(self.prints(), [])
        self.report('RUNNING', progress_percent=10)
        self.status.set('FINISH', fresh=False, progress_percent=100, job_name='Ghost')
        self.history.observe()
        self.status.value = {}
        self.history.observe()
        item = self.prints()[0]
        self.assertEqual((item['result'], item['progress'], item['name']), (None, 10, None))

    def test_states_that_do_not_start_a_print(self):
        for state in ('IDLE', 'FINISH', 'FAILED', 'SLICING', None):
            self.report(state)
        self.assertEqual(self.prints(), [])

    def test_ids_use_utc_start_time_and_count_up_on_collision(self):
        for _ in range(3):
            self.report('RUNNING', seconds=0)
            self.report('IDLE', seconds=0)
        self.assertEqual([item['id'] for item in self.prints()],
                         ['20251110-142233-3', '20251110-142233-2', '20251110-142233'])
        self.assertTrue(all(re.fullmatch(r'[0-9]{8}-[0-9]{6}(-[0-9]+)?', item['id']) for item in self.prints()))

    def test_keeps_the_newest_two_hundred(self):
        for _ in range(MAX_PRINTS + 5):
            self.report('RUNNING', seconds=1)
            self.report('FINISH', seconds=1)
        prints = self.prints()
        self.assertEqual(len(prints), MAX_PRINTS)
        self.assertEqual(prints[-1]['started'], START + 10)
        self.assertEqual(len(self.saved()), MAX_PRINTS)
        self.report('RUNNING')
        self.assertEqual(len(self.prints()), MAX_PRINTS)
        self.assertIsNone(self.prints()[0]['result'])
        self.assertEqual(self.history.list()['stats']['count'], MAX_PRINTS - 1)


class InterruptionTests(HistoryCase):
    def test_no_fresh_status_for_thirty_minutes(self):
        self.report('RUNNING', job_name='Benchy')
        last_seen = int(self.clock.now)
        self.report('RUNNING', seconds=0)
        self.clock.now = last_seen + STALE_SECONDS - 1
        self.status.set('RUNNING', fresh=False)
        self.history.observe()
        self.assertIsNone(self.prints()[0]['result'])
        self.clock.now = last_seen + STALE_SECONDS
        # Listing alone ends it, without waiting for a printer report.
        item = self.prints()[0]
        self.assertEqual((item['result'], item['ended'], item['duration_seconds']), ('interrupted', last_seen, 10))
        self.assertEqual(self.saved()[0]['result'], 'interrupted')
        # The printer coming back mid-print starts a new entry.
        self.report('RUNNING', progress_percent=60)
        prints = self.prints()
        self.assertEqual([(item['result'], item['progress']) for item in prints], [(None, 60), ('interrupted', None)])
        self.assertEqual(self.history.list()['stats']['interrupted'], 1)

    def test_printer_settings_change_or_removal(self):
        self.report('RUNNING')
        last_seen = int(self.clock.now)
        self.report('PAUSE', seconds=100)
        self.history.printer_changed()
        item = self.prints()[0]
        self.assertEqual((item['result'], item['ended'], item['duration_seconds']), ('interrupted', last_seen, 10))
        self.history.printer_changed()
        self.assertEqual(len(self.prints()), 1)

    def test_startup_ends_an_active_print_at_its_last_seen_time(self):
        self.report('RUNNING', seconds=5, job_name='Benchy', total_layers=50)
        for _ in range(30):
            self.report('RUNNING', seconds=5, job_name='Benchy', total_layers=50)
        # Last-seen alone is saved at most once a minute, so the saved value trails slightly.
        saved_seen = self.saved()[0]['last_seen']
        self.assertGreaterEqual(saved_seen, START + 150 - print_history.SEEN_SAVE_SECONDS)
        restarted = self.make()
        item = restarted.list()['prints'][0]
        self.assertEqual((item['result'], item['ended'], item['name'], item['layers']),
                         ('interrupted', saved_seen, 'Benchy', 50))
        self.assertEqual(self.saved()[0]['result'], 'interrupted')
        # It isn't resumed: a fresh RUNNING report starts a new print.
        self.status.set('RUNNING')
        restarted.observe()
        self.assertEqual([item['result'] for item in restarted.list()['prints']], [None, 'interrupted'])


class StorageTests(HistoryCase):
    def test_saved_privately_and_atomically_after_each_change(self):
        self.report('RUNNING', job_name='Benchy')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['prints.json'])
        self.assertEqual(self.saved()[0]['name'], 'Benchy')
        self.report('RUNNING', progress_percent=5)
        self.assertEqual(self.saved()[0]['progress'], 5)
        self.report('FINISH')
        self.assertEqual(self.saved()[0]['result'], 'finished')
        self.assertEqual(self.make().list(), self.history.list())

    def test_damaged_file_is_kept_aside_and_history_restarts(self):
        for content in ('{not json', '[]', '{"prints": [{"id": "x"}]}', '{"prints": "none"}'):
            with self.subTest(content=content):
                for path in self.root.iterdir():
                    path.unlink()
                self.path.write_text(content)
                with self.assertLogs(level='WARNING'):
                    history = self.make()
                self.assertEqual(history.list()['prints'], [])
                backups = [path for path in self.root.iterdir() if path.name.startswith('prints.json.corrupt-')]
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), content)

    def test_clear_keeps_the_active_print(self):
        self.report('RUNNING')
        self.report('FINISH')
        self.report('RUNNING', job_name='Current')
        self.history.clear()
        prints = self.prints()
        self.assertEqual([(item['name'], item['result']) for item in prints], [('Current', None)])
        self.assertEqual(len(self.saved()), 1)
        self.assertEqual(self.history.list()['stats']['count'], 0)
        self.report('FINISH')
        self.history.clear()
        self.assertEqual(self.history.list()['prints'], [])

    def test_unwritable_storage_keeps_recording_in_memory(self):
        folder = self.root / 'read-only'
        folder.mkdir()
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        history = PrintHistory(folder / 'prints.json', self.status, clock=self.clock)
        self.status.set('RUNNING')
        with self.assertLogs(level='WARNING'):
            history.observe()
        self.assertEqual(len(history.list()['prints']), 1)
        with self.assertRaises(OSError):
            history.clear()


class MonitorTests(unittest.TestCase):
    """The history follows a real PrinterMonitor, fed by its listeners."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        scopes = root / 'scopes.json'
        scopes.write_text(json.dumps([['eth0', '192.168.1.0/24']]))
        self.fake = FakeHTTP()
        self.addCleanup(self.fake.close)
        self.monitor = print_status.PrinterMonitor(root / 'printer.json', LANPolicy(scopes), threading.Event(),
                                                   local_discovery=lambda: [])
        self.clock = FakeClock()
        self.history = PrintHistory(storage_path(root / 'state.json'), self.monitor.snapshot, clock=self.clock)
        self.monitor.listeners.append(self.history.observe)
        self.monitor.settings_listeners.append(self.history.printer_changed)

    def poll(self, **values):
        self.fake.route(print_status.MOONRAKER_QUERY, moonraker(**values))
        self.assertEqual(self.monitor.poll(), 'connected')
        self.clock.now += 60

    def test_klipper_print_is_recorded(self):
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.poll(state='standby')
        self.poll(state='printing', duration=0, filename='Benchy.gcode')
        self.poll(state='printing', duration=60, progress=0.5, layer=10, total=20, filename='Benchy.gcode')
        self.poll(state='complete', duration=120, progress=1.0, layer=20, total=20, filename='Benchy.gcode')
        item = self.history.list()['prints'][0]
        self.assertEqual({key: item[key] for key in ('name', 'kind', 'result', 'progress', 'layers', 'duration_seconds')},
                         {'name': 'Benchy', 'kind': 'klipper', 'result': 'finished', 'progress': 100, 'layers': 20,
                          'duration_seconds': 120})

    def test_changing_or_forgetting_the_printer_interrupts_the_print(self):
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.poll(state='printing', duration=5)
        self.monitor.configure({'kind': 'klipper', 'url': self.fake.url})
        self.assertEqual(self.history.list()['prints'][0]['result'], 'interrupted')
        self.poll(state='printing', duration=5)
        self.monitor.forget()
        self.assertEqual([item['result'] for item in self.history.list()['prints']], ['interrupted', 'interrupted'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = PairingState(root / 'state.json')
        self.token = self.state.pair(self.state.open_pairing())[1]['token']
        self.clock = FakeClock()
        self.status = FakeStatus()
        self.history = PrintHistory(storage_path(root / 'state.json'), self.status, clock=self.clock)
        self.server = CameraServer(('127.0.0.1', 0), self.state, FrameBuffer(), demo=True)
        self.server.history = self.history
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)
        self.addCleanup(stop)

    def request(self, method, path, token=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Authorization': 'Bearer ' + (self.token if token is None else token)} if token != '' else {}
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def report(self, state, seconds=10, **values):
        self.status.set(state, **values)
        self.history.observe()
        self.clock.now += seconds

    def test_authentication_is_required(self):
        self.report('RUNNING')
        self.report('FINISH')
        for method in ('GET', 'DELETE'):
            for token in ('', 'wrong'):
                with self.subTest(method=method, token=token):
                    self.assertEqual(self.request(method, '/api/prints', token),
                                     (401, {'error': 'Pair this camera before viewing.'}))
        self.assertEqual(len(self.history.list()['prints']), 1)

    def test_list_shape(self):
        self.report('RUNNING', job_name='Benchy', progress_percent=10, total_layers=212)
        self.report('FINISH', seconds=5447 - 10)
        self.report('RUNNING', kind='klipper')
        code, body = self.request('GET', '/api/prints')
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {'prints', 'stats'})
        self.assertEqual(body['prints'][1], {'id': '20251110-142233', 'name': 'Benchy', 'kind': 'bambu',
                                             'started': START, 'ended': START + 10, 'result': 'finished',
                                             'duration_seconds': 10, 'progress': 100, 'layers': 212})
        self.assertEqual(body['prints'][0], {'id': '20251110-155320', 'name': None, 'kind': 'klipper',
                                             'started': START + 5447, 'ended': None, 'result': None,
                                             'duration_seconds': None, 'progress': None, 'layers': None})
        self.assertEqual(body['stats'], {'count': 1, 'finished': 1, 'failed': 0, 'stopped': 0, 'interrupted': 0,
                                         'print_seconds': 10})

    def test_delete_keeps_the_active_print(self):
        self.report('RUNNING')
        self.report('FAILED')
        self.report('PREPARE')
        self.assertEqual(self.request('DELETE', '/api/prints'), (200, {'deleted': True}))
        code, body = self.request('GET', '/api/prints')
        self.assertEqual([item['result'] for item in body['prints']], [None])
        self.assertEqual(body['stats']['count'], 0)

    def test_unavailable_without_history(self):
        self.server.history = None
        self.assertEqual(self.request('GET', '/api/prints'), (503, {'error': 'Print history is unavailable.'}))
        self.assertEqual(self.request('DELETE', '/api/prints'), (503, {'error': 'Print history is unavailable.'}))

    def test_storage_failure_on_delete(self):
        self.report('RUNNING')
        self.report('IDLE')
        self.history.path.unlink()
        self.history.path.mkdir()
        self.assertEqual(self.request('DELETE', '/api/prints'),
                         (500, {'error': 'Could not save print history on the Pi. Check its storage.'}))
        self.assertEqual(len(self.history.list()['prints']), 1)


if __name__ == '__main__':
    unittest.main()
