# Printer Pal

Watch your 3D printer from your iPhone or iPad. Printer Pal streams live video from a camera on your home Wi-Fi,
encrypted, straight to your phone. Nothing is uploaded, and there's no account to create.

Printer Pal is in **public beta**. New testers: start with the [tester guide](TESTING.md), and come say hi on
[Discord](https://discord.gg/cs4tr9stPS).

## Get the beta

**[Join the Printer Pal beta on TestFlight](https://testflight.apple.com/join/xzWMyf4J)**

- **Printer Pal**: the viewer for your iPhone or iPad (iOS 17 or later). [Join on TestFlight](https://testflight.apple.com/join/xzWMyf4J).
- **Printer Pal Camera**: turns a spare iPhone or iPad (iOS 15 or later) into a printer camera.
  [Join on TestFlight](https://testflight.apple.com/join/7pjeUe2D).

## Pick a camera

| Camera | What you need | Setup |
| --- | --- | --- |
| Raspberry Pi | A Pi with a Pi camera or USB webcam, or an existing OctoPrint / Klipper camera | [Pi companion guide](companion/README.md) |
| Spare iPhone or iPad | Any iPhone 6s / iPad Air 2 or newer on iOS 15+ | Install Printer Pal Camera, then **Add camera → Use a spare iPhone or iPad** |
| Existing MJPEG camera | A direct MJPEG stream URL | **Add camera → Use a camera URL** (not encrypted) |

No camera yet? Open the **Demo camera** on the Cameras tab to try the viewer.

## Set up a Raspberry Pi in one step

In Printer Pal, choose **Add camera → Connect your Pi → Install from this iPhone**. The app signs in to your Pi over
SSH, shows which camera it found, installs the companion, and pairs, all without a computer.

Prefer the terminal? On the Pi, run:

```bash
curl -fsSL https://github.com/meatball4801/printer-pal/releases/latest/download/install.sh | bash
```

Then scan the QR code it shows. Run the same command again to update. Details, options, and troubleshooting are in
the [companion guide](companion/README.md).

## What's included

- Live video with pinch to zoom, full screen, rotation, pause, and snapshots
- Automatic reconnection and a built-in connection check
- Timelapses recorded on your camera, one frame per layer on Bambu printers or on a timer, made into videos on your phone and kept in a Timelapses tab you can watch and share from anywhere
- Works alongside OctoPrint, Klipper (Mainsail/Fluidd), and other camera software already on your Pi
- Several printers in one app, with a live grid overview
- Print status from Bambu Lab printers, Klipper, and OctoPrint, with either a Pi or a spare iPhone: progress, layers, temperatures, and AMS slots ([Bambu guide](companion/BAMBU.md), [Klipper and OctoPrint](companion/README.md#printer-status))
- Pause, resume, or stop a print, and switch a Bambu chamber light (newer Bambu firmware may need Developer Mode for LAN controls)
- Print history for each camera, with totals for prints, finished rate, and print time
- Watch a Bambu Lab P1 or A1 printer through its own camera, with no extra hardware
- Home Screen and Lock Screen widgets with print progress, time left, and spare-phone battery
- Notifications when a print finishes, stops, or pauses, through your own iCloud (from Printer Pal Camera)
- Viewing away from home: a picture every couple of seconds through your own iCloud with a spare iPhone or iPad (encrypted, no setup), or live video through your own Tailscale network with a Pi ([guide](companion/REMOTE.md))

## Privacy and security

- Cameras talk to your phone directly on your Wi-Fi. There's no Printer Pal server, account, or analytics.
- Timelapses are off until you turn them on, and their frames stay on your camera until you make a video.
- Each camera creates its own security certificate. You pair by scanning its QR code, and the app checks that exact
  certificate on every connection.
- The Pi companion only accepts connections from your local network and opens no ports on your router.

More detail in the [security notes](companion/SECURITY.md) and the [privacy policy](PRIVACY.md).

## Known limitations

- Live video is on your home Wi-Fi unless you set up [Tailscale](companion/REMOTE.md) on a Pi. Away from home, a spare iPhone or iPad sends pictures through iCloud instead, which needs both devices signed in to the same Apple ID.
- Printer Pal Camera must stay open on the spare device; iOS doesn't allow the camera in the background.
- Printer status and controls have been tested on a limited set of printers and firmware.
- The printer's own camera works with Bambu P1 and A1 printers; X1 printers aren't supported yet.
- Widgets show the last status Printer Pal read. iOS decides how often the app can refresh in the background.
- Print alerts currently come only from Printer Pal Camera, signed in to the same Apple ID as your phone.
- USB webcams and Pi models other than the Pi 4 have had less testing. Reports are very welcome.

## Feedback

- **TestFlight:** take a screenshot in the app, or open TestFlight and tap **Send Beta Feedback**.
- **[Discord](https://discord.gg/cs4tr9stPS):** questions, ideas, and setup photos.
- **GitHub:** a [bug or hardware report](https://github.com/meatball4801/printer-pal/issues/new/choose).

Please include your iPhone model, camera type, and Pi model. Never post pairing codes, setup links, or printer access
codes.

## For developers

Working on the Pi companion? See [companion/DEVELOPMENT.md](companion/DEVELOPMENT.md).
