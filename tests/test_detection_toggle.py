"""Checks the "Person alerts" switch: off = no inference and no popups; manual open still works.

Uses the looped people photo, which would pop up immediately with alerts on.
Run: .venv/Scripts/python tests/test_detection_toggle.py
"""

import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lookout  # noqa: E402

lookout.load_state = lambda: {"detection": False}  # start with alerts off, as if saved that way
saved = {}
lookout.save_state = lambda state: saved.update(state)


def pump(seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def main():
    app = QApplication(sys.argv)  # noqa: F841
    settings = dict(lookout.DEFAULTS, gpu_helper_port=None, cameras=[{"name": "Test Cam", "url": "tests/people.jpg", "loop": True}],
                    sound="")
    a = lookout.App(settings)
    cam, popup = a.cameras[0], a.popups["Test Cam"]
    calls = {"n": 0}
    real_predict = a.cameras[0].model.predict

    def counting_predict(*args, **kw):
        calls["n"] += 1
        return real_predict(*args, **kw)

    cam.model.predict = counting_predict
    results = []

    def check(name, ok):
        print(f"{'PASS' if ok else 'FAIL'} {name}")
        results.append(ok)

    check("saved 'off' setting is restored at startup", not a.detection and not a.detection_action.isChecked())
    pump(5)
    check("alerts off: people in frame, but no popup", not popup.isVisible())
    check(f"alerts off: zero detections run ({calls['n']})", calls["n"] == 0)

    a.toggle_camera(cam)
    pump(1)
    check("alerts off: camera can still be opened by hand", popup.isVisible())
    a.toggle_camera(cam)
    pump(1)

    a.detection_action.trigger()  # tick "Person alerts" in the tray menu
    pump(3)
    check("alerts on: popup appears for the people in frame", popup.isVisible() and not popup.pinned)
    check(f"alerts on: detections running ({calls['n']})", calls["n"] > 0)
    check("setting saved as on", saved.get("detection") is True)

    a.detection_action.trigger()  # untick
    pump(1)
    n = calls["n"]
    pump(2)
    check("turning alerts off closes the auto popup", not popup.isVisible())
    check(f"turning alerts off stops detection ({calls['n'] - n} runs in 2s)", calls["n"] - n == 0)
    check("setting saved as off", saved.get("detection") is False)

    for c in a.cameras:
        c.stop()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
