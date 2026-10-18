"""Inspect only this Pi for existing video, CSI cameras, and real USB capture devices."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
import fcntl
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener
from camera_source import JPEGReader, NoRedirect

PATHS = ('/?action=stream', '/stream', '/', '/stream.mjpg', '/video', '/mjpeg', '/webcam/?action=stream')
PREFERRED_PORTS = (8080, 8081, 8082, 8000, 5000, 8888, 80)


def listening_ports(proc=Path('/proc')):
    result = set()
    for name in ('tcp', 'tcp6'):
        try:
            lines = (proc / 'net' / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 3 and fields[3] == '0A':
                try:
                    result.add(int(fields[1].split(':')[1], 16))
                except (ValueError, IndexError):
                    pass
    return result


def local_url(value, local_names):
    """Do not scan other computers or extract embedded account credentials."""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in ('http', 'https') or parts.hostname not in local_names or parts.username or parts.password:
            return None
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        return urlunsplit((parts.scheme, f'127.0.0.1:{port}', parts.path, parts.query, ''))
    except ValueError:
        return None


def configured_urls(home=Path('/home'), etc=Path('/etc')):
    names = {'127.0.0.1', 'localhost', '0.0.0.0', '::1', socket.gethostname(), socket.gethostname() + '.local'}
    try:
        names.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    files = list(home.glob('*/.octoprint/config.yaml')) + list(home.glob('*/printer_data/config/crowsnest.conf'))
    files += list(home.glob('*/printer_data/config/moonraker.conf')) + [etc / 'crowsnest.conf']
    found = []
    for file in files:
        try:
            # Read only bounded, known camera configuration files; never print their contents.
            if file.stat().st_size > 512 * 1024:
                continue
            lines = file.read_text().splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            match = re.match(r'\s*(?:stream|stream_url|streamUrl)\s*[:=]\s*[\'"]?(https?://[^\s\'"]+)', line)
            if match:
                url = local_url(match.group(1), names)
                if url:
                    found.append(url)
            match = re.match(r'\s*port\s*[:=]\s*(\d+)\s*(?:#.*)?$', line)
            if match and 1 <= int(match.group(1)) <= 65535:
                found.extend(f'http://127.0.0.1:{match.group(1)}{path}' for path in PATHS)
    return list(dict.fromkeys(found))


def probe_mjpeg(url):
    try:
        request = Request(url, headers={'Accept': 'multipart/x-mixed-replace', 'User-Agent': 'PrinterPal-detection/0.4'})
        with build_opener(NoRedirect()).open(request, timeout=1.5) as response:
            if response.headers.get_content_type() != 'multipart/x-mixed-replace':
                return False
            reader, total, deadline = JPEGReader(), 0, time.monotonic() + 3
            while time.monotonic() < deadline and total < 2 * 1024 * 1024:
                chunk = response.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if reader.feed(chunk):
                    return True
    except (OSError, ValueError, HTTPException):
        pass
    return False


def find_existing_feed(excluded_ports=(), proc=Path('/proc'), home=Path('/home'), etc=Path('/etc'), probe=probe_mjpeg):
    candidates = configured_urls(home, etc)
    ports = listening_ports(proc) - set(excluded_ports)
    ordered = [p for p in PREFERRED_PORTS if p in ports] + sorted(ports - set(PREFERRED_PORTS))
    # Bounded, loopback-only probes; no subnet scan and no camera control requests.
    candidates += [f'http://127.0.0.1:{port}{path}' for port in ordered[:16] for path in PATHS]
    candidates = [url for url in dict.fromkeys(candidates) if urlsplit(url).port not in excluded_ports]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for offset in range(0, len(candidates), 6):
            batch = candidates[offset:offset + 6]
            results = list(pool.map(probe, batch))
            for url, working in zip(batch, results):
                if working:
                    return url
    return None


def usb_capture_devices(sysfs=Path('/sys/class/video4linux'), dev=Path('/dev')):
    devices = []
    for entry in sorted(sysfs.glob('video*')):
        if not re.fullmatch(r'video\d+', entry.name):
            continue
        # Pi ISP/codec nodes also look like video devices. Require USB ancestry.
        if not any(part.startswith('usb') for part in (entry / 'device').resolve().parts):
            continue
        path = dev / entry.name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                data = bytearray(104)  # struct v4l2_capability; VIDIOC_QUERYCAP
                fcntl.ioctl(fd, 0x80685600, data, True)
                caps = struct.unpack_from('I', data, 84)[0]
                if caps & 0x80000000:
                    caps = struct.unpack_from('I', data, 88)[0]
                if caps & (0x00000001 | 0x00001000):
                    stable = next((p for p in (dev / 'v4l/by-id').glob('usb-*') if p.resolve() == path.resolve()), path)
                    devices.append(str(stable))
            finally:
                os.close(fd)
        except OSError:
            continue
    return devices


def csi_camera_present():
    command = shutil.which('rpicam-hello') or shutil.which('libcamera-hello')
    if command:
        try:
            result = subprocess.run([command, '--list-cameras'], capture_output=True, text=True, timeout=5)
            if re.search(r'^\s*\d+\s*:\s*(?:imx\d|ov\d|ar\d)', result.stdout, re.MULTILINE):
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Camera Module v2 sensor; kernel device binding exists only after discovery.
    return any(Path('/sys/bus/i2c/drivers/imx219').glob('*-00*'))


def detect_source(excluded_ports=()):
    feed = find_existing_feed(excluded_ports)
    if feed:
        return {'mode': 'mjpeg', 'source_url': feed, 'device': None}, 'Reusing a working local MJPEG camera feed.'
    devices = usb_capture_devices()
    if csi_camera_present():
        return {'mode': 'picamera2', 'source_url': None, 'device': None}, 'Attached Raspberry Pi camera detected.'
    if devices:
        return {'mode': 'usb', 'source_url': None, 'device': devices[0]}, f'USB capture camera detected: {devices[0]}.'
    return {'mode': 'picamera2', 'source_url': None, 'device': None}, 'No working feed or USB camera found. Setting up Pi camera capture; it will retry until a camera is available.'
