"""Frigate as the hub's camera + detection source.

With a Frigate source the hub runs no detection of its own: Frigate's review items (what its own
notifications are built on) become Lookout alerts - a review with a person at the configured
severity raises person_detected for its camera, and its end raises person_gone. Cameras are
imported from Frigate's config; a camera the hub already has (same name) keeps its own streams.

Frigate is reached either on its internal API (http://host:5000, no login: only bind it to
localhost) or its authenticated one (https://host:8971 with a Frigate user).
"""

import asyncio
import json
import logging
import ssl
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("lookout-hub")


class FrigateError(Exception):
    pass


class Frigate:
    def __init__(self, cfg, on_review, on_status=None):
        """cfg: {url, user?, password?, severity ("alert" | "detection")}.
        on_review(kind, review) is called for each person review ("start" | "end")."""
        self.url = cfg["url"].rstrip("/")
        self.user = cfg.get("user")
        self.password = cfg.get("password")
        self.severity = cfg.get("severity", "alert")
        self.on_review = on_review
        self.on_status = on_status or (lambda connected: None)
        self.session = None
        self.connected = False
        self.version = None
        self._task = None
        self._active = {}      # review id -> camera, for reviews we raised

    # ---- HTTP ------------------------------------------------------------------------------

    def _ssl(self):
        if self.url.startswith("https"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False          # Frigate's own certificate is self-signed
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    async def open(self):
        if self.session is None:
            # unsafe=True: keep the login cookie even though Frigate is usually reached by IP address
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                                 cookie_jar=aiohttp.CookieJar(unsafe=True),
                                                 connector=aiohttp.TCPConnector(ssl=self._ssl() or False))
        if self.user:
            async with self.session.post(f"{self.url}/api/login",
                                         json={"user": self.user, "password": self.password or ""}) as r:
                if r.status != 200:
                    raise FrigateError(f"Frigate login failed ({r.status})")
        async with self.session.get(f"{self.url}/api/version") as r:
            if r.status in (401, 403):
                raise FrigateError("Frigate wants a login: give a user and password, or use its internal port 5000")
            if r.status != 200:
                raise FrigateError(f"not a Frigate API ({r.status})")
            self.version = (await r.text()).strip().strip('"')

    async def get_json(self, path):
        async with self.session.get(f"{self.url}{path}") as r:
            if r.status != 200:
                raise FrigateError(f"{path}: {r.status}")
            return await r.json(content_type=None)

    async def get_bytes(self, path):
        async with self.session.get(f"{self.url}{path}") as r:
            if r.status != 200:
                raise FrigateError(f"{path}: {r.status}")
            return await r.read()

    async def close(self):
        if self._task:
            self._task.cancel()
        if self.session:
            await self.session.close()

    # ---- cameras -----------------------------------------------------------------------------

    async def cameras(self):
        """Frigate's cameras as hub cameras: [{id, name, url, frigate}]. The stream is Frigate's
        go2rtc restream when it has one for the camera, else the camera's record/detect input."""
        cfg = await self.get_json("/api/config")
        host = urlsplit(self.url).hostname
        restreams = (cfg.get("go2rtc") or {}).get("streams") or {}
        out = []
        for name, cam in (cfg.get("cameras") or {}).items():
            if cam.get("enabled") is False:
                continue
            inputs = ((cam.get("ffmpeg") or {}).get("inputs")) or []
            by_role = {r: i.get("path") for i in inputs for r in (i.get("roles") or [])}
            url = (f"rtsp://{host}:8554/{name}" if name in restreams
                   else by_role.get("record") or by_role.get("detect") or (inputs[0].get("path") if inputs else None))
            friendly = ((cam.get("ui") or {}).get("name")) or cam.get("friendly_name") or name.replace("_", " ").title()
            out.append({"id": name, "name": friendly, "url": url, "frigate": name})
        return out

    async def connected_cameras(self):
        """{camera: True/False} from Frigate's stats (frames arriving)."""
        stats = await self.get_json("/api/stats")
        return {k: (v.get("camera_fps") or 0) > 0 for k, v in (stats.get("cameras") or {}).items()}

    # ---- snapshots / visitors ----------------------------------------------------------------

    async def snapshot(self, review):
        """JPEG of the person behind a review: its first detection's snapshot, else the
        camera's latest frame."""
        for det in (review.get("data") or {}).get("detections") or []:
            try:
                return await self.get_bytes(f"/api/events/{det}/snapshot.jpg?bbox=0&crop=0")
            except FrigateError:
                continue
        return await self.get_bytes(f"/api/{review['camera']}/latest.jpg")

    async def recent(self, camera, limit=10):
        """Recent person alerts for a camera: [{id, detection, start, end}] newest first."""
        items = await self.get_json(f"/api/review?cameras={camera}&severity={self.severity}&limit={limit * 3}")
        out = []
        for r in items:
            data = r.get("data") or {}
            if "person" not in (data.get("objects") or []):
                continue
            dets = data.get("detections") or []
            out.append({"id": r["id"], "detection": dets[0] if dets else None,
                        "start": r.get("start_time"), "end": r.get("end_time")})
            if len(out) >= limit:
                break
        return out

    # ---- live reviews --------------------------------------------------------------------------

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        backoff = 1
        ws_url = self.url.replace("http", "ws", 1) + "/ws"
        while True:
            try:
                async with self.session.ws_connect(ws_url, heartbeat=30) as ws:
                    self._set_connected(True)
                    backoff = 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("frigate: connection lost: %s", e)
            self._set_connected(False)
            # Anything we raised can't be ended by a message we'll never get: end it now
            for rid, cam in list(self._active.items()):
                self._active.pop(rid, None)
                self.on_review("end", {"id": rid, "camera": cam, "reason": "frigate_disconnected"})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            try:
                if self.user:
                    await self.open()       # session cookie may have expired
            except Exception:
                pass

    def _set_connected(self, ok):
        if ok != self.connected:
            self.connected = ok
            log.info("frigate: %s", "connected" if ok else "disconnected")
            self.on_status(ok)

    def _message(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("topic") != "reviews":
            return
        payload = msg.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return
        after = (payload or {}).get("after") or {}
        rid, cam = after.get("id"), after.get("camera")
        if not rid or not cam:
            return
        data = after.get("data") or {}
        is_person = "person" in (data.get("objects") or [])
        wanted = after.get("severity") == "alert" or self.severity == "detection"
        kind = payload.get("type")
        if kind in ("new", "update") and is_person and wanted and rid not in self._active and not after.get("end_time"):
            self._active[rid] = cam
            self.on_review("start", after)
        elif (kind == "end" or after.get("end_time")) and rid in self._active:
            self._active.pop(rid, None)
            self.on_review("end", after)
