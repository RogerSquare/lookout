"""Lookout - pops up a live camera feed in the corner of the screen when a person is detected.

Reads each camera's RTSP stream directly, runs YOLO person detection on the GPU, and shows an
Apple TV-style picture-in-picture window that hides itself once the person has left. The window
can be dragged and flung to any screen corner, FaceTime-style.
"""

import argparse
import collections
import ctypes
import io
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import urllib.request
import winreg
import winsound
from pathlib import Path

import av
import numpy as np
from PIL import Image
from PySide6.QtCore import QEasingCurve, QLockFile, QPointF, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QFontMetricsF, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from tray_flyout import Flyout

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "Lookout"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "cameras": [],
    "model": "yolo11s.pt",
    "device": "cuda:0",         # or "cpu"
    "detect_fps": 5,
    "confidence": 0.5,
    "trigger_hits": 2,          # positives needed...
    "trigger_window": 3,        # ...within this many consecutive checks
    "hide_after_seconds": 15,
    "popup_width": 480,
    "enlarged_width": 960,
    "margin": 24,
    "sound": "C:/Windows/Media/Windows Notify Calendar.wav",
    "hub_url": None,            # e.g. "http://10.0.0.127:8765": alerts from the Lookout Hub
    "frigate_url": None,        # e.g. "https://10.0.0.127:8971": "Open Frigate" in the tray flyout
    "gpu_helper_port": 8770,    # serve the hub's detection on this PC's GPU; null to disable
    "gpu_helper_model": "yolo11l.pt",
    "warm_preview": True,       # hub mode: keep each camera's small stream running so popups open live
}

NO_VIDEO_TIMEOUT = 6            # connected but no decoded video for this long: drop and reconnect

log = logging.getLogger("lookout")


class Camera:
    """Decodes one stream on a background thread and runs person detection on another."""

    def __init__(self, cfg, settings, model, model_lock):
        self.name = cfg["name"]
        self.url = cfg["url"] if "://" in cfg["url"] else str(APP_DIR / cfg["url"])
        self.loop = cfg.get("loop", False)  # for test sources (image/video files)
        self.settings = settings
        self.model = model
        self.model_lock = model_lock

        self.latest = None          # newest av.VideoFrame
        self.latest_time = 0.0
        self.connected = False
        self.person = False
        self.last_person_time = 0.0
        self.triggered = False
        self.dismissed = False
        self.detect_enabled = True  # person alerts on/off (tray); off = no inference at all
        self.hits = collections.deque(maxlen=settings["trigger_window"])
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._read_loop, name=f"read-{self.name}", daemon=True).start()
        threading.Thread(target=self._detect_loop, name=f"detect-{self.name}", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _read_loop(self, stop=None, url=None, sink=None, label=None):
        """Decode `url` (default: the camera's) until `stop`, handing each frame to `sink`
        (default: self.latest). Reconnects on errors, and when a connected stream stops
        producing pictures for NO_VIDEO_TIMEOUT seconds."""
        stop = stop or self._stop
        url = url or self.url
        label = label or self.name
        sink = sink or self._set_latest
        backoff = 1
        while not stop.is_set():
            try:
                # Low-latency options for live streams only; they make file sources decode nothing.
                # Small probe: connect in well under a second instead of analysing for seconds.
                options = ({"rtsp_transport": "tcp", "fflags": "nobuffer", "flags": "low_delay",
                            "probesize": "65536", "analyzeduration": "500000"}
                           if url.startswith("rtsp") else {})
                with av.open(url, options=options, timeout=(5, 5)) as container:
                    stream = container.streams.video[0]
                    stream.thread_type = "AUTO"
                    last_picture = time.time()
                    announced = False
                    for packet in container.demux():
                        if stop.is_set():
                            return
                        if packet.stream.index == stream.index:
                            for frame in packet.decode():
                                last_picture = time.time()
                                if not announced:
                                    announced = True
                                    if not self.connected or label != self.name:
                                        log.info("%s: connected (%dx%d)", label, frame.width, frame.height)
                                    self.connected = True
                                    backoff = 1
                                sink(frame)
                                if self.loop:
                                    time.sleep(1 / 15)
                        if time.time() - last_picture > NO_VIDEO_TIMEOUT:
                            raise RuntimeError(f"no video for {NO_VIDEO_TIMEOUT}s")
                if self.loop:
                    stop.wait(1 / 15)
                    continue
                log.warning("%s: stream ended", label)
            except Exception as e:  # network drops, camera reboots, bad URL, stuck restream
                log.warning("%s: stream error: %s", label, e)
            if label == self.name:
                self.connected = False
            stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _set_latest(self, frame):
        self.latest = frame
        self.latest_time = time.time()

    def _detect_loop(self):
        interval = 1 / self.settings["detect_fps"]
        while not self._stop.is_set():
            started = time.time()
            frame = self.latest
            if self.detect_enabled and frame is not None and self.connected and started - self.latest_time < 5:
                try:
                    img = frame.reformat(width=960, height=540, format="bgr24").to_ndarray()
                    with self.model_lock:
                        result = self.model.predict(img, classes=[0], conf=self.settings["confidence"],
                                                    imgsz=640, device=self.settings["device"], verbose=False)[0]
                except Exception:
                    log.exception("%s: detection failed", self.name)
                    self._stop.wait(5)
                    continue
                found = len(result.boxes) > 0
                self.hits.append(found)
                self.person = found
                if found:
                    self.last_person_time = time.time()
                if not self.triggered and sum(self.hits) >= self.settings["trigger_hits"]:
                    self.triggered = True
                    best = float(result.boxes.conf.max()) if found else 0.0
                    log.info("%s: person detected (conf %.2f)", self.name, best)
            self._stop.wait(max(0.0, interval - (time.time() - started)))

    def person_gone(self):
        return time.time() - self.last_person_time > self.settings["hide_after_seconds"]

    def reset(self):
        self.triggered = False
        self.hits.clear()


class HubFeed:
    """Lookout Hub client: keeps the SSE alert stream open on a thread (reconnecting with backoff)
    and queues events for the Qt thread, which drains them in App.tick()."""

    def __init__(self, url):
        self.url = url.rstrip("/")
        self.events = queue.Queue()
        self.connected = False
        threading.Thread(target=self._run, name="hub-feed", daemon=True).start()

    @staticmethod
    def probe(url, timeout=3):
        """The hub's current state, or None if it can't be reached."""
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=timeout) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return None

    def _run(self):
        backoff = 1
        while True:
            try:
                # The hub sends a keepalive every 15 s, so a 40 s silence means a dead connection
                with urllib.request.urlopen(self.url + "/events", timeout=40) as resp:
                    self.connected = True
                    backoff = 1
                    kind, data = None, []
                    for raw in resp:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("event:"):
                            kind = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line:
                            if kind and data:
                                try:
                                    self.events.put(json.loads("\n".join(data)))
                                except ValueError:
                                    pass
                            kind, data = None, []
            except (OSError, ValueError) as e:
                if self.connected:
                    log.warning("hub connection lost: %s", e)
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def set_alerts(self, enabled, camera=None):
        """Shared alerts switch (POST /api/alerts), sent off the UI thread; the hub's `alerts`
        event confirms it."""
        body = {"enabled": enabled, **({"camera": camera} if camera else {})}

        def send():
            req = urllib.request.Request(self.url + "/api/alerts", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5).close()
            except OSError as e:
                log.warning("could not change hub alerts: %s", e)

        threading.Thread(target=send, daemon=True).start()

    def fetch(self, path, timeout=5):
        try:
            with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
                return r.read()
        except OSError:
            return None


class HubCamera(Camera):
    """A camera whose "person here" state comes from the hub. Its small restream (sd) keeps
    decoding in the background, so a popup opens on live video at once; the full-HD restream is
    only decoded while a popup is enlarged, and the picture falls back to sd if HD stalls."""

    FRESH = 1.5   # a frame newer than this (seconds) counts as live

    def __init__(self, info, settings, feed):
        streams = info.get("streams", {})
        self.hd_url = streams.get("hd") or streams.get("sd") or ""
        self.sd_url = streams.get("sd") or self.hd_url
        super().__init__({"name": info["name"], "url": self.sd_url, "loop": info.get("loop", False)},
                         settings, model=None, model_lock=None)
        self.id = info["id"]
        self.feed = feed
        self.connected = info.get("connected", False)
        self.person = info.get("person", False)
        self.alerts = info.get("alerts", True)
        self.warm = settings.get("warm_preview", True)
        self._sd = self._hd = None             # (thread, stop event)
        self._sd_frame = self._hd_frame = self._still = None
        self._sd_time = self._hd_time = 0.0
        self._popup_open = False

    # The popup reads .latest: freshest HD while enlarged, else sd, else the alert still
    @property
    def latest(self):
        now = time.time()
        if self._hd_frame is not None and now - self._hd_time < self.FRESH:
            return self._hd_frame
        if self._sd_frame is not None and now - self._sd_time < self.FRESH:
            return self._sd_frame
        return self._still if self._still is not None else self._sd_frame

    @latest.setter
    def latest(self, value):   # Camera.__init__ assigns None
        self._still = value

    def _start_reader(self, url, label, sink):
        stop = threading.Event()
        t = threading.Thread(target=self._read_loop, args=(stop, url, sink, label), name=f"read-{label}", daemon=True)
        t.start()
        return t, stop

    @staticmethod
    def _alive(reader):
        return reader is not None and reader[0].is_alive() and not reader[1].is_set()

    def _sd_sink(self, frame):
        self._sd_frame, self._sd_time = frame, time.time()

    def _hd_sink(self, frame):
        self._hd_frame, self._hd_time = frame, time.time()

    def start(self):
        if self.warm and self.sd_url:
            self._ensure_sd()

    def stop(self):
        self.close_stream()
        if self._sd:
            self._sd[1].set()
            self._sd = None

    def _ensure_sd(self):
        if not self._alive(self._sd):
            self._sd = self._start_reader(self.sd_url, f"{self.name} (preview)", self._sd_sink)

    @property
    def streaming(self):
        return self._popup_open

    def open_stream(self, snapshot_path=None):
        self._popup_open = True
        self._ensure_sd()
        # The alert still covers the moment before live video, unless sd is already live
        if snapshot_path and not (self._sd_frame is not None and time.time() - self._sd_time < self.FRESH):
            threading.Thread(target=self._load_snapshot, args=(snapshot_path,), daemon=True).start()

    def want_hd(self, on):
        """Popup enlarged: decode full HD (sd keeps covering until HD has a picture)."""
        if on and self.hd_url and self.hd_url != self.sd_url and self._popup_open:
            if not self._alive(self._hd):
                self._hd = self._start_reader(self.hd_url, f"{self.name} (HD)", self._hd_sink)
        elif self._hd:
            self._hd[1].set()
            self._hd, self._hd_frame = None, None

    def _load_snapshot(self, path):
        """Show the hub's detection frame at once while live video connects."""
        data = self.feed.fetch(path)
        if data and self._popup_open:
            img = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
            self._still = av.VideoFrame.from_ndarray(img, format="rgb24")

    def close_stream(self):
        self._popup_open = False
        self.want_hd(False)
        self._still = None
        if not self.warm and self._sd:
            self._sd[1].set()
            self._sd, self._sd_frame = None, None

    def person_gone(self):
        # The hub already waits hide_after_seconds after the last sighting before person_gone
        return not self.person


class Spring:
    """Damped spring for one value; slightly under-damped so moves settle with a small bounce."""

    def __init__(self, value, stiffness=380.0, damping_ratio=0.78):
        self.value = self.target = float(value)
        self.velocity = 0.0
        self.k = stiffness
        self.c = 2 * math.sqrt(stiffness) * damping_ratio

    def step(self, dt):
        accel = -self.k * (self.value - self.target) - self.c * self.velocity
        self.velocity += accel * dt
        self.value += self.velocity * dt

    def settled(self):
        return abs(self.value - self.target) < 0.4 and abs(self.velocity) < 4

    def snap(self, value):
        self.value = self.target = float(value)
        self.velocity = 0.0


class Popup(QWidget):
    """Frameless always-on-top picture-in-picture window that never steals focus.

    Drag it anywhere; on release it flings to the nearest screen corner (projected from the drag
    velocity, like FaceTime). Click to toggle enlarged. All motion runs on spring physics.
    Windows 11 draws the rounded corners, border and shadow (DWM), so it reads as a native window.
    """

    DRAG_THRESHOLD = 6        # px before a press counts as a drag rather than a click
    FLING_PROJECTION = 0.18   # seconds of momentum to project when choosing the corner
    FRAME_SIZE = (1280, 720)  # video is rendered once at this size and scaled by the painter
    EDGE = 8                  # px band along the border that resizes instead of drags
    MIN_WIDTH = 280
    MAX_WIDTH_FRACTION = 0.7  # of the screen's width
    STACK_GAP = 12            # px between stacked popups

    def __init__(self, camera, settings, state, on_dismiss):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.camera = camera
        self.settings = settings
        self.state = state
        self.on_dismiss = on_dismiss
        self.enlarged = False
        self.image = None
        self.pinned = False       # opened by hand: no auto-hide until toggled off
        self.closing = False
        self.press = None         # (global pos, window pos) at mouse press
        self.dragging = False
        self.resizing = None      # (edges, start geometry) while an edge is being dragged
        self.trail = collections.deque(maxlen=6)  # (time, global pos) for release velocity
        self.hovered = False
        self.hover_button = None  # "expand" / "close" while the pointer is over one
        self.slot = 0             # stack position when several popups are up
        self.opened_at = 0.0
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)  # hover cursor changes on the resize edges
        self.setCursor(Qt.OpenHandCursor)

        self.springs = [Spring(0) for _ in range(4)]  # x, y, w, h
        self.anim = QTimer(self, interval=8, timeout=self._animate)
        self.last_step = time.perf_counter()
        self.video_timer = QTimer(self, interval=66, timeout=self._refresh)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.finished.connect(self._fade_finished)

    # ---- geometry -------------------------------------------------------------------------

    def _size(self):
        base = self.state.get("width", self.settings["popup_width"])
        w = min(base * 2, self._max_width()) if self.enlarged else base
        return w, round(w * 9 / 16)

    def _max_width(self):
        return round(self._screen().availableGeometry().width() * self.MAX_WIDTH_FRACTION)

    def _screen(self):
        name = self.state.get("screen")
        for s in QApplication.screens():
            if s.name() == name:
                return s
        return QApplication.primaryScreen()

    def _corner_rect(self, corner, screen=None, size=None):
        area = (screen or self._screen()).availableGeometry()
        w, h = size or self._size()
        m = self.settings["margin"]
        x = area.left() + m if "left" in corner else area.right() - w - m + 1
        y = area.top() + m if "top" in corner else area.bottom() - h - m + 1
        # Several popups at once stack away from the corner instead of covering each other
        step = (h + self.STACK_GAP) * self.slot
        y += step if "top" in corner else -step
        return QRectF(x, y, w, h)

    def set_slot(self, slot):
        """Stack position among the visible popups (0 = in the corner)."""
        if slot != self.slot:
            self.slot = slot
            if self.isVisible() and not self.closing and not self.dragging and not self.resizing:
                self._animate_to(self._corner_rect(self.state.get("corner", "bottom-right"), self._screen()))

    def relayout(self):
        """Spring to the remembered corner/size (after they change in the tray flyout)."""
        if self.isVisible() and not self.closing:
            self.enlarged = False
            self._animate_to(self._corner_rect(self.state.get("corner", "bottom-right"), self._screen()))

    def _animate_to(self, rect):
        for spring, v in zip(self.springs, (rect.x(), rect.y(), rect.width(), rect.height())):
            spring.target = v
        if not self.anim.isActive():
            self.last_step = time.perf_counter()
            self.anim.start()

    def _jump_to(self, rect):
        for spring, v in zip(self.springs, (rect.x(), rect.y(), rect.width(), rect.height())):
            spring.snap(v)
        self._apply()

    def _apply(self):
        x, y, w, h = (round(s.value) for s in self.springs)
        self.setGeometry(x, y, max(w, 40), max(h, 24))

    def _animate(self):
        now = time.perf_counter()
        dt, self.last_step = min(now - self.last_step, 1 / 30), now
        for _ in range(4):  # substeps keep the stiff spring stable at uneven frame times
            for s in self.springs:
                s.step(dt / 4)
        self._apply()
        if all(s.settled() for s in self.springs):
            for s in self.springs:
                s.snap(s.target)
            self._apply()
            self.anim.stop()

    # ---- show / hide ----------------------------------------------------------------------

    def present(self):
        self.closing = False
        self.enlarged = False
        self.opened_at = time.time()
        corner = self.state.get("corner", "bottom-right")
        target = self._corner_rect(corner)
        # Grow out of the corner: start at 80% size, pinned to the same corner
        sw, sh = target.width() * 0.8, target.height() * 0.8
        sx = target.x() if "left" in corner else target.right() - sw
        sy = target.y() if "top" in corner else target.bottom() - sh
        self._jump_to(QRectF(sx, sy, sw, sh))
        self._refresh()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade(1.0, 180)
        self._animate_to(target)
        self.video_timer.start()

    def close_popup(self):
        if not self.isVisible() or self.closing:
            return
        self.closing = True
        r = self.geometry()
        self._animate_to(QRectF(r.center().x() - r.width() * 0.4, r.center().y() - r.height() * 0.4,
                                r.width() * 0.8, r.height() * 0.8))
        self._fade(0.0, 160)

    def _fade(self, end, ms):
        self.fade.stop()
        self.fade.setDuration(ms)
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(end)
        self.fade.setEasingCurve(QEasingCurve.OutCubic)
        self.fade.start()

    def _fade_finished(self):
        if self.closing:
            self.anim.stop()
            self.video_timer.stop()
            self.hide()
            self.image = None
            self.closing = False
            self.pinned = False

    # ---- video ----------------------------------------------------------------------------

    def _refresh(self):
        if hasattr(self.camera, "want_hd"):
            self.camera.want_hd(self.enlarged and self.isVisible() and not self.closing)
        frame = self.camera.latest
        if frame is not None:
            w, h = self.FRAME_SIZE
            # Scale to cover 16:9, then center-crop, so non-16:9 sources aren't stretched
            scale = max(w / frame.width, h / frame.height)
            sw, sh = max(w, round(frame.width * scale)), max(h, round(frame.height * scale))
            arr = frame.reformat(width=sw, height=sh, format="rgb24").to_ndarray()
            x, y = (sw - w) // 2, (sh - h) // 2
            arr = arr[y:y + h, x:x + w].copy()
            self.image = QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGB888).copy()
        self.update()

    # ---- painting -------------------------------------------------------------------------

    def _close_rect(self):
        return QRectF(self.width() - 8 - 32, 8, 32, 28)

    def _expand_rect(self):
        return QRectF(self.width() - 8 - 32 - 4 - 32, 8, 32, 28)

    def showEvent(self, event):
        super().showEvent(event)
        _native_window_style(int(self.winId()))

    def enterEvent(self, event):
        self.hovered = True
        self.update()

    def leaveEvent(self, event):
        self.hovered = False
        self.hover_button = None
        self.update()

    def paintEvent(self, _):
        # Corners, border and shadow come from DWM (see _native_window_style); paint the full rect
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(32, 32, 32))
        if self.image is not None:
            p.drawImage(QRectF(self.rect()), self.image)
        else:
            p.setPen(QColor(255, 255, 255, 160))
            p.setFont(QFont("Segoe UI Variable Text", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "Connecting…")

        # Status pill (Fluent acrylic-style flyout surface)
        live = self.camera.person
        if live:
            status = "Person detected"
        elif self.camera.last_person_time:
            status = "Person left " + _ago(self.camera.last_person_time)
        else:
            status = "Live"
        title_font = QFont("Segoe UI Variable Text Semibold", 10)
        sub_font = QFont("Segoe UI Variable Small", 8)
        tw = QFontMetricsF(title_font).horizontalAdvance(self.camera.name)
        sw = QFontMetricsF(sub_font).horizontalAdvance(status)
        pill = QRectF(8, 8, 30 + max(tw, sw) + 12, 40)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(28, 28, 28, 200))
        p.drawRoundedRect(pill, 4, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 99, 97) if live else QColor(255, 255, 255, 110))  # Fluent critical / neutral
        p.drawEllipse(QPointF(pill.x() + 15, pill.y() + 14), 4, 4)
        p.setPen(QColor(255, 255, 255))
        p.setFont(title_font)
        p.drawText(QRectF(pill.x() + 26, pill.y() + 3, tw + 4, 20), Qt.AlignVCenter, self.camera.name)
        p.setPen(QColor(255, 255, 255, 200))
        p.setFont(sub_font)
        p.drawText(QRectF(pill.x() + 26, pill.y() + 20, sw + 4, 16), Qt.AlignVCenter, status)

        # Caption buttons, shown on hover like the Windows media mini-player
        if self.hovered:
            icon_font = QFont("Segoe Fluent Icons", 9)
            for name, rect, glyph in (("expand", self._expand_rect(), "" if self.enlarged else ""),
                                      ("close", self._close_rect(), "")):
                hot = self.hover_button == name
                if hot and name == "close":
                    bg = QColor(196, 43, 28)  # Windows close-button red
                else:
                    bg = QColor(60, 60, 60, 220) if hot else QColor(28, 28, 28, 200)
                p.setPen(QPen(QColor(255, 255, 255, 22), 1))
                p.setBrush(bg)
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(QColor(255, 255, 255))
                p.setFont(icon_font)
                p.drawText(rect, Qt.AlignCenter, glyph)

    # ---- mouse: click to enlarge, drag + fling to a corner, drag an edge to resize --------------

    def _edges_at(self, pos):
        e = self.EDGE
        edges = set()
        if pos.x() < e:
            edges.add("left")
        elif pos.x() > self.width() - e:
            edges.add("right")
        if pos.y() < e:
            edges.add("top")
        elif pos.y() > self.height() - e:
            edges.add("bottom")
        return frozenset(edges)

    @staticmethod
    def _edge_cursor(edges):
        if edges in ({"left", "top"}, {"right", "bottom"}):
            return Qt.SizeFDiagCursor
        if edges in ({"right", "top"}, {"left", "bottom"}):
            return Qt.SizeBDiagCursor
        if edges & {"left", "right"}:
            return Qt.SizeHorCursor
        if edges:
            return Qt.SizeVerCursor
        return Qt.OpenHandCursor

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._close_rect().contains(event.position()):
            self.on_dismiss(self.camera)
            return
        if self._expand_rect().contains(event.position()):
            self._toggle_enlarged()
            return
        self.anim.stop()  # grab it mid-flight; springs keep their current value
        for s in self.springs:
            s.velocity = 0.0
        g = event.globalPosition()
        edges = self._edges_at(event.position())
        if edges:
            self.resizing = (edges, QRectF(*(s.value for s in self.springs)), g, self._current_corner())
            return
        self.press = (g, QPointF(self.springs[0].value, self.springs[1].value))
        self.dragging = False
        self.trail.clear()
        self.trail.append((time.perf_counter(), g))

    def _resize_to(self, g):
        edges, r0, start, corner = self.resizing
        dx, dy = g.x() - start.x(), g.y() - start.y()
        # Width implied by each dragged edge; 16:9 is locked, so the bigger change wins
        candidates = []
        if "left" in edges:
            candidates.append(r0.width() - dx)
        if "right" in edges:
            candidates.append(r0.width() + dx)
        if "top" in edges:
            candidates.append((r0.height() - dy) * 16 / 9)
        if "bottom" in edges:
            candidates.append((r0.height() + dy) * 16 / 9)
        w = max(candidates, key=lambda c: abs(c - r0.width()))
        w = max(self.MIN_WIDTH, min(w, self._max_width()))
        h = w * 9 / 16
        # Keep the opposite side fixed; for a single edge, stay pinned to the snapped corner
        pin_right = "left" in edges or ("right" not in edges and "right" in corner)
        pin_bottom = "top" in edges or ("bottom" not in edges and "bottom" in corner)
        x = r0.right() - w if pin_right else r0.x()
        y = r0.bottom() - h if pin_bottom else r0.y()
        for s, v in zip(self.springs, (x, y, w, h)):
            s.snap(v)
        self._apply()

    def mouseMoveEvent(self, event):
        if self.resizing:
            self._resize_to(event.globalPosition())
            return
        if self.press is None:
            if not event.buttons():
                pos = event.position()
                button = ("close" if self._close_rect().contains(pos)
                          else "expand" if self._expand_rect().contains(pos) else None)
                if button != self.hover_button:
                    self.hover_button = button
                    self.update()
                self.setCursor(Qt.ArrowCursor if button else self._edge_cursor(self._edges_at(pos)))
            return
        g = event.globalPosition()
        start, origin = self.press
        delta = g - start
        if not self.dragging and (abs(delta.x()) + abs(delta.y())) < self.DRAG_THRESHOLD:
            return
        if not self.dragging:
            self.dragging = True
            self.setCursor(Qt.ClosedHandCursor)
        self.trail.append((time.perf_counter(), g))
        self.springs[0].snap(origin.x() + delta.x())
        self.springs[1].snap(origin.y() + delta.y())
        self.springs[2].snap(self.springs[2].target)
        self.springs[3].snap(self.springs[3].target)
        self._apply()

    def mouseReleaseEvent(self, event):
        if self.resizing:
            corner = self.resizing[3]
            self.resizing = None
            # The dragged size becomes the new normal size; spring back into the corner exactly
            self.enlarged = False
            self.state["width"] = round(self.springs[2].value)
            save_state(self.state)
            self._animate_to(self._corner_rect(corner))
            return
        if self.press is None:
            return
        self.press = None
        self.setCursor(Qt.OpenHandCursor)
        if not self.dragging:
            self._toggle_enlarged()
            return
        self.dragging = False

        # Release velocity from the last few drag samples
        (t0, p0), (t1, p1) = self.trail[0], self.trail[-1]
        dt = max(t1 - t0, 1e-3)
        vx, vy = (p1.x() - p0.x()) / dt, (p1.y() - p0.y()) / dt
        if time.perf_counter() - t1 > 0.08:  # paused before letting go: no fling
            vx = vy = 0.0

        w, h = self.width(), self.height()
        cx = self.springs[0].value + w / 2 + vx * self.FLING_PROJECTION
        cy = self.springs[1].value + h / 2 + vy * self.FLING_PROJECTION
        screen = QApplication.screenAt(QPointF(cx, cy).toPoint()) or QApplication.screenAt(self.geometry().center()) \
            or QApplication.primaryScreen()
        area = screen.availableGeometry()
        corner = ("top" if cy < area.center().y() else "bottom") + "-" + ("left" if cx < area.center().x() else "right")
        self.state.update(corner=corner, screen=screen.name())
        save_state(self.state)

        self.springs[0].velocity, self.springs[1].velocity = vx, vy  # carry momentum into the snap
        self._animate_to(self._corner_rect(corner, screen))

    def _toggle_enlarged(self):
        self.enlarged = not self.enlarged
        self._animate_to(self._corner_rect(self._current_corner()))

    def _current_corner(self):
        area = self._screen().availableGeometry()
        c = self.geometry().center()
        return ("top" if c.y() < area.center().y() else "bottom") + "-" + ("left" if c.x() < area.center().x() else "right")


def _native_window_style(hwnd):
    """Ask DWM for Windows 11 rounded corners, dark border and the standard window shadow."""
    class MARGINS(ctypes.Structure):
        _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

    dwm = ctypes.windll.dwmapi
    for attr, value in ((33, 2),   # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
                        (20, 1)):  # DWMWA_USE_IMMERSIVE_DARK_MODE
        v = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    # A 1px frame extension is what gives a frameless window the native drop shadow
    dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(MARGINS(0, 0, 1, 0)))


def _ago(t):
    s = int(time.time() - t)
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago"


def _taskbar_is_light():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def _tray_icon(paused=False):
    """Monochrome Segoe Fluent glyph, like Windows' own tray icons (Video, or VideoOff when paused)."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(QColor(0, 0, 0) if _taskbar_is_light() else QColor(255, 255, 255))
    font = QFont("Segoe Fluent Icons")
    font.setPixelSize(52)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, "" if paused else "")
    p.end()
    return QIcon(pix)


class GpuHelperServer:
    """Detection service for the Lookout Hub on quad-box: POST /detect with a JPEG, get back person
    boxes found by a larger model on this PC's GPU. The hub falls back to its own CPU whenever this
    doesn't answer (PC off, asleep, app closed), so nothing depends on it being up.

        POST /detect   image/jpeg -> {"boxes": [[cx, cy, w, h, score], ...], "ms": ..., "model": ...}
                       (boxes as fractions of the image, person class only, score > 0.05)
        GET  /health   -> {"ok": true, "model": ...}
    """

    def __init__(self, model, model_name, port, device):
        import io
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        import numpy as np
        from PIL import Image

        import queue

        server_self = self
        self.requests = 0
        # All GPU work runs on ONE long-lived thread. The HTTP server handles each request on a new
        # thread, and PyTorch keeps per-thread CUDA state that is never freed when a thread ends:
        # predicting on request threads leaked ~1.2 MB per request (~12 MB/s at the hub's 10/s).
        jobs = queue.Queue()

        def gpu_worker():
            while True:
                img, done = jobs.get()
                try:
                    started = time.perf_counter()
                    r = model.predict(img, classes=[0], conf=0.05, imgsz=640, device=device, verbose=False)[0]
                    done["boxes"] = [[*map(float, xywh), float(c)]
                                     for xywh, c in zip(r.boxes.xywhn.tolist(), r.boxes.conf.tolist())]
                    done["ms"] = round((time.perf_counter() - started) * 1000, 1)
                except Exception as e:  # keep the worker alive whatever one image does
                    done["error"] = str(e)
                done["event"].set()

        threading.Thread(target=gpu_worker, name="gpu-helper-worker", daemon=True).start()

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._json(200, {"ok": True, "model": model_name, "requests": server_self.requests})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/detect":
                    return self._json(404, {"error": "not found"})
                try:
                    data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    img = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))[:, :, ::-1]  # BGR
                except Exception as e:
                    return self._json(400, {"error": f"bad image: {e}"})
                done = {"event": threading.Event()}
                jobs.put((np.ascontiguousarray(img), done))
                if not done["event"].wait(5):
                    return self._json(503, {"error": "busy"})
                if "error" in done:
                    return self._json(500, {"error": done["error"]})
                server_self.requests += 1
                self._json(200, {"boxes": done["boxes"], "model": model_name, "ms": done["ms"]})

            def log_message(self, *args):  # quiet: up to ~10 requests a second
                pass

        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="gpu-helper", daemon=True).start()
        log.info("GPU helper for the hub listening on :%d (%s)", port, model_name)

    def stop(self):
        self.httpd.shutdown()


class App:
    def __init__(self, settings):
        from ultralytics import YOLO  # slow import; keep it out of module load

        self.settings = settings
        self.gpu_helper = None
        if settings.get("gpu_helper_port"):
            helper_name = settings["gpu_helper_model"]
            helper_path = APP_DIR / "models" / helper_name
            helper_path.parent.mkdir(exist_ok=True)
            try:
                self.gpu_helper = GpuHelperServer(YOLO(str(helper_path)), helper_name,
                                                  settings["gpu_helper_port"], settings["device"])
            except OSError as e:  # port in use (second copy, another service)
                log.warning("GPU helper not started: %s", e)
        # Hub mode: the Lookout Hub on quad-box does the detection for every client and owns the
        # shared alerts switch. Without a reachable hub, detect locally as before (standalone).
        self.feed = None
        hub_state = HubFeed.probe(settings["hub_url"]) if settings.get("hub_url") else None
        self.hub_mode = hub_state is not None
        self.state = load_state()
        if self.hub_mode:
            self.feed = HubFeed(settings["hub_url"])
            self.cameras = [HubCamera(c, settings, self.feed) for c in hub_state["cameras"]]
            log.info("hub mode (%s): %s", settings["hub_url"], ", ".join(c.name for c in self.cameras))
        else:
            if settings.get("hub_url"):
                log.warning("hub unreachable at %s: detecting locally (standalone)", settings["hub_url"])
            model_path = APP_DIR / "models" / settings["model"]
            model_path.parent.mkdir(exist_ok=True)
            model = YOLO(str(model_path))
            model_lock = threading.Lock()
            self.cameras = [Camera(c, settings, model, model_lock) for c in settings["cameras"]]
        self.popups = {cam.name: Popup(cam, settings, self.state, self.dismiss) for cam in self.cameras}
        self.paused_until = 0.0
        self.icon_paused = False
        if self.hub_mode:
            self.detection = hub_state["alerts"]["enabled"]
        else:
            self.set_detection(self.state.get("detection", True), save=False)

        self.tray = QSystemTrayIcon(_tray_icon())
        self.tray.activated.connect(self._tray_activated)
        menu = QMenu()
        self.camera_actions = {}
        for cam in self.cameras:
            act = QAction(cam.name, menu, checkable=True)
            act.triggered.connect(lambda _=False, c=cam: self.toggle_camera(c))
            menu.addAction(act)
            self.camera_actions[cam.name] = act
        menu.addSeparator()
        self.detection_action = QAction("Person alerts", menu, checkable=True)
        self.detection_action.setChecked(self.detection)
        self.detection_action.triggered.connect(lambda checked: self.set_detection(checked))
        menu.addAction(self.detection_action)
        self.camera_alert_actions = {}
        if self.hub_mode:
            # Per-camera alerts, shared through the hub with the projector
            sub = menu.addMenu("Alerts from")
            for cam in self.cameras:
                act = QAction(cam.name, sub, checkable=True)
                act.setChecked(cam.alerts)
                act.triggered.connect(lambda checked, c=cam: self.feed.set_alerts(checked, c.id))
                sub.addAction(act)
                self.camera_alert_actions[cam.id] = act
        self.pause_action = QAction("Pause for 1 hour", menu)
        self.pause_action.triggered.connect(self.toggle_pause)
        menu.addAction(self.pause_action)
        open_log = QAction("Open log", menu)
        open_log.triggered.connect(lambda: os.startfile(DATA_DIR / "lookout.log"))
        menu.addAction(open_log)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.show()
        self.menu = menu
        self.flyout = Flyout(self, _native_window_style)

        for cam in self.cameras:
            cam.start()
        self.tick_timer = QTimer(interval=200, timeout=self.tick)
        self.tick_timer.start()

    def show_popup(self, cam, pinned=False):
        """pinned: opened by hand, so it stays up (no chime, no auto-hide) until toggled off."""
        popup = self.popups[cam.name]
        if pinned:
            popup.pinned = True
        if popup.isVisible() and not popup.closing:
            return
        if isinstance(cam, HubCamera):
            # Alert: the frame that raised it; by hand: the newest frame. Live video follows in ~1 s.
            cam.open_stream(getattr(cam, "snapshot", None) if not pinned else f"/api/cameras/{cam.id}/live.jpg")
        # Stack above any popup already up
        popup.set_slot(sum(1 for p in self.popups.values() if p is not popup and p.isVisible() and not p.closing))
        popup.present()
        if not pinned:
            self._chime()

    def toggle_camera(self, cam):
        popup = self.popups[cam.name]
        if popup.isVisible() and not popup.closing:
            self.dismiss(cam)
        else:
            log.info("%s: opened from tray", cam.name)
            self.show_popup(cam, pinned=True)

    def _tray_activated(self, reason):
        # Left-click the tray icon: the Windows 11-style flyout (cameras, alerts, popup settings)
        if reason == QSystemTrayIcon.Trigger:
            self.flyout.toggle()

    # ---- actions used by the tray flyout ---------------------------------------------------

    @property
    def paused(self):
        return time.time() < self.paused_until

    def camera_alerts(self, cam):
        if self.hub_mode:
            return cam.alerts
        return self.state.get("camera_alerts", {}).get(cam.name, True)

    def set_camera_alerts(self, cam, on):
        if self.hub_mode:
            cam.alerts = on  # optimistic; the hub's `alerts` event confirms it
            self.feed.set_alerts(on, cam.id)
        else:
            self.state.setdefault("camera_alerts", {})[cam.name] = on
            save_state(self.state)
        if not on and self.popups[cam.name].isVisible() and not self.popups[cam.name].pinned:
            self.popups[cam.name].close_popup()
        log.info("%s: alerts %s", cam.name, "on" if on else "off")

    def chime_enabled(self):
        return self.state.get("chime", True)

    def set_chime(self, on):
        self.state["chime"] = on
        save_state(self.state)

    def set_corner(self, corner):
        self.state["corner"] = corner
        save_state(self.state)
        for p in self.popups.values():
            p.relayout()

    def set_popup_width(self, width):
        self.state["width"] = width
        save_state(self.state)
        for p in self.popups.values():
            p.relayout()

    def show_camera_from_flyout(self, cam):
        self.flyout.hide()
        self.toggle_camera(cam)

    def preview_popup(self):
        """Show what an alert looks like (chime included), then close it again."""
        self.flyout.hide()
        cam = self.cameras[0]
        self.show_popup(cam, pinned=True)
        self._chime()
        QTimer.singleShot(8000, lambda: self.popups[cam.name].isVisible()
                          and self.popups[cam.name].pinned and self.dismiss(cam))

    def open_frigate(self):
        self.flyout.hide()
        os.startfile(self.settings["frigate_url"])

    def thumbnail(self, cam):
        """A small QImage of the camera's newest frame (called off the UI thread)."""
        if self.hub_mode:
            data = self.feed.fetch(f"/api/cameras/{cam.id}/live.jpg", timeout=3)
            img = QImage.fromData(data) if data else None
            return img if img is not None and not img.isNull() else None
        frame = cam.latest
        if frame is None:
            return None
        arr = frame.reformat(width=320, height=180, format="rgb24").to_ndarray()
        return QImage(arr.data, 320, 180, arr.strides[0], QImage.Format_RGB888).copy()

    def _chime(self):
        if self.chime_enabled() and self.settings.get("sound"):
            winsound.PlaySound(self.settings["sound"], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def dismiss(self, cam):
        self.popups[cam.name].close_popup()
        # A person still in frame shouldn't immediately re-pop what was just closed
        cam.dismissed = not cam.person_gone()
        log.info("%s: popup dismissed", cam.name)

    def set_detection(self, enabled, save=True):
        """Person alerts on/off. Off stops inference entirely; cameras can still be opened by hand.
        In hub mode this is the hub's shared switch (the projector follows it too)."""
        self.detection = enabled
        if self.hub_mode:
            if save:
                self.feed.set_alerts(enabled)
                log.info("person alerts %s (hub)", "on" if enabled else "off")
            if not enabled:
                for cam in self.cameras:
                    popup = self.popups[cam.name]
                    if popup.isVisible() and not popup.pinned:
                        popup.close_popup()
            return
        for cam in self.cameras:
            cam.detect_enabled = enabled
            if not enabled:
                cam.person = False
                cam.reset()
                popup = self.popups[cam.name]
                if popup.isVisible() and not popup.pinned:
                    popup.close_popup()
        if save:
            self.state["detection"] = enabled
            save_state(self.state)
            log.info("person alerts %s", "on" if enabled else "off")

    def toggle_pause(self):
        if time.time() < self.paused_until:
            self.paused_until = 0.0
            log.info("resumed")
        else:
            self.paused_until = time.time() + 3600
            for cam in self.cameras:
                self.popups[cam.name].close_popup()
            log.info("paused for 1 hour")

    def _drain_hub(self):
        """Apply queued hub events (state, person_detected/gone, alerts, camera_status)."""
        by_id = {c.id: c for c in self.cameras}
        while True:
            try:
                ev = self.feed.events.get_nowait()
            except queue.Empty:
                return
            kind = ev.get("type")
            cam = by_id.get(ev.get("camera"))
            if kind == "state":
                self.detection = ev["alerts"]["enabled"]
                for info in ev["cameras"]:
                    c = by_id.get(info["id"])
                    if c:
                        c.connected, c.alerts = info["connected"], info["alerts"]
                        if c.person and not info["person"]:
                            c.person = False  # missed person_gone while disconnected
            elif kind == "person_detected" and cam:
                cam.person, cam.last_person_time, cam.triggered = True, time.time(), True
                cam.snapshot = ev.get("snapshot")
                log.info("%s: person detected by hub (conf %.2f)", cam.name, ev.get("confidence", 0))
            elif kind == "person_gone" and cam:
                cam.person, cam.last_person_time = False, time.time()
            elif kind == "camera_status" and cam:
                cam.connected = ev["connected"]
            elif kind == "alerts":
                self.detection = ev["enabled"]
                for cid, on in ev.get("cameras", {}).items():
                    if cid in by_id:
                        by_id[cid].alerts = on
                if not self.detection:
                    self.set_detection(False, save=False)
            self.detection_action.setChecked(self.detection)
            for cid, act in self.camera_alert_actions.items():
                act.setChecked(by_id[cid].alerts)

    def tick(self):
        if self.hub_mode:
            self._drain_hub()
        paused = time.time() < self.paused_until
        self.pause_action.setText("Resume" if paused else "Pause for 1 hour")
        self.pause_action.setEnabled(self.detection)
        alerts_off = paused or not self.detection
        if alerts_off != self.icon_paused:
            self.icon_paused = alerts_off
            self.tray.setIcon(_tray_icon(alerts_off))
        states = []
        for cam in self.cameras:
            popup = self.popups[cam.name]
            states.append(f"{cam.name}: {'connected' if cam.connected else 'offline'}")
            if cam.person_gone():
                if popup.isVisible() and not popup.pinned and not popup.closing:
                    popup.close_popup()
                    log.info("%s: person gone, popup hidden", cam.name)
                cam.dismissed = False
                if cam.triggered:
                    cam.reset()
            if (cam.triggered and self.detection and self.camera_alerts(cam) and not cam.dismissed
                    and not paused and not popup.isVisible()):
                self.show_popup(cam)
            if isinstance(cam, HubCamera) and cam.streaming and not popup.isVisible():
                cam.close_stream()  # no popup, no decoding
            self.camera_actions[cam.name].setChecked(popup.isVisible() and not popup.closing)
        # Keep stacked popups packed against the corner as others close (oldest nearest the corner)
        up = sorted((p for p in self.popups.values() if p.isVisible() and not p.closing), key=lambda p: p.opened_at)
        for slot, p in enumerate(up):
            p.set_slot(slot)
        if self.flyout.isVisible():
            self.flyout.sync()
        mode = " (alerts off)" if not self.detection else " (paused)" if paused else ""
        if self.hub_mode:
            source = "hub" + ("" if self.feed.connected else " unreachable, retrying")
        else:
            source = "standalone (local detection)"
        self.tray.setToolTip(f"Lookout{mode} - {source}\n" + "\n".join(states))


STATE_FILE = DATA_DIR / "state.json"


def load_state():
    """UI state remembered across runs (which corner/screen the popup was last dropped in)."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log.warning("could not save state: %s", e)


def load_settings(path):
    settings = dict(DEFAULTS)
    # utf-8-sig: tolerate a BOM (Windows PowerShell 5.1 and Notepad add one)
    settings.update(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    if not settings["cameras"]:
        raise SystemExit(f"No cameras configured in {path}")
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(DATA_DIR / "lookout.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout if sys.stdout else open(os.devnull, "w"))])

    threading.excepthook = lambda a: log.error("thread %s crashed", a.thread.name,
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(DATA_DIR / "lookout.lock"))
    if not lock.tryLock(100):
        log.info("another instance is already running")
        return 0

    settings = load_settings(args.config)
    log.info("starting with %d camera(s): %s", len(settings["cameras"]), ", ".join(c["name"] for c in settings["cameras"]))
    _app = App(settings)  # noqa: F841 - keep a reference so Qt objects stay alive
    code = app.exec()
    for cam in _app.cameras:
        cam.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
