# Lookout

See who's at the door on your Apple Watch. Lookout watches your own cameras for people and sends
an alert with a picture to your watch and iPhone, with the camera live one tap away. No accounts,
no cloud: it all runs on a small server at home, the **Lookout Hub**.

**[Set it up](https://rogersquare.github.io/lookout/)**: install the hub with one command, add
your cameras (or connect Frigate), and pair your iPhone by scanning a QR code.

| Folder | What it is |
|---|---|
| [`hub/`](hub) | Lookout Hub: person detection (RF-DETR) or Frigate alerts, the setup page, the app API, camera restreams (go2rtc). Docker image `ghcr.io/rogersquare/lookout-hub` for x86-64 and ARM64. |
| [`apple/`](apple) | iPhone and Apple Watch apps (SwiftUI), with a watch complication. |
| [`install/`](install) | Installers and Compose files for Linux, NAS, Raspberry Pi, Windows and Mac. |
| [`docs/`](docs) | The setup guide ([rogersquare.github.io/lookout](https://rogersquare.github.io/lookout/)). |
| [`tv/`](tv) | Android TV app: pops the camera up over whatever is playing. |
| `lookout.py` | Windows tray app: pops the camera up in a corner of the screen. |

## Install the hub

```sh
curl -fsSL https://github.com/RogerSquare/lookout/releases/latest/download/install.sh | sh
```

Windows (Docker Desktop): `irm https://github.com/RogerSquare/lookout/releases/latest/download/install.ps1 | iex`.
Synology and other NAS boxes: see [the install guide](https://rogersquare.github.io/lookout/install.html).

## Security

Every phone and watch pairs with its own key (Secure Enclave on Apple devices) and signs each
request; the hub's certificate is pinned at pairing. See
[how Lookout keeps your cameras private](https://rogersquare.github.io/lookout/security.html).
Report problems privately via [security advisories](https://github.com/RogerSquare/lookout/security/advisories/new).

## Windows tray app

Tray app that pops up a live camera feed in a corner of the screen when a **person** is detected
(cars, pets and lighting changes are ignored), Apple TV-style. Drag the popup anywhere and it
flings to the nearest screen corner, FaceTime-style; click it to enlarge, X to dismiss.

- Reads each camera's RTSP stream directly (no NAS / Surveillance Station dependency)
- YOLO11s person detection on the GPU, ~5 checks/sec per camera
- Pops up after 2 positive checks out of 3 (~0.5 s); hides 15 s after the person leaves
- Remembers the corner/screen you last dropped it on (`%LOCALAPPDATA%\Lookout\state.json`)
- Tray menu: show a camera now, pause 1 hour, open log, quit

### Setup

```
python -m venv .venv
.venv\Scripts\pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json   # then put the camera RTSP URL(s) in it
.venv\Scripts\pythonw lookout.py
```

The YOLO weights download to `models\` on first run. Every setting in `config.example.json`
is optional except `cameras`.

### Tray flyout

Left-click the tray icon for a Windows 11-style flyout (native rounded corners and shadow, your
accent color):
- **Camera tiles:** a live thumbnail of each camera. Click one to show just that camera; its
  switch turns alerts from that camera on or off.
- **Person alerts**, **Chime**, and **Pause alerts for 1 hour**.
- **Popup position:** four corners. **Popup size:** S/M/L/XL. Changes apply to open popups too.
- **Preview** shows an alert popup with the chime for 8 s. **Open Frigate** opens `frigate_url`.

When several cameras alert at once, their popups stack away from the corner instead of
overlapping, and the stack closes up as they go. Right-click still opens the plain menu.

### Hub mode

With `"hub_url": "http://<hub address>:8765"` (your Lookout Hub), the app takes its
cameras and alerts from the hub instead of detecting locally:
- Popups follow the hub's `person_detected` / `person_gone` events, the same as the projector app.
- Video is only decoded while a popup is up. It opens on the hub's snapshot, then the live stream.
- Tray **Person alerts** and **Alerts from → camera** are the hub's shared switches, so the
  projector follows them too.
- If the hub can't be reached at startup, the app detects locally from `cameras` (standalone).
  The tray tooltip says which mode is active.

### GPU helper for the hub

While it runs, the app also serves `POST /detect` on port 8770 for the Lookout Hub
(see `hub/README.md`). The hub uses this PC's GPU when it answers and its own CPU when it doesn't.
`"gpu_helper_port": null` turns it off, and `"gpu_helper_model"` picks the model (default
`yolo11l.pt`).

### Autostart

`install-autostart.ps1` adds a shortcut to the Startup folder; `install-autostart.ps1 -Remove`
takes it out.

### Tests

- `tests\config.test.json` - looped still photo with people; the popup should appear within ~2 s
- `tests\config.arrive_leave.json` - people for 4 s then an empty street; popup appears, then
  hides itself 15 s after they leave
- `tests\test_popup_motion.py` - drives drag/fling/click with synthetic mouse events and checks
  the popup lands exactly in each corner

Log: `%LOCALAPPDATA%\Lookout\lookout.log`

## License

MIT, see [LICENSE](LICENSE). The hub image includes [go2rtc](https://github.com/AlexxIT/go2rtc)
(MIT), [FFmpeg](https://ffmpeg.org) (LGPL/GPL) and the [RF-DETR](https://github.com/roboflow/rf-detr)
models (Apache-2.0). The Windows tray app's local detection uses
[Ultralytics YOLO](https://github.com/ultralytics/ultralytics) (AGPL-3.0), installed separately.
