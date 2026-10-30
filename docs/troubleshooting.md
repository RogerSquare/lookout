# Troubleshooting

**I lost the setup code.** Run `docker logs lookout-hub` on the hub's machine and look for the line
starting `NEW HUB:`. Once a password is set, the code is gone and the page asks for the password.

**I forgot the setup page password.** Stop the hub, delete `data/settings.json`'s
`admin_password` line (or ask for help in an issue), and start it again: it prints a new setup
code. Cameras and paired devices are kept.

**The app says it can't reach the hub.**
- Is the iPhone on the same Wi-Fi as the hub? Away from home you need an
  [away address](away.md).
- On Windows, allow TCP ports 8765, 8766 and 8555 in Windows Firewall for private networks.
- Did the hub's address change? At home the iPhone follows the hub automatically. If it doesn't,
  give the hub a fixed address in your router (a "DHCP reservation").

**"This iPhone was removed".** The device was removed on the setup page's **Apps** list. Choose
**Pair Again** and scan a new code.

**A camera says Offline.** Open it on the setup page and choose **Test stream**. Check the
username, password and address, and that the camera's RTSP option is switched on in its own app.

**Too many alerts.** Open the camera on the setup page and outline only the area people have to
stand in, and raise **Ignore people shorter than**. With Frigate, use its alert zones and choose
**Frigate alerts** on the Detection page.

**Alerts are slow.** Notifications go through Apple and usually arrive within a few seconds. The
hub itself reacts in well under a second; check its log for `person` lines to compare.

**The Apple Watch shows Set up on iPhone.** Open Lookout on the iPhone (it must be paired), or
choose **Set Up Apple Watch** in its Settings.

Still stuck? [Open an issue](https://github.com/RogerSquare/lookout/issues) with what you tried and
the output of `docker logs lookout-hub` (it never prints passwords or keys).
