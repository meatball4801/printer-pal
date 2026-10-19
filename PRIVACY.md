# Privacy policy

*Last updated October 18, 2026*

This policy covers the Printer Pal and Printer Pal Camera apps and the Printer Pal companion for Raspberry Pi. The
short version: Printer Pal doesn't collect your data. Video and printer information stay on your devices and your
local network.

## What Printer Pal doesn't do

- It has no accounts, no servers, and no analytics or advertising SDKs.
- It doesn't track you or share data with third parties.
- It doesn't upload, store, or record your camera video.

## What stays on your devices

- **On your iPhone or iPad:** your camera's name and address, viewing preferences, and an optional remote address.
  Each camera's access token and certificate fingerprint are kept in the device's Keychain. The public SSH identity of
  each Pi you install the companion on is also saved, so the app can warn you if it changes. Removing a camera
  deletes its saved details.
- **On a Raspberry Pi or a spare iPhone running the camera:** its settings, its own security certificate, and hashes
  of the access tokens of paired phones. If you connect a Bambu Lab printer, its address, serial number, and LAN
  access code are saved in a private file on the Pi, or in the Keychain on a spare iPhone or iPad. Disconnecting the
  printer in Printer Pal removes the access code. `sudo print-monitor uninstall --confirm` removes everything from a Pi.
  On a spare device, iOS can keep an app's Keychain items after the app is deleted, so disconnect the printer and
  forget paired phones first.

## How data moves

- **Video and status** go directly from your camera to your paired phones over an encrypted connection on your local
  network. If you set up Tailscale, they travel over your own Tailscale network instead; Tailscale is a separate
  service with its own [privacy policy](https://tailscale.com/privacy-policy).
- **Printer status** is read by your camera (Pi or spare iPhone) directly from your printer on your network. Printer Pal doesn't sign in to
  Bambu Cloud.
- **Installing from the app:** your Pi's password is sent only to that Pi, over SSH, and isn't saved.
- **Installing and updating the companion** downloads it from GitHub, and the Pi downloads system packages from its
  usual package servers. Those services may log requests as they normally do.
- **Snapshots** are shared only when you choose Share, to the destination you pick.
- **Diagnostic summaries** are created only when you check a connection, and shared only if you tap Share. They
  contain app and companion versions, the check result, and uptime. They don't contain addresses, names, images,
  codes, or credentials.

## Permissions

- **Local Network:** to find cameras and Pis on your Wi-Fi and connect to them.
- **Camera (Printer Pal):** only to scan a camera's pairing QR code.
- **Camera (Printer Pal Camera):** to stream the spare device's camera to your paired phones.

## Beta testing

If you test through TestFlight, Apple shares the feedback, screenshots, and crash reports you choose to send, along
with your device model and iOS version, under Apple's TestFlight terms.
This is used only to fix problems and improve the apps. Anything you post in a GitHub issue is public.

## Children

Printer Pal isn't directed at children and doesn't knowingly collect information from anyone.

## Changes and contact

Changes to this policy will be posted here with a new date. For questions, open an issue at
[github.com/meatball4801/printer-pal/issues](https://github.com/meatball4801/printer-pal/issues) or ask on
[Discord](https://discord.gg/cs4tr9stPS).
