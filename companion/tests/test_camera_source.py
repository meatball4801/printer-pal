from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import FrameBuffer, PairingState
from camera_source import CameraWorker, JPEGReader, MAX_FRAME_BYTES, stream_mjpeg
from usb_camera import stream_usb
from unittest.mock import patch
from control import remaining_seconds


class SourceTests(unittest.TestCase):
    def test_first_boot_clock_sync_does_not_expire_displayed_pairing_code(self):
        with patch('control.time.monotonic', return_value=100), patch('control.time.time', return_value=9999999999):
            self.assertEqual(remaining_seconds({'expires_at': 10, 'expires_monotonic': 700}), 600)
            self.assertEqual(remaining_seconds({'expires_at': 10, 'expires_monotonic': 50}), 0)

    def test_usb_capture_reads_encoder_output_and_stops_the_process(self):
        with tempfile.TemporaryDirectory() as directory:
            encoder = Path(directory) / 'encoder'
            encoder.write_text('#!' + sys.executable + '\nimport os,time\nwhile True:\n os.write(1,b"\\xff\\xd8encoder-frame\\xff\\xd9")\n time.sleep(0.05)\n')
            encoder.chmod(0o755)
            stop, frames, errors = threading.Event(), FrameBuffer(), []
            def capture():
                try:
                    stream_usb('/dev/video0', frames, stop)
                except Exception as error:
                    errors.append(error)
            with patch('usb_camera.shutil.which', side_effect=lambda name: str(encoder) if name == 'ffmpeg' else None):
                thread = threading.Thread(target=capture)
                thread.start()
                try:
                    deadline = time.monotonic() + 3
                    while not frames.is_ready() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(frames.frame, b'\xff\xd8encoder-frame\xff\xd9')
                finally:
                    stop.set()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_fragmented_upstream_frames_and_multiple_frames(self):
        reader = JPEGReader()
        first, second = b'\xff\xd8first\xff\xd9', b'\xff\xd8second\xff\xd9'
        self.assertEqual(reader.feed(b'--boundary\r\n\xff'), [])
        self.assertEqual(reader.feed(first[1:3]), [])
        self.assertEqual(reader.feed(first[3:] + b'\r\n--boundary\r\n' + second), [first, second])

    def test_unbounded_or_oversized_upstream_data_is_rejected(self):
        reader = JPEGReader()
        reader.feed(b'x' * MAX_FRAME_BYTES)
        self.assertLessEqual(len(reader.buffer), 1)
        with self.assertRaises(ValueError):
            reader.feed(b'\xff\xd8' + b'x' * MAX_FRAME_BYTES)

    def test_pairing_code_local_file_is_private_and_removed_after_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'pairing.json'
            state = PairingState(Path(directory) / 'state.json', file)
            code = state.open_pairing()
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state.pair(code)[0], 200)
            self.assertFalse(file.exists())

    def test_upstream_disconnect_recovers_without_manual_restart(self):
        first = threading.Event()
        disconnect = threading.Event()
        recovered = threading.Event()
        stop = threading.Event()
        counter = [0]
        frames = FrameBuffer()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                counter[0] += 1
                count = counter[0]
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=test')
                self.end_headers()
                frame = b'\xff\xd8' + str(count).encode() + b'\xff\xd9'
                self.wfile.write(b'--test\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                self.wfile.flush()
                if count == 1:
                    first.set()
                    disconnect.wait(5)
                else:
                    recovered.set()
                    stop.wait(5)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = CameraWorker({'mode': 'mjpeg', 'source_url': f'http://127.0.0.1:{server.server_port}/stream'}, frames, stop, None)
        try:
            worker.start()
            self.assertTrue(first.wait(3))
            deadline = time.monotonic() + 2
            while not frames.is_ready() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(frames.is_ready())
            disconnect.set()
            self.assertTrue(recovered.wait(6))
            deadline = time.monotonic() + 2
            while not frames.is_ready() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(frames.is_ready())
            self.assertEqual(frames.frame, b'\xff\xd82\xff\xd9')
        finally:
            stop.set()
            disconnect.set()
            worker.thread.join(12)
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_html_page_is_not_treated_as_video(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html>camera</html>')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(ValueError, 'MJPEG'):
                stream_mjpeg(f'http://127.0.0.1:{server.server_port}/', FrameBuffer(), threading.Event())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
