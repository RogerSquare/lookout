"""Windows 11-style flyout for the Lookout tray icon (left-click), like the Quick Settings and volume
flyouts: camera tiles with live thumbnails and per-camera alert switches, the shared Person alerts
switch, chime, pause, and popup position/size/preview. Mirrors the projector app's home screen.

The window is drawn by DWM with native rounded corners and shadow (see lookout._native_window_style);
everything inside is painted with Segoe UI Variable / Segoe Fluent Icons and the user's accent color.
"""

import queue
import threading
import winreg

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

BG = QColor(32, 32, 32)
CARD = QColor(43, 43, 43)
CARD_HOVER = QColor(50, 50, 50)
STROKE = QColor(255, 255, 255, 20)
TEXT = QColor(255, 255, 255)
TEXT_2 = QColor(255, 255, 255, 155)
RED = QColor(255, 99, 97)
CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]
SIZES = [("S", 360), ("M", 480), ("L", 640), ("XL", 800)]


def accent_color():
    """The Windows accent color (DWM AccentColor is ABGR), or Windows' default blue."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as k:
            abgr = winreg.QueryValueEx(k, "AccentColor")[0]
        c = QColor(abgr & 0xFF, (abgr >> 8) & 0xFF, (abgr >> 16) & 0xFF)
        # Windows lightens the accent for controls on dark surfaces
        return c.lighter(135) if c.lightness() < 110 else c
    except OSError:
        return QColor(76, 194, 255)


def font(size, weight="Text", bold=False):
    f = QFont(f"Segoe UI Variable {weight}" + (" Semibold" if bold else ""))
    f.setPixelSize(size)
    return f


def label(text, size=13, color=TEXT, bold=False, weight="Text"):
    w = QLabel(text)
    w.setFont(font(size, weight, bold))
    w.setStyleSheet(f"color: rgba({color.red()},{color.green()},{color.blue()},{color.alpha()}); background: transparent;")
    return w


class Switch(QWidget):
    """Windows 11 toggle switch (40x20): accent pill when on, outline when off, sliding knob."""

    def __init__(self, checked, on_toggle, accent):
        super().__init__()
        self.checked = checked
        self.on_toggle = on_toggle
        self.accent = accent
        self.setFixedSize(40, 20)
        self.setCursor(Qt.PointingHandCursor)
        self.pos_ = 1.0 if checked else 0.0
        self.anim = QVariantAnimation(self, duration=140, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._moved)

    def _moved(self, v):
        self.pos_ = v
        self.update()

    def set_checked(self, checked, animate=True):
        if checked == self.checked:
            return
        self.checked = checked
        if animate:
            self.anim.stop()
            self.anim.setStartValue(self.pos_)
            self.anim.setEndValue(1.0 if checked else 0.0)
            self.anim.start()
        else:
            self.pos_ = 1.0 if checked else 0.0
            self.update()

    def mouseReleaseEvent(self, e):
        if self.rect().contains(e.position().toPoint()):
            self.set_checked(not self.checked)
            self.on_toggle(self.checked)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self.checked:
            p.setPen(Qt.NoPen)
            p.setBrush(self.accent)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 140), 1))
            p.setBrush(QColor(0, 0, 0, 40))
        p.drawRoundedRect(r, 10, 10)
        knob = 12 if self.checked else 10
        x = 10 + self.pos_ * 20
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0) if self.checked else QColor(255, 255, 255, 200))
        p.drawEllipse(QPointF(x, 10), knob / 2, knob / 2)


class Segmented(QWidget):
    """Row of options; the selected one is filled with the accent color."""

    def __init__(self, options, index, on_pick, accent, icon_font=False):
        super().__init__()
        self.options, self.index, self.on_pick, self.accent = options, index, on_pick, accent
        self.icon_font = icon_font
        self.hover = -1
        self.setFixedHeight(32)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _cell(self, i):
        w = self.width() / len(self.options)
        return QRectF(i * w + 2, 2, w - 4, self.height() - 4)

    def mouseMoveEvent(self, e):
        i = int(e.position().x() // (self.width() / len(self.options)))
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e):
        self.hover = -1
        self.update()

    def mouseReleaseEvent(self, e):
        i = int(e.position().x() // (self.width() / len(self.options)))
        if 0 <= i < len(self.options):
            self.index = i
            self.update()
            self.on_pick(i)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(STROKE, 1))
        p.setBrush(QColor(255, 255, 255, 10))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        for i, opt in enumerate(self.options):
            c = self._cell(i)
            if i == self.index:
                p.setPen(Qt.NoPen)
                p.setBrush(self.accent)
                p.drawRoundedRect(c, 4, 4)
                p.setPen(QColor(0, 0, 0))
            else:
                if i == self.hover:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 255, 255, 18))
                    p.drawRoundedRect(c, 4, 4)
                p.setPen(TEXT)
            if opt in CORNERS:
                self._corner_icon(p, c, opt, selected=i == self.index)
                continue
            p.setFont(QFont("Segoe Fluent Icons", 9) if self.icon_font else font(12, bold=i == self.index))
            p.drawText(c, Qt.AlignCenter, opt)

    @staticmethod
    def _corner_icon(p, cell, corner, selected):
        """A little screen outline with the popup's corner filled in."""
        w, h = 22, 14
        scr = QRectF(cell.center().x() - w / 2, cell.center().y() - h / 2, w, h)
        ink = QColor(0, 0, 0) if selected else QColor(255, 255, 255, 210)
        p.setPen(QPen(ink, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(scr, 2.5, 2.5)
        bw, bh = 8, 5
        x = scr.left() + 2.5 if "left" in corner else scr.right() - 2.5 - bw
        y = scr.top() + 2.5 if "top" in corner else scr.bottom() - 2.5 - bh
        p.setPen(Qt.NoPen)
        p.setBrush(ink)
        p.drawRoundedRect(QRectF(x, y, bw, bh), 1.2, 1.2)


class Button(QWidget):
    """Flat Fluent button: icon glyph + text; subtle fill on hover."""

    def __init__(self, glyph, text, on_click):
        super().__init__()
        self.glyph, self.text, self.on_click = glyph, text, on_click
        self.hover = False
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if self.rect().contains(e.position().toPoint()):
            self.on_click()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(STROKE, 1))
        p.setBrush(QColor(255, 255, 255, 22 if self.hover else 12))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        p.setPen(TEXT)
        p.setFont(QFont("Segoe Fluent Icons", 9))
        p.drawText(QRectF(12, 0, 16, self.height()), Qt.AlignCenter, self.glyph)
        p.setFont(font(12))
        p.drawText(QRectF(34, 0, self.width() - 40, self.height()), Qt.AlignVCenter, self.text)


class CameraTile(QWidget):
    """Live thumbnail (click to show that camera), name with a red dot while someone is there,
    and the camera's alert switch."""

    W, H = 160, 90

    def __init__(self, cam, alerts_on, on_show, on_alerts, accent):
        super().__init__()
        self.cam = cam
        self.on_show = on_show
        self.image = None
        self.hover = False
        self.setFixedWidth(self.W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.thumb = QWidget()
        self.thumb.setFixedSize(self.W, self.H)
        self.thumb.paintEvent = self._paint_thumb
        self.thumb.setCursor(Qt.PointingHandCursor)
        self.thumb.mouseReleaseEvent = lambda e: self.on_show(self.cam)
        self.thumb.enterEvent = lambda e: self._set_hover(True)
        self.thumb.leaveEvent = lambda e: self._set_hover(False)
        lay.addWidget(self.thumb)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.name = label(cam.name, 13, bold=True)
        row.addWidget(self.name, 1)
        self.switch = Switch(alerts_on, lambda on: on_alerts(self.cam, on), accent)
        self.switch.setToolTip("Alerts from this camera")
        row.addWidget(self.switch)
        lay.addLayout(row)

    def _set_hover(self, on):
        self.hover = on
        self.thumb.update()

    def _paint_thumb(self, _):
        p = QPainter(self.thumb)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.W, self.H), 6, 6)
        p.setClipPath(path)
        p.fillRect(0, 0, self.W, self.H, CARD)
        if self.image is not None:
            img = self.image.scaled(self.W, self.H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawImage(QPointF((self.W - img.width()) / 2, (self.H - img.height()) / 2), img)
        else:
            p.setPen(TEXT_2)
            p.setFont(font(11))
            p.drawText(QRectF(0, 0, self.W, self.H), Qt.AlignCenter,
                       "Offline" if not self.cam.connected else "Loading…")
        if self.cam.person:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(QRectF(6, 6, 64, 18), 9, 9)
            p.setBrush(RED)
            p.drawEllipse(QPointF(15, 15), 3.5, 3.5)
            p.setPen(TEXT)
            p.setFont(font(10, bold=True))
            p.drawText(QRectF(22, 6, 46, 18), Qt.AlignVCenter, "Person")
        if self.hover:
            p.fillRect(0, 0, self.W, self.H, QColor(255, 255, 255, 25))
            p.setPen(TEXT)
            p.setFont(QFont("Segoe Fluent Icons", 16))
            p.drawText(QRectF(0, 0, self.W, self.H), Qt.AlignCenter, "")  # Play
        p.setClipping(False)
        p.setPen(QPen(STROKE, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.W - 1, self.H - 1), 6, 6)


class Flyout(QWidget):
    """The tray flyout. `app` provides the state and actions (see lookout.App)."""

    WIDTH = 380

    def __init__(self, app, style_window):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        self.app = app
        self.style_window = style_window
        self.accent = accent_color()
        self.thumbs = queue.Queue()
        self.setFixedWidth(self.WIDTH)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), BG)
        self.setPalette(pal)
        self.refresh_timer = QTimer(self, interval=1500, timeout=self._refresh)
        self.ui_timer = QTimer(self, interval=150, timeout=self._apply_thumbs)
        self._build()

    # ---- layout ---------------------------------------------------------------------------

    def _build(self):
        a = self.app
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(label("Lookout", 15, bold=True, weight="Display"), 1)
        self.status = label("", 11, TEXT_2)
        head.addWidget(self.status)
        root.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        self.tiles = []
        for i, cam in enumerate(a.cameras):
            t = CameraTile(cam, a.camera_alerts(cam), a.show_camera_from_flyout, a.set_camera_alerts, self.accent)
            grid.addWidget(t, i // 2, i % 2)
            self.tiles.append(t)
        root.addLayout(grid)

        root.addWidget(self._divider())
        self.sw_alerts = self._switch_row(root, "", "Person alerts", a.detection, a.set_detection)
        self.sw_chime = self._switch_row(root, "", "Chime", a.chime_enabled(), a.set_chime)
        self.pause_btn = Button("", "", a.toggle_pause)
        root.addWidget(self.pause_btn)

        root.addWidget(self._divider())
        root.addWidget(label("Popup position", 12, TEXT_2))
        self.pos_seg = Segmented(CORNERS,  # drawn as little screens with the corner filled
                                 CORNERS.index(a.state.get("corner", "bottom-right")),
                                 lambda i: a.set_corner(CORNERS[i]), self.accent)
        root.addWidget(self.pos_seg)
        root.addWidget(label("Popup size", 12, TEXT_2))
        width = a.state.get("width", a.settings["popup_width"])
        idx = min(range(len(SIZES)), key=lambda i: abs(SIZES[i][1] - width))
        self.size_seg = Segmented([s for s, _ in SIZES], idx, lambda i: a.set_popup_width(SIZES[i][1]), self.accent)
        root.addWidget(self.size_seg)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addWidget(Button("", "Preview", a.preview_popup))
        if a.settings.get("frigate_url"):
            foot.addWidget(Button("", "Open Frigate", a.open_frigate))
        root.addLayout(foot)

    def _divider(self):
        d = QWidget()
        d.setFixedHeight(1)
        d.setStyleSheet("background: rgba(255,255,255,20);")
        return d

    def _switch_row(self, parent, glyph, text, checked, on_toggle):
        row = QHBoxLayout()
        icon = QLabel(glyph)
        icon.setFont(QFont("Segoe Fluent Icons", 11))
        icon.setStyleSheet("color: white; background: transparent;")
        icon.setFixedWidth(22)
        row.addWidget(icon)
        row.addWidget(label(text, 13), 1)
        sw = Switch(checked, on_toggle, self.accent)
        row.addWidget(sw)
        parent.addLayout(row)
        return sw

    # ---- live state -----------------------------------------------------------------------

    def sync(self):
        """Pull the current state into the controls (also called while open, on hub events)."""
        a = self.app
        self.sw_alerts.set_checked(a.detection)
        self.sw_chime.set_checked(a.chime_enabled())
        for t in self.tiles:
            t.switch.set_checked(a.camera_alerts(t.cam))
            t.thumb.update()
        self.pause_btn.text = "Resume alerts" if a.paused else "Pause alerts for 1 hour"
        self.pause_btn.glyph = "" if a.paused else ""
        self.pause_btn.update()
        hub = ("Hub connected" if a.feed.connected else "Hub unreachable") if a.hub_mode else "Standalone"
        self.status.setText(hub)

    def _refresh(self):
        for t in self.tiles:
            threading.Thread(target=self._fetch_thumb, args=(t,), daemon=True).start()
        self.sync()

    def _fetch_thumb(self, tile):
        img = self.app.thumbnail(tile.cam)
        if img is not None:
            self.thumbs.put((tile, img))

    def _apply_thumbs(self):
        while True:
            try:
                tile, img = self.thumbs.get_nowait()
            except queue.Empty:
                return
            tile.image = img
            tile.thumb.update()

    # ---- show / hide ----------------------------------------------------------------------

    def toggle(self):
        if self.isVisible():
            self.hide()
            return
        self.sync()
        self.adjustSize()
        area = QApplication.primaryScreen().availableGeometry()
        # Bottom-right, above the taskbar, like Windows' own flyouts
        self.move(area.right() - self.width() - 12 + 1, area.bottom() - self.height() - 12 + 1)
        self.show()
        self.raise_()
        self.activateWindow()
        self._refresh()
        self.refresh_timer.start()
        self.ui_timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        self.style_window(int(self.winId()))

    def hideEvent(self, e):
        self.refresh_timer.stop()
        self.ui_timer.stop()
        super().hideEvent(e)
