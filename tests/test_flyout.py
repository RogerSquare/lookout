"""Tray flyout + popup stacking, standalone mode with two looped test cameras.

Checks: the flyout shows a tile per camera with live thumbnails, its switches drive the app
(person alerts, chime, per-camera alerts, position, size), two popups stack instead of overlapping,
and the stack packs back into the corner when one closes.
Run: .venv/Scripts/python tests/test_flyout.py [--shots DIR]
"""

import argparse
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import lookout  # noqa: E402

lookout.save_state = lambda state: None


def pump(seconds, until=None):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    args = ap.parse_args()
    app = QApplication(sys.argv)
    lookout.load_state = lambda: {}
    settings = dict(lookout.DEFAULTS, gpu_helper_port=None, sound="", frigate_url="https://example.invalid",
                    cameras=[{"name": "Front Door", "url": "tests/people.jpg", "loop": True},
                             {"name": "Yard", "url": "tests/empty.jpg", "loop": True}])
    a = lookout.App(settings)
    a.set_detection(False, save=False)  # keep real detection out of the way of this UI test
    results = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
        results.append(bool(ok))

    def shot(name, rect=None):
        if args.shots:
            g = rect or app.primaryScreen().geometry()
            app.primaryScreen().grabWindow(0, g.x(), g.y(), g.width(), g.height()).save(f"{args.shots}/{name}.png")

    pump(2)
    a._tray_activated(QSystemTrayIcon.Trigger)
    f = a.flyout
    check("flyout opens on tray left-click", pump(1, until=f.isVisible))
    check("one tile per camera", [t.cam.name for t in f.tiles] == ["Front Door", "Yard"])
    check("tiles get live thumbnails", pump(4, until=lambda: all(t.image is not None for t in f.tiles)))
    area = app.primaryScreen().availableGeometry()
    check("flyout sits bottom-right above the taskbar",
          abs(f.geometry().right() - (area.right() - 11)) <= 2 and abs(f.geometry().bottom() - (area.bottom() - 11)) <= 2,
          str(f.geometry().getRect()))
    pump(2)  # let the thumbnails land before the screenshot
    shot("flyout", f.geometry().adjusted(-30, -30, 30, 30))

    f.sw_chime.mouseReleaseEvent(type("E", (), {"position": lambda s: f.sw_chime.rect().center().toPointF()})())
    check("Chime switch -> app setting", a.chime_enabled() is False)
    f.tiles[1].switch.on_toggle(False)
    check("tile switch -> per-camera alerts", a.camera_alerts(a.cameras[1]) is False)
    f.size_seg.on_pick(2)
    check("size L -> popup width 640", a.state.get("width") == 640)
    f.pos_seg.on_pick(1)
    check("position top-right -> corner", a.state.get("corner") == "top-right")
    f.size_seg.on_pick(0)
    f.pos_seg.on_pick(3)

    # Two popups at once must stack, not overlap
    f.tiles[0].on_show(a.cameras[0])
    pump(0.3)
    a.toggle_camera(a.cameras[1])
    pump(1.5)
    p0, p1 = a.popups["Front Door"], a.popups["Yard"]
    g0, g1 = p0.geometry(), p1.geometry()
    check("two popups both visible", p0.isVisible() and p1.isVisible())
    check("they don't overlap (second stacks above the first)", not g0.intersects(g1) and g1.bottom() < g0.top(),
          f"{g0.getRect()} / {g1.getRect()}")
    shot("stacked", g1.united(g0).adjusted(-20, -20, 20, 20))
    a.toggle_camera(a.cameras[0])  # close the one in the corner
    pump(1.5)
    check("remaining popup slides back into the corner", p1.isVisible() and abs(p1.geometry().bottom() - g0.bottom()) <= 2,
          f"{p1.geometry().getRect()}")
    a.toggle_camera(a.cameras[1])
    pump(0.8)

    for c in a.cameras:
        c.stop()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
