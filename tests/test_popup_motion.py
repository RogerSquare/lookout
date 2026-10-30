"""Drives the popup with synthetic mouse events and checks where the springs land it.

Run: .venv/Scripts/python tests/test_popup_motion.py [--shots DIR]
"""

import argparse
import sys
import time
from pathlib import Path

import av
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lookout  # noqa: E402

lookout.save_state = lambda state: None  # never touch the real remembered corner


class FakeCamera:
    name = "Test Cam"
    person = True

    def __init__(self):
        with av.open(str(Path(__file__).parent / "people.jpg")) as c:
            self.latest = next(c.decode(c.streams.video[0]))
        self.last_person_time = time.time()


def pump(seconds, popup=None, samples=None):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents()
        if samples is not None:
            g = popup.geometry()
            samples.append((g.x(), g.y(), g.width(), g.height()))
        time.sleep(0.004)


def send(popup, kind, global_pos, buttons):
    local = QPointF(popup.mapFromGlobal(global_pos.toPoint()))
    button = Qt.LeftButton if kind != QEvent.MouseMove else Qt.NoButton
    QApplication.sendEvent(popup, QMouseEvent(kind, local, global_pos, button, buttons, Qt.NoModifier))


def drag(popup, to, duration, pause_before_release=0.0, start=None):
    start = start or QPointF(popup.geometry().center())
    send(popup, QEvent.MouseButtonPress, start, Qt.LeftButton)
    steps = max(4, int(duration / 0.012))
    for i in range(1, steps + 1):
        pos = start + (to - start) * (i / steps)
        send(popup, QEvent.MouseMove, pos, Qt.LeftButton)
        pump(duration / steps)
    pump(pause_before_release)
    send(popup, QEvent.MouseButtonRelease, to, Qt.NoButton)


def click(popup):
    c = QPointF(popup.geometry().center())
    send(popup, QEvent.MouseButtonPress, c, Qt.LeftButton)
    send(popup, QEvent.MouseButtonRelease, c, Qt.NoButton)


def check(name, popup, corner, samples, enlarged=False):
    expected = popup._corner_rect(corner).toRect()
    got = popup.geometry()
    ok = got == expected and popup.enlarged == enlarged
    # Overshoot: how far past the target the window travelled on the way in
    xs = [s[0] for s in samples] or [got.x()]
    ys = [s[1] for s in samples] or [got.y()]
    over_x = max(0, max(xs) - expected.x()) if xs[0] < expected.x() else max(0, expected.x() - min(xs))
    over_y = max(0, max(ys) - expected.y()) if ys[0] < expected.y() else max(0, expected.y() - min(ys))
    print(f"{'PASS' if ok else 'FAIL'} {name}: corner={popup._current_corner()} geom={got.getRect()} "
          f"expected={expected.getRect()} overshoot=({over_x},{over_y})px frames={len(samples)}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="directory to save mid-animation screenshots")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    settings = dict(lookout.DEFAULTS)
    popup = lookout.Popup(FakeCamera(), settings, {}, on_dismiss=lambda cam: None)
    area = app.primaryScreen().availableGeometry()
    results = []

    def shot(name):
        if args.shots:
            app.primaryScreen().grabWindow(0).save(str(Path(args.shots) / f"{name}.png"))

    s = []
    popup.present()
    pump(0.08)
    shot("1_appearing")
    pump(0.9, popup, s)
    results.append(check("appear in bottom-right", popup, "bottom-right", s))

    s = []
    drag(popup, QPointF(area.center().x() - 200, area.center().y() - 150), duration=0.12)  # fast fling up-left
    pump(0.1, popup, s)
    shot("2_flinging")
    pump(1.2, popup, s)
    results.append(check("fling to top-left", popup, "top-left", s))

    s = []
    click(popup)
    pump(0.12, popup, s)
    shot("3_enlarging")
    pump(1.0, popup, s)
    results.append(check("click enlarges in place", popup, "top-left", s, enlarged=True))

    s = []
    click(popup)
    pump(1.0, popup, s)
    results.append(check("click shrinks back", popup, "top-left", s))

    # Slow drag past centre toward bottom-right, then hold still before letting go: no fling,
    # so it should snap to the quadrant it was dropped in.
    s = []
    drag(popup, QPointF(area.center().x() + 300, area.center().y() + 200), duration=0.8, pause_before_release=0.2)
    pump(1.2, popup, s)
    results.append(check("slow drop snaps to nearest (bottom-right)", popup, "bottom-right", s))

    # Fling sideways from bottom-right: momentum should carry it to bottom-left even though
    # it's released on the right half of the screen.
    s = []
    drag(popup, QPointF(area.center().x() + 250, area.bottom() - 200), duration=0.1)
    pump(1.2, popup, s)
    results.append(check("fling carries to bottom-left", popup, "bottom-left", s))

    # --- edge resizing (popup is in bottom-left now) ---
    def check_size(name, width, corner):
        g = popup.geometry()
        expected = popup._corner_rect(corner).toRect()
        ok = (g == expected and abs(g.width() - width) <= 1 and abs(g.height() - round(width * 9 / 16)) <= 1)
        print(f"{'PASS' if ok else 'FAIL'} {name}: geom={g.getRect()} expected={expected.getRect()} "
              f"want width={width} saved width={popup.state.get('width')}")
        return ok

    g = popup.geometry()
    inner_corner = QPointF(g.right() - 2, g.top() + 2)  # top-right = the inner corner here
    send(popup, QEvent.MouseMove, inner_corner, Qt.NoButton)
    ok = popup.cursor().shape() == Qt.SizeBDiagCursor
    print(f"{'PASS' if ok else 'FAIL'} hovering the inner corner shows the diagonal resize cursor")
    results.append(ok)

    drag(popup, inner_corner + QPointF(200, -40), duration=0.3, start=inner_corner)
    pump(0.8)
    results.append(check_size("corner drag grows 480->680, stays pinned bottom-left, 16:9", 680, "bottom-left"))

    g = popup.geometry()
    right_edge = QPointF(g.right() - 2, g.center().y())
    drag(popup, right_edge - QPointF(900, 0), duration=0.3, start=right_edge)
    pump(0.8)
    results.append(check_size("right-edge drag shrinks, clamped to 280 min", 280, "bottom-left"))

    click(popup)
    pump(1.0)
    results.append(check_size("click enlarges to 2x the custom size", 560, "bottom-left"))
    click(popup)
    pump(1.0)

    g = popup.geometry()
    top_edge = QPointF(g.center().x(), g.top() + 2)
    drag(popup, top_edge - QPointF(0, 90), duration=0.3, start=top_edge)
    pump(0.8)
    results.append(check_size("top-edge drag grows by height (90px -> +160 wide)", 440, "bottom-left"))

    popup.close_popup()
    pump(0.6)
    print(f"{'PASS' if not popup.isVisible() else 'FAIL'} hide animation completes and window hides")
    results.append(not popup.isVisible())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
