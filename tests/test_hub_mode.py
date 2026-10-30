"""Desktop app in hub mode, against a fake Lookout Hub (SSE + REST, same shapes as hub/hub.py).

Checks: cameras come from the hub, alerts open/close popups (snapshot first, then live video, no
decoding while hidden), the tray switches go to the hub, and an unreachable hub means standalone.
Run: .venv/Scripts/python tests/test_hub_mode.py
"""

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtWidgets import QApplication

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import lookout  # noqa: E402

lookout.save_state = lambda state: None
PHOTO = (HERE / "people.jpg").read_bytes()


class FakeHub:
    def __init__(self):
        self.outbox = queue.Queue()      # events to push to SSE subscribers
        self.posts = []                  # bodies of POST /api/alerts
        self.alerts = {"enabled": True, "cameras": {"door": True, "yard": True}}
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/state":
                    self._send(json.dumps(hub.state()).encode(), "application/json")
                elif self.path.startswith("/api/cameras/"):
                    self._send(PHOTO, "image/jpeg")
                elif self.path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(hub.sse(hub.state()))
                    self.wfile.flush()
                    while True:
                        try:
                            ev = hub.outbox.get(timeout=1)
                        except queue.Empty:
                            continue
                        try:
                            self.wfile.write(hub.sse(ev))
                            self.wfile.flush()
                        except OSError:
                            return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                hub.posts.append(body)
                if "camera" in body:
                    hub.alerts["cameras"][body["camera"]] = body["enabled"]
                else:
                    hub.alerts["enabled"] = body["enabled"]
                hub.outbox.put({"type": "alerts", **hub.alerts})
                self._send(json.dumps(hub.alerts).encode(), "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def state(self):
        cams = [{"id": cid, "name": name, "connected": True, "person": False, "alerts": self.alerts["cameras"][cid],
                 "streams": {"hd": "tests/people.jpg"}, "loop": True}
                for cid, name in (("door", "Front Door"), ("yard", "Yard"))]
        return {"type": "state", "alerts": {"enabled": self.alerts["enabled"], "cameras": self.alerts["cameras"]},
                "cameras": cams}

    @staticmethod
    def sse(ev):
        return f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()


def pump(seconds, until=None):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())


def main():
    qapp = QApplication(sys.argv)  # noqa: F841
    hub = FakeHub()
    settings = dict(lookout.DEFAULTS, hub_url=hub.url, gpu_helper_port=None, sound="",
                    cameras=[{"name": "Local Only", "url": "tests/empty.jpg"}])
    a = lookout.App(settings)
    results = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
        results.append(bool(ok))

    door, yard = a.cameras[0], a.cameras[1]
    pd = a.popups["Front Door"]
    check("hub mode: cameras come from the hub, not the local config",
          a.hub_mode and [c.name for c in a.cameras] == ["Front Door", "Yard"]
          and all(isinstance(c, lookout.HubCamera) for c in a.cameras))
    check("hub mode: no camera is decoding video while nothing is shown", not door.streaming and not yard.streaming)
    pump(2, until=lambda: a.feed.connected)
    check("SSE connected", a.feed.connected)
    pump(0.5)
    check("tooltip says hub mode", "hub" in a.tray.toolTip() and "standalone" not in a.tray.toolTip(),
          a.tray.toolTip().splitlines()[0])

    t0 = time.perf_counter()
    hub.outbox.put({"type": "person_detected", "camera": "door", "name": "Front Door", "confidence": 0.9,
                    "snapshot": "/api/cameras/door/snapshot.jpg?t=1", "streams": {}})
    shown = pump(3, until=lambda: pd.isVisible())
    check("person_detected opens the popup", shown, f"{time.perf_counter() - t0:.2f}s")
    check("popup opened the camera stream", door.streaming)
    got_frame = pump(3, until=lambda: door.latest is not None)
    check("popup has a picture (snapshot or live frame)", got_frame)
    check("other camera stays idle", not yard.streaming and not a.popups["Yard"].isVisible())

    hub.outbox.put({"type": "person_gone", "camera": "door", "reason": "left", "duration": 3})
    hidden = pump(3, until=lambda: not pd.isVisible())
    check("person_gone closes the popup", hidden)
    pump(0.6)
    check("stream closed once the popup is gone", not door.streaming)

    a.detection_action.trigger()  # untick "Person alerts" in the tray
    pump(2, until=lambda: hub.posts)
    check("tray Person alerts off -> POST to the hub", hub.posts[-1:] == [{"enabled": False}], str(hub.posts[-1:]))
    pump(1)
    check("hub's alerts event keeps the tray in sync", not a.detection and not a.detection_action.isChecked())
    hub.outbox.put({"type": "person_detected", "camera": "door", "name": "Front Door", "confidence": 0.9,
                    "snapshot": "", "streams": {}})
    pump(1.5)
    check("no popup while alerts are off", not pd.isVisible())
    hub.outbox.put({"type": "person_gone", "camera": "door", "reason": "alerts_off", "duration": 1})
    a.detection_action.trigger()
    pump(2, until=lambda: a.detection)

    a.camera_alert_actions["yard"].trigger()  # untick Yard under "Alerts from"
    pump(2, until=lambda: {"enabled": False, "camera": "yard"} in hub.posts)
    check("tray per-camera alerts -> POST with the camera", {"enabled": False, "camera": "yard"} in hub.posts)
    pump(1)
    check("per-camera switch reflects the hub", not yard.alerts and not a.camera_alert_actions["yard"].isChecked())

    a.toggle_camera(yard)  # open a camera by hand
    pump(1.5)
    check("open by hand still works in hub mode (pinned, streaming)",
          a.popups["Yard"].isVisible() and a.popups["Yard"].pinned and yard.streaming)
    a.toggle_camera(yard)
    pump(1)

    # Hub down at start: standalone mode with the local config
    s2 = dict(settings, hub_url="http://127.0.0.1:9")
    b = lookout.App(s2)
    check("hub unreachable at start -> standalone with local cameras",
          not b.hub_mode and [c.name for c in b.cameras] == ["Local Only"]
          and not isinstance(b.cameras[0], lookout.HubCamera))
    pump(0.5)
    check("tooltip says standalone", "standalone" in b.tray.toolTip(), b.tray.toolTip().splitlines()[0])
    for c in b.cameras:
        c.stop()

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
