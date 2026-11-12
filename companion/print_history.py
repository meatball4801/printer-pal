"""Print history, recorded on the Pi from the printer status it already reads.

Nothing leaves the Pi except to a paired phone that asks for it.
"""
import json
import logging
import os
from pathlib import Path
import threading
import time

MAX_PRINTS = 200
STALE_SECONDS = 30 * 60
# The last-seen time alone is saved at most this often, to spare the SD card. Every other change is saved at once.
SEEN_SAVE_SECONDS = 60
STARTED_STATES = ('RUNNING', 'PREPARE', 'PAUSE')
RESULTS = {'FINISH': 'finished', 'FAILED': 'failed', 'IDLE': 'stopped'}
RESULT_NAMES = ('finished', 'failed', 'stopped', 'interrupted')
KINDS = ('bambu', 'klipper', 'octoprint')
PUBLIC_FIELDS = ('id', 'name', 'kind', 'started', 'ended', 'result', 'duration_seconds', 'progress', 'layers')


def storage_path(state_file):
    """prints.json lives next to state.json, inside the service's private state directory."""
    return Path(state_file).with_name('prints.json')


def _whole(value, low=0, high=None):
    return type(value) is int and value >= low and (high is None or value <= high)


def _valid_entry(value):
    if not isinstance(value, dict) or set(value) != set(PUBLIC_FIELDS) | {'last_seen'}:
        return False
    checks = {'id': lambda v: isinstance(v, str) and 0 < len(v) <= 32,
              'name': lambda v: v is None or (isinstance(v, str) and len(v) <= 200),
              'kind': lambda v: v is None or v in KINDS,
              'started': _whole,
              'ended': lambda v: v is None or _whole(v),
              'result': lambda v: v is None or v in RESULT_NAMES,
              'duration_seconds': lambda v: v is None or _whole(v),
              'progress': lambda v: v is None or _whole(v, 0, 100),
              'layers': lambda v: v is None or _whole(v),
              'last_seen': _whole}
    if not all(check(value[key]) for key, check in checks.items()):
        return False
    # Active prints have no end yet; ended prints have all three.
    return (value['ended'] is None) == (value['result'] is None) == (value['duration_seconds'] is None)


class PrintHistory:
    def __init__(self, path, snapshot=lambda: {}, clock=time.time):
        self.path, self.snapshot, self.clock = Path(path), snapshot, clock
        self.lock = threading.RLock()
        self.prints = self._load()
        self.saved_seen = None
        with self.lock:
            # A print left active from before a restart, crash, or power loss isn't resumed.
            if self._active() is not None:
                self._interrupt()
                self._save_quietly()

    # Called by PrinterMonitor after each accepted report or poll, outside its lock.

    def observe(self):
        snapshot = self.snapshot() or {}
        now = int(self.clock())
        with self.lock:
            changed = self._expire(now)
            if snapshot.get('fresh'):
                changed |= self._update(snapshot, now)
            if changed or self._seen_due(now):
                self._save_quietly()

    def printer_changed(self):
        """The printer settings were changed or removed: an active print ends as interrupted."""
        with self.lock:
            if self._active() is not None:
                self._interrupt()
                self._save_quietly()

    # Called from HTTP handler threads.

    def list(self):
        now = int(self.clock())
        with self.lock:
            if self._expire(now):
                self._save_quietly()
            items = sorted(self.prints, key=lambda item: (item['started'], item['id']), reverse=True)
            ended = [item for item in items if item['result'] is not None]
            stats = {'count': len(ended)}
            for result in RESULT_NAMES:
                stats[result] = sum(1 for item in ended if item['result'] == result)
            stats['print_seconds'] = sum(item['duration_seconds'] for item in ended)
            return {'prints': [{key: item[key] for key in PUBLIC_FIELDS} for item in items], 'stats': stats}

    def clear(self):
        """Delete every ended print. An active print is kept."""
        now = int(self.clock())
        with self.lock:
            self._expire(now)
            kept = [item for item in self.prints if item['result'] is None]
            self._write(kept)
            self.prints = kept

    # Internals. Callers hold self.lock.

    def _active(self):
        return next((item for item in self.prints if item['result'] is None), None)

    def _expire(self, now):
        active = self._active()
        if active is not None and now - active['last_seen'] >= STALE_SECONDS:
            self._interrupt()
            return True
        return False

    def _interrupt(self):
        active = self._active()
        self._end(active, 'interrupted', active['last_seen'])

    def _end(self, item, result, ended):
        item['ended'] = max(ended, item['started'])
        item['result'] = result
        item['duration_seconds'] = item['ended'] - item['started']

    def _update(self, snapshot, now):
        fields = snapshot.get('fields') or {}
        state = fields.get('state')
        active = self._active()
        if active is None:
            if state not in STARTED_STATES:
                return False
            active = {'id': self._new_id(now), 'name': None, 'kind': snapshot.get('kind') if snapshot.get('kind') in KINDS else None,
                      'started': now, 'ended': None, 'result': None, 'duration_seconds': None, 'progress': None,
                      'layers': None, 'last_seen': now}
            self.prints.append(active)
            self._trim()
            changed = True
        else:
            active['last_seen'] = now
            changed = False
        name, progress, layers = fields.get('job_name'), fields.get('progress_percent'), fields.get('total_layers')
        if active['name'] is None and isinstance(name, str) and name.strip():
            active['name'], changed = name[:200], True
        if _whole(progress, 0, 100) and progress != active['progress']:
            active['progress'], changed = progress, True
        if _whole(layers) and layers != active['layers']:
            active['layers'], changed = layers, True
        if state in RESULTS:
            if state == 'FINISH':
                active['progress'] = 100
            self._end(active, RESULTS[state], now)
            changed = True
        return changed

    def _seen_due(self, now):
        active = self._active()
        return (active is not None and active['last_seen'] != self.saved_seen
                and (self.saved_seen is None or now - self.saved_seen >= SEEN_SAVE_SECONDS))

    def _new_id(self, now):
        base = time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))
        taken = {item['id'] for item in self.prints}
        number = 1
        while (base if number == 1 else f'{base}-{number}') in taken:
            number += 1
        return base if number == 1 else f'{base}-{number}'

    def _trim(self):
        """Keep the newest MAX_PRINTS. The active print is always the newest."""
        if len(self.prints) > MAX_PRINTS:
            self.prints.sort(key=lambda item: (item['started'], item['id']))
            del self.prints[:len(self.prints) - MAX_PRINTS]

    def _load(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            value = None
        prints = value.get('prints') if isinstance(value, dict) else None
        if isinstance(prints, list) and all(_valid_entry(item) for item in prints) \
                and len({item['id'] for item in prints}) == len(prints) \
                and sum(1 for item in prints if item['result'] is None) <= 1:
            prints = sorted(prints, key=lambda item: (item['started'], item['id']))
            return prints[-MAX_PRINTS:]
        # Keep a damaged file for inspection rather than overwrite it on the next change.
        backup = self.path.with_name(f'{self.path.name}.corrupt-{int(time.time())}')
        try:
            os.replace(self.path, backup)
        except OSError:
            pass
        logging.warning('Print history was unreadable and has been reset.')
        return []

    def _write(self, prints):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f'.{self.path.name}.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'prints': prints}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    def _save_quietly(self):
        active = self._active()
        try:
            self._write(self.prints)
        except OSError as error:
            logging.warning('Could not save print history (%s)', type(error).__name__)
            return
        self.saved_seen = active['last_seen'] if active else None
