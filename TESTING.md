# Printer Pal beta tester guide

Thanks for helping test Printer Pal! This guide covers getting set up, what to try, and how to send feedback.

**Join the community on [Discord](https://discord.gg/cs4tr9stPS)** to ask questions, share your setup, and hear about
new builds.

## 1. Get the apps

Install [TestFlight](https://apps.apple.com/app/testflight/id899247664) from the App Store, then open the invite link on
your iPhone or iPad:

- **Printer Pal** (iOS 17+): the viewer, on your main iPhone or iPad. **[Join the beta](https://testflight.apple.com/join/xzWMyf4J)**
- **Printer Pal Camera** (iOS 15+): only if you want to use a spare iPhone or iPad as the camera.
  **[Join the beta](https://testflight.apple.com/join/7pjeUe2D)** on the spare device.

No camera handy? Open the **Demo camera** on the Cameras tab to try the viewer first.

## 2. Set up a camera

Pick whichever fits what you have:

- **Raspberry Pi:** in Printer Pal, choose **Add camera → Connect your Pi → Install from this iPhone**, or run the
  one-line installer from the [companion guide](companion/README.md). It works on a fresh Pi and on one that already
  runs OctoPrint, Klipper, or another camera.
- **Spare iPhone or iPad:** install Printer Pal Camera on it, mount it facing your printer, plug it in, and open it.
  Then in Printer Pal choose **Add camera → Use a spare iPhone or iPad** and scan its code.
- **An existing MJPEG camera:** **Add camera → Use a camera URL**.

## 3. What to try

You don't need to do everything. Whatever fits your setup helps.

- [ ] Set up from scratch and pair. Was anything confusing?
- [ ] Watch a whole print. Does video stay smooth and reconnect on its own?
- [ ] Lock your phone, switch apps, or walk out of Wi-Fi range and back.
- [ ] Restart the Pi, or unplug and replug the camera.
- [ ] Try zoom, full screen, rotation, pause, and sharing a snapshot.
- [ ] **Bambu Lab owners:** connect your printer under Settings → Bambu printer and check progress, layers,
      temperatures, and AMS slots.
- [ ] **Spare-device camera:** leave it running and dimmed for a few hours. Does it stay cool and connected?
- [ ] **Optional:** set up [viewing away from home](companion/REMOTE.md) with Tailscale.

## 4. Send feedback

- **In the app:** take a screenshot and tap **Share Beta Feedback**, or open TestFlight and tap **Send Beta Feedback**.
  Crash reports are sent automatically if you allow it.
- **On Discord:** quick questions, ideas, and photos of your setup.
- **On GitHub:** a [bug report or hardware report](https://github.com/meatball4801/printer-pal/issues/new/choose).
  Hardware reports, even "it just works", tell us which Pis, cameras, printers, and old iPhones work.

Please include your iPhone model, camera type, and Pi model. **Never share** pairing codes, setup QR codes or links,
printer access codes, or passwords; they give access to your camera or printer.

Useful details for problems:

- In Printer Pal: **Settings → Check connection → Share** creates a summary without private details.
- On the Pi: `sudo print-monitor status` and `sudo print-monitor logs`.

## Known limitations

- One camera at a time in Printer Pal.
- Viewing works on your home Wi-Fi unless you set up Tailscale.
- Printer Pal Camera must stay open on the spare device; iOS stops the camera in the background.
- Bambu status is experimental.

## Updating and removing

- **Apps:** TestFlight installs new builds; you'll get a notification.
- **Pi companion:** run the install command again, or use **Install from this iPhone** again. Pairing and settings
  are kept.
- **Remove from a Pi:** `sudo print-monitor uninstall --confirm`.

Your data stays on your devices; see the [privacy policy](PRIVACY.md).
