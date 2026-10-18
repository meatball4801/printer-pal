# Bambu Lab printer status (experimental)

See print progress, time left, layers, temperatures, and AMS slots under your camera view. Works with a Raspberry Pi
companion; a spare iPhone camera can't show printer status. Tested on a Bambu Lab A1 with AMS Lite (firmware
01.08.01.00). Other models and firmware may restrict local access; reports are welcome.

Keep the printer in its existing normal/cloud-connected mode. The Pi opens one
outgoing MQTT-over-TLS connection directly to the printer's private IPv4 address
on port 8883. The phone continues to use its paired, certificate-verified HTTPS
connection to the Pi. No Bambu account, cloud broker, local MQTT broker, control
plugin, or router port forwarding is used. Firmware may restrict access or
status refreshes; failed access does not cause a mode change or cloud fallback.

## Setup

1. Install or update the Pi companion. Camera pairing and settings are kept.
2. Open **Settings → Camera → Bambu printer** in the app, or use the printer
   panel below the camera viewer. Find nearby printers or enter the printer's
   local IPv4 address and serial number.
3. Enter the eight-character LAN access code from the printer. Leave its LAN
   Only switch off. Do not paste the access code into support messages.
4. Save, then check connection. The initial status may take several seconds.
   Confirm Bambu Handy and the normal printing workflow still work too.

Discovery listens for up to eight seconds for broadcast and multicast SSDP
announcements on UDP 1990 and 2021,
only when requested by a paired phone. It accepts only attached local sources
with matching announced/source addresses and bounded, validated fields. It does
not treat discovery as authentication. Existing firewall rules may block those
announcements; manual entry remains available. If UFW is active, the installer
allows these two UDP ports only from attached private IPv4 subnets on their
physical interfaces. No listener runs permanently in the background, no search
packets are sent, and announcements never trigger a connection or send credentials.

## Data and secrets

- Printer settings are stored in `/var/lib/print-monitor/printer.json`, mode
  0600, inside the service's private 0700 state directory. The access code is
  received only through an authenticated HTTPS POST; it is never returned by
  the API. Disconnecting the printer removes the saved file and stops monitoring.
- Local MQTT authenticates as `bblp` using that code. The credential may permit
  more than monitoring on the printer; our implementation exposes no physical
  control API. It subscribes to the serial-specific report topic and publishes
  only the fixed `pushing/pushall` status-refresh request, at most every 30 seconds.
- TLS verifies the manufacturer CA chain, certificate validity, and printer
  serial as the certificate hostname/SNI. It does not disable certificate
  verification, silently trust an unknown certificate, or use an environment proxy.
- Older published Bambu CA certificates need Python's strict X.509 extension
  enforcement disabled. Certificate signatures, trust chain, dates, and serial
  checks remain enabled. Missing issuers or unknown device identities fail closed.
- Only recognized status fields are retained in memory. Raw MQTT, access codes,
  and exception messages are not logged. The phone diagnostic summary remains
  separate and does not include printer identifiers, credentials, or job names.
- Partial updates merge. Missing fields remain unknown. Values expire after
  90 seconds; disconnection hides them immediately. Reconnection starts with an
  empty report. A new job clears previous progress, time, layer, and name fields.
- AMS data includes reported slot material/color and active tray. Missing data
  is not presented as an empty slot or as a measured remaining spool quantity.

## Public CA provenance

`bambu-printer-ca.pem` is the public printer CA bundle from Bambu Studio:

- Source: https://github.com/bambulab/BambuStudio/blob/79df7ae309be398ed30c9f397c13c9911c2edc57/resources/cert/printer.cer
- SHA-256: `36f2bcee347ec7adce719b5fd350099591a4d3d0ec4e039c7019890d78e152a0`
- It contains public CA certificates only. No Bambu client certificate or private
  key is included, generated, extracted, or used by this integration.

Protocol references: [Bambu's authorization/status distinction](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/),
[hybrid local monitoring](https://github.com/greghesp/ha-bambulab/blob/main/docs/entities.mdx),
[community SSDP observations](https://github.com/ClusterM/open-bamboo-networking/blob/master/research/06.01-ssdp.md).
The implementation here is original; no integration source code was copied.
