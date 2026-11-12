"""Printer status and controls: Bambu Lab over local MQTT, Klipper (Moonraker) and OctoPrint over local HTTP.

One printer is configured at a time. Every kind produces the same snapshot for the app, the timelapse recorder, and
the print history. Control commands (pause, resume, stop, and a Bambu chamber light) are only sent when a paired phone
asks for one, to the same saved address, and never through a cloud service.
"""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import bambu_monitor
from bambu_monitor import FRESH_SECONDS, PRIVATE_NETWORKS, ReportState, clean_text, make_client, number, valid_host
from camera_source import NoRedirect

HTTP_KINDS = ('klipper', 'octoprint')
SERVICE_NAMES = {'klipper': 'Moonraker', 'octoprint': 'OctoPrint'}
EXAMPLE_URLS = {'klipper': 'http://127.0.0.1:7125', 'octoprint': 'http://127.0.0.1:5000'}
POLL_SECONDS = 2.5
REQUEST_TIMEOUT = 4
DISCOVERY_TIMEOUT = 1.5
MAX_RESPONSE_BYTES = 256 * 1024
API_KEY = re.compile(r'[A-Za-z0-9_-]{16,128}')
FIELDS = ('state', 'progress_percent', 'remaining_minutes', 'current_layer', 'total_layers',
          'nozzle_temp', 'bed_temp', 'job_name', 'active_tray', 'ams_slots')
MOONRAKER_QUERY = '/printer/objects/query?webhooks&print_stats&virtual_sdcard&display_status&extruder&heater_bed'
MOONRAKER_STATES = {'standby': 'IDLE', 'printing': 'RUNNING', 'paused': 'PAUSE', 'complete': 'FINISH',
                    'cancelled': 'FAILED', 'error': 'FAILED'}

# Printer controls
CONTROLS = {'bambu': ('pause', 'resume', 'stop', 'light'), 'klipper': ('pause', 'resume', 'stop'),
            'octoprint': ('pause', 'resume', 'stop')}
ACTIONS = ('pause', 'resume', 'stop', 'light_on', 'light_off')
CONTROL_INTERVAL = 1.0
BAMBU_SEND_TIMEOUT = 3
BAMBU_LIGHT = ('{"system":{"sequence_id":"0","command":"ledctrl","led_node":"chamber_light","led_mode":"%s",'
               '"led_on_time":500,"led_off_time":500,"loop_times":0,"interval_time":0}}')
BAMBU_COMMANDS = {'pause': '{"print":{"sequence_id":"0","command":"pause","param":""}}',
                  'resume': '{"print":{"sequence_id":"0","command":"resume","param":""}}',
                  'stop': '{"print":{"sequence_id":"0","command":"stop","param":""}}',
                  'light_on': BAMBU_LIGHT % 'on', 'light_off': BAMBU_LIGHT % 'off'}
MOONRAKER_COMMANDS = {'pause': '/printer/print/pause', 'resume': '/printer/print/resume', 'stop': '/printer/print/cancel'}
OCTOPRINT_COMMANDS = {'pause': {'command': 'pause', 'action': 'pause'}, 'resume': {'command': 'pause', 'action': 'resume'},
                      'stop': {'command': 'cancel'}}
# The states each action needs, and the error otherwise. Lights work in any state.
ACTION_STATES = {'pause': (('RUNNING', 'PREPARE'), "The printer isn't printing."),
                 'resume': (('PAUSE',), "The print isn't paused."),
                 'stop': (('RUNNING', 'PREPARE', 'PAUSE'), "The printer isn't printing.")}
NOT_CONNECTED = "The printer isn't connected right now."


# Errors from one HTTP poll. Each names the connection value it is reported as.

class StatusError(Exception):
    phase = 'unreachable'


class UnexpectedStatus(StatusError):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class AuthenticationFailed(StatusError):
    phase = 'authentication_failed'


class CertificateFailed(StatusError):
    phase = 'certificate_failed'


class NotReady(StatusError):
    """The server answered, but the printer isn't ready (Klipper starting, or OctoPrint not connected to it)."""
    phase = 'waiting'


class ControlError(Exception):
    """A printer control request that can't be sent, with its HTTP status and app-facing message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# Settings

def valid_http_host(value, policy):
    """Loopback, or the same attached private LAN rule as Bambu printers."""
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise ValueError('Use the printer’s IP address, or localhost, in the address.') from None
    if address.is_loopback:
        return str(address)
    if policy is None:
        # Saved settings are checked against the live network policy before every request.
        if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS):
            raise ValueError('Invalid saved printer address.')
        return str(address)
    return valid_host(str(address), policy)


def url_host(url):
    return urlsplit(url).hostname


def validated_url(value, kind, policy):
    example = EXAMPLE_URLS[kind]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Enter the {SERVICE_NAMES[kind]} address, like {example}.')
    value = value.strip()
    if len(value) > 256 or any(character.isspace() or not character.isprintable() for character in value):
        raise ValueError(f'Check the address. It should look like {example}.')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError('Check the address and its port number.') from None
    if parts.scheme not in ('http', 'https'):
        raise ValueError(f'The address must start with http:// or https://, like {example}.')
    if '@' in parts.netloc:
        raise ValueError('Remove the user name and password from the address.')
    if parts.query or parts.fragment or parts.path not in ('', '/') or '?' in value or '#' in value:
        raise ValueError(f'Use only the address and port, like {example}.')
    if not parts.hostname:
        raise ValueError(f'Enter the {SERVICE_NAMES[kind]} address, like {example}.')
    if port == 0:
        raise ValueError('Check the address and its port number.')
    host = valid_http_host('127.0.0.1' if parts.hostname == 'localhost' else parts.hostname, policy)
    netloc = f'[{host}]' if ':' in host else host
    if port and port != (443 if parts.scheme == 'https' else 80):
        netloc += f':{port}'
    return f'{parts.scheme}://{netloc}'


def validated_api_key(value, required):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError('Enter the OctoPrint API key.')
        return None
    if not isinstance(value, str) or not API_KEY.fullmatch(value.strip()):
        raise ValueError('The API key must be 16 to 128 letters, numbers, dashes, or underscores.')
    return value.strip()


def validated_printer(value, policy):
    """The settings to save. Bambu keeps its original format, with no kind, so older companions can read it."""
    if not isinstance(value, dict):
        raise ValueError('Printer address, serial number, and access code are required.')
    kind = value.get('kind', 'bambu')
    if kind == 'bambu':
        return bambu_monitor.validated_settings({key: item for key, item in value.items() if key != 'kind'}, policy)
    if not isinstance(kind, str) or kind not in HTTP_KINDS:
        raise ValueError('Printer type must be bambu, klipper, or octoprint.')
    if set(value) - {'kind', 'url', 'api_key'}:
        raise ValueError('Send only kind, url, and api_key.')
    settings = {'kind': kind, 'url': validated_url(value.get('url'), kind, policy)}
    key = validated_api_key(value.get('api_key'), required=kind == 'octoprint')
    if key:
        settings['api_key'] = key
    return settings


def printer_kind(settings):
    return None if settings is None else settings.get('kind', 'bambu')


# HTTP

def make_opener():
    # No environment proxies and no redirects: requests go only to the validated address.
    return build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))


def read_limited(response, limit, truncate):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        if not truncate:
            raise StatusError()
        raw = raw[:limit]
    return raw


def http_get(opener, url, api_key=None, timeout=REQUEST_TIMEOUT, limit=MAX_RESPONSE_BYTES, truncate=False):
    """(status, body). Raises a StatusError when nothing answers. Never logs the key or the response."""
    headers = {'Accept': 'application/json', 'User-Agent': 'PrinterPal-status'}
    if api_key:
        headers['X-Api-Key'] = api_key
    try:
        with opener.open(Request(url, headers=headers), timeout=timeout) as response:
            return response.status, read_limited(response, limit, truncate)
    except HTTPError as error:
        try:
            body = read_limited(error, limit, True)
        except (OSError, HTTPException, ValueError, AttributeError):
            body = b''
        finally:
            error.close()
        return error.code, body
    except URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            raise CertificateFailed() from None
        raise StatusError() from None
    except ssl.SSLError:
        raise CertificateFailed() from None
    except (OSError, HTTPException, ValueError):
        raise StatusError() from None


def http_post(opener, url, api_key=None, body=None, timeout=REQUEST_TIMEOUT):
    """The status of one control request. Same rules as http_get: raises a StatusError when nothing answers."""
    headers = {'Accept': 'application/json', 'User-Agent': 'PrinterPal-status'}
    if api_key:
        headers['X-Api-Key'] = api_key
    data = b''
    if body is not None:
        data = json.dumps(body, separators=(',', ':')).encode()
        headers['Content-Type'] = 'application/json'
    try:
        with opener.open(Request(url, data=data, headers=headers, method='POST'), timeout=timeout) as response:
            try:
                read_limited(response, MAX_RESPONSE_BYTES, True)
            except (OSError, HTTPException, ValueError):
                pass  # The status already says whether the command was accepted.
            return response.status
    except HTTPError as error:
        error.close()
        return error.code
    except URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            raise CertificateFailed() from None
        raise StatusError() from None
    except ssl.SSLError:
        raise CertificateFailed() from None
    except (OSError, HTTPException, ValueError):
        raise StatusError() from None


def send_http_control(opener, settings, kind, action):
    """Send a Klipper or OctoPrint control. Raises ControlError unless the server accepts it."""
    if kind == 'klipper':
        url, body = settings['url'] + MOONRAKER_COMMANDS[action], None
    else:
        url, body = settings['url'] + '/api/job', OCTOPRINT_COMMANDS[action]
    try:
        status = http_post(opener, url, settings.get('api_key'), body)
    except StatusError:
        status = None
    except Exception as error:
        # Do not log keys, addresses, responses, or exception text.
        logging.warning('Printer control request failed (%s)', type(error).__name__)
        status = None
    if status is not None and 200 <= status < 300:
        return
    if status in (401, 403):
        raise ControlError(403, 'Check the API key.')
    if status == 409:
        raise ControlError(409, "The printer can't do that right now.")
    raise ControlError(502, "The printer didn't accept the command.")


class PendingPublish:
    """A Bambu command handed from an HTTP thread to the MQTT session thread, which alone uses the client."""

    def __init__(self, generation, payload):
        self.generation, self.payload = generation, payload
        self.taken = False
        self.sent = False
        self.done = threading.Event()


def fetch_json(opener, url, api_key=None, timeout=REQUEST_TIMEOUT):
    status, body = http_get(opener, url, api_key, timeout)
    if status in (401, 403):
        raise AuthenticationFailed()
    if status != 200:
        raise UnexpectedStatus(status)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise StatusError() from None
    if not isinstance(data, dict):
        raise StatusError()
    return data


# Status mapping. Both return the same fields as a Bambu report; AMS fields are always null.

def section(parent, name):
    value = parent.get(name) if isinstance(parent, dict) else None
    return value if isinstance(value, dict) else {}


def temperature(value, high):
    value = number(value, -20, high)
    return None if value is None else round(value, 1)


def job_name(value):
    if not isinstance(value, str):
        return None
    name = value.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'\.(gcode|gco|g|bgcode)$', '', name, flags=re.IGNORECASE)
    return clean_text(name)


def minutes(seconds):
    if seconds is None or not math.isfinite(seconds):
        return None
    return number(math.ceil(seconds / 60), 0, 100000, True)


def moonraker_fields(data):
    status = section(section(data, 'result'), 'status')
    if not status:
        raise StatusError()
    klippy = section(status, 'webhooks').get('state')
    if klippy == 'startup':
        raise NotReady()
    stats = section(status, 'print_stats')
    state = MOONRAKER_STATES.get(stats.get('state')) if isinstance(stats.get('state'), str) else None
    duration = number(stats.get('print_duration'), 0, 10 ** 8)
    if state == 'RUNNING' and duration == 0:
        # Klipper only counts print time once extrusion starts: before that it's heating and homing.
        state = 'PREPARE'
    if klippy in ('shutdown', 'error'):
        state = 'FAILED'
    fields = dict.fromkeys(FIELDS)
    fields['state'] = state
    fields['nozzle_temp'] = temperature(section(status, 'extruder').get('temperature'), 500)
    fields['bed_temp'] = temperature(section(status, 'heater_bed').get('temperature'), 200)
    if state in (None, 'IDLE'):
        return fields
    progress = number(section(status, 'display_status').get('progress'), 0, 1)
    if progress is None:
        progress = number(section(status, 'virtual_sdcard').get('progress'), 0, 1)
    info = section(stats, 'info')
    fields['job_name'] = job_name(stats.get('filename'))
    fields['current_layer'] = number(info.get('current_layer'), 0, 100000, True)
    fields['total_layers'] = number(info.get('total_layer'), 0, 100000, True)
    if progress is not None:
        fields['progress_percent'] = int(progress * 100)
    if state == 'FINISH':
        fields['progress_percent'], fields['remaining_minutes'] = 100, 0
    elif state in ('RUNNING', 'PAUSE') and progress and duration:
        fields['remaining_minutes'] = minutes(duration / progress - duration)
    return fields


def octoprint_fields(job, printer):
    flags = section(section(printer, 'state'), 'flags')
    if not flags:
        raise StatusError()
    text = section(printer, 'state').get('text')
    progress = section(job, 'progress')
    file = section(section(job, 'job'), 'file')
    name = job_name(file.get('display')) or job_name(file.get('name'))
    completion = number(progress.get('completion'), 0, 100)

    def flag(key):
        return flags.get(key) is True

    if flag('error') or flag('closedOrError') or flag('cancelling'):
        state = 'FAILED'
    elif flag('paused') or flag('pausing'):
        state = 'PAUSE'
    elif flag('printing') or flag('resuming') or flag('finishing'):
        state = 'PREPARE' if isinstance(text, str) and text.startswith('Starting') else 'RUNNING'
    elif flag('operational') or flag('ready'):
        state = 'FINISH' if name and completion is not None and completion >= 100 else 'IDLE'
    else:
        state = None
    temperatures = section(printer, 'temperature')
    fields = dict.fromkeys(FIELDS)
    fields['state'] = state
    fields['nozzle_temp'] = temperature(section(temperatures, 'tool0').get('actual'), 500)
    fields['bed_temp'] = temperature(section(temperatures, 'bed').get('actual'), 200)
    if state in (None, 'IDLE'):
        return fields
    fields['job_name'] = name
    if completion is not None:
        fields['progress_percent'] = int(completion)
    if state == 'FINISH':
        fields['progress_percent'], fields['remaining_minutes'] = 100, 0
    elif state != 'FAILED':
        fields['remaining_minutes'] = minutes(number(progress.get('printTimeLeft'), 0, 10 ** 8))
    return fields


def poll_moonraker(opener, settings):
    try:
        data = fetch_json(opener, settings['url'] + MOONRAKER_QUERY, settings.get('api_key'))
    except UnexpectedStatus as error:
        if error.status == 503:
            raise NotReady() from None  # Moonraker is up but Klipper isn't connected to it.
        raise
    return moonraker_fields(data)


def poll_octoprint(opener, settings):
    try:
        printer = fetch_json(opener, settings['url'] + '/api/printer', settings['api_key'])
    except UnexpectedStatus as error:
        if error.status == 409:
            raise NotReady() from None  # OctoPrint is up but not connected to the printer.
        raise
    return octoprint_fields(fetch_json(opener, settings['url'] + '/api/job', settings['api_key']), printer)


class PolledState:
    """The latest complete HTTP status. Same freshness rules as a Bambu report."""

    def __init__(self):
        self.fields = dict.fromkeys(FIELDS)
        self.updated = None

    def update(self, fields, now):
        self.fields, self.updated = dict(fields), now

    def snapshot(self, now, connected):
        age = max(0, int(now - self.updated)) if self.updated is not None else None
        fresh = connected and age is not None and age < FRESH_SECONDS
        return {'fresh': fresh, 'age_seconds': age, 'fields': dict(self.fields) if fresh else dict.fromkeys(FIELDS)}


# Local discovery: only this Pi, with requests that need no key.

def probe_moonraker(opener, base, timeout):
    """None if not Moonraker, otherwise whether it needs an API key."""
    try:
        status, body = http_get(opener, base + '/server/info', timeout=timeout)
        data = json.loads(body)
    except (StatusError, ValueError, UnicodeError, RecursionError):
        return None
    if status == 200 and isinstance(section(data, 'result').get('klippy_state'), str):
        return False
    if status in (401, 403) and isinstance(section(data, 'error').get('message'), str):
        return True
    return None


def probe_octoprint(opener, base, timeout):
    """True if OctoPrint answers here. It always needs an API key."""
    for path in ('/api/version', '/', '/login/'):
        try:
            status, body = http_get(opener, base + path, timeout=timeout, limit=64 * 1024, truncate=True)
        except StatusError:
            return None
        if status == 200 and b'OctoPrint' in body:
            return True
    return None


def discover_local(host='127.0.0.1', moonraker_ports=(7125,), octoprint_ports=(5000, 80), opener=None,
                   timeout=DISCOVERY_TIMEOUT):
    opener = opener or make_opener()
    computer = clean_text(socket.gethostname(), 40)
    tasks = [('klipper', port) for port in moonraker_ports] + [('octoprint', port) for port in octoprint_ports]

    def base(port):
        return f'http://{host}' if port == 80 else f'http://{host}:{port}'

    def probe(task):
        kind, port = task
        return (probe_moonraker if kind == 'klipper' else probe_octoprint)(opener, base(port), timeout)

    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
        results = list(pool.map(probe, tasks))
    found = []
    for (kind, port), needs_key in zip(tasks, results):
        if needs_key is None:
            continue
        service = 'Klipper' if kind == 'klipper' else 'OctoPrint'
        found.append({'kind': kind, 'url': base(port), 'name': f'{service} on {computer}' if computer else service,
                      'host': None, 'serial': None, 'mode': None, 'needs_api_key': needs_key})
    return found


class PrinterMonitor:
    def __init__(self, path, policy, stop, client_factory=make_client, clock=time.monotonic, opener=None,
                 poll_interval=POLL_SECONDS, local_discovery=discover_local, send_timeout=BAMBU_SEND_TIMEOUT):
        self.path, self.policy, self.stop = Path(path), policy, stop
        self.client_factory, self.clock = client_factory, clock
        self.opener = opener or make_opener()
        self.poll_interval = poll_interval
        self.discover_local = local_discovery
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.changed = threading.Event()
        self.generation = 0
        self.settings = None
        self.kind = None
        self.phase = 'not_configured'
        self.connected = False
        self.report = ReportState()
        # Called after each accepted report or poll, outside the lock (for example to wake the timelapse recorder).
        self.listeners = []
        # Called after the printer settings are saved or removed, outside the lock (for example to end a print in the
        # print history).
        self.settings_listeners = []
        # Bambu commands waiting for the MQTT session thread. Guarded by self.lock.
        self.outbox = []
        self.send_timeout = send_timeout
        self.control_lock = threading.Lock()
        self.last_control = None
        if self.path.exists():
            try:
                self._use(validated_printer(json.loads(self.path.read_text()), None), 'connecting')
            except (OSError, ValueError):
                self.phase = 'invalid_configuration'
        self.thread = threading.Thread(target=self.run, name='printer-status', daemon=True)

    def start(self):
        self.thread.start()

    def _use(self, settings, phase):
        """Callers hold the lock (or are the constructor)."""
        self.settings, self.kind = settings, printer_kind(settings)
        self.generation += 1
        self.connected = False
        self.report = PolledState() if self.kind in HTTP_KINDS else ReportState()
        self.phase = phase
        # Commands for the previous printer are never sent to the new one.
        for item in self.outbox:
            item.done.set()
        self.outbox = []
        self.changed.set()

    def configure(self, value):
        # Only one printer at a time: saving any kind replaces the previous settings.
        settings = validated_printer(value, self.policy)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(settings, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._use(settings, 'connecting')
        self.notify(self.settings_listeners)

    def forget(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self._use(None, 'not_configured')
        self.notify(self.settings_listeners)

    def notify(self, listeners=None):
        for listener in list(self.listeners if listeners is None else listeners):
            try:
                listener()
            except Exception as error:
                logging.warning('Printer status listener failed (%s)', type(error).__name__)

    def snapshot(self):
        # Built field by field: access codes and API keys are never part of it.
        with self.lock:
            settings, kind = self.settings, self.kind
            host = None
            if kind == 'bambu':
                host = settings['host']
            elif kind in HTTP_KINDS:
                host = url_host(settings['url'])
            return {'configured': settings is not None, 'kind': kind, 'connection': self.phase, 'host': host,
                    'serial': settings['serial'] if kind == 'bambu' else None,
                    **self.report.snapshot(self.clock(), self.connected),
                    'controls': list(CONTROLS.get(kind, ()))}

    # Controls, called from HTTP handler threads

    def control(self, body):
        """Send one control action from a request body. Raises ControlError; returns None once it's sent."""
        with self.control_lock:
            now = self.clock()
            if self.last_control is not None and 0 <= now - self.last_control < CONTROL_INTERVAL:
                raise ControlError(429, 'Wait a moment and try again.')
            self.last_control = now
        if not isinstance(body, dict) or set(body) != {'action'} or not isinstance(body['action'], str) \
                or body['action'] not in ACTIONS:
            raise ControlError(400, 'Unknown printer action.')
        action = body['action']
        with self.lock:
            settings, kind, generation = self.settings, self.kind, self.generation
            if settings is None:
                raise ControlError(409, 'Connect a printer first.')
            if ('light' if action.startswith('light_') else action) not in CONTROLS.get(kind, ()):
                raise ControlError(400, "This printer doesn't support that.")
            status = self.report.snapshot(self.clock(), self.connected)
            if not status['fresh']:
                raise ControlError(409, NOT_CONNECTED)
            if action in ACTION_STATES:
                states, message = ACTION_STATES[action]
                if status['fields'].get('state') not in states:
                    raise ControlError(409, message)
            if kind == 'bambu':
                pending = PendingPublish(generation, BAMBU_COMMANDS[action])
                self.outbox.append(pending)
        if kind == 'bambu':
            self._wait_for_publish(pending)
            return
        try:
            # An address change must not redirect stored keys to a new subnet.
            valid_http_host(url_host(settings['url']), self.policy)
        except ValueError:
            raise ControlError(409, NOT_CONNECTED) from None
        send_http_control(self.opener, settings, kind, action)

    def _wait_for_publish(self, pending):
        if not pending.done.wait(self.send_timeout):
            with self.lock:
                if not pending.taken:
                    # The session never picked it up (still connecting, or the connection dropped). Don't send it late.
                    if pending in self.outbox:
                        self.outbox.remove(pending)
                    raise ControlError(409, NOT_CONNECTED)
            pending.done.wait(self.send_timeout)
        if not pending.sent:
            raise ControlError(409, NOT_CONNECTED)

    def discover(self, include_local=True):
        """Bambu printers on the network, plus Klipper and OctoPrint on this Pi when include_local is set.

        Printer Pal 1.0 can't read the Klipper and OctoPrint items, so the handler only asks for them when the app
        says it understands them."""
        if not include_local:
            if not self.scan_lock.acquire(blocking=False):
                raise ValueError('A printer search is already running.')
            try:
                return [{'kind': 'bambu', 'url': None, **item} for item in bambu_monitor.discover(self.policy, self.stop)]
            finally:
                self.scan_lock.release()
        if not self.scan_lock.acquire(blocking=False):
            raise ValueError('A printer search is already running.')
        try:
            local = []

            def search_this_pi():
                try:
                    local.extend(self.discover_local())
                except Exception as error:
                    logging.warning('Local printer search failed (%s)', type(error).__name__)

            # Probe this Pi while listening for Bambu announcements.
            worker = threading.Thread(target=search_this_pi, name='printer-discovery', daemon=True)
            worker.start()
            failure = None
            try:
                found = [{'kind': 'bambu', 'url': None, **item} for item in bambu_monitor.discover(self.policy, self.stop)]
            except (OSError, ValueError) as error:
                found, failure = [], error
            worker.join(10)
            if failure is not None and not local:
                raise failure
            return found + list(local)
        finally:
            self.scan_lock.release()

    def run(self):
        delay = 2
        while not self.stop.is_set():
            with self.lock:
                settings, kind, generation = self.settings, self.kind, self.generation
            if settings is None:
                self.changed.wait(1)
                self.changed.clear()
                continue
            if kind == 'bambu':
                delay = self._bambu_session(settings, generation, delay)
            else:
                self._poll_until_changed(generation)

    # Klipper and OctoPrint

    def poll(self):
        """One HTTP status request. Returns the connection value."""
        with self.lock:
            settings, kind, generation = self.settings, self.kind, self.generation
        if kind not in HTTP_KINDS:
            return self.phase
        fields = None
        try:
            # An address change must not redirect stored keys to a new subnet.
            valid_http_host(url_host(settings['url']), self.policy)
        except ValueError:
            phase = 'network_unavailable'
        else:
            try:
                fields = (poll_moonraker if kind == 'klipper' else poll_octoprint)(self.opener, settings)
                phase = 'connected'
            except StatusError as error:
                phase = error.phase
            except Exception as error:
                # Do not log keys, addresses, responses, or exception text.
                logging.warning('Printer status request failed (%s)', type(error).__name__)
                phase = 'unreachable'
        with self.lock:
            if generation != self.generation:
                return phase
            self.phase = phase
            self.connected = fields is not None
            if fields is not None:
                self.report.update(fields, self.clock())
        if fields is not None:
            self.notify()
        return phase

    def _poll_until_changed(self, generation):
        delay = self.poll_interval
        while not self.stop.is_set() and generation == self.generation:
            phase = self.poll()
            if phase == 'connected':
                delay = self.poll_interval
            else:
                # Back off while the server is down; recheck sooner while it waits for the printer.
                delay = min(10 if phase == 'waiting' else 30, max(self.poll_interval, delay * 2))
            if generation != self.generation:
                break
            self.changed.wait(delay)
            self.changed.clear()

    # Bambu

    def _bambu_session(self, settings, generation, delay):
        """One MQTT connection, until it drops or the settings change. Returns the next retry delay."""
        client = None
        started = self.clock()
        try:
            valid_host(settings['host'], self.policy)
            client = self.client_factory(settings)
            topic = f"device/{settings['serial']}/report"

            def on_connect(current, _userdata, _flags, reason, _properties=None,
                           expected_generation=generation, expected_topic=topic, cfg=settings):
                with self.lock:
                    if expected_generation != self.generation:
                        return
                    self.connected = reason == 0
                    self.phase = 'waiting' if self.connected else 'authentication_failed'
                    self.report = ReportState()
                if reason == 0:
                    current.subscribe(expected_topic, qos=0)
                    # Ask for a full status report. Control commands are only sent from _publish_pending.
                    current.publish(f"device/{cfg['serial']}/request",
                                    '{"pushing":{"sequence_id":"0","command":"pushall"}}', qos=0, retain=False)

            def on_message(_current, _userdata, message, expected_generation=generation, expected_topic=topic):
                if message.topic != expected_topic:
                    return
                with self.lock:
                    if expected_generation != self.generation:
                        return
                    self.report.update(message.payload, self.clock())
                    if self.report.updated is not None:
                        self.phase = 'connected'
                self.notify()

            def on_disconnect(*_args, expected_generation=generation):
                with self.lock:
                    if expected_generation == self.generation:
                        self.connected = False
                        if self.phase != 'authentication_failed':
                            self.phase = 'disconnected'

            client.on_connect, client.on_message, client.on_disconnect = on_connect, on_message, on_disconnect
            client.connect(settings['host'], 8883, keepalive=30)
            refresh = self.clock()
            while not self.stop.is_set() and generation == self.generation:
                # An address change must not redirect stored access to a new subnet.
                valid_host(settings['host'], self.policy)
                # A short wait keeps control commands from phones prompt.
                if client.loop(timeout=0.25) != 0:
                    raise ConnectionError()
                self._publish_pending(client, settings, generation)
                if self.clock() - refresh >= 30:
                    client.publish(f"device/{settings['serial']}/request",
                                   '{"pushing":{"sequence_id":"0","command":"pushall"}}', qos=0, retain=False)
                    refresh = self.clock()
            delay = 2
        except Exception as error:
            # Do not log credentials, serials, addresses, raw reports, or exception text.
            with self.lock:
                if generation == self.generation:
                    self.connected = False
                    if isinstance(error, ssl.SSLError):
                        self.phase = 'certificate_failed'
                    elif isinstance(error, ImportError):
                        self.phase = 'dependency_missing'
                    elif isinstance(error, ValueError):
                        self.phase = 'network_unavailable'
                    elif self.phase != 'authentication_failed':
                        self.phase = 'disconnected'
        finally:
            self._drop_pending(generation)
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass
        if generation != self.generation:
            return delay
        if self.clock() - started > 60:
            delay = 2
        self.changed.wait(delay)
        self.changed.clear()
        return min(30, delay * 2)

    def _publish_pending(self, client, settings, generation):
        """Publish control commands queued by HTTP threads. Runs on the session thread, between loop calls."""
        with self.lock:
            if not self.connected or generation != self.generation:
                return
            pending = [item for item in self.outbox if item.generation == generation]
            for item in pending:
                item.taken = True
                self.outbox.remove(item)
        for item in pending:
            try:
                info = client.publish(f"device/{settings['serial']}/request", item.payload, qos=0, retain=False)
                item.sent = getattr(info, 'rc', None) == 0
            except Exception as error:
                logging.warning('Printer control publish failed (%s)', type(error).__name__)
                item.sent = False
            finally:
                item.done.set()

    def _drop_pending(self, generation):
        """The session ended: commands still waiting for it fail rather than go out on a later connection."""
        with self.lock:
            for item in [item for item in self.outbox if item.generation == generation]:
                self.outbox.remove(item)
                item.done.set()
