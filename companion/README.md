# Printer Pal companion for Raspberry Pi

The companion turns a Raspberry Pi into a secure camera for the Printer Pal app. It finds your camera, streams it over
an encrypted connection on your Wi-Fi, and starts automatically when the Pi boots.

## What you need

- A Raspberry Pi running Raspberry Pi OS (Bookworm or later recommended). OctoPi, MainsailOS, and other Debian-based
  systems with systemd work too.
- One of:
  - a Raspberry Pi camera module,
  - a USB webcam, or
  - camera software that's already running, such as OctoPrint's webcam, crowsnest, or mjpg-streamer.
- The Pi and your iPhone on the same Wi-Fi network, and SSH enabled on the Pi. Raspberry Pi Imager can turn SSH on
  when you set up the card.

## Install

**From the app (easiest).** In Printer Pal, choose **Add camera → Connect your Pi → Install from this iPhone**. Pick
your Pi, sign in with its username and password, check the camera it found, and tap **Install Printer Pal**. The app pairs
automatically when it finishes. Your password is only used for this setup and isn't saved.

**From the terminal.** Over SSH on the Pi, run this as your normal user:

```bash
curl -fsSL https://github.com/meatball4801/printer-pal/releases/latest/download/install.sh | bash
```

It asks for your password once, installs what it needs, and finishes by showing a QR code. In Printer Pal, choose
**Add camera → Connect your Pi → Scan setup code** and scan it.

The script checks the installer's checksum before running anything. Installing can take a few minutes, especially if
the Pi is installing system updates in the background.

### Already have a camera running?

That's fine. If the Pi already runs OctoPrint, Klipper, or other camera software, the companion shares that camera's
existing video feed instead of taking the camera over. Your current setup keeps working, and the companion uses its
own port (32187 by default).

## Update

Run the install command again, or use **Install from this iPhone** again. Your camera choice, settings, and paired
phones are kept.

## Installer options

Add options after `bash -s --`:

```bash
curl -fsSL https://github.com/meatball4801/printer-pal/releases/latest/download/install.sh | bash -s -- --rescan
```

| Option | What it does |
| --- | --- |
| `--rescan` | Look for cameras again instead of keeping the current choice |
| `--mode usb --device /dev/video0` | Use a specific USB webcam |
| `--mode picamera2` | Use the Raspberry Pi camera module |
| `--source-url 'http://127.0.0.1:8080/?action=stream'` | Share an existing MJPEG feed |
| `--port 32189` | Use a different port |
| `--detect` | Show which camera would be used, without installing |
| `--check` | Download and verify the installer without installing |

A shared feed must be a direct MJPEG address. RTSP, HLS, and pages that need a login aren't supported.

## Commands on the Pi

```bash
sudo print-monitor status                    # is the companion running?
sudo print-monitor pair --new                # show a new pairing QR and code
sudo print-monitor logs                      # recent log messages
sudo print-monitor restart                   # restart the companion
sudo print-monitor forget-phones --confirm   # remove access for every paired phone
sudo print-monitor remote tailscale on       # allow viewing over your Tailscale network (or off)
sudo print-monitor uninstall --confirm       # remove the companion completely
```

Pairing codes and setup links are private: don't post them anywhere.

## Troubleshooting

- **The app can't find the Pi.** Make sure the phone and Pi are on the same Wi-Fi, and that Printer Pal has Local
  Network access (iPhone Settings → Printer Pal). You can also scan the QR from `sudo print-monitor pair --new`.
- **"Pairing is closed" or the code expired.** Run `sudo print-monitor pair --new` and scan the new QR.
- **"Camera isn't ready".** Check the camera cable or USB connection, then run `sudo print-monitor logs`. To pick a
  different camera, run the install command with `--rescan`.
- **Certificate error after reinstalling the Pi's system.** A fresh system has a new identity. Remove the camera in
  Printer Pal and pair again.
- **Firewall.** The companion adds its own rules for its port only, and only for your local network. If you use
  another firewall tool, allow TCP port 32187 from your local network.

## Remove the companion

```bash
sudo print-monitor uninstall --confirm
```

This removes the companion, its settings, its security identity, and all phone pairings. Shared packages it may have
installed, such as Python libraries, ffmpeg, and avahi, are left in place. Then remove the camera in Printer Pal.

## More

- [Security notes](SECURITY.md)
- [Bambu Lab printer status (experimental)](BAMBU.md)
- [Viewing away from home with Tailscale](REMOTE.md)
- [Development and releases](DEVELOPMENT.md)
