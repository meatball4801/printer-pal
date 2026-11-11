# Companion development

## Layout

| File | What it does |
| --- | --- |
| `agent.py` | The HTTPS server: pairing, status, snapshots, and the MJPEG stream |
| `camera_source.py`, `usb_camera.py`, `autodetect.py` | Pi camera, USB webcam, and existing-feed capture and detection |
| `print_status.py` | The printer status monitor: Klipper (Moonraker) and OctoPrint over local HTTP, and Bambu |
| `bambu_monitor.py` | The Bambu Lab local MQTT protocol: settings, TLS, reports, and discovery |
| `timelapse.py` | Timelapse recording, storage limits, and cleanup |
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
- Printer status, all with the same token:
  - `GET /api/printer`: `configured`, `kind` (`bambu`, `klipper`, `octoprint`, or `null` when none is set up),
    `connection`, `fresh`, `age_seconds`, `host`, `serial`, and `fields` (`state`, `progress_percent`,
    `remaining_minutes`, `current_layer`, `total_layers`, `nozzle_temp`, `bed_temp`, `job_name`, `active_tray`,
    `ams_slots`). For Klipper and OctoPrint, `host` comes from the URL, `serial` is `null`, the AMS fields are `null`,
    and layers are `null` unless Moonraker reports them. `state` is `IDLE`, `PREPARE`, `RUNNING`, `PAUSE`, `FINISH`,
    or `FAILED` for every kind.
  - `connection` is `not_configured`, `invalid_configuration`, `connecting`, `waiting`, `connected`, `disconnected`,
    `authentication_failed`, `certificate_failed`, `network_unavailable`, `dependency_missing`, or `unreachable`.
    `waiting` for Klipper or OctoPrint means the server answered but the printer isn't ready; `unreachable` means no
    usable answer from the URL. `disconnected` and `dependency_missing` are Bambu only.
  - `POST /api/printer`: `{"host", "serial", "access_code"}` (no `kind`, or `"kind": "bambu"`),
    `{"kind": "klipper", "url": "http://127.0.0.1:7125"}` with an optional `"api_key"`, or
    `{"kind": "octoprint", "url": "http://127.0.0.1:5000", "api_key": "..."}`. URLs are http or https, loopback or an
    attached private LAN address, with no credentials, query, or path. Keys are 16 to 128 of `A-Z a-z 0-9 _ -`.
    Saving replaces any previous printer. A 400 carries a short `error`. Keys are never returned.
  - `DELETE /api/printer`: forget the printer.
  - `GET /api/printers/discover`: `{"printers": [...]}`. Each item has `kind`, `name`, `url`, `host`, `serial`, and
    `mode`. Bambu items have `url: null`; Klipper and OctoPrint items found on this Pi have `host`, `serial`, and
    `mode` `null` and add `needs_api_key`, and are only included when the request sends
    `X-Printer-Pal-Discover: all` (Printer Pal 1.1 and later). Takes about eight seconds.
- A 503 for the stream carries `X-Printer-Pal-Reason: camera-not-ready` or `viewers-full`
- Timelapses, all with the same token:
  - `GET /api/timelapse`: settings, the current recording, and storage use. `POST` the same with
    `{"enabled": false, "mode": "layer", "interval_seconds": 10}` (mode `layer` or `interval`, 2 to 600 seconds)
  - `POST /api/timelapse/start` and `/api/timelapse/stop`: a 409 if already recording or not recording
  - `GET /api/timelapses`: newest first
  - `GET /api/timelapses/<id>/poster`: the latest frame. `GET /api/timelapses/<id>/frames`: every frame as MJPEG
    (boundary `FRAME`, frame count in `X-Printer-Pal-Frames`), ending with `--FRAME--`
  - `DELETE /api/timelapses/<id>`: a 409 while it's recording

Timelapses are stored in `timelapses/<id>/` next to the state file (`meta.json` and `000001.jpg` onward), with their
settings in `timelapse.json`. Ids are the UTC start time, `YYYYMMDD-HHMMSS`, with `-2` and so on added on collision.
Printer Pal Camera on iOS implements the same API and limits.

Cameras advertise `_printmonitor._tcp` with a `tls=1` TXT record. The setup QR is
`https://<host>:<port>/#pin=<sha256 of the certificate>&code=<6 digits>`.

## Tests

```bash
python3 -m unittest discover -s companion/tests
```

Tests need Python's `cryptography` and `paho-mqtt` packages. Klipper and OctoPrint tests use a local fake server.
CI also runs the firewall checks in an isolated network namespace and the real UFW inside the systemd sandbox
(`companion/tests/linux-*.sh`).

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
