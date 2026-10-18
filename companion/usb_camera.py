"""Generic Linux UVC/V4L2 webcams, encoded as MJPEG by FFmpeg."""
import re
import selectors
import shutil
import subprocess
import time
from camera_source import JPEGReader


def input_options(device):
    command = shutil.which('v4l2-ctl')
    if not command:
        return []
    try:
        result = subprocess.run([command, '--device', device, '--list-formats-ext'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    mjpeg = re.search(r"^\s*\[\d+\]: 'MJPG'.*?(?=^\s*\[\d+\]:|\Z)", result.stdout, re.MULTILINE | re.DOTALL)
    prefix = ['-input_format', 'mjpeg'] if mjpeg else []
    formats = mjpeg.group(0) if mjpeg else result.stdout
    sizes = [(int(w), int(h)) for w, h in re.findall(r'Size: Discrete (\d+)x(\d+)', formats)]
    for preferred in ((1280, 720), (640, 480), (320, 240)):
        if preferred in sizes:
            return prefix + ['-video_size', f'{preferred[0]}x{preferred[1]}']
    return prefix  # Let the webcam negotiate its native default if sizes are non-discrete.


def stream_usb(device, frames, stop):
    command = shutil.which('ffmpeg')
    if not command:
        raise RuntimeError('USB capture requires ffmpeg')
    arguments = [command, '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'v4l2']
    arguments += input_options(device)
    arguments += ['-i', device, '-an', '-vf', 'fps=15,scale=w=min(1280\\,iw):h=-2',
                  '-c:v', 'mjpeg', '-q:v', '5', '-f', 'image2pipe', 'pipe:1']
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    reader = JPEGReader()
    last_frame = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not stop.is_set():
                for key, _ in selector.select(timeout=0.5):
                    chunk = key.fileobj.read1(65536)
                    if not chunk:
                        raise ConnectionError('USB capture exited')
                    for frame in reader.feed(chunk):
                        frames.write(frame)
                        last_frame = time.monotonic()
                if time.monotonic() - last_frame > 10:
                    raise TimeoutError('USB camera stopped producing frames')
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
