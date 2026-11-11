"""On-device timelapses. Frames stay on the Pi until a paired phone deletes them or they expire."""
import calendar
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time

ID_PATTERN = re.compile(r'[0-9]{8}-[0-9]{6}(-[0-9]{1,3})?')
MAX_FRAMES = 3000
STORAGE_LIMIT_BYTES = 1024 ** 3
MIN_FREE_BYTES = 500 * 1024 ** 2
RETENTION_SECONDS = 30 * 24 * 3600
FRAME_MAX_AGE = 3
DEFAULT_SETTINGS = {'enabled': False, 'mode': 'layer', 'interval_seconds': 10}
STOP_REASONS = ('finished', 'stopped', 'frame_limit', 'storage_full', 'interrupted')
ENDED_STATES = ('FINISH', 'FAILED', 'IDLE')
NO_PRINTER = {'available': False, 'state': None, 'layer': None, 'job_name': None}


class TimelapseConflict(Exception):
    """The request clashes with the current recording (HTTP 409)."""


class UnknownTimelapse(LookupError):
    """No timelapse has this id (HTTP 404)."""


def valid_id(value):
    # fullmatch, not match with $: a trailing newline must not pass.
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def validated_settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SETTINGS):
        raise ValueError('Send enabled, mode, and interval_seconds.')
    if type(value['enabled']) is not bool:
        raise ValueError('enabled must be true or false.')
    if not isinstance(value['mode'], str) or value['mode'] not in ('layer', 'interval'):
        raise ValueError('mode must be layer or interval.')
    if type(value['interval_seconds']) is not int or not 2 <= value['interval_seconds'] <= 600:
        raise ValueError('interval_seconds must be a whole number from 2 to 600.')
    return {key: value[key] for key in DEFAULT_SETTINGS}


def storage_paths(state_file):
    """Timelapses live next to state.json, inside the service's private state directory."""
    state_file = Path(state_file)
    return state_file.with_name('timelapses'), state_file.with_name('timelapse.json')


def frame_reader(buffer, max_age=FRAME_MAX_AGE, clock=time.monotonic):
    """Latest camera frame, or None if the camera hasn't produced one recently.

    Capture runs continuously whether or not anyone is watching, so the latest frame is fresh.
    """
    def read():
        with buffer.condition:
            if buffer.frame is None or clock() - buffer.updated > max_age:
                return None
            return buffer.frame
    return read


def printer_status(monitor):
    if monitor is None:
        return dict(NO_PRINTER)
    snapshot = monitor.snapshot()
    if not snapshot.get('fresh'):
        return dict(NO_PRINTER)
    fields = snapshot.get('fields', {})
    return {'available': True, 'state': fields.get('state'), 'layer': fields.get('current_layer'),
            'job_name': fields.get('job_name')}


def id_time(identifier):
    try:
        return calendar.timegm(time.strptime(identifier[:15], '%Y%m%d-%H%M%S'))
    except ValueError:
        return 0


class Recording:
    def __init__(self, identifier, trigger, manual, interval, layer, due):
        self.id, self.trigger, self.manual = identifier, trigger, manual
        self.interval, self.last_layer, self.next_capture = interval, layer, due
        self.last_frame = None


class TimelapseRecorder:
    def __init__(self, directory, settings_path, read_frame, printer=lambda: dict(NO_PRINTER), stop=None,
                 clock=time.time, monotonic=time.monotonic, disk_free=None):
        self.directory, self.settings_path = Path(directory), Path(settings_path)
        self.read_frame, self.printer = read_frame, printer
        self.stop = stop or threading.Event()
        self.clock, self.monotonic = clock, monotonic
        self.disk_free = disk_free or (lambda: shutil.disk_usage(self.directory).free)
        self.lock = threading.RLock()
        self.woken = threading.Event()
        self.items = {}
        self.recording = None
        self.last_state = None
        self.closed = False
        self.settings = self._load_settings()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock:
            self._scan()
            self._prune(self.clock())
        self.thread = threading.Thread(target=self.run, name='timelapse', daemon=True)

    # Settings

    def _load_settings(self):
        try:
            return validated_settings(json.loads(self.settings_path.read_text()))
        except FileNotFoundError:
            return dict(DEFAULT_SETTINGS)
        except (OSError, ValueError):
            logging.warning('Timelapse settings were unreadable; using the defaults.')
            return dict(DEFAULT_SETTINGS)

    def configure(self, value):
        settings = validated_settings(value)
        with self.lock:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._atomic_json(self.settings_path, settings)
            self.settings = settings
        self.wake()
        return self.state()

    # Public API, called from HTTP handler threads

    def state(self):
        status = self.printer()
        with self.lock:
            current = None
            if self.recording:
                item = self.items[self.recording.id]
                current = {'id': item['id'], 'started': item['started'], 'frames': item['frames'],
                           'trigger': self.recording.trigger, 'manual': self.recording.manual}
            return {**self.settings,
                    'layer_available': bool(status['available'] and status['layer'] is not None),
                    'recording': current,
                    'storage': {'used_bytes': self._used(), 'limit_bytes': STORAGE_LIMIT_BYTES}}

    def start_recording(self):
        status = self.printer()
        with self.lock:
            if self.recording:
                raise TimelapseConflict('Already recording.')
            self._start(True, status, self.clock(), self.monotonic())
        return self.state()

    def stop_recording(self):
        with self.lock:
            if not self.recording:
                raise TimelapseConflict('Not recording.')
            self._finish('stopped', self.clock(), final=True)
        return self.state()

    def list(self):
        with self.lock:
            items = sorted(self.items.values(), key=lambda item: (item['started'], item['id']), reverse=True)
            return {'timelapses': [dict(item) for item in items]}

    def poster(self, identifier):
        with self.lock:
            item = self._item(identifier)
            if not item['frames']:
                return None
            path = self.directory / identifier / f"{item['frames']:06d}.jpg"
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise UnknownTimelapse(identifier) from None

    def frame_paths(self, identifier):
        """Frames that exist now. A recording keeps adding frames after this returns."""
        with self.lock:
            item = self._item(identifier)
            folder = self.directory / identifier
            return [folder / f'{index:06d}.jpg' for index in range(1, item['frames'] + 1)]

    def delete(self, identifier):
        with self.lock:
            self._item(identifier)
            if self.recording and self.recording.id == identifier:
                raise TimelapseConflict('Stop the recording first.')
            self._remove(identifier)

    # Background thread

    def start(self):
        self.thread.start()

    def wake(self):
        self.woken.set()

    def run(self):
        while not self.stop.is_set():
            self.woken.wait(1)
            self.woken.clear()
            if self.stop.is_set():
                break
            try:
                self.poll()
            except Exception as error:
                logging.warning('Timelapse update failed (%s)', type(error).__name__)

    def poll(self):
        """React to printer status and time. Called every second and on each printer report."""
        status = self.printer()
        now, monotonic = self.clock(), self.monotonic()
        with self.lock:
            if self.closed:
                return
            self._prune(now)
            state = status['state'] if status['available'] else None
            layer = status['layer'] if status['available'] else None
            if state is not None:
                previous, self.last_state = self.last_state, state
                if self.recording and not self.recording.manual and state in ENDED_STATES:
                    self._finish('finished', now, final=True)
                elif (not self.recording and self.settings['enabled'] and state == 'RUNNING'
                      and previous not in (None, 'RUNNING')):
                    try:
                        self._start(False, status, now, monotonic)
                    except OSError as error:
                        logging.warning('Could not start a timelapse (%s)', type(error).__name__)
                    return
            current = self.recording
            if current is None:
                return
            item = self.items[current.id]
            if item['name'] is None and status['available'] and status['job_name']:
                item['name'] = status['job_name']
                self._save_meta(item)
            if current.trigger == 'layer':
                if layer is not None:
                    if current.last_layer is not None and layer > current.last_layer:
                        self._capture(now)
                    current.last_layer = layer
            elif state != 'PAUSE' and monotonic >= current.next_capture:
                # Keep a steady cadence; after a pause or a stall, restart it from now.
                current.next_capture += current.interval
                if current.next_capture <= monotonic:
                    current.next_capture = monotonic + current.interval
                self._capture(now)

    def close(self):
        """On shutdown, finish a recording in progress. It is not resumed on the next start."""
        with self.lock:
            if self.recording:
                self._finish('interrupted', self.clock(), final=False)
            self.closed = True

    # Internals. Callers hold self.lock.

    def _item(self, identifier):
        # Validate before any filesystem access; only indexed ids ever reach a path.
        if not valid_id(identifier) or identifier not in self.items:
            raise UnknownTimelapse(identifier)
        return self.items[identifier]

    def _used(self):
        return sum(item['bytes'] for item in self.items.values())

    def _start(self, manual, status, now, monotonic):
        self._prune(now)
        identifier = self._new_id(now)
        layer = status['layer'] if status['available'] else None
        trigger = 'layer' if self.settings['mode'] == 'layer' and layer is not None else 'interval'
        item = {'id': identifier, 'name': status['job_name'] if status['available'] else None,
                'started': int(now), 'ended': None, 'frames': 0, 'bytes': 0, 'recording': True, 'stop_reason': None}
        try:
            self._write_meta(item)
        except OSError:
            shutil.rmtree(self.directory / identifier, ignore_errors=True)
            raise
        self.items[identifier] = item
        interval = self.settings['interval_seconds']
        self.recording = Recording(identifier, trigger, manual, interval, layer, monotonic + interval)
        self._capture(now)

    def _new_id(self, now):
        base = time.strftime('%Y%m%d-%H%M%S', time.gmtime(int(now)))
        for number in range(1, 1000):
            identifier = base if number == 1 else f'{base}-{number}'
            if identifier in self.items:
                continue
            try:
                (self.directory / identifier).mkdir(mode=0o700)
            except FileExistsError:
                continue
            return identifier
        raise OSError('No free timelapse id')

    def _capture(self, now):
        frame = self.read_frame()
        if not frame:
            return
        if not self._write_frame(frame, now):
            self._finish('storage_full', now, final=False)
        elif self.items[self.recording.id]['frames'] >= MAX_FRAMES:
            self._finish('frame_limit', now, final=False)

    def _write_frame(self, frame, now):
        current = self.recording
        item = self.items[current.id]
        size = len(frame)
        if not self._make_room(size):
            return False
        try:
            if self.disk_free() - size < MIN_FREE_BYTES:
                return False
            folder = self.directory / current.id
            index = item['frames'] + 1
            temporary = folder / '.frame.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(frame)
            os.replace(temporary, folder / f'{index:06d}.jpg')
            item['frames'] = index
            item['bytes'] += size
            current.last_frame = int(now)
            self._write_meta(item)
        except OSError as error:
            logging.warning('Could not save a timelapse frame (%s)', type(error).__name__)
            return False
        return True

    def _finish(self, reason, now, final):
        current = self.recording
        item = self.items[current.id]
        if final and item['frames'] < MAX_FRAMES:
            frame = self.read_frame()
            if frame:
                self._write_frame(frame, now)
        self.recording = None
        item['recording'] = False
        item['stop_reason'] = reason
        item['ended'] = (current.last_frame or item['started']) if reason == 'interrupted' else int(now)
        self._save_meta(item)

    def _prune(self, now):
        for identifier, item in list(self.items.items()):
            if not item['recording'] and now - (item['ended'] or item['started']) > RETENTION_SECONDS:
                try:
                    self._remove(identifier)
                except OSError:
                    logging.warning('Could not remove an expired timelapse.')
        self._make_room(0)

    def _make_room(self, needed):
        """Delete the oldest finished timelapses until `needed` more bytes fit under the cap."""
        while self._used() + needed > STORAGE_LIMIT_BYTES:
            finished = [item for item in self.items.values() if not item['recording']]
            if not finished:
                return False
            oldest = min(finished, key=lambda item: (item['started'], item['id']))
            try:
                self._remove(oldest['id'])
            except OSError:
                logging.warning('Could not remove an old timelapse.')
                return False
        return True

    def _remove(self, identifier):
        path = self.directory / identifier
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            raise OSError('Timelapse could not be removed')
        del self.items[identifier]

    def _atomic_json(self, path, value):
        temporary = path.with_name(f'.{path.name}.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def _write_meta(self, item):
        self._atomic_json(self.directory / item['id'] / 'meta.json', item)

    def _save_meta(self, item):
        try:
            self._write_meta(item)
        except OSError as error:
            logging.warning('Could not save timelapse details (%s)', type(error).__name__)

    def _scan(self):
        for entry in sorted(self.directory.iterdir()):
            if not valid_id(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            meta = self._read_meta(entry)
            if meta is None or meta['recording']:
                # Left over from a crash or power loss: finish it as interrupted at its last frame.
                frames, size, last = self._count(entry)
                started = meta['started'] if meta else id_time(entry.name)
                meta = {'id': entry.name, 'name': meta['name'] if meta else None, 'started': started,
                        'ended': last or started, 'frames': frames, 'bytes': size, 'recording': False,
                        'stop_reason': 'interrupted'}
                self._save_meta(meta)
            self.items[entry.name] = meta

    def _read_meta(self, folder):
        try:
            value = json.loads((folder / 'meta.json').read_text())
        except (OSError, ValueError):
            return None
        checks = {'id': lambda v: v == folder.name, 'name': lambda v: v is None or isinstance(v, str),
                  'started': lambda v: type(v) is int, 'ended': lambda v: v is None or type(v) is int,
                  'frames': lambda v: type(v) is int and 0 <= v <= MAX_FRAMES,
                  'bytes': lambda v: type(v) is int and v >= 0, 'recording': lambda v: type(v) is bool,
                  'stop_reason': lambda v: v is None or v in STOP_REASONS}
        if not isinstance(value, dict) or set(value) != set(checks) or not all(check(value[key]) for key, check in checks.items()):
            return None
        return value

    def _count(self, folder):
        frames = size = 0
        last = None
        while frames < MAX_FRAMES:
            try:
                info = (folder / f'{frames + 1:06d}.jpg').stat()
            except OSError:
                break
            frames += 1
            size += info.st_size
            last = int(info.st_mtime)
        return frames, size, last
