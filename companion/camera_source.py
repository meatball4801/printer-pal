"""Capture supervision and reuse of an existing HTTP MJPEG feed."""
import logging
import threading
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_FRAME_BYTES = 2 * 1024 * 1024


class JPEGReader:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > MAX_FRAME_BYTES:
                    raise ValueError("Upstream frame exceeds the size limit")
                break
            if end + 2 > MAX_FRAME_BYTES:
                raise ValueError("Upstream frame exceeds the size limit")
            frames.append(bytes(self.buffer[:end + 2]))
            del self.buffer[:end + 2]
        return frames


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def stream_mjpeg(url, frames, stop):
    request = Request(url, headers={"Accept": "multipart/x-mixed-replace", "User-Agent": "PrinterPal"})
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        if response.headers.get_content_type() != "multipart/x-mixed-replace":
            raise ValueError("Source is not a direct MJPEG stream")
        reader = JPEGReader()
        last_frame = time.monotonic()
        while not stop.is_set():
            chunk = response.read1(65536)
            if not chunk:
                raise ConnectionError("Upstream camera disconnected")
            for frame in reader.feed(chunk):
                frames.write(frame)
                last_frame = time.monotonic()
            if time.monotonic() - last_frame > 10:
                raise TimeoutError("Upstream stopped producing frames")


class CameraWorker:
    def __init__(self, config, frames, stop, camera_factory):
        self.config, self.frames, self.stop = config, frames, stop
        self.camera_factory = camera_factory
        self.thread = threading.Thread(target=self.run, name="camera-source", daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        delay = 2
        while not self.stop.is_set():
            camera = None
            started = time.monotonic()
            self.frames.clear()
            try:
                current = self.config
                if current["mode"] == "auto":
                    from autodetect import detect_source
                    detected, _ = detect_source(excluded_ports=(current.get("port", 32187),))
                    current = {**current, **detected}
                if current["mode"] == "usb":
                    from usb_camera import stream_usb
                    stream_usb(current["device"], self.frames, self.stop)
                elif current["mode"] == "mjpeg":
                    stream_mjpeg(current["source_url"], self.frames, self.stop)
                else:
                    camera = self.camera_factory(self.frames)
                    while not self.stop.wait(0.5):
                        if time.monotonic() - started > 10 and not self.frames.is_ready():
                            raise TimeoutError("Camera stopped producing frames")
                if self.stop.is_set():
                    break
            except Exception as error:
                # Never log upstream URLs, credentials, or response bodies.
                logging.warning("Camera source unavailable (%s); retrying", type(error).__name__)
            finally:
                self.frames.clear()
                if camera:
                    try:
                        camera.stop_recording()
                    except Exception:
                        pass
                    try:
                        camera.close()
                    except Exception:
                        pass
            if time.monotonic() - started > 30:
                delay = 2
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 30)
