import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control
import timelapse
from agent import CameraServer, FrameBuffer, PairingState
from timelapse import (TimelapseConflict, TimelapseRecorder, UnknownTimelapse, frame_reader, printer_status,
                       storage_paths, valid_id, validated_settings)

DAY = 24 * 3600


class FakeClock:
    def __init__(self, now=1762784553.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeFrames:
    def __init__(self):
        self.count = 0
        self.available = True

    def __call__(self):
        if not self.available:
            return None
        self.count += 1
        return b'\xff\xd8frame-%04d\xff\xd9' % self.count


class FakePrinter:
    def __init__(self):
        self.status = dict(timelapse.NO_PRINTER)

    def set(self, **values):
        self.status = {'available': True, 'state': None, 'layer': None, 'job_name': None, **values}

    def __call__(self):
        return dict(self.status)


class RecorderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory, self.settings_path = storage_paths(self.root / 'state.json')
        self.clock = FakeClock()
        self.frames = FakeFrames()
        self.printer = FakePrinter()
        self.free = 10 * 1024 ** 3
        self.recorder = self.make()

    def make(self):
        return TimelapseRecorder(self.directory, self.settings_path, self.frames, self.printer,
                                 clock=self.clock, monotonic=self.clock, disk_free=lambda: self.free)

    def configure(self, **changes):
        return self.recorder.configure({**timelapse.DEFAULT_SETTINGS, **changes})

    def current(self):
        return self.recorder.state()['recording']

    def item(self, identifier):
        return next(item for item in self.recorder.list()['timelapses'] if item['id'] == identifier)

    def advance(self, seconds):
        self.clock.now += seconds
        self.recorder.poll()


class SettingsTests(RecorderTestCase):
    def test_off_by_default_and_reports_storage(self):
        state = self.recorder.state()
        self.assertEqual(state, {'enabled': False, 'mode': 'layer', 'interval_seconds': 10, 'layer_available': False,
                                 'recording': None, 'storage': {'used_bytes': 0, 'limit_bytes': 1073741824}})
        self.printer.set(state='RUNNING', layer=3)
        self.recorder.poll()
        self.printer.set(state='RUNNING', layer=4)
        self.recorder.poll()
        self.assertEqual(self.recorder.list(), {'timelapses': []})
        self.assertTrue(self.recorder.state()['layer_available'])

    def test_validation(self):
        valid = {'enabled': True, 'mode': 'interval', 'interval_seconds': 15}
        self.assertEqual(validated_settings(valid), valid)
        for interval in (2, 600):
            validated_settings({**valid, 'interval_seconds': interval})
        invalid = [None, [], {}, {'enabled': True, 'mode': 'layer'}, {**valid, 'extra': 1},
                   {**valid, 'enabled': 1}, {**valid, 'enabled': 'true'}, {**valid, 'mode': 'video'},
                   {**valid, 'mode': ['layer']}, {**valid, 'interval_seconds': 1}, {**valid, 'interval_seconds': 601},
                   {**valid, 'interval_seconds': 10.0}, {**valid, 'interval_seconds': True},
                   {**valid, 'interval_seconds': '10'}]
        for value in invalid:
            with self.assertRaises(ValueError, msg=value):
                validated_settings(value)

    def test_settings_persist_privately_and_bad_files_fall_back(self):
        state = self.configure(enabled=True, mode='interval', interval_seconds=30)
        self.assertEqual((state['enabled'], state['mode'], state['interval_seconds']), (True, 'interval', 30))
        self.assertEqual(self.settings_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.make().settings, {'enabled': True, 'mode': 'interval', 'interval_seconds': 30})
        for damaged in ('{not json', '{"enabled": true}', '[]'):
            self.settings_path.write_text(damaged)
            self.assertEqual(self.make().settings, timelapse.DEFAULT_SETTINGS)

    def test_mode_change_does_not_affect_recording(self):
        self.recorder.start_recording()
        self.assertEqual(self.current()['trigger'], 'interval')
        self.configure(mode='layer')
        self.assertEqual(self.current()['trigger'], 'interval')


class RecordingTests(RecorderTestCase):
    def test_manual_start_and_stop(self):
        state = self.recorder.start_recording()
        identifier = state['recording']['id']
        self.assertEqual(identifier, '20251110-142233')
        self.assertEqual(state['recording'], {'id': identifier, 'started': 1762784553, 'frames': 1,
                                              'trigger': 'interval', 'manual': True})
        with self.assertRaisesRegex(TimelapseConflict, 'Already recording.'):
            self.recorder.start_recording()
        self.assertEqual(self.item(identifier)['stop_reason'], None)
        self.assertTrue(self.item(identifier)['recording'])
        self.clock.now += 5
        state = self.recorder.stop_recording()
        self.assertIsNone(state['recording'])
        with self.assertRaisesRegex(TimelapseConflict, 'Not recording.'):
            self.recorder.stop_recording()
        item = self.item(identifier)
        folder = self.directory / identifier
        frames = [folder / '000001.jpg', folder / '000002.jpg']
        self.assertEqual(item, {'id': identifier, 'name': None, 'started': 1762784553, 'ended': 1762784558,
                                'frames': 2, 'bytes': sum(path.stat().st_size for path in frames),
                                'recording': False, 'stop_reason': 'stopped'})
        self.assertEqual(json.loads((folder / 'meta.json').read_text()), item)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['000001.jpg', '000002.jpg', 'meta.json'])
        self.assertEqual(self.recorder.state()['storage']['used_bytes'], item['bytes'])
        self.assertEqual(self.recorder.poster(identifier), frames[1].read_bytes())
        self.assertEqual(self.recorder.frame_paths(identifier), frames)

    def test_ids_get_a_suffix_on_collision_and_list_is_newest_first(self):
        first = self.recorder.start_recording()['recording']['id']
        self.recorder.stop_recording()
        second = self.recorder.start_recording()['recording']['id']
        self.recorder.stop_recording()
        self.clock.now += 1
        third = self.recorder.start_recording()['recording']['id']
        self.assertEqual((first, second, third), ('20251110-142233', '20251110-142233-2', '20251110-142234'))
        self.assertEqual([item['id'] for item in self.recorder.list()['timelapses']], [third, second, first])

    def test_layer_trigger_captures_once_per_layer_increase(self):
        self.printer.set(state='RUNNING', layer=5, job_name='Benchy')
        recording = self.recorder.start_recording()['recording']
        self.assertEqual((recording['trigger'], recording['frames']), ('layer', 1))
        self.assertEqual(self.item(recording['id'])['name'], 'Benchy')
        self.advance(600)
        self.assertEqual(self.current()['frames'], 1)
        self.printer.set(state='RUNNING', layer=6)
        self.advance(1)
        self.advance(1)
        self.assertEqual(self.current()['frames'], 2)
        self.printer.set(state='RUNNING', layer=9)
        self.advance(1)
        self.assertEqual(self.current()['frames'], 3)
        self.printer.status = dict(timelapse.NO_PRINTER)
        self.advance(60)
        self.printer.set(state='RUNNING', layer=9)
        self.advance(1)
        self.assertEqual(self.current()['frames'], 3)
        self.recorder.stop_recording()
        self.assertEqual(self.item(recording['id'])['frames'], 4)

    def test_layer_mode_without_printer_falls_back_to_interval(self):
        self.assertEqual(self.recorder.state()['layer_available'], False)
        self.assertEqual(self.recorder.start_recording()['recording']['trigger'], 'interval')

    def test_interval_trigger_and_pause(self):
        self.configure(mode='interval', interval_seconds=10)
        self.printer.set(state='RUNNING', layer=1)
        self.assertEqual(self.recorder.start_recording()['recording']['trigger'], 'interval')
        self.advance(9)
        self.assertEqual(self.current()['frames'], 1)
        self.advance(1)
        self.assertEqual(self.current()['frames'], 2)
        self.printer.set(state='PAUSE', layer=1)
        for _ in range(5):
            self.advance(10)
        self.assertEqual(self.current()['frames'], 2)
        self.printer.set(state='RUNNING', layer=1)
        self.advance(1)
        self.assertEqual(self.current()['frames'], 3)
        self.advance(5)
        self.assertEqual(self.current()['frames'], 3)

    def test_missing_camera_frames_are_skipped(self):
        self.frames.available = False
        identifier = self.recorder.start_recording()['recording']['id']
        self.advance(30)
        self.assertEqual(self.current()['frames'], 0)
        with self.assertRaises(UnknownTimelapse):
            self.recorder.poster('../' + identifier)
        self.assertIsNone(self.recorder.poster(identifier))

    def test_frame_reader_ignores_stale_frames(self):
        buffer = FrameBuffer()
        clock = FakeClock(100)
        read = frame_reader(buffer, clock=clock)
        self.assertIsNone(read())
        buffer.write(b'\xff\xd8x\xff\xd9')
        clock.now = buffer.updated + 1
        self.assertEqual(read(), b'\xff\xd8x\xff\xd9')
        clock.now = buffer.updated + 4
        self.assertIsNone(read())

    def test_printer_status_needs_fresh_reports(self):
        self.assertFalse(printer_status(None)['available'])
        monitor = Mock()
        monitor.snapshot.return_value = {'fresh': False, 'fields': {'state': 'RUNNING', 'current_layer': 3}}
        self.assertEqual(printer_status(monitor), timelapse.NO_PRINTER)
        monitor.snapshot.return_value = {'fresh': True, 'fields': {'state': 'RUNNING', 'current_layer': 3, 'job_name': 'Cube'}}
        self.assertEqual(printer_status(monitor), {'available': True, 'state': 'RUNNING', 'layer': 3, 'job_name': 'Cube'})


class AutoRecordTests(RecorderTestCase):
    def setUp(self):
        super().setUp()
        self.configure(enabled=True)

    def report(self, state, layer=1, job_name=None):
        self.printer.set(state=state, layer=layer, job_name=job_name)
        self.advance(1)

    def test_starts_on_running_and_stops_on_finish(self):
        self.report('IDLE')
        self.report('PREPARE')
        self.assertIsNone(self.current())
        self.report('RUNNING', layer=1)
        recording = self.current()
        self.assertEqual((recording['manual'], recording['trigger'], recording['frames']), (False, 'layer', 1))
        self.report('RUNNING', layer=2, job_name='Benchy')
        self.report('RUNNING', layer=3, job_name='Other')
        self.report('FINISH', layer=3)
        self.assertIsNone(self.current())
        item = self.item(recording['id'])
        self.assertEqual((item['stop_reason'], item['frames'], item['name']), ('finished', 4, 'Benchy'))
        self.assertEqual(item['ended'], int(self.clock.now))

    def test_failed_and_idle_also_stop(self):
        for ending in ('FAILED', 'IDLE'):
            self.report('PREPARE')
            self.report('RUNNING')
            identifier = self.current()['id']
            self.report(ending)
            self.assertEqual(self.item(identifier)['stop_reason'], 'finished')

    def test_pause_does_not_split_the_recording(self):
        self.report('PREPARE')
        self.report('RUNNING')
        identifier = self.current()['id']
        self.report('PAUSE')
        self.report('RUNNING')
        self.assertEqual(self.current()['id'], identifier)
        self.assertEqual(len(self.recorder.list()['timelapses']), 1)

    def test_needs_printer_status_and_a_state_change(self):
        self.report('RUNNING')
        self.assertIsNone(self.current(), 'first report after start is not a change to RUNNING')
        self.printer.status = dict(timelapse.NO_PRINTER)
        self.advance(1)
        self.report('RUNNING')
        self.assertIsNone(self.current())
        self.configure(enabled=False)
        self.report('IDLE')
        self.report('RUNNING')
        self.assertIsNone(self.current())

    def test_manual_recording_ignores_printer_state(self):
        self.report('IDLE')
        self.recorder.start_recording()
        self.report('RUNNING')
        self.report('FINISH')
        self.assertTrue(self.current()['manual'])
        self.recorder.stop_recording()

    def test_auto_recording_continues_if_auto_record_is_turned_off(self):
        self.report('IDLE')
        self.report('RUNNING')
        identifier = self.current()['id']
        self.configure(enabled=False)
        self.report('RUNNING', layer=2)
        self.report('FINISH')
        self.assertEqual(self.item(identifier)['stop_reason'], 'finished')

    def test_monitor_reports_wake_the_recorder(self):
        from print_status import PrinterMonitor
        monitor = PrinterMonitor(self.root / 'printer.json', None, threading.Event())
        woken = []
        monitor.listeners.append(lambda: woken.append(True))
        monitor.listeners.append(Mock(side_effect=RuntimeError()))
        monitor.notify()
        self.assertEqual(woken, [True])
        monitor.listeners = [self.recorder.wake]
        monitor.notify()
        self.assertTrue(self.recorder.woken.is_set())


class LimitTests(RecorderTestCase):
    def finished(self, age_days, size=100):
        """A finished timelapse that ended `age_days` ago."""
        started = int(self.clock.now - age_days * DAY)
        identifier = self.recorder._new_id(started)
        frame = b'x' * size
        (self.directory / identifier / '000001.jpg').write_bytes(frame)
        item = {'id': identifier, 'name': None, 'started': started, 'ended': started + 60, 'frames': 1,
                'bytes': size, 'recording': False, 'stop_reason': 'stopped'}
        with self.recorder.lock:
            self.recorder.items[identifier] = item
            self.recorder._write_meta(item)
        return identifier

    def test_frame_limit(self):
        self.configure(mode='interval', interval_seconds=2)
        with patch('timelapse.MAX_FRAMES', 5):
            identifier = self.recorder.start_recording()['recording']['id']
            for _ in range(10):
                self.advance(2)
        self.assertIsNone(self.current())
        item = self.item(identifier)
        self.assertEqual((item['frames'], item['stop_reason']), (5, 'frame_limit'))
        self.assertFalse((self.directory / identifier / '000006.jpg').exists())

    def test_storage_cap_deletes_oldest_finished_then_stops(self):
        old = self.finished(3)
        newer = self.finished(2)
        frame_size = len(FakeFrames()())
        limit = 200 + frame_size
        self.configure(mode='interval', interval_seconds=2)
        with patch('timelapse.STORAGE_LIMIT_BYTES', limit):
            identifier = self.recorder.start_recording()['recording']['id']
            ids = [item['id'] for item in self.recorder.list()['timelapses']]
            self.assertIn(old, ids, 'still under the cap')
            self.advance(2)
            ids = [item['id'] for item in self.recorder.list()['timelapses']]
            self.assertNotIn(old, ids)
            self.assertFalse((self.directory / old).exists())
            self.assertIn(newer, ids)
            for _ in range(40):
                self.advance(2)
            item = self.item(identifier)
            self.assertEqual(item['stop_reason'], 'storage_full')
            self.assertEqual(item['frames'], limit // frame_size)
            self.assertFalse((self.directory / newer).exists())
            self.assertLessEqual(self.recorder.state()['storage']['used_bytes'], limit)

    def test_over_cap_is_cleaned_before_a_new_recording(self):
        old = self.finished(3, size=200)
        with patch('timelapse.STORAGE_LIMIT_BYTES', 150):
            self.recorder.start_recording()
        self.assertNotIn(old, [item['id'] for item in self.recorder.list()['timelapses']])

    def test_low_disk_space_stops(self):
        identifier = self.recorder.start_recording()['recording']['id']
        self.free = 500 * 1024 ** 2
        self.advance(10)
        item = self.item(identifier)
        self.assertEqual((item['frames'], item['stop_reason']), (1, 'storage_full'))

    def test_retention(self):
        expired = self.finished(31)
        kept = self.finished(29)
        self.recorder.poll()
        ids = [item['id'] for item in self.recorder.list()['timelapses']]
        self.assertEqual(ids, [kept])
        self.assertFalse((self.directory / expired).exists())
        recording = self.recorder.start_recording()['recording']['id']
        self.clock.now += 40 * DAY
        self.recorder.poll()
        self.assertEqual([item['id'] for item in self.recorder.list()['timelapses']], [recording])
        restarted = self.make()
        self.assertEqual(list(restarted.items), [recording])

    def test_shutdown_and_crash_finish_as_interrupted(self):
        self.configure(mode='interval', interval_seconds=10)
        identifier = self.recorder.start_recording()['recording']['id']
        self.advance(10)
        last_frame = int(self.clock.now)
        self.clock.now += 7
        self.recorder.close()
        item = self.item(identifier)
        self.assertEqual((item['stop_reason'], item['ended'], item['frames']), ('interrupted', last_frame, 2))
        self.recorder.poll()
        self.assertEqual(self.item(identifier)['frames'], 2)

        crashed = self.recorder._new_id(self.clock.now)
        folder = self.directory / crashed
        for index in (1, 2):
            (folder / f'{index:06d}.jpg').write_bytes(b'abc')
        os.utime(folder / '000002.jpg', (1762790000, 1762790000))
        (folder / 'meta.json').write_text(json.dumps({'id': crashed, 'name': 'Cube', 'started': 1762789000,
                                                      'ended': None, 'frames': 1, 'bytes': 3, 'recording': True,
                                                      'stop_reason': None}))
        restarted = self.make()
        self.assertIsNone(restarted.state()['recording'])
        item = restarted.items[crashed]
        self.assertEqual(item, {'id': crashed, 'name': 'Cube', 'started': 1762789000, 'ended': 1762790000,
                                'frames': 2, 'bytes': 6, 'recording': False, 'stop_reason': 'interrupted'})
        self.assertEqual(json.loads((folder / 'meta.json').read_text()), item)
        self.assertEqual(restarted.items[identifier]['stop_reason'], 'interrupted')


class IdTests(RecorderTestCase):
    def test_pattern(self):
        for value in ('20261110-142233', '20261110-142233-2', '20261110-142233-999'):
            self.assertTrue(valid_id(value), value)
        for value in ('', None, 20261110, '..', '../20261110-142233', '20261110-142233/../..', '20261110-142233-1000',
                      '20261110-142233\n', '20261110-142233-', '2026111-142233', '20261110_142233',
                      '２０２６１１１０-１４２２３３', '20261110-142233/meta.json', '%2e%2e'):
            self.assertFalse(valid_id(value), value)

    def test_traversal_never_reaches_the_disk(self):
        secret = self.root / 'secret'
        secret.mkdir()
        (secret / 'meta.json').write_text('{}')
        for value in ('../secret', '..', '/etc', '20261110-142233/../../secret'):
            for call in (self.recorder.poster, self.recorder.frame_paths, self.recorder.delete):
                with self.assertRaises(UnknownTimelapse):
                    call(value)
        self.assertTrue(secret.exists())

    def test_scan_ignores_unexpected_entries(self):
        (self.directory / 'notes').mkdir()
        (self.directory / '20261110-142233').write_text('a file')
        os.symlink(self.root, self.directory / '20261110-142234')
        self.assertEqual(self.make().items, {})


class UninstallStorageTests(unittest.TestCase):
    def test_default_storage_is_removed_by_uninstall(self):
        directory, settings = storage_paths('/var/lib/print-monitor/state.json')
        self.assertEqual(directory, Path('/var/lib/print-monitor/timelapses'))
        self.assertEqual(settings, Path('/var/lib/print-monitor/timelapse.json'))
        self.assertIn('/var/lib/print-monitor', control.INSTALLED_PATHS)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frame = root / 'var/lib/print-monitor/timelapses/20261110-142233/000001.jpg'
            frame.parent.mkdir(parents=True)
            frame.write_bytes(b'jpeg')
            (root / 'var/lib/print-monitor/timelapse.json').write_text('{}')
            runner = Mock(side_effect=lambda command, **_: subprocess.CompletedProcess(command, 0, stdout=''))
            with patch('control.shutil.which', return_value=None), patch('builtins.print'):
                control.uninstall(root, runner)
            self.assertFalse((root / 'var/lib/print-monitor').exists())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = PairingState(root / 'state.json')
        code = self.state.open_pairing()
        self.token = self.state.pair(code)[1]['token']
        self.clock = FakeClock()
        self.frames = FakeFrames()
        self.recorder = TimelapseRecorder(*storage_paths(root / 'state.json'), self.frames, clock=self.clock,
                                          monotonic=self.clock, disk_free=lambda: 10 * 1024 ** 3)
        self.server = CameraServer(('127.0.0.1', 0), self.state, FrameBuffer(), demo=True)
        self.server.timelapse = self.recorder
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method='GET', body=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {'Authorization': 'Bearer ' + self.token} if token else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data, response

    def json(self, path, method='GET', body=None):
        status, data, _ = self.request(path, method, body)
        return status, json.loads(data)

    def recorded(self, frames=3):
        identifier = self.json('/api/timelapse/start', 'POST')[1]['recording']['id']
        for _ in range(frames - 2):
            self.clock.now += 10
            self.recorder.poll()
        self.json('/api/timelapse/stop', 'POST')
        return identifier

    def test_every_endpoint_requires_authentication(self):
        identifier = self.recorded()
        routes = [('GET', '/api/timelapse'), ('POST', '/api/timelapse'), ('POST', '/api/timelapse/start'),
                  ('POST', '/api/timelapse/stop'), ('GET', '/api/timelapses'),
                  ('GET', f'/api/timelapses/{identifier}/poster'), ('GET', f'/api/timelapses/{identifier}/frames'),
                  ('DELETE', f'/api/timelapses/{identifier}'), ('GET', '/api/timelapses/../../etc/passwd')]
        for method, path in routes:
            self.assertEqual(self.request(path, method, token=False)[0], 401, path)
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
            connection.request(method, path, headers={'Authorization': 'Bearer wrong'})
            self.assertEqual(connection.getresponse().status, 401, path)
            connection.close()
        self.assertEqual(len(self.recorder.list()['timelapses']), 1)
        self.assertIsNone(self.recorder.state()['recording'])

    def test_settings_start_stop_and_list(self):
        status, body = self.json('/api/timelapse')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'enabled': False, 'mode': 'layer', 'interval_seconds': 10, 'layer_available': False,
                                'recording': None, 'storage': {'used_bytes': 0, 'limit_bytes': 1073741824}})
        settings = {'enabled': True, 'mode': 'interval', 'interval_seconds': 15}
        status, body = self.json('/api/timelapse', 'POST', json.dumps(settings))
        self.assertEqual((status, body['enabled'], body['mode'], body['interval_seconds']), (200, True, 'interval', 15))
        for invalid in ('not json', '[]', '{}', json.dumps({**settings, 'interval_seconds': 1}),
                        json.dumps({**settings, 'other': 1}), 'x' * 2048):
            status, body = self.json('/api/timelapse', 'POST', invalid)
            self.assertEqual(status, 400, invalid)
            self.assertIn('error', body)
        status, body = self.json('/api/timelapse/start', 'POST')
        self.assertEqual(status, 200)
        self.assertEqual(body['recording']['trigger'], 'interval')
        self.assertTrue(body['recording']['manual'])
        self.assertEqual(self.json('/api/timelapse/start', 'POST'), (409, {'error': 'Already recording.'}))
        status, body = self.json('/api/timelapses')
        self.assertEqual((status, body['timelapses'][0]['recording'], body['timelapses'][0]['stop_reason']), (200, True, None))
        self.assertEqual(self.json('/api/timelapse/stop', 'POST')[0], 200)
        self.assertEqual(self.json('/api/timelapse/stop', 'POST'), (409, {'error': 'Not recording.'}))
        status, body = self.json('/api/timelapses')
        item = body['timelapses'][0]
        self.assertEqual(set(item), {'id', 'name', 'started', 'ended', 'frames', 'bytes', 'recording', 'stop_reason'})
        self.assertEqual((item['frames'], item['recording'], item['stop_reason']), (2, False, 'stopped'))

    def test_poster_and_frames(self):
        identifier = self.recorded(frames=3)
        frames = [path.read_bytes() for path in self.recorder.frame_paths(identifier)]
        self.assertEqual(len(frames), 3)
        status, data, response = self.request(f'/api/timelapses/{identifier}/poster')
        self.assertEqual((status, data, response.getheader('Content-Type')), (200, frames[-1], 'image/jpeg'))
        status, data, response = self.request(f'/api/timelapses/{identifier}/frames')
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'multipart/x-mixed-replace; boundary=FRAME')
        self.assertEqual(response.getheader('X-Printer-Pal-Frames'), '3')
        expected = b''.join(b'--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(frame)).encode()
                            + b'\r\n\r\n' + frame + b'\r\n' for frame in frames) + b'--FRAME--\r\n'
        self.assertEqual(data, expected)

    def test_frames_of_a_recording_in_progress_and_empty_poster(self):
        self.frames.available = False
        identifier = self.json('/api/timelapse/start', 'POST')[1]['recording']['id']
        self.assertEqual(self.request(f'/api/timelapses/{identifier}/poster')[0], 404)
        status, data, response = self.request(f'/api/timelapses/{identifier}/frames')
        self.assertEqual((status, data, response.getheader('X-Printer-Pal-Frames')), (200, b'--FRAME--\r\n', '0'))
        self.frames.available = True
        self.clock.now += 10
        self.recorder.poll()
        _, _, response = self.request(f'/api/timelapses/{identifier}/frames')
        self.assertEqual(response.getheader('X-Printer-Pal-Frames'), '1')

    def test_unknown_and_invalid_ids_return_404(self):
        identifier = self.recorded()
        for path in ('/api/timelapses/20200101-000000/poster', '/api/timelapses/20200101-000000/frames',
                     '/api/timelapses/../state.json', '/api/timelapses/..%2F..%2Fstate.json/poster',
                     '/api/timelapses/%2e%2e/frames', f'/api/timelapses/{identifier}-1000/poster',
                     f'/api/timelapses/{identifier}/meta.json', f'/api/timelapses/{identifier}',
                     f'/api/timelapses/{identifier}/poster/extra', '/api/timelapse/other'):
            self.assertEqual(self.request(path)[0], 404, path)
        for path in ('/api/timelapses/20200101-000000', '/api/timelapses/..', f'/api/timelapses/{identifier}/poster'):
            self.assertEqual(self.request(path, 'DELETE')[0], 404, path)

    def test_delete(self):
        identifier = self.recorded()
        recording = self.json('/api/timelapse/start', 'POST')[1]['recording']['id']
        self.assertEqual(self.json(f'/api/timelapses/{recording}', 'DELETE'), (409, {'error': 'Stop the recording first.'}))
        self.assertEqual(self.json(f'/api/timelapses/{identifier}', 'DELETE'), (200, {'deleted': True}))
        self.assertFalse((self.recorder.directory / identifier).exists())
        self.assertEqual(self.request(f'/api/timelapses/{identifier}', 'DELETE')[0], 404)
        self.assertEqual([item['id'] for item in self.json('/api/timelapses')[1]['timelapses']], [recording])

    def test_unavailable_without_a_recorder(self):
        self.server.timelapse = None
        self.assertEqual(self.json('/api/timelapse')[0], 503)
        self.assertEqual(self.request('/api/timelapse', token=False)[0], 401)


if __name__ == '__main__':
    unittest.main()
