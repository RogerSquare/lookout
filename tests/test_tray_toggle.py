"""Checks the tray toggle: a hand-opened popup stays up with nobody in frame until toggled off.

Uses an empty-street image (no people), so nothing auto-triggers. Takes ~25 s because it waits
past the auto-hide delay. Run: .venv/Scripts/python tests/test_tray_toggle.py [--shot FILE]
"""

import argparse
import sys
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import lookout  # noqa: E402

lookout.save_state = lambda state: None


def pump(seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", help="save a screenshot of the popup with its hover buttons showing")
    args = ap.parse_args()

    empty = HERE / "empty.jpg"
    if not empty.exists():
        Image.open(HERE / "people.jpg").crop((0, 0, 810, 300)).resize((1280, 720)).save(empty)

    app = QApplication(sys.argv)
    settings = dict(lookout.DEFAULTS, gpu_helper_port=None, cameras=[{"name": "Test Cam", "url": "tests/empty.jpg", "loop": True}],
                    sound="")
    a = lookout.App(settings)
    cam, popup = a.cameras[0], a.popups["Test Cam"]
    results = []

    def check(name, ok):
        print(f"{'PASS' if ok else 'FAIL'} {name}")
        results.append(ok)

    pump(4)
    check("nothing pops up with nobody in frame", not popup.isVisible())

    a._tray_activated(QSystemTrayIcon.Trigger)  # left-click the tray icon
    pump(1.0)
    check("tray left-click opens the flyout (not every camera)", a.flyout.isVisible() and not popup.isVisible())
    a.flyout.tiles[0].on_show(cam)               # click the camera's tile in the flyout
    pump(1.0)
    check("flyout tile opens that camera and closes the flyout",
          popup.isVisible() and popup.pinned and not a.flyout.isVisible())
    check("tray menu item shows as checked", a.camera_actions["Test Cam"].isChecked())

    if args.shot:
        popup.hovered, popup.hover_button = True, "close"
        popup.update()
        pump(0.3)
        g = popup.geometry().adjusted(-40, -40, 40, 40)
        app.primaryScreen().grabWindow(0, g.x(), g.y(), g.width(), g.height()).save(args.shot)
        popup.hovered, popup.hover_button = False, None

    pump(settings["hide_after_seconds"] + 3)
    check(f"still open after {settings['hide_after_seconds'] + 3}s with nobody in frame (no auto-hide)",
          popup.isVisible() and not popup.closing)

    a.toggle_camera(cam)  # menu item / toggle off
    pump(0.8)
    check("toggling again closes it", not popup.isVisible())
    check("menu item unchecked", not a.camera_actions["Test Cam"].isChecked())

    a.toggle_camera(cam)
    pump(0.8)
    c = QPointF(popup._close_rect().center())
    g = QPointF(popup.mapToGlobal(c.toPoint()))
    QApplication.sendEvent(popup, QMouseEvent(QEvent.MouseButtonPress, c, g, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    pump(0.8)
    check("X button closes a hand-opened popup", not popup.isVisible())

    for c in a.cameras:
        c.stop()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
