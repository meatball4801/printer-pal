from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import BadStatusLine
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autodetect import configured_urls, detect_source, find_existing_feed, listening_ports, local_url, probe_mjpeg, usb_capture_devices
from config import validate_config
from usb_camera import input_options


class DetectionTests(unittest.TestCase):
    def test_non_http_listener_is_skipped_without_aborting_detection(self):
        with patch('autodetect.build_opener') as opener:
            opener.return_value.open.side_effect = BadStatusLine('SSH-2.0-server')
            self.assertFalse(probe_mjpeg('http://127.0.0.1:22/stream'))

    def test_only_listening_local_ports_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'net').mkdir()
            (root / 'net/tcp').write_text('header\n0: 00000000:1F90 00000000:0000 0A\n1: 00000000:1F91 00000000:0000 01\n')
            self.assertEqual(listening_ports(root), {8080})

    def test_config_urls_stay_on_this_device_and_do_not_extract_credentials(self):
        names = {'localhost', '127.0.0.1', 'printer-camera.local'}
        self.assertEqual(local_url('http://printer-camera.local:8080/stream', names), 'http://127.0.0.1:8080/stream')
        self.assertIsNone(local_url('http://another-pi.local/stream', names))
        self.assertIsNone(local_url('http://user:secret@localhost/stream', names))

    def test_known_camera_config_and_stream_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'home/pi/.octoprint/config.yaml'
            file.parent.mkdir(parents=True)
            file.write_text('webcam:\n  stream: http://localhost:8080/?action=stream\n  secret: must-not-be-returned\n')
            self.assertEqual(configured_urls(root / 'home', root / 'etc'), ['http://127.0.0.1:8080/?action=stream'])
            result = find_existing_feed(proc=root / 'proc', home=root / 'home', etc=root / 'etc', probe=lambda url: url.endswith('?action=stream'))
            self.assertEqual(result, 'http://127.0.0.1:8080/?action=stream')
            self.assertIsNone(find_existing_feed(excluded_ports=(8080,), proc=root / 'proc', home=root / 'home', etc=root / 'etc', probe=lambda _: True))

    def test_source_priority_and_no_camera_auto_fallback(self):
        with patch('autodetect.find_existing_feed', return_value='http://127.0.0.1:8080/stream'):
            config, _ = detect_source()
            self.assertEqual(config['mode'], 'mjpeg')
        with patch('autodetect.find_existing_feed', return_value=None), patch('autodetect.usb_capture_devices', return_value=['/dev/video0']), patch('autodetect.csi_camera_present', return_value=False):
            config, _ = detect_source()
            self.assertEqual(config, {'mode': 'usb', 'source_url': None, 'device': '/dev/video0'})
        with patch('autodetect.find_existing_feed', return_value=None), patch('autodetect.usb_capture_devices', return_value=[]), patch('autodetect.csi_camera_present', return_value=True):
            self.assertEqual(detect_source()[0]['mode'], 'picamera2')

    def test_usb_metadata_and_pi_codec_nodes_are_not_camera_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, dev = root / 'sys', root / 'dev'
            sysfs.mkdir(); dev.mkdir()
            usb = root / 'devices/usb1/camera'; usb.mkdir(parents=True)
            codec = root / 'devices/platform/codec'; codec.mkdir(parents=True)
            for index, parent in [(0, usb), (1, usb), (10, codec)]:
                entry = sysfs / f'video{index}'; entry.mkdir()
                (entry / 'device').symlink_to(parent)
                (dev / entry.name).touch()
            def query(fd, request, data, mutate):
                # First USB node captures; the second is metadata only.
                index = query.count; query.count += 1
                struct.pack_into('I', data, 84, 0x80000000)
                struct.pack_into('I', data, 88, 0x1 if index == 0 else 0x00800000)
            query.count = 0
            with patch('autodetect.fcntl.ioctl', side_effect=query):
                self.assertEqual(usb_capture_devices(sysfs, dev), [str(dev / 'video0')])
            self.assertEqual(query.count, 2)  # The Pi codec was not even opened.

    def test_usb_input_uses_a_supported_mjpeg_resolution(self):
        formats = "[0]: 'YUYV'\n Size: Discrete 640x480\n[1]: 'MJPG'\n Size: Discrete 1280x720\n Size: Discrete 640x480\n"
        with patch('usb_camera.shutil.which', return_value='/usr/bin/v4l2-ctl'), patch('usb_camera.subprocess.run', return_value=subprocess.CompletedProcess([], 0, formats, '')):
            self.assertEqual(input_options('/dev/video0'), ['-input_format', 'mjpeg', '-video_size', '1280x720'])
        self.assertEqual(validate_config({'mode': 'usb', 'device': '/dev/v4l/by-id/usb-camera-video-index0'})['mode'], 'usb')
        with self.assertRaises(ValueError):
            validate_config({'mode': 'usb', 'device': '/etc/passwd'})

    def test_probe_requires_actual_multipart_frame(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame' if self.path == '/stream' else 'text/html')
                self.end_headers()
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8fixture\xff\xd9\r\n')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            self.assertTrue(probe_mjpeg(url + '/stream'))
            self.assertFalse(probe_mjpeg(url + '/dashboard'))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

