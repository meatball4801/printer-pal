# Companion development

## Layout

| File | What it does |
| --- | --- |
| `agent.py` | The HTTPS server: pairing, status, snapshots, and the MJPEG stream |
| `camera_source.py`, `usb_camera.py`, `autodetect.py` | Pi camera, USB webcam, and existing-feed capture and detection |
| `bambu_monitor.py` | Read-only Bambu Lab status over the printer's local MQTT |
| `network_guard.py` | The firewall boundary for the companion port (nftables and UFW) |
| `transport.py` | The device's TLS identity |
| `install.py`, `install.sh` | The installer that runs on the Pi |
| `control.py` | The `print-monitor` command |
| `bootstrap.sh` | Template for the release's `install.sh` (the one-line installer) |
| `build_release.py` | Builds the release files into `dist/` |

## Protocol

Cameras speak a small HTTPS protocol, which Printer Pal Camera on iOS also implements:

- `GET /api/info`: name, protocol version, whether pairing is open
- `POST /api/pair` with `{"code": "123456"}`: returns an access token
- `GET /api/status`, `GET /snapshot`, and `GET /stream` (MJPEG, boundary `FRAME`): need
  `Authorization: Bearer <token>`
- `/api/printer` and `/api/printers/discover`: Bambu status
- A 503 for the stream carries `X-Printer-Pal-Reason: camera-not-ready` or `viewers-full`

Cameras advertise `_printmonitor._tcp` with a `tls=1` TXT record. The setup QR is
`https://<host>:<port>/#pin=<sha256 of the certificate>&code=<6 digits>`.

## Tests

```bash
python3 -m unittest discover -s companion/tests
```

Tests need Python's `cryptography` and `paho-mqtt` packages. CI also runs the firewall checks in an isolated network
namespace and the real UFW inside the systemd sandbox (`companion/tests/linux-*.sh`).

## Trying changes

- Install your working copy on a Pi from a Mac or Linux machine:
  `bash companion/install-on-pi.sh pi@printer-camera.local`.
- Serve a local release so a Debug build of the app's **Install from this iPhone** can use it:
  `bash companion/serve-dev-release.sh`.
- Run the agent locally without a camera:
  `python3 companion/agent.py --host 127.0.0.1 --insecure-http --test-image fixture.jpg --state /tmp/pp/state.json --pair`.
  The HTTP flag only works on loopback.

## Releases

Set `VERSION` in `agent.py`, then push a `companion-vX.Y.Z` tag. The **Publish companion release** workflow runs the
tests, checks that the tag matches `VERSION`, builds the installer, and publishes it as the latest release. The
one-line installer always downloads `install.sh` from the latest release, and each `install.sh` pins its release's
installer checksum.
