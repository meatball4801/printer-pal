# Security notes

Printer Pal is designed so your camera is only visible to phones you pair, on your own network. This page explains how,
and what it doesn't cover.

## Pairing and encryption

- Each camera (Pi or spare iPhone) creates its own private key and certificate the first time it's set up. Releases
  contain no shared keys or codes.
- The setup QR contains the camera's certificate fingerprint and a one-use code that expires after ten minutes. The
  app checks that exact certificate before sending the code, and again on every connection afterwards. A different
  certificate stops the connection; there's no fallback to an unencrypted connection.
- Pairing allows five wrong codes per minute. A successful pairing gives the phone a random 256-bit access token. The
  camera stores only a hash of it; the phone keeps the token and certificate fingerprint in its Keychain.
- Video, status, and snapshots all require that token. `sudo print-monitor forget-phones --confirm` revokes every
  phone immediately, including streams in progress.
- Connections use TLS 1.2 or newer. Cameras added with **Use a camera URL** over plain HTTP aren't encrypted.

## Network boundary

- The companion only accepts connections from networks the Pi is physically attached to. It adds its own nftables
  table for its port and leaves your other firewall rules alone. If UFW is active, it adds matching rules for its
  port only.
- It never opens ports on your router, uses UPnP, or relays video through a server. Printer Pal has no cloud service.
- Remote viewing through Tailscale is off unless you turn it on with `sudo print-monitor remote tailscale on`. It then
  allows the camera port from your Tailscale network only.
- The camera service runs as its own unprivileged user with a read-only view of the system and limits on connections,
  request sizes, and viewers.

## Installing

The one-line installer downloads a small script from the latest GitHub release. That script contains the release
installer's SHA-256 checksum and won't run a download that doesn't match. This protects against corrupted or tampered
downloads, but it isn't an independent signature: it trusts this GitHub repository.

**Install from this iPhone** connects to your Pi over SSH. The app asks you to confirm the Pi's SSH identity the first
time, warns you if it changes, and never saves your password.

## What this doesn't cover

Printer Pal secures its own connection. It doesn't secure the rest of your Pi, your network, or other software such
as an existing camera server, which keeps its own settings. Anyone you've paired, or anyone with a device on your
Tailscale network if you enable it, can view the camera.

## Reporting a security issue

Please report security problems privately through this repository's **Security → Report a vulnerability** page rather
than a public issue.
