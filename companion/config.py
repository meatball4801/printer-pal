"""Shared, validated companion configuration. No credentials belong in command lines."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG = {"name": "Printer Pal camera", "port": 32187, "mode": "picamera2", "source_url": None, "device": None, "tls": True,
                  "remote_tailscale": False}


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    config = {**DEFAULT_CONFIG, **value}
    if type(config['tls']) is not bool:
        raise ValueError('TLS must be enabled or disabled explicitly')
    if type(config['remote_tailscale']) is not bool:
        raise ValueError('Tailscale access must be enabled or disabled explicitly')
    if config["mode"] not in ("auto", "picamera2", "mjpeg", "usb"):
        raise ValueError("Camera mode must be auto, picamera2, usb, or mjpeg")
    if config["mode"] == "usb" and (not isinstance(config["device"], str) or not re.fullmatch(r'/dev/(?:video\d+|v4l/by-id/usb-[^/\x00]+)', config["device"])):
        raise ValueError("USB capture requires a /dev/videoN or USB /dev/v4l/by-id camera device")
    if type(config["port"]) is not int or not 1024 <= config["port"] <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    if not isinstance(config["name"], str) or not config["name"].strip() or len(config["name"]) > 80:
        raise ValueError("Camera name must contain 1–80 characters")
    config["name"] = config["name"].strip()
    if config["mode"] == "mjpeg":
        url = config["source_url"]
        if not isinstance(url, str):
            raise ValueError("An existing MJPEG stream URL is required")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError("Use an HTTP/HTTPS MJPEG URL without embedded login credentials")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("Invalid camera source port") from None
        if parts.hostname in ("localhost", "127.0.0.1", "::1") and (port or (443 if parts.scheme == "https" else 80)) == config["port"]:
            raise ValueError("The existing camera must use a different port from Printer Pal")
    return config


def load_config(path):
    file = Path(path)
    return validate_config(json.loads(file.read_text()) if file.exists() else {})
