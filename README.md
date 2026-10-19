# Printer Pal

Watch your 3D printer from your iPhone or iPad. Printer Pal streams live video from a camera on your home Wi-Fi,
encrypted, straight to your phone. Nothing is uploaded or recorded, and there's no account to create.

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
- Works alongside OctoPrint, Klipper (Mainsail/Fluidd), and other camera software already on your Pi
- Bambu Lab print status through your camera, Pi or spare iPhone: progress, layers, temperatures, and AMS slots (experimental, [guide](companion/BAMBU.md))
- Optional viewing away from home through your own Tailscale network ([guide](companion/REMOTE.md))

## Privacy and security

- Cameras talk to your phone directly on your Wi-Fi. There's no Printer Pal server, account, analytics, or recording.
- Each camera creates its own security certificate. You pair by scanning its QR code, and the app checks that exact
  certificate on every connection.
- The Pi companion only accepts connections from your local network and opens no ports on your router.

More detail in the [security notes](companion/SECURITY.md) and the [privacy policy](PRIVACY.md).

## Known limitations

- One camera per app for now.
- Viewing is on your home Wi-Fi unless you set up [Tailscale](companion/REMOTE.md).
- Printer Pal Camera must stay open on the spare device; iOS doesn't allow the camera in the background.
- Bambu status is experimental and has been tested on a limited set of printers and firmware.
- USB webcams and Pi models other than the Pi 4 have had less testing. Reports are very welcome.

## Feedback

- **TestFlight:** take a screenshot in the app, or open TestFlight and tap **Send Beta Feedback**.
- **[Discord](https://discord.gg/cs4tr9stPS):** questions, ideas, and setup photos.
- **GitHub:** a [bug or hardware report](https://github.com/meatball4801/printer-pal/issues/new/choose).

Please include your iPhone model, camera type, and Pi model. Never post pairing codes, setup links, or printer access
codes.

## For developers

Working on the Pi companion? See [companion/DEVELOPMENT.md](companion/DEVELOPMENT.md).
