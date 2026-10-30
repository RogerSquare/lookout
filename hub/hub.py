"""Lookout Hub - always-on person detection that broadcasts alerts to the Lookout clients.

Runs headless on an always-on box (quad-box). Each camera is decoded on its own thread and
checked for people with YOLO11s (ONNX Runtime, CPU) a few times a second, using the same
trigger rules as the desktop app: 2 positive checks out of 3 raise `person_detected`, and
`person_gone` follows once nobody has been seen for `hide_after_seconds`.

Clients (desktop tray app, projector app) subscribe to GET /events (Server-Sent Events); the
first event on every connection is the full current `state`. The alerts switch is shared:

    GET  /events                          SSE stream: state, person_detected, person_gone,
                                          alerts, camera_status
    GET  /api/state                       same payload as the `state` event
    POST /api/alerts                      {"enabled": bool[, "camera": id]}
    POST /api/test                        {"camera": id, "seconds": n} fake alert, for client testing
    GET  /api/cameras/{id}/snapshot.jpg   frame that raised the latest detection (or live)
    GET  /api/cameras/{id}/live.jpg       newest frame
    GET  /healthz

Watch / iPhone app (token-protected when `watch_token` is set: `Authorization: Bearer <token>`
or `?token=<token>`, since AVPlayer can't add headers to HLS requests):

    GET  /api/watch/cameras                    cameras with person state, last seen, frame + HLS urls
    POST /api/watch/alerts                     the shared alerts switch, as POST /api/alerts
    POST /api/watch/devices                    register an app's push token for person alerts
    GET  /api/watch/cameras/{id}/frame.jpg     newest frame, ?w= scales it down (watch is ~400 px)
    GET  /api/watch/cameras/{id}/snapshot.jpg  the frame that raised the latest alert, ?w= scaled
    GET  /api/watch/cameras/{id}/stream.m3u8   go2rtc HLS for the camera, proxied so one host,
                                               port and token cover everything; ?v=phone picks a
                                               variant from the camera's hls_variants
    GET  /api/watch/cameras/{id}/live.mjpeg    newest frames as multipart JPEG, self-pacing
                                               (?w=480&fps=10): live view without a video player
"""

import asyncio
import collections
import hmac
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import av
import numpy as np
import onnxruntime as ort
from aiohttp import web
from PIL import Image

import detect
from auth import CSRF_HEADER, SESSION_COOKIE, SESSION_DAYS, Auth
from detect import NO_BOXES
from frigate import Frigate, FrigateError
from pairing import Pairing
from store import ConfigError, Store, mask_url
from streams import Go2rtc, lan_ip, resolve

CONFIG_FILE = Path(os.environ.get("LOOKOUT_CONFIG", "/config/config.json"))  # legacy; imported once
WEB = Path(__file__).resolve().parent / "web"
VERSION = os.environ.get("LOOKOUT_VERSION", "dev")
DATA_DIR = Path(os.environ.get("LOOKOUT_DATA", "/data"))

DEFAULTS = {
    "cameras": [],
    "model": "auto",            # "auto": picked by the hardware profile; or a model name / .onnx path
    "models_dir": "/models",
    "model_families": ["rfdetr"],   # families "auto" may pick (yolo is AGPL: personal installs only)
    "detect_power": "balanced",     # power | balanced | accuracy: how much CPU/GPU detection may use
    "threads": "auto",          # ONNX Runtime threads for detection; "auto": a quarter of the CPUs (2-8)
    "detect_fps": 5,
    "confidence": 0.5,
    "trigger_hits": 2,          # positives needed...
    "trigger_window": 3,        # ...within this many consecutive checks
    "hide_after_seconds": 15,
    "host": "0.0.0.0",
    "port": 8765,
    "gpu_helper": None,         # e.g. "http://10.0.0.250:8770" (desktop Lookout app's GPU helper)
    "gpu_helper_timeout_ms": 400,
    "go2rtc_api": "http://127.0.0.1:1985",  # go2rtc's HTTP API, source of the watch HLS streams
    "watch_token": None,        # required on /api/watch/* when set (the store's app token)
    "go2rtc": "embedded",       # "embedded": the hub runs go2rtc for cameras with a `url`; "off"
    "go2rtc_rtsp_port": 8555,
    "go2rtc_api_port": 1985,
    "public_host": None,        # address clients use for RTSP restreams; default: this machine's LAN IP
    "https_port": 8766,         # the app API over TLS (self-signed; apps pin it at pairing)
    "hub_name": "Lookout Hub",
    "remote_url": None,         # how apps reach the hub away from home, e.g. https://lookout.example.com
    "allow_legacy_token": True, # accept the old shared app token (turn off once every app is paired)
    # Apple push for person alerts: {"key_file": "/secrets/apns-XXXX.p8", "key_id": "XXXX",
    # "team_id": "..."}; "url" overrides the APNs host (tests)
    "apns": None,
}

log = logging.getLogger("lookout-hub")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _ExternalDetector:
    """Stands in for the detector while another system (Frigate) does the detecting."""
    name = "frigate"
    ms = None

    def boxes(self, frame):
        return NO_BOXES


class GpuHelper:
    """Asks the desktop PC's GPU helper (desktop Lookout app, POST /detect) for the boxes, and
    falls back to the local CPU detector for any frame it can't answer in time. After a failure
    the helper is left alone for BACKOFF_S, so an asleep or switched-off PC costs nothing."""

    BACKOFF_S = 15

    def __init__(self, url, timeout_s, fallback, on_change):
        self.url = url.rstrip("/") + "/detect"
        self.timeout_s = timeout_s
        self.fallback = fallback
        self.on_change = on_change    # called with the new detector name when it switches
        self.retry_at = 0.0
        self.name = fallback.name
        self.lock = threading.Lock()

    def _set(self, name, why=""):
        with self.lock:
            changed = name != self.name
            self.name = name
        if changed:
            log.info("detector: %s%s", name, f" ({why})" if why else "")
            self.on_change(name)

    def boxes(self, frame):
        if time.time() >= self.retry_at:
            try:
                req = urllib.request.Request(self.url, data=_jpeg(frame, quality=85),
                                             headers={"Content-Type": "image/jpeg"})
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    result = json.loads(resp.read())
                self._set("pc-gpu")
                b = np.asarray(result["boxes"], dtype=np.float32)
                return b.reshape(-1, 5) if b.size else NO_BOXES
            except (OSError, ValueError, KeyError) as e:  # refused, timeout, bad reply
                self.retry_at = time.time() + self.BACKOFF_S
                self._set(self.fallback.name, f"GPU helper unavailable: {e}")
        return self.fallback.boxes(frame)


def person_score(boxes, min_height=0.0, zone=None):
    """Best person score, counting only people big enough and standing inside the zone.

    min_height is the smallest box height as a fraction of the frame (distant passers-by are
    small); zone is an optional polygon of [x, y] fractions that a person's feet must be inside.
    """
    if not len(boxes):
        return 0.0
    ok = boxes[:, 3] >= min_height
    if zone:
        feet = zip(boxes[:, 0], boxes[:, 1] + boxes[:, 3] / 2)  # bottom-centre of each box
        ok &= np.array([_inside(x, y, zone) for x, y in feet], dtype=bool)
    return float(boxes[ok, 4].max()) if ok.any() else 0.0


def _inside(x, y, poly):
    """Ray-casting point-in-polygon test (poly = [[x, y], ...] in frame fractions)."""
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


class Camera:
    """Decodes one stream on a background thread and runs person detection on another."""

    def __init__(self, cfg, settings, detector, hub):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.role = cfg.get("role", "other")  # "door" cameras get "Someone's at the door" alerts
        self.url = cfg.get("detect_url") or cfg["url"]   # no go2rtc: read the camera directly
        self.min_height = float(cfg.get("min_person_height", 0.0))  # fraction of frame height
        self.zone = cfg.get("zone")  # optional [[x, y], ...] polygon in frame fractions
        self.streams = cfg.get("streams", {})
        # go2rtc stream for HLS: explicit, or the path of the sd/hd restream URL ("kitchen_sd")
        self.hls_src = cfg.get("hls_src") or _stream_name(self.streams.get("sd") or self.streams.get("hd"))
        # Other go2rtc streams picked with stream.m3u8?v=<name>, e.g. {"phone": "kitchen_phone"}
        self.hls_variants = cfg.get("hls_variants", {})
        self.loop = cfg.get("loop", False)  # for test sources (image/video files)
        self.settings = settings
        self.detector = detector
        self.hub = hub

        self.latest = None          # newest av.VideoFrame
        self.latest_time = 0.0
        self.connected = False
        self.person = False         # alert currently raised
        self.confidence = 0.0
        self.since = None           # ISO time the current alert started
        self.last_person_time = 0.0
        self.last_seen = None       # ISO time a person was last seen (survives person_gone)
        self.test_until = 0.0       # a POST /api/test alert counts as "person" until then
        self.snapshot = None       # JPEG bytes of the frame that raised the latest alert
        self.hits = collections.deque(maxlen=settings["trigger_window"])
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._read_loop, name=f"read-{self.id}", daemon=True).start()
        threading.Thread(target=self._detect_loop, name=f"detect-{self.id}", daemon=True).start()

    def stop(self):
        self._stop.set()

    def info(self):
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "connected": self.connected,
            "person": self.person,
            "confidence": round(self.confidence, 3),
            "since": self.since,
            "alerts": self.hub.camera_alerts(self.id),
            "streams": self.streams,
            "snapshot": f"/api/cameras/{self.id}/snapshot.jpg",
        }

    def watch_info(self):
        base = f"/api/watch/cameras/{self.id}"
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "connected": self.connected,
            "person": self.person or time.time() < self.test_until,
            "since": self.since,
            "last_seen": self.last_seen,
            "alerts": self.hub.camera_alerts(self.id),
            "frame": f"{base}/frame.jpg",
            "hls": f"{base}/stream.m3u8" if self.hls_src else None,
            "mjpeg": f"{base}/live.mjpeg",
        }

    def _set_connected(self, connected):
        if connected != self.connected:
            self.connected = connected
            self.hub.publish_threadsafe({"type": "camera_status", "camera": self.id, "connected": connected})

    def _read_loop(self):
        backoff = 1   # a camera added with go2rtc restarting gets refused once; retry soon
        while not self._stop.is_set():
            try:
                # Low-latency options for live streams only; they make file sources decode nothing
                options = ({"rtsp_transport": "tcp", "fflags": "nobuffer", "flags": "low_delay"}
                           if self.url.startswith("rtsp") else {})
                with av.open(self.url, options=options, timeout=(10, 10)) as container:
                    stream = container.streams.video[0]
                    stream.thread_type = "AUTO"
                    if not self.connected:
                        log.info("%s: connected (%dx%d)", self.id, stream.codec_context.width, stream.codec_context.height)
                    self._set_connected(True)
                    backoff = 2
                    for frame in container.decode(stream):
                        if self._stop.is_set():
                            return
                        self.latest = frame
                        self.latest_time = time.time()
                        if self.loop:
                            time.sleep(1 / 15)
                if self.loop:
                    self._stop.wait(1 / 15)
                    continue
                log.warning("%s: stream ended", self.id)
            except Exception as e:  # network drops, camera reboots, bad URL
                log.warning("%s: stream error: %s", self.id, e)
            self._set_connected(False)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _detect_loop(self):
        while not self._stop.is_set():
            interval = 1 / self.hub.detect_fps     # the hub may lower it when detection falls behind
            started = time.time()
            if not self.hub.camera_alerts(self.id):
                # Alerts off: no inference at all, and drop any raised alert so clients hide it
                if self.person:
                    self._raise_gone(reason="alerts_off")
                self.hits.clear()
                self._stop.wait(0.5)
                continue
            if self.hub.frigate is not None:
                # Frigate decides when someone is there (Hub.on_frigate_review)
                self.hits.clear()
                self._stop.wait(1)
                continue
            frame = self.latest
            if frame is not None and self.connected and started - self.latest_time < 5:
                try:
                    score = person_score(self.hub.detector.boxes(frame), self.min_height, self.zone)
                except Exception:
                    log.exception("%s: detection failed", self.id)
                    self._stop.wait(5)
                    continue
                found = score >= self.settings["confidence"]
                self.hits.append(found)
                if found:
                    self.last_person_time = time.time()
                    self.last_seen = _now_iso()
                    self.confidence = max(self.confidence, score) if self.person else score
                if not self.person and sum(self.hits) >= self.settings["trigger_hits"]:
                    self._raise_detected(frame, score)
            if self.person and time.time() - self.last_person_time > self.settings["hide_after_seconds"]:
                self._raise_gone(reason="left")
            self._stop.wait(max(0.0, interval - (time.time() - started)))

    def _raise_detected(self, frame, score):
        self.snapshot = _jpeg(frame)
        self.person = True
        self.confidence = score
        self.since = _now_iso()
        log.info("%s: person detected (conf %.2f)", self.id, score)
        self.hub.publish_threadsafe({
            "type": "person_detected", "camera": self.id, "name": self.name, "role": self.role,
            "confidence": round(score, 3), "since": self.since, "streams": self.streams,
            "snapshot": f"/api/cameras/{self.id}/snapshot.jpg?t={int(time.time() * 1000)}",
        })

    def raise_from(self, snapshot, score=1.0, source="frigate"):
        """A person alert decided elsewhere (Frigate), with its snapshot JPEG."""
        if snapshot:
            self.snapshot = snapshot
        self.person = True
        self.confidence = score
        self.since = _now_iso()
        self.last_seen = self.since
        self.last_person_time = time.time()
        log.info("%s: person detected (%s)", self.id, source)
        self.hub.publish_threadsafe({
            "type": "person_detected", "camera": self.id, "name": self.name, "role": self.role,
            "confidence": round(score, 3), "since": self.since, "streams": self.streams, "source": source,
            "snapshot": f"/api/cameras/{self.id}/snapshot.jpg?t={int(time.time() * 1000)}",
        })

    def _raise_gone(self, reason):
        duration = round(time.time() - datetime.fromisoformat(self.since).timestamp(), 1) if self.since else 0
        self.person = False
        self.since = None
        self.hits.clear()
        log.info("%s: person gone (%s, %.0fs)", self.id, reason, duration)
        self.hub.publish_threadsafe({"type": "person_gone", "camera": self.id, "reason": reason,
                                     "duration": duration})


def _jpeg(frame, quality=85, width=None):
    if width and width < frame.width:
        frame = frame.reformat(width=width, height=max(2, round(frame.height * width / frame.width)))
    buf = io.BytesIO()
    Image.fromarray(frame.to_ndarray(format="rgb24")).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _stream_name(url):
    """go2rtc stream name from a restream URL: rtsp://host:8555/kitchen_sd -> kitchen_sd."""
    return urlsplit(url).path.strip("/") or None if url else None


def _with_token(playlist, token):
    """Adds ?token= to every URI in an HLS playlist, so AVPlayer's follow-up requests pass auth."""
    def add(uri):
        return f"{uri}{'&' if '?' in uri else '?'}token={token}"
    out = []
    for line in playlist.splitlines():
        if line and not line.startswith("#"):
            line = add(line)
        elif 'URI="' in line:
            head, rest = line.split('URI="', 1)
            uri, tail = rest.split('"', 1)
            line = f'{head}URI="{add(uri)}"{tail}'
        out.append(line)
    return "\n".join(out) + "\n"


class Push:
    """Sends person alerts to the registered watch/iPhone apps through Apple Push (APNs).

    Devices register their push token with POST /api/watch/devices; they're kept in
    data/devices.json. A token Apple reports as gone (410, BadDeviceToken) is dropped."""

    HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}

    def __init__(self, cfg, path):
        self.cfg = cfg
        self.path = path
        self.devices = self._load()
        self._jwt = (None, 0.0)
        self._client = None

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.devices, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save push devices: %s", e)

    def register(self, token, topic, env, device_id=None):
        known = self.devices.get(token)
        self.devices[token] = {"topic": topic, "env": env, "device_id": device_id or (known or {}).get("device_id"),
                               "added": known["added"] if known else _now_iso(), "seen": _now_iso()}
        self._save()
        if not known:
            log.info("push: registered %s device %s...", topic, token[:8])

    def forget_device(self, device_id):
        """A removed device gets no more alerts."""
        gone = [t for t, d in self.devices.items() if d.get("device_id") == device_id]
        for t in gone:
            self.devices.pop(t, None)
        if gone:
            self._save()

    def _token(self):
        import jwt  # PyJWT (ES256 needs cryptography)
        value, made = self._jwt
        if value is None or time.time() - made > 40 * 60:   # Apple wants a new one every <60 min
            key = Path(self.cfg["key_file"]).read_text(encoding="utf-8")
            value = jwt.encode({"iss": self.cfg["team_id"], "iat": int(time.time())}, key,
                               algorithm="ES256", headers={"kid": self.cfg["key_id"]})
            self._jwt = (value, time.time())
        return value

    def _http(self):
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=10)
        return self._client

    async def warm(self):
        """Sign the JWT and open the HTTP/2 connection to APNs ahead of the first alert, so
        that alert doesn't also pay for the TLS handshake (~0.3 s)."""
        try:
            self._token()
            host = self.cfg.get("url") or self.HOSTS["production"]
            await self._http().get(host, timeout=5)    # any response; we only want the connection
        except Exception as e:
            log.info("push: warm-up skipped: %s", e)

    @staticmethod
    def _kind(topic):
        return "watch" if topic.endswith(".watchkitapp") else "phone"

    async def send(self, cam_id, cam_name, test=False, target=None, role="other"):
        """target: "watch", "phone" or "all"; default is the apns "targets" setting (watch).
        Sending to both makes a locked iPhone mirror its copy to the watch: two alerts."""
        target = target or self.cfg.get("targets", "watch")
        if not self.devices:
            return
        self._http()
        payload = {
            "aps": {"alert": {"title": "Someone's at the door" if role == "door" else "Person detected",
                              "subtitle": cam_name,
                              "body": "Test alert" if test else "Tap to see who it is"},
                    "sound": "default", "thread-id": cam_id, "category": "LOOKOUT_PERSON",
                    # Doorbell-style: shown through Focus modes and notification summaries
                    "interruption-level": "time-sensitive", "relevance-score": 1.0},
            "camera": cam_id,
            # The detection frame, for the watch's notification screen (token added by the app)
            "snapshot": f"/api/watch/cameras/{cam_id}/snapshot.jpg?t={int(time.time())}",
        }
        auth = f"bearer {self._token()}"
        for token, dev in list(self.devices.items()):
            if target != "all" and self._kind(dev["topic"]) != target:
                continue
            host = self.cfg.get("url") or self.HOSTS.get(dev["env"], self.HOSTS["production"])
            try:
                r = await self._client.post(f"{host}/3/device/{token}", json=payload, headers={
                    "authorization": auth, "apns-topic": dev["topic"], "apns-push-type": "alert",
                    "apns-priority": "10", "apns-collapse-id": f"person-{cam_id}",
                    "apns-expiration": str(int(time.time()) + 300)})
            except Exception as e:  # network trouble: try again on the next alert
                log.warning("push: %s... failed: %s", token[:8], e)
                continue
            if r.status_code == 200:
                log.info("push: sent %s alert to %s %s...", cam_id, self._kind(dev["topic"]), token[:8])
            else:
                reason = (r.json() if r.content else {}).get("reason", "")
                log.warning("push: %s... refused %s %s", token[:8], r.status_code, reason)
                if r.status_code == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                    self.devices.pop(token, None)
                    self._save()


class Hub:
    """Owns the cameras, the shared alerts switch, and fan-out of events to SSE subscribers."""

    def __init__(self, settings, store=None):
        self.settings = settings
        self.store = store
        self.auth = Auth(store) if store else None
        self.pairing = Pairing(store, DATA_DIR) if store else None
        self.zeroconf = None
        self.state_file = DATA_DIR / "state.json"
        self.alerts = self._load_alerts()
        self.subscribers = set()
        self.next_id = 1
        self.loop = None
        self._local_fps = settings["detect_fps"]   # what the local detector can afford
        self.source = (store.source() if store else None) or {"type": "builtin"}
        self.frigate = None         # Frigate client while it is the source (set at startup)
        self.choice = None          # what the profile picked: {model, backend, ms, fps}
        self._profile = None
        self.local = (_ExternalDetector() if self.source["type"] == "frigate"
                      else self._build_detector(len(settings["cameras"])))
        detector = self.local
        if settings.get("gpu_helper") and self.source["type"] != "frigate":
            # The PC's GPU does the work when it answers; the local detector covers every other frame
            detector = GpuHelper(settings["gpu_helper"], settings["gpu_helper_timeout_ms"] / 1000,
                                 detector, on_change=self._detector_changed)
        self.detector = detector
        self.go2rtc = None
        if settings["go2rtc"] == "embedded":
            g = Go2rtc(DATA_DIR, settings["go2rtc_api_port"], settings["go2rtc_rtsp_port"])
            if g.available:
                self.go2rtc = g
                settings["go2rtc_api"] = f"http://127.0.0.1:{settings['go2rtc_api_port']}"
            else:
                log.warning("go2rtc not found: cameras need a detect_url (or run the hub image)")
        self.public_host = settings["public_host"] or lan_ip()
        self.cameras = {c["id"]: Camera(self._resolve(c), settings, detector, self) for c in settings["cameras"]}
        self.push = Push(settings["apns"], DATA_DIR / "devices.json") if settings.get("apns") else None

    # ---- detector selection --------------------------------------------------------------

    def _build_detector(self, n_cameras):
        """The local detector: from the hardware profile ("auto"), or a named model / path."""
        s = self.settings
        cat = detect.catalog(s["models_dir"])
        model = s["model"]
        if model != "auto":
            # Explicit choice (older hubs: a .onnx path) on the best backend that runs it
            spec = next((dict(v, name=k) for k, v in cat.items() if k == model or v["path"] == model), None)
            if spec is None and str(model).endswith(".onnx"):
                spec = {"name": Path(model).stem, "family": "yolo", "size": "small", "path": model,
                        "resolution": 640, "person_class": 0}
            if spec is None:
                raise SystemExit(f"model {model!r} not found in {s['models_dir']}")
            for backend, providers in detect.backends():
                if backend != "cpu" and detect.benchmark(spec, backend, providers, s["threads"], seconds=0.3) is None:
                    continue                # loads but gets different answers than plain CPU
                try:
                    det = detect.Detector(spec, providers, s["threads"], backend)
                    self.choice = {"model": spec["name"], "size": spec.get("size", "small"), "backend": backend,
                                   "fps": self._local_fps, "auto": False}
                    log.info("detector: %s on %s (set explicitly)", spec["name"], backend)
                    return det
                except Exception as e:
                    log.info("detector: %s on %s failed: %s", spec["name"], backend, str(e)[:100])
            raise SystemExit("no backend could load the model")
        prof = self.store.profile() if self.store else None
        hw = detect.hardware()
        if not prof or prof.get("hardware") != hw or not prof.get("results"):
            log.info("profiling detection on this hardware (%s, %s cpus, %s)", hw["arch"], hw["cpus"],
                     "/".join(hw["backends"]))
            prof = detect.profile(s["models_dir"], tuple(s["model_families"]), s["threads"])
            if self.store:
                self.store.set_profile(prof)
        self._profile = prof
        choice = detect.choose(prof, n_cameras, s["detect_fps"], s["detect_power"])
        if choice is None:
            raise SystemExit(f"no usable person model in {s['models_dir']}")
        spec = dict(cat[choice["model"]], name=choice["model"])
        providers = dict(detect.backends())[choice["backend"]]
        self._local_fps = choice["fps"]
        self.choice = {**choice, "auto": True}
        log.info("detector: %s on %s (%.0f ms), %d checks/s per camera [%s]", choice["model"], choice["backend"],
                 choice["ms"], self._local_fps, s["detect_power"])
        return detect.Detector(spec, providers, s["threads"], choice["backend"])

    @property
    def detect_fps(self):
        """Checks per second per camera: the full rate while the PC's GPU is doing the work,
        otherwise what the local detector can afford."""
        if isinstance(self.detector, GpuHelper) and self.detector.name == "pc-gpu":
            return self.settings["detect_fps"]
        return self._local_fps

    def _swap_local(self, det):
        self.local = det
        if isinstance(self.detector, GpuHelper):
            self.detector.fallback = det
        else:
            self.detector = det
        self.publish_threadsafe({"type": "detector", "detector": self.detector.name, "choice": self.choice})

    def _rebuild_detector(self):
        """Pick again (camera count, power setting or profile changed) and swap it in live."""
        self._swap_local(self._build_detector(len(self.cameras)))

    def watch_load(self):
        """Background: if detection takes more than its budget for a minute, step down - a smaller
        model if the profile has one, else fewer checks per second (never below 1)."""
        def run():
            over = 0
            while True:
                time.sleep(20)
                if self.frigate is not None or self.settings["model"] != "auto" or self.local.ms is None:
                    continue
                if isinstance(self.detector, GpuHelper) and self.detector.name == "pc-gpu":
                    continue                    # the PC is doing the work
                cams = sum(1 for c in self.cameras.values() if c.connected and self.camera_alerts(c.id))
                load = cams * self._local_fps * self.local.ms / 1000
                budget = detect.BUDGET.get(self.settings["detect_power"], 0.55)
                over = over + 1 if load > budget * 1.4 else 0
                if over < 3:
                    continue
                over = 0
                sizes = detect.SIZES
                cur = sizes.index(self.choice.get("size", "nano")) if self.choice.get("size") in sizes else 0
                smaller = [r for r in (self._profile or {}).get("results", [])
                           if r["size"] in sizes and sizes.index(r["size"]) < cur]
                if smaller:
                    pick = max(smaller, key=lambda r: sizes.index(r["size"]))
                    log.warning("detection over budget (%.0f%% of a second): switching to %s", load * 100, pick["model"])
                    spec = dict(detect.catalog(self.settings["models_dir"])[pick["model"]], name=pick["model"])
                    self.choice = {**pick, "fps": self._local_fps, "auto": True, "throttled": True}
                    self._swap_local(detect.Detector(spec, dict(detect.backends())[pick["backend"]],
                                                     self.settings["threads"], pick["backend"]))
                elif self._local_fps > 1:
                    self._local_fps -= 1
                    self.choice = {**self.choice, "fps": self._local_fps, "throttled": True}
                    log.warning("detection over budget (%.0f%% of a second): %d checks/s per camera",
                                load * 100, self._local_fps)
                    self.publish_threadsafe({"type": "detector", "detector": self.detector.name, "choice": self.choice})
        threading.Thread(target=run, name="detector-load", daemon=True).start()

    def detector_info(self):
        cams = sum(1 for c in self.cameras.values() if c.connected and self.camera_alerts(c.id))
        ms = self.local.ms
        return {"active": self.detector.name, "choice": self.choice, "power": self.settings["detect_power"],
                "ms": round(ms, 1) if ms else None, "checks_per_s": self.detect_fps,
                "load": round(cams * self._local_fps * ms / 1000, 2) if ms else None,
                "profile": getattr(self, "_profile", None)}

    # ---- Frigate source ----------------------------------------------------------------

    async def start_frigate(self, cfg, import_cameras=True):
        """Connect to Frigate, import its cameras the hub doesn't have, and follow its reviews."""
        fr = Frigate(cfg, on_review=self.on_frigate_review,
                     on_status=lambda ok: self.publish({"type": "source", "source": "frigate", "connected": ok}))
        try:
            await fr.open()
        except BaseException:
            await fr.close()
            raise
        added = []
        if import_cameras and self.store:
            have = {c["id"] for c in self.store.cameras()}
            for cam in await fr.cameras():
                if cam["id"] not in have and cam["url"]:
                    self.store.put_camera({"id": cam["id"], "name": cam["name"], "url": cam["url"], "role": "other"})
                    added.append(cam["id"])
        fr.start()
        self.frigate = fr
        log.info("frigate %s at %s is the detection source%s", fr.version, fr.url,
                 f" (imported {', '.join(added)})" if added else "")
        return added

    def on_frigate_review(self, kind, review):
        cam = self.cameras.get(review["camera"])
        if cam is None:
            return
        if kind == "start":
            if not self.camera_alerts(cam.id) or cam.person:
                return
            async def raise_it():
                try:
                    snap = await self.frigate.snapshot(review)
                except Exception as e:
                    log.info("frigate: no snapshot for %s: %s", review["id"], e)
                    snap = None
                cam.raise_from(snap, source="frigate")
            asyncio.ensure_future(raise_it())
        elif cam.person:
            cam._raise_gone(reason=review.get("reason", "frigate"))

    async def stop_frigate(self):
        """Stop following Frigate, and close every open alert: whichever source raised it, the
        next source can't end it."""
        if self.frigate is not None:
            fr, self.frigate = self.frigate, None
            await fr.close()
        for cam in self.cameras.values():
            if cam.person:
                cam._raise_gone(reason="source_changed")

    def _resolve(self, cfg):
        if self.go2rtc:
            return resolve(cfg, self.settings["go2rtc_rtsp_port"], self.public_host)
        return cfg

    def apply_cameras(self):
        """Bring the running cameras in line with the store: start new or changed ones, stop
        removed ones, regenerate go2rtc, and send every client the new state."""
        wanted = {c["id"]: c for c in self.store.cameras()}
        if self.go2rtc:
            self.go2rtc.apply(list(wanted.values()))
        for cid in list(self.cameras):
            if cid not in wanted:
                self.cameras.pop(cid).stop()
                log.info("%s: removed", cid)
        for cid, raw in wanted.items():
            cfg = self._resolve(raw)
            cam = self.cameras.get(cid)
            if cam and cam.cfg == cfg:
                continue
            if cam:
                cam.stop()
            self.cameras[cid] = Camera(cfg, self.settings, self.detector, self)
            self.cameras[cid].start()
        if self.frigate is None and self.settings["model"] == "auto" and self.choice:
            want = detect.choose(self._profile, len(self.cameras), self.settings["detect_fps"],
                                 self.settings["detect_power"])
            if want and (want["model"], want["backend"], want["fps"]) != \
                    (self.choice["model"], self.choice["backend"], self.choice["fps"]):
                self._rebuild_detector()     # more or fewer cameras: a different model may fit
            log.info("%s: %s", cid, "updated" if cam else "added")
        self.publish_threadsafe(self.state())

    # ---- shared alerts switch -----------------------------------------------------------

    def _load_alerts(self):
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {"enabled": bool(data.get("enabled", True)), "cameras": dict(data.get("cameras", {}))}
        except (OSError, ValueError):
            return {"enabled": True, "cameras": {}}

    def _save_alerts(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.alerts), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as e:
            log.warning("could not save alerts state: %s", e)

    def camera_alerts(self, cam_id):
        return self.alerts["enabled"] and self.alerts["cameras"].get(cam_id, True)

    def set_alerts(self, enabled, cam_id=None):
        if cam_id is None:
            self.alerts["enabled"] = enabled
        else:
            self.alerts["cameras"][cam_id] = enabled
        self._save_alerts()
        log.info("alerts %s%s", "on" if enabled else "off", f" for {cam_id}" if cam_id else "")
        self.publish({"type": "alerts", **self.alerts_payload()})

    def alerts_payload(self):
        return {"enabled": self.alerts["enabled"],
                "cameras": {cid: self.alerts["cameras"].get(cid, True) for cid in self.cameras}}

    def state(self):
        return {"type": "state", "alerts": self.alerts_payload(), "detector": self.detector.name,
                "source": self.source["type"],
                "cameras": [c.info() for c in self.cameras.values()]}

    def _detector_changed(self, name):
        self.publish_threadsafe({"type": "detector", "detector": name})

    # ---- events ---------------------------------------------------------------------------

    def publish(self, event):
        event = {**event, "time": _now_iso()}
        eid = self.next_id
        self.next_id += 1
        for q in list(self.subscribers):
            q.put_nowait((eid, event))
        if self.push and event["type"] == "person_detected":
            asyncio.ensure_future(self.push.send(event["camera"], event.get("name", event["camera"]),
                                                 test=event.get("test", False), target=event.get("push"),
                                                 role=event.get("role", "other")))

    def publish_threadsafe(self, event):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.publish, event)

    # ---- HTTP -----------------------------------------------------------------------------

    async def events(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = asyncio.Queue()
        self.subscribers.add(q)
        try:
            await resp.write(_sse(0, {**self.state(), "time": _now_iso()}, retry_ms=3000))
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    if item is None:  # shutting down: end the stream so the server can exit
                        break
                    eid, event = item
                    await resp.write(_sse(eid, event))
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")  # keeps proxies and idle sockets open
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.subscribers.discard(q)
        return resp

    async def get_state(self, request):
        return web.json_response(self.state())

    async def post_alerts(self, request):
        try:
            body = await request.json()
            enabled = body["enabled"]
            cam_id = body.get("camera")
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text='expected {"enabled": true|false, "camera": optional id}')
        if not isinstance(enabled, bool):
            raise web.HTTPBadRequest(text='"enabled" must be true or false')
        if cam_id is not None and cam_id not in self.cameras:
            raise web.HTTPNotFound(text=f"unknown camera {cam_id!r}")
        self.set_alerts(enabled, cam_id)
        return web.json_response(self.alerts_payload())

    async def post_test(self, request):
        """Fake an alert end to end (no detection): person_detected now, person_gone after N s."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        cam = self.cameras.get(body.get("camera") or next(iter(self.cameras)))
        if cam is None:
            raise web.HTTPNotFound(text="unknown camera")
        seconds = max(1, min(int(body.get("seconds", 15)), 120))
        log.info("%s: test alert for %ds", cam.id, seconds)
        cam.test_until = time.time() + seconds   # the watch app reads person state, not events
        if cam.latest is not None:                # a test alert's "detection frame" is the frame now
            cam.snapshot = await asyncio.get_running_loop().run_in_executor(None, _jpeg, cam.latest)
        push = body.get("push") if body.get("push") in ("watch", "phone", "all") else None
        self.publish({"type": "person_detected", "camera": cam.id, "name": cam.name, "role": cam.role,
                      "confidence": 1.0,
                      "push": push,
                      "since": _now_iso(), "streams": cam.streams, "test": True,
                      "snapshot": f"/api/cameras/{cam.id}/live.jpg?t={int(time.time() * 1000)}"})
        self.loop.call_later(seconds, self.publish, {"type": "person_gone", "camera": cam.id,
                                                     "reason": "test", "duration": seconds})
        return web.json_response({"camera": cam.id, "seconds": seconds})

    def _camera(self, request):
        cam = self.cameras.get(request.match_info["cam"])
        if cam is None:
            raise web.HTTPNotFound(text="unknown camera")
        return cam

    async def snapshot(self, request):
        cam = self._camera(request)
        if cam.snapshot is not None:
            return web.Response(body=cam.snapshot, content_type="image/jpeg",
                                headers={"Cache-Control": "no-store"})
        return await self.live(request)

    async def live(self, request):
        cam = self._camera(request)
        frame = cam.latest
        if frame is None:
            raise web.HTTPServiceUnavailable(text="no frame yet")
        body = await asyncio.get_running_loop().run_in_executor(None, _jpeg, frame)
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- watch / iPhone app --------------------------------------------------------------

    OPEN_WATCH = {"/api/watch/pair", "/api/watch/hub"}

    def _watch_identity(self, request):
        """Who is asking: a paired device (signed header, or a media token for URLs), the old shared
        token while it's allowed (returns None), or nobody (401)."""
        auth = request.headers.get("Authorization", "")
        if self.pairing and auth.startswith("Lookout "):
            dev = self.pairing.verify(auth, request.method, request.raw_path)
            if dev is None:
                raise web.HTTPUnauthorized(text="bad signature, clock, or device not paired")
            return dev
        given = auth[7:] if auth.startswith("Bearer ") else request.query.get("token", "")
        if self.pairing and given.startswith("m."):
            dev = self.pairing.check_media(given)
            if dev is None:
                raise web.HTTPUnauthorized(text="media token expired or device removed")
            return dev
        legacy = self.settings.get("watch_token")
        if (self.settings.get("allow_legacy_token") and legacy and given
                and hmac.compare_digest(given.encode(), legacy.encode())):
            return None
        raise web.HTTPUnauthorized(text="pair this app with the hub")

    @web.middleware
    async def watch_auth(self, request, handler):
        # Requests that came in through the Cloudflare tunnel (lookout.r-that.com) carry
        # Cf-Connecting-Ip. From the internet only the token-protected app routes exist; the
        # LAN-only client routes (/events, /api/alerts, /api/test, ...) don't.
        if "Cf-Connecting-Ip" in request.headers and not request.path.startswith("/api/watch/"):
            raise web.HTTPNotFound()
        if request.path.startswith("/api/watch/") and request.path not in self.OPEN_WATCH:
            request["device"] = self._watch_identity(request)
        return await handler(request)

    # ---- admin API (setup page) --------------------------------------------------------

    OPEN_ADMIN = {"/api/admin/session", "/api/admin/setup", "/api/admin/login", "/api/admin/logout"}

    @web.middleware
    async def admin_auth(self, request, handler):
        if request.path.startswith("/api/admin/") and request.path not in self.OPEN_ADMIN:
            auth = request.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else ""
            if self.store is None:
                raise web.HTTPUnauthorized(text="sign in first")
            if given:
                if not hmac.compare_digest(given.encode(), self.store.admin_token.encode()):
                    raise web.HTTPUnauthorized(text="sign in first")
            elif self.auth.valid(request.cookies.get(SESSION_COOKIE)):
                if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
                    raise web.HTTPForbidden(text="missing X-Lookout-CSRF header")
            else:
                raise web.HTTPUnauthorized(text="sign in first")
        return await handler(request)

    def _signed_in(self, resp, token):
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", path="/",
                        max_age=SESSION_DAYS * 86400)
        return resp

    async def admin_session(self, request):
        return web.json_response({"claimed": self.auth.claimed,
                                  "signed_in": self.auth.valid(request.cookies.get(SESSION_COOKIE)),
                                  "version": VERSION})

    async def admin_setup(self, request):
        body = await self._json_body(request)
        try:
            token = self.auth.claim(body.get("code", ""), body.get("password", ""))
        except PermissionError as e:
            raise web.HTTPForbidden(text=str(e))
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        log.info("setup: admin password set; the setup code is no longer valid")
        (DATA_DIR / "setup-code").unlink(missing_ok=True)
        return self._signed_in(web.json_response({"ok": True}), token)

    async def admin_login(self, request):
        body = await self._json_body(request)
        try:
            token = self.auth.login(body.get("password", ""))
        except PermissionError as e:
            raise web.HTTPForbidden(text=str(e))
        return self._signed_in(web.json_response({"ok": True}), token)

    async def admin_logout(self, request):
        self.auth.logout(request.cookies.get(SESSION_COOKIE))
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def admin_password(self, request):
        body = await self._json_body(request)
        try:
            token = self.auth.change_password(body.get("current", ""), body.get("new", ""))
        except PermissionError as e:
            raise web.HTTPForbidden(text=str(e))
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return self._signed_in(web.json_response({"ok": True}), token)

    async def admin_discover(self, request):
        """Cameras on the network that answer ONVIF discovery (through the hub's go2rtc)."""
        if not self.go2rtc:
            return web.json_response({"cameras": [], "note": "discovery needs the hub's built-in go2rtc"})
        try:
            status, ctype, body = await self._go2rtc("/api/onvif", {})
            found = json.loads(body).get("sources", []) if status == 200 and body.strip().startswith(b"{") else []
        except Exception:
            found = []
        return web.json_response({"cameras": [{"name": f.get("name"), "url": f.get("url")} for f in found]})

    async def admin_restart(self, request):
        """Exit; Docker's restart policy starts the hub again with the new settings."""
        log.info("restart requested from the setup page")
        asyncio.get_running_loop().call_later(0.5, os._exit, 0)
        return web.json_response({"ok": True})

    async def index(self, request):
        # Versioned asset links, so a browser picks up new CSS/JS after a hub update
        html = (WEB / "index.html").read_text(encoding="utf-8")
        stamp = str(int(max((WEB / f).stat().st_mtime for f in ("app.css", "app.js"))))
        html = html.replace("/static/app.css", f"/static/app.css?v={stamp}").replace("/static/app.js", f"/static/app.js?v={stamp}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def admin_config(self, request):
        return web.json_response({**self.store.public(), "hub": {
            "version": VERSION, "go2rtc": "embedded" if self.go2rtc else "external",
            "public_host": self.public_host, "rtsp_port": self.settings["go2rtc_rtsp_port"],
            "detector": self.detector.name}})

    async def _json_body(self, request):
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="expected a JSON body")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="expected a JSON object")
        return body

    async def admin_put_camera(self, request):
        body = await self._json_body(request)
        if "id" in request.match_info:
            body["id"] = request.match_info["id"]
        try:
            cam = self.store.put_camera(body)
        except ConfigError as e:
            raise web.HTTPBadRequest(text=str(e))
        await asyncio.get_running_loop().run_in_executor(None, self.apply_cameras)
        out = dict(cam)
        for key in ("url", "sub_url", "detect_url"):
            if key in out:
                out[key] = mask_url(out[key])
        return web.json_response(out)

    async def admin_delete_camera(self, request):
        if not self.store.delete_camera(request.match_info["id"]):
            raise web.HTTPNotFound(text="unknown camera")
        await asyncio.get_running_loop().run_in_executor(None, self.apply_cameras)
        return web.json_response({"ok": True})

    LIVE_SETTINGS = {"detect_power", "detect_fps", "confidence", "hide_after_seconds", "allow_legacy_token", "remote_url",
                     "hub_name"}

    async def admin_settings(self, request):
        patch = await self._json_body(request)
        try:
            settings = self.store.update_settings(patch)
        except ConfigError as e:
            raise web.HTTPBadRequest(text=str(e))
        live = set(patch) & self.LIVE_SETTINGS
        for k in live:
            self.settings[k] = patch[k]
        if live & {"detect_power", "detect_fps"} and self.frigate is None and self.settings["model"] == "auto":
            await asyncio.get_running_loop().run_in_executor(None, self._rebuild_detector)
        return web.json_response({"settings": settings, "restart_needed": bool(set(patch) - self.LIVE_SETTINGS)})

    async def admin_probe(self, request):
        """{"url": ...} -> can the hub open it? Resolution and codec, for the setup page's Test."""
        url = (await self._json_body(request)).get("url")
        if not isinstance(url, str) or not url:
            raise web.HTTPBadRequest(text='expected {"url": ...}')

        def probe():
            opts = {"rtsp_transport": "tcp"} if url.startswith("rtsp") else {}
            with av.open(url, options=opts, timeout=(8, 8)) as c:
                v = c.streams.video[0]
                for frame in c.decode(v):
                    return {"ok": True, "width": frame.width, "height": frame.height,
                            "codec": v.codec_context.name}
            return {"ok": False, "error": "no video frames"}
        try:
            result = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, probe), 15)
        except Exception as e:
            result = {"ok": False, "error": mask_url(str(e))[:300]}
        return web.json_response(result)

    async def admin_get_source(self, request):
        src = dict(self.source)
        if src.get("password"):
            src["password"] = "***"
        return web.json_response({**src, "connected": self.frigate.connected if self.frigate else None,
                                  "version": self.frigate.version if self.frigate else None})

    async def admin_put_source(self, request):
        """{"type": "builtin"} or {"type": "frigate", "url": ..., "user"?, "password"?, "severity"?}.
        Frigate is tested before anything changes; its cameras are imported."""
        body = await self._json_body(request)
        kind = body.get("type")
        if kind not in ("builtin", "frigate"):
            raise web.HTTPBadRequest(text='type must be "builtin" or "frigate"')
        if kind == "frigate":
            if not isinstance(body.get("url"), str) or not body["url"].startswith(("http://", "https://")):
                raise web.HTTPBadRequest(text="url must be Frigate's http(s) address")
            if body.get("password") == "***":
                body["password"] = self.source.get("password")
            if body.get("severity", "alert") not in ("alert", "detection"):
                raise web.HTTPBadRequest(text="severity must be alert or detection")
            cfg = {k: body[k] for k in ("type", "url", "user", "password", "severity") if body.get(k)}
            # Try it before touching the current source: a typo mustn't interrupt detection
            probe = Frigate(cfg, on_review=lambda *a: None)
            try:
                await probe.open()
                await probe.cameras()
            except (FrigateError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                raise web.HTTPBadRequest(text=f"can't use Frigate there: {e}")
            finally:
                await probe.close()
            await self.stop_frigate()
            added = await self.start_frigate(cfg)
        else:
            cfg, added = {"type": "builtin"}, []
            await self.stop_frigate()
        self.source = cfg
        self.store.set_source(cfg)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.apply_cameras)
        if kind == "frigate":
            self.local = self.detector = _ExternalDetector()    # no PC GPU helper either: Frigate detects
            self.publish({"type": "detector", "detector": self.detector.name, "choice": None})
        else:
            self.local = await loop.run_in_executor(None, self._build_detector, len(self.cameras))
            self.detector = self.local
            if self.settings.get("gpu_helper"):
                self.detector = GpuHelper(self.settings["gpu_helper"], self.settings["gpu_helper_timeout_ms"] / 1000,
                                          self.local, on_change=self._detector_changed)
            self.publish({"type": "detector", "detector": self.detector.name, "choice": self.choice})
        self.publish({**self.state()})
        return web.json_response({"type": kind, "imported": added})

    async def watch_visitors(self, request):
        """Recent person alerts for a camera (from Frigate), newest first, with thumbnails."""
        cam = self._camera(request)
        if self.frigate is None:
            return web.json_response({"visitors": []})
        try:
            items = await self.frigate.recent(self.frigate_name(cam), int(request.query.get("limit", 10)))
        except (FrigateError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=f"frigate: {e}")
        return web.json_response({"visitors": [
            {"id": v["id"], "start": v["start"], "end": v["end"],
             "thumbnail": f"/api/watch/visitors/{v['detection']}.jpg" if v["detection"] else None}
            for v in items]})

    async def watch_visitor_image(self, request):
        if self.frigate is None:
            raise web.HTTPNotFound()
        det = request.match_info["det"]
        if not all(c.isalnum() or c in ".-_" for c in det):
            raise web.HTTPNotFound()
        h = max(64, min(int(request.query.get("h", 240)) if request.query.get("h", "").isdigit() else 240, 1080))
        try:
            body = await self.frigate.get_bytes(f"/api/events/{det}/snapshot.jpg?bbox=0&height={h}")
        except (FrigateError, aiohttp.ClientError) as e:
            raise web.HTTPNotFound(text=str(e))
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    def frigate_name(self, cam):
        return cam.cfg.get("frigate") or cam.id

    # ---- pairing ---------------------------------------------------------------------------

    def hub_info(self):
        return {"hub_id": self.pairing.hub_id, "name": self.settings["hub_name"], "version": VERSION,
                "fingerprint": self.pairing.fingerprint, "https_port": self.settings["https_port"],
                "http_port": self.settings["port"], "address": self.public_host,
                "remote_url": self.settings.get("remote_url")}

    async def watch_hub(self, request):
        return web.json_response(self.hub_info())

    async def watch_pair(self, request):
        body = await self._json_body(request)
        if "Cf-Connecting-Ip" in request.headers and len(str(body.get("code") or "")) < 12:
            raise web.HTTPForbidden(text="away from home, pair by scanning the QR code")
        try:
            rec = self.pairing.pair(body.get("code"), body.get("name"), body.get("platform"), body.get("public_key"))
        except PermissionError as e:
            raise web.HTTPForbidden(text=str(e))
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        log.info("paired %s (%s) as %s", rec["name"], rec["platform"], rec["id"])
        self.publish({"type": "devices"})
        return web.json_response({"device_id": rec["id"], "hub": self.hub_info()})

    async def watch_media_token(self, request):
        dev = request.get("device")
        if dev is None:
            raise web.HTTPForbidden(text="media tokens are for paired devices")
        return web.json_response(self.pairing.media_token(dev["id"]))

    async def watch_pairing_code(self, request):
        """A paired iPhone asks for a code to pair its watch with."""
        dev = request.get("device")
        if dev is None:
            raise web.HTTPForbidden(text="only a paired device can add another")
        return web.json_response(self.pairing.new_code(kind="companion", by=dev["id"]))

    async def watch_unpair(self, request):
        """A paired device removes itself (Unpair in the app)."""
        dev = request.get("device")
        if dev is None:
            raise web.HTTPForbidden(text="only a paired device can unpair itself")
        self.pairing.revoke(dev["id"])
        if self.push:
            self.push.forget_device(dev["id"])
        log.info("device %s (%s) unpaired itself", dev["id"], dev["name"])
        self.publish({"type": "devices"})
        return web.json_response({"ok": True})

    def pairing_link(self, code):
        from urllib.parse import urlencode
        q = {"h": self.pairing.hub_id, "n": self.settings["hub_name"], "a": self.public_host,
             "p": self.settings["https_port"], "q": self.settings["port"], "fp": self.pairing.fingerprint,
             "c": code["secret"]}
        if self.settings.get("remote_url"):
            q["r"] = self.settings["remote_url"]
        return "lookout://pair?" + urlencode(q)

    async def admin_new_pairing(self, request):
        import segno
        code = self.pairing.new_code(kind="app", by="setup-page")
        link = self.pairing_link(code)
        svg = segno.make(link, error="m").svg_inline(scale=6, border=2, dark="#14202B", light="#FFFFFF")
        return web.json_response({**code, "link": link, "qr_svg": svg, "hub": self.hub_info()})

    async def admin_devices(self, request):
        return web.json_response({"devices": self.pairing.devices(),
                                  "allow_legacy_token": bool(self.settings.get("allow_legacy_token")),
                                  "hub": self.hub_info()})

    async def admin_revoke(self, request):
        dev = request.match_info["id"]
        if not self.pairing.revoke(dev):
            raise web.HTTPNotFound(text="unknown device")
        if self.push:
            self.push.forget_device(dev)
        log.info("removed device %s", dev)
        self.publish({"type": "devices"})
        return web.json_response({"ok": True})

    async def admin_detector(self, request):
        return web.json_response(self.detector_info())

    async def admin_benchmark(self, request):
        """Re-time the models on this hardware (about 2 s per model) and pick again."""
        def run():
            prof = detect.profile(self.settings["models_dir"], tuple(self.settings["model_families"]),
                                  self.settings["threads"])
            self.store.set_profile(prof)
            self._rebuild_detector()
        await asyncio.get_running_loop().run_in_executor(None, run)
        return web.json_response(self.detector_info())

    async def watch_devices(self, request):
        """{"token": hex push token, "topic": app bundle id, "env": "production"|"sandbox"}"""
        try:
            body = await request.json()
            token, topic = body["token"].lower(), body["topic"]
            env = body.get("env", "production")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPBadRequest(text='expected {"token": ..., "topic": ..., "env": ...}')
        if not re.fullmatch(r"[0-9a-f]{32,200}", token) or not topic.startswith("dev.rogersquare.lookout")                 or env not in Push.HOSTS:
            raise web.HTTPBadRequest(text="bad token, topic or env")
        if self.push is None:
            raise web.HTTPServiceUnavailable(text="push not configured on this hub")
        dev = request.get("device")
        self.push.register(token, topic, env, device_id=dev["id"] if dev else None)
        return web.json_response({"ok": True})

    async def watch_cameras(self, request):
        return web.json_response({"alerts": self.alerts_payload(),
                                  "cameras": [c.watch_info() for c in self.cameras.values()]})

    async def watch_frame(self, request):
        cam = self._camera(request)
        frame = cam.latest
        if frame is None:
            raise web.HTTPServiceUnavailable(text="no frame yet")
        try:
            width = max(64, min(int(request.query.get("w", 0)), 1920)) if "w" in request.query else None
        except ValueError:
            raise web.HTTPBadRequest(text="w must be a number of pixels")
        body = await asyncio.get_running_loop().run_in_executor(None, lambda: _jpeg(frame, 80, width))
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def watch_snapshot(self, request):
        """The frame that raised the latest alert (what the person-alert notification shows),
        ?w= scaled; falls back to the newest frame before any alert."""
        cam = self._camera(request)
        try:
            width = max(64, min(int(request.query.get("w", 0)), 1920)) if "w" in request.query else None
        except ValueError:
            raise web.HTTPBadRequest(text="w must be a number of pixels")
        if cam.snapshot is None:
            return await self.watch_frame(request)
        body = cam.snapshot
        if width:
            def scale(data):
                img = Image.open(io.BytesIO(data))
                if width < img.width:
                    img = img.resize((width, max(2, round(img.height * width / img.width))), Image.LANCZOS)
                out = io.BytesIO()
                img.save(out, "JPEG", quality=80)
                return out.getvalue()
            body = await asyncio.get_running_loop().run_in_executor(None, scale, body)
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def watch_mjpeg(self, request):
        """Live view without a video player: newest frames as multipart JPEG on one connection.

        Self-pacing: the next frame is only encoded once the previous write has drained, so a
        slow link (a watch on the phone's Bluetooth) gets fewer, always-current frames instead
        of a growing backlog. ?w= width (default 480), ?fps= cap (default 10, max 15)."""
        cam = self._camera(request)
        try:
            width = max(64, min(int(request.query.get("w", 480)), 1920))
            fps = max(1, min(int(request.query.get("fps", 10)), 15))
        except ValueError:
            raise web.HTTPBadRequest(text="w and fps must be numbers")
        resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame",
                                           "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        loop = asyncio.get_running_loop()
        last = None
        try:
            while True:
                started = loop.time()
                frame = cam.latest
                if frame is not None and frame is not last:
                    last = frame
                    body = await loop.run_in_executor(None, lambda: _jpeg(frame, 70, width))
                    await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % len(body) + body + b"\r\n")
                await asyncio.sleep(max(0.0, 1 / fps - (loop.time() - started)))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _go2rtc(self, path, query):
        """GET from go2rtc's API; returns (status, content type, body)."""
        url = self.settings["go2rtc_api"].rstrip("/") + path
        try:
            async with self.http.get(url, params=query) as r:
                return r.status, r.content_type, await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=f"go2rtc unavailable: {e}")

    def _hls_response(self, request, status, ctype, body):
        if status != 200:
            raise web.HTTPBadGateway(text=f"go2rtc answered {status}")
        # players fetch segments by URL, so carry the token this request used (media or legacy)
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.query.get("token")
        if ctype in ("application/vnd.apple.mpegurl", "application/x-mpegurl") and token:
            body = _with_token(body.decode(), token).encode()
        return web.Response(body=body, content_type=ctype, headers={"Cache-Control": "no-store"})

    async def watch_stream(self, request):
        """Master playlist. Its relative URIs (hls/playlist.m3u8?id=...) resolve under this
        camera's path, so the follow-up requests come back through watch_hls."""
        cam = self._camera(request)
        src = cam.hls_variants.get(request.query.get("v", ""), cam.hls_src)
        if not src:
            raise web.HTTPNotFound(text="no HLS stream for this camera")
        return self._hls_response(request, *await self._go2rtc("/api/stream.m3u8", {"src": src, "mp4": ""}))

    async def watch_hls(self, request):
        self._camera(request)
        name = request.match_info["file"]
        if name not in ("playlist.m3u8", "init.mp4", "segment.m4s", "segment.ts"):
            raise web.HTTPNotFound()
        query = {k: v for k, v in request.query.items() if k != "token"}
        return self._hls_response(request, *await self._go2rtc(f"/api/hls/{name}", query))

    async def healthz(self, request):
        return web.json_response({"ok": True, "version": VERSION, "detector": self.detector.name,
                                  "source": self.source["type"],
                                  "cameras": {c.id: c.connected for c in self.cameras.values()}})

    async def on_startup(self, app):
        self.loop = asyncio.get_running_loop()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        if self.push:
            asyncio.ensure_future(self.push.warm())
        if self.go2rtc:
            self.go2rtc.apply(self.store.cameras() if self.store else [c.cfg for c in self.cameras.values()])
            self.go2rtc.watchdog()
        for cam in self.cameras.values():
            cam.start()
        self.watch_load()
        await self.advertise()
        if self.source["type"] == "frigate":
            async def connect():
                while self.frigate is None and self.source["type"] == "frigate":
                    try:
                        await self.start_frigate(self.source, import_cameras=False)
                    except Exception as e:
                        log.warning("frigate: can't connect yet (%s), retrying", e)
                        await asyncio.sleep(10)
            asyncio.ensure_future(connect())

    async def advertise(self):
        """Announce the hub on the LAN as _lookout._tcp, so apps find it without typing an address."""
        if not self.pairing or os.environ.get("LOOKOUT_BONJOUR") == "0":
            return
        try:
            import socket
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
            info = ServiceInfo("_lookout._tcp.local.", f"{self.settings['hub_name']} {self.pairing.hub_id[:4]}._lookout._tcp.local.",
                               addresses=[socket.inet_aton(self.public_host)], port=self.settings["https_port"],
                               properties={"id": self.pairing.hub_id, "v": VERSION, "http": str(self.settings["port"]),
                                           "a": self.public_host, "n": self.settings["hub_name"],
                                           "p": str(self.settings["https_port"]),
                                           "fp": self.pairing.fingerprint[:16]},
                               server=f"lookout-{self.pairing.hub_id[:8]}.local.")
            self.zeroconf = AsyncZeroconf()
            await self.zeroconf.async_register_service(info)
            self._zc_info = info
            log.info("announced on the LAN as %s", info.name)
        except Exception as e:
            log.warning("Bonjour announcement failed: %s", e)

    async def on_cleanup(self, app):
        if self.zeroconf:
            try:
                await self.zeroconf.async_unregister_service(self._zc_info)
                await self.zeroconf.async_close()
            except Exception:
                pass
        await self.stop_frigate()
        await self.http.close()

    async def on_shutdown(self, app):
        for cam in self.cameras.values():
            cam.stop()
        if self.go2rtc:
            self.go2rtc.stop()
        for q in list(self.subscribers):  # open SSE streams would otherwise hold shutdown open
            q.put_nowait(None)

    def app(self):
        app = web.Application(middlewares=[self.watch_auth, self.admin_auth])
        app.router.add_get("/events", self.events)
        app.router.add_get("/api/state", self.get_state)
        app.router.add_post("/api/alerts", self.post_alerts)
        app.router.add_post("/api/test", self.post_test)
        app.router.add_get("/api/cameras/{cam}/snapshot.jpg", self.snapshot)
        app.router.add_get("/api/cameras/{cam}/live.jpg", self.live)
        app.router.add_get("/healthz", self.healthz)
        app.router.add_get("/api/watch/cameras", self.watch_cameras)
        app.router.add_post("/api/watch/alerts", self.post_alerts)   # same switch, token-protected
        app.router.add_post("/api/watch/devices", self.watch_devices)
        app.router.add_get("/api/watch/cameras/{cam}/frame.jpg", self.watch_frame)
        app.router.add_get("/api/watch/cameras/{cam}/snapshot.jpg", self.watch_snapshot)
        app.router.add_get("/api/watch/cameras/{cam}/stream.m3u8", self.watch_stream)
        app.router.add_get("/api/watch/cameras/{cam}/live.mjpeg", self.watch_mjpeg)
        app.router.add_get("/api/watch/cameras/{cam}/hls/{file}", self.watch_hls)
        app.router.add_get("/", self.index)
        app.router.add_static("/static/", WEB, show_index=False)
        app.router.add_get("/api/admin/session", self.admin_session)
        app.router.add_post("/api/admin/setup", self.admin_setup)
        app.router.add_post("/api/admin/login", self.admin_login)
        app.router.add_post("/api/admin/logout", self.admin_logout)
        app.router.add_post("/api/admin/password", self.admin_password)
        app.router.add_get("/api/admin/discover", self.admin_discover)
        app.router.add_post("/api/admin/restart", self.admin_restart)
        app.router.add_get("/api/admin/config", self.admin_config)
        app.router.add_post("/api/admin/cameras", self.admin_put_camera)
        app.router.add_post("/api/admin/cameras/probe", self.admin_probe)
        app.router.add_put("/api/admin/cameras/{id}", self.admin_put_camera)
        app.router.add_delete("/api/admin/cameras/{id}", self.admin_delete_camera)
        app.router.add_put("/api/admin/settings", self.admin_settings)
        app.router.add_get("/api/admin/detector", self.admin_detector)
        app.router.add_get("/api/admin/source", self.admin_get_source)
        app.router.add_post("/api/admin/pairing", self.admin_new_pairing)
        app.router.add_get("/api/admin/devices", self.admin_devices)
        app.router.add_delete("/api/admin/devices/{id}", self.admin_revoke)
        app.router.add_get("/api/watch/hub", self.watch_hub)
        app.router.add_post("/api/watch/pair", self.watch_pair)
        app.router.add_get("/api/watch/media-token", self.watch_media_token)
        app.router.add_post("/api/watch/pairing-code", self.watch_pairing_code)
        app.router.add_post("/api/watch/unpair", self.watch_unpair)
        app.router.add_put("/api/admin/source", self.admin_put_source)
        app.router.add_get("/api/watch/cameras/{cam}/visitors", self.watch_visitors)
        app.router.add_get("/api/watch/visitors/{det}.jpg", self.watch_visitor_image)
        app.router.add_post("/api/admin/detector/benchmark", self.admin_benchmark)
        app.on_startup.append(self.on_startup)
        app.on_shutdown.append(self.on_shutdown)
        app.on_cleanup.append(self.on_cleanup)
        return app


def _sse(eid, event, retry_ms=None):
    lines = []
    if retry_ms:
        lines.append(f"retry: {retry_ms}")
    lines += [f"id: {eid}", f"event: {event['type']}", f"data: {json.dumps(event)}", "", ""]
    return "\n".join(lines).encode()


# Environment overrides, for port clashes on a NAS (and tests running beside a live hub)
ENV_INTS = {"LOOKOUT_PORT": "port", "LOOKOUT_RTSP_PORT": "go2rtc_rtsp_port", "LOOKOUT_GO2RTC_API_PORT": "go2rtc_api_port",
            "LOOKOUT_HTTPS_PORT": "https_port"}
# Docker Desktop (Windows/Mac) can't see the host's address from inside the container: the
# installer passes it in, so apps and restreams get the right one
ENV_STRS = {"LOOKOUT_PUBLIC_HOST": "public_host", "LOOKOUT_HUB_NAME": "hub_name"}


def load_settings(store):
    settings = store.settings(DEFAULTS)
    settings["cameras"] = store.cameras()
    for env, key in ENV_INTS.items():
        if os.environ.get(env):
            settings[key] = int(os.environ[env])
    for env, key in ENV_STRS.items():
        if os.environ.get(env):
            settings[key] = os.environ[env]
    if settings["threads"] == "auto":
        settings["threads"] = max(2, min(8, (os.cpu_count() or 4) // 4))
    return settings


class _RedactToken(logging.Filter):
    """Keeps the watch token (sent as ?token= by AVPlayer) out of the access log."""

    def filter(self, record):
        record.msg = re.sub(r"token=[^&\s\"]+", "token=***", record.getMessage())
        record.args = ()
        return True


def reset_password():
    """docker exec lookout-hub python hub.py --reset-password: forget the setup page password and
    sign everyone out, so the next start prints a new setup code. Cameras and devices are kept."""
    store = Store(DATA_DIR / "settings.json", legacy_config=CONFIG_FILE)
    with store.lock:
        store.data.pop("admin_password", None)
        store.data.pop("sessions", None)
        store._save()
    print("Password removed. Restart the hub (docker restart lookout-hub), then open the setup "
          "link it prints in its log (docker logs lookout-hub).")


def main():
    if "--reset-password" in sys.argv:
        return reset_password()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").addFilter(_RedactToken())
    threading.excepthook = lambda a: log.error("thread %s crashed", a.thread.name,
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    store = Store(DATA_DIR / "settings.json", legacy_config=CONFIG_FILE)
    if store.imported:
        log.info("imported %s into %s", CONFIG_FILE, store.path)
    token_file = DATA_DIR / "admin-token"
    if not token_file.exists():
        token_file.write_text(store.admin_token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        log.info("admin token for the setup API written to %s", token_file)
    if not store.data.get("admin_password"):
        code = Auth(store).setup_code
        (DATA_DIR / "setup-code").write_text(code + "\n", encoding="utf-8")
        port = os.environ.get("LOOKOUT_PORT", "8765")
        url = f"http://{os.environ.get('LOOKOUT_PUBLIC_HOST') or store.data['settings'].get('public_host') or lan_ip()}:{port}/?setup={code}"
        log.info("NEW HUB: open %s (or http://<this machine>:%s and enter setup code %s) to set a password",
                 url, port, code)
        try:                                      # scan with a phone instead of typing
            import segno
            buf = io.StringIO()
            segno.make(url, error="m").terminal(out=buf, compact=True)
            log.info("or scan this with your phone's camera:" + chr(10) + "%s", buf.getvalue())
        except Exception:
            pass
    settings = load_settings(store)
    names = ", ".join(c["id"] for c in settings["cameras"])
    log.info("Lookout Hub %s starting with %d camera(s)%s", VERSION, len(settings["cameras"]),
             f": {names}" if names else " - add one with POST /api/admin/cameras")
    hub = Hub(settings, store)
    asyncio.run(serve(hub, settings))


async def serve(hub, settings):
    """HTTP for the LAN clients, setup page and media; HTTPS (self-signed, pinned by apps) for the
    app API. Stops cleanly on SIGTERM/SIGINT."""
    import signal
    import ssl
    runner = web.AppRunner(hub.app(), shutdown_timeout=3, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    await web.TCPSite(runner, settings["host"], settings["port"]).start()
    if hub.pairing:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(hub.pairing.cert_file, hub.pairing.key_file)
        await web.TCPSite(runner, settings["host"], settings["https_port"], ssl_context=ctx).start()
        log.info("app API on https://%s:%d (certificate %s...)", hub.public_host, settings["https_port"],
                 hub.pairing.fingerprint[:16])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


if __name__ == "__main__":
    main()
