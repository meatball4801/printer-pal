import http.client
import json
import socket
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import VERSION, CameraServer, FrameBuffer, PairingState, make_server


class NetworkTests(unittest.TestCase):
    @unittest.skipUnless(socket.has_dualstack_ipv6(), 'Dual stack is not available on this host')
    def test_default_listener_serves_ipv4_and_ipv6(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PairingState(Path(directory) / 'state.json')
            server = make_server('0.0.0.0', 0, state, FrameBuffer(), 'Network test')
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for host in ['127.0.0.1', '::1']:
                    connection = http.client.HTTPConnection(host, server.server_address[1], timeout=3)
                    try:
                        connection.request('GET', '/api/info')
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read())['protocol'], 1)
                    finally:
                        connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.state = PairingState(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_pairing_is_closed_until_opened(self):
        self.assertFalse(self.state.is_open())
        self.assertEqual(self.state.pair("123456")[0], 409)

    def test_code_is_one_use_and_token_survives_restart(self):
        code = self.state.open_pairing()
        status, result = self.state.pair(code)
        self.assertEqual(status, 200)
        token = result["token"]
        self.assertTrue(self.state.authorized("Bearer " + token))
        self.assertNotIn(token, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        restarted = PairingState(self.path)
        self.assertTrue(restarted.authorized("Bearer " + token))
        self.assertFalse(restarted.is_open())
        self.assertEqual(self.state.pair(code)[0], 409)

    def test_corrupt_state_is_set_aside_and_reset(self):
        for damaged in ("{not json", '{"tokens": "abc"}', "[]"):
            self.path.write_text(damaged)
            restarted = PairingState(self.path)
            self.assertEqual(restarted.data["tokens"], [])
            self.assertIsInstance(restarted.data["device_id"], str)
            self.assertEqual(json.loads(self.path.read_text())["tokens"], [])
            backups = list(self.path.parent.glob("state.json.corrupt-*"))
            self.assertTrue(backups)
            self.assertEqual(backups[0].read_text(), damaged)
            for backup in backups:
                backup.unlink()

    def test_wrong_code_and_expired_code(self):
        self.state.open_pairing()
        self.assertEqual(self.state.pair("not-a-code")[0], 403)
        self.state.expires = time.monotonic() - 1
        self.assertFalse(self.state.is_open())
        self.assertEqual(self.state.pair(self.state.code)[0], 409)

    def test_non_ascii_and_non_string_code(self):
        self.state.open_pairing()
        for code in [None, 123456, "١٢٣٤٥٦", {}, "12345"]:
            self.assertEqual(self.state.pair(code)[0], 403)

    def test_pairing_rate_limit(self):
        code = self.state.open_pairing()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(self.state.pair(wrong)[0], 403)
        self.assertEqual(self.state.pair(code)[0], 429)
        self.state.failed_attempts = [time.monotonic() - 61] * 5
        self.assertEqual(self.state.pair(code)[0], 200)

    def test_invalid_tokens_are_rejected(self):
        code = self.state.open_pairing()
        token = self.state.pair(code)[1]["token"]
        for header in [None, "", "Bearer wrong", "Basic " + token, "Bearer " + "x" * 300]:
            self.assertFalse(self.state.authorized(header))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = PairingState(Path(self.temp.name) / "state.json")
        self.code = self.state.open_pairing()
        self.frames = FrameBuffer()
        self.server = CameraServer(("127.0.0.1", 0), self.state, self.frames, demo=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Authorization": "Bearer " + token} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    def pair(self):
        status, data = self.request("/api/pair", "POST", json.dumps({"code": self.code}))
        self.assertEqual(status, 200)
        return json.loads(data)["token"]

    def test_discovery_info_contains_no_pairing_secret(self):
        status, data = self.request("/api/info")
        info = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(info["protocol"], 1)
        self.assertTrue(info["pairing_open"])
        self.assertNotIn("code", info)
        self.assertNotIn("tokens", info)
        self.assertNotIn('device_id', info)
        self.assertEqual(info["version"], VERSION)

    def test_video_and_status_require_authentication(self):
        for path in ["/stream", "/snapshot", "/api/status"]:
            self.assertEqual(self.request(path)[0], 401)

    def test_pair_then_status_and_snapshot(self):
        token = self.pair()
        self.assertEqual(self.request("/snapshot", token=token)[0], 503)
        frame = b"\xff\xd8fixture\xff\xd9"
        self.frames.write(frame)
        status, data = self.request("/snapshot", token=token)
        self.assertEqual((status, data), (200, frame))
        _, data = self.request("/api/status", token=token)
        self.assertTrue(json.loads(data)["camera_ready"])
        self.assertFalse(json.loads(data)["printer_connected"])

    def test_malformed_pairing_requests(self):
        for body in ["not json", "[]", "{}", "x" * 2048]:
            status, _ = self.request("/api/pair", "POST", body)
            self.assertIn(status, [400, 403])

    def test_stream_delivers_multipart_frame(self):
        token = self.pair()
        frame = b"\xff\xd8test-frame\xff\xd9"
        self.frames.write(frame)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/stream", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("multipart/x-mixed-replace", response.getheader("Content-Type"))
        self.assertEqual(response.readline(), b"--FRAME\r\n")
        self.assertEqual(response.readline(), b"Content-Type: image/jpeg\r\n")
        self.assertEqual(response.readline(), f"Content-Length: {len(frame)}\r\n".encode())
        self.assertEqual(response.readline(), b"\r\n")
        self.assertEqual(response.read(len(frame)), frame)
        connection.close()
        self.frames.write(frame)

    def test_stale_camera_is_reported(self):
        token = self.pair()
        self.frames.write(b"frame")
        self.frames.updated = time.monotonic() - 11
        self.assertEqual(self.request("/stream", token=token)[0], 503)
        _, data = self.request("/api/status", token=token)
        self.assertFalse(json.loads(data)["camera_ready"])

    def test_unavailable_stream_reports_reason_header(self):
        token = self.pair()

        def reason():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
            connection.request("GET", "/stream", headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status, response.getheader("X-Printer-Pal-Reason")

        self.assertEqual(reason(), (503, "camera-not-ready"))
        self.frames.write(b"frame")
        for _ in range(4):
            self.server.stream_slots.acquire()
        try:
            self.assertEqual(reason(), (503, "viewers-full"))
        finally:
            for _ in range(4):
                self.server.stream_slots.release()

    def test_unknown_routes(self):
        self.assertEqual(self.request("/not-found")[0], 404)


class BufferTests(unittest.TestCase):
    def test_no_frame_timeout_and_sequence_changes(self):
        frames = FrameBuffer()
        self.assertFalse(frames.is_ready())
        self.assertEqual(frames.next_frame(0, timeout=0.001), (0, None))
        frames.write(b"one")
        self.assertEqual(frames.next_frame(0, timeout=0.001), (1, b"one"))
        frames.write(b"two")
        self.assertEqual(frames.next_frame(1, timeout=0.001), (2, b"two"))


if __name__ == "__main__":
    unittest.main()
