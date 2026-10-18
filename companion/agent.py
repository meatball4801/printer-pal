#!/usr/bin/env python3
"""Print Monitor local camera companion. Python 3.9+, Picamera2 on Raspberry Pi OS."""
import argparse
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from config import load_config, validate_config
from camera_source import CameraWorker

VERSION = "0.6.3"


class FrameBuffer(io.BufferedIOBase):
    def __init__(self):
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.updated = 0.0

    def write(self, data):
        with self.condition:
            self.frame = bytes(data)
            self.sequence += 1
            self.updated = time.monotonic()
            self.condition.notify_all()
        return len(data)

    def next_frame(self, sequence, timeout=12):
        with self.condition:
            changed = self.condition.wait_for(lambda: self.sequence != sequence, timeout)
            return (self.sequence, self.frame) if changed else (sequence, None)

    def is_ready(self):
        with self.condition:
            return self.frame is not None and time.monotonic() - self.updated < 10

    def clear(self):
        with self.condition:
            self.frame = None
            self.updated = 0
            self.condition.notify_all()


class PairingState:
    def __init__(self, path, pairing_file=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = self._load()
        if self.data is None:
            self.data = {"device_id": secrets.token_hex(16), "tokens": []}
            self._save()
        self.code = None
        self.expires = 0
        self.failed_attempts = []
        self.pairing_file = Path(pairing_file) if pairing_file else None

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            data = None
        if (isinstance(data, dict) and isinstance(data.get("device_id"), str)
                and isinstance(data.get("tokens"), list)
                and all(isinstance(token, str) for token in data["tokens"])):
            return data
        # A damaged file (for example after SD-card corruption) would otherwise stop the
        # service on every restart. Keep it for inspection; phones must pair again.
        backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(self.path, backup)
        except OSError:
            pass
        logging.warning("Pairing state was unreadable and has been reset; pair phones again.")
        return None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(self.data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.path)

    def open_pairing(self):
        with self.lock:
            self.code = f"{secrets.randbelow(1000000):06d}"
            self.expires = time.monotonic() + 600
            self.failed_attempts = []
            if self.pairing_file:
                self.pairing_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.pairing_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump({"code": self.code, "expires_at": time.time() + 600,
                               "expires_monotonic": self.expires}, output)
            return self.code

    def is_open(self):
        with self.lock:
            return self.code is not None and time.monotonic() < self.expires

    def pair(self, code):
        with self.lock:
            now = time.monotonic()
            self.failed_attempts = [t for t in self.failed_attempts if now - t < 60]
            if len(self.failed_attempts) >= 5:
                return 429, {"error": "Too many attempts. Wait one minute."}
            if self.code is None or now >= self.expires:
                return 409, {"error": "Pairing is closed. Open a new pairing window on the Pi."}
            if (not isinstance(code, str) or len(code) != 6 or not code.isascii()
                    or not code.isdecimal() or not hmac.compare_digest(code, self.code)):
                self.failed_attempts.append(now)
                return 403, {"error": "That pairing code did not match."}
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            previous = list(self.data["tokens"])
            self.data["tokens"] = (previous + [digest])[-8:]
            try:
                self._save()
            except OSError:
                self.data["tokens"] = previous
                return 500, {"error": "Could not save pairing. Check the Pi storage."}
            self.code = None
            if self.pairing_file:
                self.pairing_file.unlink(missing_ok=True)
            return 200, {"token": token, "device_id": self.data["device_id"], "stream_path": "/stream"}

    def authorized(self, header):
        if not header or not header.startswith("Bearer ") or len(header) > 256:
            return False
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        with self.lock:
            return any(hmac.compare_digest(digest, saved) for saved in self.data["tokens"])

    def forget_phones(self):
        with self.lock:
            previous = self.data['tokens']
            self.data['tokens'] = []
            try:
                self._save()
            except OSError:
                self.data['tokens'] = previous
                raise
            self.code = None
            if self.pairing_file:
                self.pairing_file.unlink(missing_ok=True)


class CameraServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state, frames, name="Printer camera", demo=False, tls_context=None, network_policy=None):
        super().__init__(address, CameraHandler)
        self.state, self.frames, self.name, self.demo = state, frames, name, demo
        self.started = time.monotonic()
        self.stream_slots = threading.BoundedSemaphore(4)
        self.request_slots = threading.BoundedSemaphore(32)
        self.tls_context, self.network_policy = tls_context, network_policy
        self.printer = None

    def verify_request(self, request, client_address):
        return self.network_policy is None or self.network_policy.allows(client_address[0])

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        connection = request
        try:
            request.settimeout(5)
            if self.tls_context:
                connection = self.tls_context.wrap_socket(request, server_side=True)
            self.finish_request(connection, client_address)
        except OSError:
            pass
        except Exception as error:
            logging.warning('Request failed (%s)', type(error).__name__)
        finally:
            self.shutdown_request(connection)
            self.request_slots.release()


class DualStackCameraServer(CameraServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(host, port, state, frames, name, demo=False, tls_context=None, network_policy=None):
    # Bonjour may resolve either family. Accept IPv4 and IPv6 on the same port.
    if host == "0.0.0.0" and socket.has_dualstack_ipv6():
        return DualStackCameraServer(("::", port), state, frames, name, demo, tls_context, network_policy)
    return CameraServer((host, port), state, frames, name, demo, tls_context, network_policy)


class BoundedRequestInput:
    def __init__(self, source):
        self.source, self.header_bytes = source, 0

    def readline(self, size=-1):
        value = self.source.readline(min(size if size >= 0 else 16385, 16385))
        self.header_bytes += len(value)
        if self.header_bytes > 16384:
            raise ValueError('Request headers exceed the limit')
        return value

    def __getattr__(self, name):
        return getattr(self.source, name)


class CameraHandler(BaseHTTPRequestHandler):
    server_version = 'PrinterPal'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self.rfile = BoundedRequestInput(self.rfile)

    def log_message(self, *_):
        # Avoid logging pairing bodies, authorization headers, or stream credentials.
        pass

    def send_json(self, status, body, reason=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        if reason:
            # The app cancels error responses before reading the body.
            self.send_header("X-Printer-Pal-Reason", reason)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/pair", "/api/printer"):
            self.send_json(404, {"error": "Not found"})
            return
        if path == '/api/printer':
            if not self.server.state.authorized(self.headers.get('Authorization')):
                self.send_json(401, {'error': 'Pair this camera before configuring a printer.'})
                return
            if self.server.tls_context is None:
                self.send_json(403, {'error': 'Printer setup requires HTTPS.'})
                return
            if self.server.printer is None:
                self.send_json(503, {'error': 'Printer monitoring is unavailable.'})
                return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError()
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send_json(400, {"error": "Invalid setup request"})
            return
        if path == '/api/printer':
            try:
                self.server.printer.configure(body)
            except ValueError as error:
                self.send_json(400, {'error': str(error)})
            except OSError:
                self.send_json(500, {'error': 'Could not save printer settings on the Pi.'})
            else:
                self.send_json(200, self.server.printer.snapshot())
            return
        status, result = self.server.state.pair(body.get("code"))
        self.send_json(status, result)

    def do_DELETE(self):
        if urlsplit(self.path).path != '/api/printer':
            self.send_json(404, {'error': 'Not found'})
            return
        if not self.server.state.authorized(self.headers.get('Authorization')):
            self.send_json(401, {'error': 'Pair this camera before configuring a printer.'})
            return
        if self.server.tls_context is None or self.server.printer is None:
            self.send_json(403, {'error': 'Printer setup requires the current HTTPS companion.'})
            return
        try:
            self.server.printer.forget()
        except OSError:
            self.send_json(500, {'error': 'Could not remove printer settings.'})
        else:
            self.send_json(200, self.server.printer.snapshot())

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/info":
            self.send_json(200, {"protocol": 1, "version": VERSION, "name": self.server.name,
                                 "pairing_open": self.server.state.is_open(),
                                 "demo": self.server.demo})
            return
        if path not in ("/api/status", "/api/printer", "/api/printers/discover", "/snapshot", "/stream"):
            self.send_json(404, {"error": "Not found"})
            return
        if not self.server.state.authorized(self.headers.get("Authorization")):
            self.send_json(401, {"error": "Pair this camera before viewing."})
            return
        if path == "/api/status":
            printer = self.server.printer.snapshot() if self.server.printer else {}
            self.send_json(200, {"camera_ready": self.server.frames.is_ready(),
                                 "uptime_seconds": int(time.monotonic() - self.server.started),
                                 "printer_connected": printer.get('fresh', False), "demo": self.server.demo})
            return
        if path in ('/api/printer', '/api/printers/discover'):
            if self.server.printer is None:
                self.send_json(503, {'error': 'Printer monitoring is unavailable.'})
            elif path == '/api/printer':
                self.send_json(200, self.server.printer.snapshot())
            else:
                try:
                    self.send_json(200, {'printers': self.server.printer.discover()})
                except (OSError, ValueError):
                    self.send_json(503, {'error': 'Printer discovery is unavailable. Enter its local address and serial number instead.'})
            return
        if not self.server.frames.is_ready():
            self.send_json(503, {"error": "Camera is not producing frames."}, reason="camera-not-ready")
            return
        if path == "/snapshot":
            with self.server.frames.condition:
                frame = self.server.frames.frame
            if frame is None:
                self.send_json(503, {"error": "Camera is reconnecting."})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(frame)
            return
        if not self.server.stream_slots.acquire(blocking=False):
            self.send_json(503, {"error": "All camera viewer slots are in use."}, reason="viewers-full")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sequence = -1
            while True:
                if not self.server.state.authorized(self.headers.get('Authorization')):
                    break
                sequence, frame = self.server.frames.next_frame(sequence)
                if frame is None:
                    break
                self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            self.server.stream_slots.release()


def start_camera(frames):
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    camera = Picamera2()
    try:
        config = camera.create_video_configuration(main={"size": (1280, 720)},
                                                   controls={"FrameRate": 15})
        camera.configure(config)
        camera.start_recording(MJPEGEncoder(), FileOutput(frames))
    except Exception:
        camera.close()
        raise
    return camera


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--config", type=Path, default=Path("/etc/print-monitor/config.json"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--name")
    parser.add_argument("--pairing-file", type=Path)
    parser.add_argument("--state", default="/var/lib/print-monitor/state.json")
    parser.add_argument("--pair", action="store_true", help="Open pairing on startup even if already paired")
    parser.add_argument("--test-image", type=Path, help="Development only: stream a JPEG fixture instead of a Pi camera")
    parser.add_argument("--insecure-http", action="store_true", help="Development only: HTTP on loopback")
    args = parser.parse_args()
    if args.insecure_http and args.host not in ('127.0.0.1', '::1', 'localhost'):
        parser.error('--insecure-http is restricted to loopback development')
    try:
        config = load_config(args.config)
        for key in ("port", "name"):
            if getattr(args, key) is not None:
                config[key] = getattr(args, key)
        config = validate_config(config)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    state = PairingState(args.state, args.pairing_file)
    frames = FrameBuffer()
    worker = None
    stop = threading.Event()
    if args.test_image:
        image = args.test_image.read_bytes()
        if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
            parser.error("The test fixture must be a JPEG")
        def produce():
            while not stop.is_set():
                frames.write(image)
                stop.wait(0.2)
        threading.Thread(target=produce, daemon=True).start()
    else:
        worker = CameraWorker(config, frames, stop, start_camera)
        worker.start()
    from network_guard import LANPolicy
    from transport import server_context
    context = server_context() if config['tls'] and not args.insecure_http else None
    server = make_server(args.host, config["port"], state, frames, config["name"], bool(args.test_image), context, LANPolicy())
    from bambu_monitor import PrinterMonitor
    server.printer = PrinterMonitor(Path(args.state).with_name('printer.json'), server.network_policy, stop)
    server.printer.start()

    def open_pairing(*_):
        code = state.open_pairing()
        if args.pairing_file:
            print("Pairing is open. Run: sudo print-monitor pair", flush=True)
        else:
            print(f"Pairing code: {code} (one use, expires in 10 minutes)", flush=True)

    if not state.data["tokens"] or args.pair:
        open_pairing()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, open_pairing)

    def forget_phones(*_):
        try:
            state.forget_phones()
        except OSError:
            # Raising inside a signal handler would stop serve_forever.
            logging.warning("Could not save revoked phone access; previous access is kept.")

    signal.signal(signal.SIGUSR2, forget_phones)

    def shutdown(*_):
        stop.set()
        server.printer.changed.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Printer Pal {VERSION} listening on port {server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.thread.join(timeout=12)
        server.printer.thread.join(timeout=3)


if __name__ == "__main__":
    main()
