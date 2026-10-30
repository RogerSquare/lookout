# Lookout on your PC and TV

These are earlier, tinkerer-grade companions to the hub. They aren't packaged yet.

**Windows tray app** (`lookout.py` in the repository). Pops up a live camera in a corner of the
screen when someone is at the door, FaceTime-style; drag it to any corner. It needs Python. With
`"hub_url": "http://<hub address>:8765"` in its `config.json`, it follows the hub's alerts and
switches. Without a hub it can detect on its own, using the PC's GPU. See the repository's
[README](https://github.com/RogerSquare/lookout#windows-tray-app).

**Android TV / Google TV app** (`tv/` in the repository). Shows the camera over whatever is playing
when someone is at the door. Build it with Android Studio and install it with `adb`.

Both use the hub's home-network alerts feed, so they only work at home.
