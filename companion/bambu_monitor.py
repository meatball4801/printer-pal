"""Local status-only Bambu MQTT bridge. No cloud login or printer control API."""
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import struct
from contextlib import ExitStack
import socket
import ssl
import tempfile
import threading
import time

CA_FILE = Path(__file__).with_name('bambu-printer-ca.pem')
FRESH_SECONDS = 90
MAX_REPORT_BYTES = 256 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def valid_host(value, policy):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise ValueError('Enter the printer’s local IPv4 address.') from None
    if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS) or not policy.allows(str(address)):
        raise ValueError('The printer must be on an attached private local network.')
    return str(address)


def validated_settings(value, policy=None):
    if not isinstance(value, dict) or set(value) != {'host', 'serial', 'access_code'}:
        raise ValueError('Printer address, serial number, and access code are required.')
    if not isinstance(value['host'], str) or not isinstance(value['serial'], str) or not isinstance(value['access_code'], str):
        raise ValueError('Printer settings must be text.')
    host = value['host'].strip()
    serial = value['serial'].strip().upper()
    code = value['access_code'].strip()
    if not re.fullmatch(r'[A-Z0-9]{8,32}', serial):
        raise ValueError('Enter the printer serial number, not its display name.')
    if not re.fullmatch(r'[A-Za-z0-9]{8}', code):
        raise ValueError('Enter the eight-character LAN access code.')
    if policy is not None:
        host = valid_host(host, policy)
    else:
        address = ipaddress.ip_address(host)
        if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS):
            raise ValueError('Invalid saved printer address.')
    return {'host': host, 'serial': serial, 'access_code': code}


class PrinterTLSContext(ssl.SSLContext):
    """Verify Bambu's CA and the serial identity, rather than its changing IP."""
    def __new__(cls, serial, ca_file=CA_FILE):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, serial, ca_file=CA_FILE):
        self.serial = serial
        self.load_verify_locations(cafile=str(ca_file))
        self.minimum_version = ssl.TLSVersion.TLSv1_2
        # Older official BBL roots lack the extensions required by Python 3.13's
        # stricter default. Chain/signature, date, and serial checks remain on.
        self.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
        self.check_hostname = True
        self.verify_mode = ssl.CERT_REQUIRED

    def wrap_socket(self, sock, *args, **kwargs):
        kwargs['server_hostname'] = self.serial
        return super().wrap_socket(sock, *args, **kwargs)


def make_client(settings, ca_file=CA_FILE):
    import paho.mqtt.client as mqtt
    class LocalClient(mqtt.Client):
        # Paho otherwise honors environment/global SOCKS proxies. A local
        # printer connection must go directly to the validated LAN address.
        def _get_proxy(self):
            return None
    arguments = {'client_id': 'printer-pal-' + secrets.token_hex(6), 'protocol': mqtt.MQTTv311}
    if hasattr(mqtt, 'CallbackAPIVersion'):
        arguments['callback_api_version'] = mqtt.CallbackAPIVersion.VERSION2
    client = LocalClient(**arguments)
    client.username_pw_set('bblp', settings['access_code'])
    client.tls_set_context(PrinterTLSContext(settings['serial'], ca_file))
    # No tls_insecure_set, cloud broker, proxy, or arbitrary publish endpoint.
    return client


def clean_text(value, maximum=100):
    if not isinstance(value, str):
        return None
    return ''.join(character for character in value if character.isprintable())[:maximum] or None


def number(value, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
        return None
    if integer and value != int(value):
        return None
    return int(value) if integer else value


class ReportState:
    JOB_FIELDS = {'progress_percent', 'remaining_minutes', 'current_layer', 'total_layers', 'job_name'}
    FIELDS = {'mc_percent': ('progress_percent', 0, 100, True),
              'mc_remaining_time': ('remaining_minutes', 0, 100000, True),
              'layer_num': ('current_layer', 0, 100000, True),
              'total_layer_num': ('total_layers', 0, 100000, True),
              'nozzle_temper': ('nozzle_temp', -20, 500, False),
              'bed_temper': ('bed_temp', -20, 200, False)}

    def __init__(self):
        self.values = {}
        self.identifiers = {}
        self.slots = {}
        self.updated = None

    def update(self, payload, now):
        if len(payload) > MAX_REPORT_BYTES:
            return
        try:
            root = json.loads(payload)
        except (ValueError, UnicodeError, RecursionError):
            return
        report = root.get('print') if isinstance(root, dict) else None
        if not isinstance(report, dict) or report.get('command') not in (None, 'push_status'):
            return
        # Messages are deltas. Merge only recognized fields; never keep raw MQTT.
        new_job = False
        for key in ('subtask_id', 'gcode_file', 'subtask_name'):
            value = clean_text(report.get(key), 160)
            if value and value != '0':
                new_job |= key in self.identifiers and self.identifiers[key] != value
                self.identifiers[key] = value
        state = report.get('gcode_state')
        previous = self.values.get('state', (None, 0))[0]
        new_job |= state in ('PREPARE', 'SLICING', 'RUNNING') and previous in ('IDLE', 'FINISH', 'FAILED')
        if new_job:
            self.values = {key: value for key, value in self.values.items() if key not in self.JOB_FIELDS}
        accepted = False
        if state in ('IDLE', 'RUNNING', 'PAUSE', 'FINISH', 'FAILED', 'PREPARE', 'SLICING'):
            self.values['state'] = (state, now)
            accepted = True
        for source, (target, low, high, integer) in self.FIELDS.items():
            if source in report:
                value = number(report[source], low, high, integer)
                self.values[target] = (value, now)
                accepted |= value is not None
        if 'subtask_name' in report:
            self.values['job_name'] = (clean_text(report['subtask_name']), now)
            accepted = True
        ams = report.get('ams')
        if isinstance(ams, dict):
            active = ams.get('tray_now')
            if isinstance(active, str) and active.isascii() and active.isdecimal():
                active = int(active)
            if type(active) is int:
                self.values['active_tray'] = (active if 0 <= active < 16 else None, now)
                accepted = True
            units = ams.get('ams')
            if isinstance(units, list):
                # Merge tray deltas too; absence of a tray is not an empty slot.
                for unit in units[:4]:
                    if not isinstance(unit, dict) or str(unit.get('id')) not in ('0', '1', '2', '3'):
                        continue
                    trays = unit.get('tray')
                    if not isinstance(trays, list):
                        continue
                    for tray in trays[:4]:
                        if not isinstance(tray, dict) or str(tray.get('id')) not in ('0', '1', '2', '3'):
                            continue
                        index = int(unit['id']) * 4 + int(tray['id'])
                        item = self.slots.setdefault(index, {})
                        if 'tray_type' in tray:
                            item['material'] = (clean_text(tray['tray_type'], 24), now)
                        if 'tray_color' in tray:
                            color = tray['tray_color']
                            item['color'] = (color.upper() if isinstance(color, str) and re.fullmatch('[0-9A-Fa-f]{8}', color) else None, now)
                        accepted |= bool(item)
        if accepted:
            self.updated = now

    def snapshot(self, now, connected):
        age = max(0, int(now - self.updated)) if self.updated is not None else None
        fresh = connected and age is not None and age < FRESH_SECONDS
        fields = {name: (value if fresh and now - updated < FRESH_SECONDS else None)
                  for name, (value, updated) in self.values.items()}
        fields['ams_slots'] = [{'index': index, **{key: (item[0] if fresh and now - item[1] < FRESH_SECONDS else None)
                                                   for key, item in slot.items()}}
                               for index, slot in sorted(self.slots.items())] if fresh else []
        return {'fresh': fresh, 'age_seconds': age, 'fields': fields}


def parse_announcement(data, peer, policy):
    if len(data) > 4096:
        return None
    try:
        host = valid_host(peer, policy)
        lines = data.decode('utf-8').splitlines()
        if not lines or lines[0] != 'NOTIFY * HTTP/1.1':
            return None
        headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
        headers = {key.strip().lower(): value.strip() for key, value in headers.items()}
        if headers.get('nt') != 'urn:bambulab-com:device:3dprinter:1':
            return None
        serial = headers.get('usn', '').upper()
        if not re.fullmatch('[A-Z0-9]{8,32}', serial) or headers.get('location') != host:
            return None
        return {'host': host, 'serial': serial, 'name': clean_text(headers.get('devname.bambu.com')) or 'Bambu printer',
                'mode': clean_text(headers.get('devconnect.bambu.com'), 20)}
    except (ValueError, UnicodeError):
        return None


class PrinterMonitor:
    def __init__(self, path, policy, stop, client_factory=make_client, clock=time.monotonic):
        self.path, self.policy, self.stop = Path(path), policy, stop
        self.client_factory, self.clock = client_factory, clock
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.changed = threading.Event()
        self.generation = 0
        self.settings = None
        self.phase = 'not_configured'
        self.connected = False
        self.report = ReportState()
        if self.path.exists():
            try:
                self.settings = validated_settings(json.loads(self.path.read_text()))
                self.phase = 'connecting'
            except (OSError, ValueError):
                self.phase = 'invalid_configuration'
        self.thread = threading.Thread(target=self.run, name='printer-status', daemon=True)

    def start(self):
        self.thread.start()

    def configure(self, value):
        settings = validated_settings(value, self.policy)
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
            self.settings = settings
            self.generation += 1
            self.connected = False
            self.report = ReportState()
            self.phase = 'connecting'
            self.changed.set()

    def forget(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.settings = None
            self.generation += 1
            self.connected = False
            self.report = ReportState()
            self.phase = 'not_configured'
            self.changed.set()

    def snapshot(self):
        with self.lock:
            return {'configured': self.settings is not None, 'connection': self.phase,
                    'host': self.settings['host'] if self.settings else None,
                    'serial': self.settings['serial'] if self.settings else None,
                    **self.report.snapshot(self.clock(), self.connected)}

    def discover(self):
        if not self.scan_lock.acquire(blocking=False):
            raise ValueError('A printer search is already running.')
        try:
            found = {}
            with ExitStack() as stack:
                listeners = []
                for port in (1990, 2021):
                    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    stack.callback(listener.close)
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        listener.bind(('', port))
                    except OSError:
                        # A service may already own one port. Still try the other.
                        continue
                    for interface in self.policy.discovery_interfaces():
                        try:
                            membership = struct.pack('=4s4sI', socket.inet_aton('239.255.255.250'),
                                                     bytes(4), socket.if_nametoindex(interface))
                            listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                        except OSError:
                            # Broadcast announcements remain usable without multicast.
                            continue
                    listener.setblocking(False)
                    listeners.append(listener)
                if not listeners:
                    raise ValueError('Printer discovery ports are busy. Enter the printer details manually.')
                deadline = time.monotonic() + 8
                count = 0
                while time.monotonic() < deadline and not self.stop.is_set() and count < 128:
                    ready, _, _ = select.select(listeners, [], [], min(0.5, max(0, deadline - time.monotonic())))
                    for listener in ready:
                        if count >= 128:
                            break
                        try:
                            data, peer = listener.recvfrom(4097)
                        except BlockingIOError:
                            continue
                        count += 1
                        item = parse_announcement(data, peer[0], self.policy)
                        if item and len(found) < 32:
                            found[item['serial']] = item
            return list(found.values())
        finally:
            self.scan_lock.release()

    def run(self):
        delay = 2
        while not self.stop.is_set():
            with self.lock:
                settings, generation = self.settings, self.generation
            if settings is None:
                self.changed.wait(1)
                self.changed.clear()
                continue
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
                        # Status refresh only; never physical-control commands.
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
                    if client.loop(timeout=1) != 0:
                        raise ConnectionError()
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
                if client:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
            if generation != self.generation:
                continue
            if self.clock() - started > 60:
                delay = 2
            self.changed.wait(delay)
            self.changed.clear()
            delay = min(30, delay * 2)
