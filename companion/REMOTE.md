# Watch your printer away from home (Tailscale)

Printer Pal works on your home Wi-Fi out of the box. To watch from anywhere, you can connect your Pi and iPhone with
[Tailscale](https://tailscale.com), a free-for-personal-use service that builds a private, encrypted network between
your own devices. Printer Pal doesn't run any servers for this, and nothing is exposed to the internet: there's no port
forwarding, and only devices signed in to your Tailscale account can reach the Pi.

Video stays encrypted twice: by Tailscale's WireGuard tunnel, and by Printer Pal's own HTTPS connection, which still
checks the Pi's certificate from the pairing QR. Only phones you've already paired can view the camera.

This is optional and needs companion 0.6.2 and Printer Pal 0.8.0 build 12 or later. It works with a Raspberry Pi
companion; a spare iPhone or iPad running Printer Pal Camera can't be reached this way yet.

## 1. Set up Tailscale on the Pi

Over SSH on the Pi:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

```bash
sudo tailscale up
```

`tailscale up` prints a sign-in link. Open it on your phone or computer and sign in. Use the same account on every
device you want in this network.

## 2. Allow Printer Pal over Tailscale

The companion only accepts your home network until you turn this on:

```bash
sudo print-monitor remote tailscale on
```

It prints the Pi's Tailscale address, such as `100.101.102.103`. The camera port is opened to your Tailscale network
only; no other ports change, and printer discovery stays on your home network.

## 3. Set up your iPhone

1. Install **Tailscale** from the App Store, sign in with the same account, and turn it on.
2. In **Printer Pal → Settings → Away from home**, enter the address from step 2 as the **Remote address**. If you use
   Tailscale's MagicDNS, the Pi's name (for example `printer-camera`) works too.

At home, Printer Pal still connects directly. When the home address can't be reached, it tries the remote address
automatically, for the live view, the connection check, and Bambu printer status.

To test it, turn off Wi-Fi on your iPhone and open your camera.

## Turning it off

```bash
sudo print-monitor remote tailscale off
```

You can also clear the remote address in Printer Pal. To remove Tailscale from the Pi entirely, run
`sudo tailscale down`, then uninstall it with your package manager.

## Troubleshooting

- **Still can't connect away from home:** check `tailscale status` on the Pi and that Tailscale is on in the iPhone's
  Tailscale app. Run `sudo print-monitor remote tailscale on` again; it reports whether Tailscale is connected.
- **"Camera access expired":** the phone's pairing was revoked on the Pi. Pair again at home.
- **Certificate error:** the address reached a different device. Check the remote address.

Tailscale is a separate service with its own [terms](https://tailscale.com/legal/terms) and
[plans](https://tailscale.com/pricing); its free Personal plan is for non-commercial use. Printer Pal isn't affiliated
with Tailscale.
