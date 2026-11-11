"""Pan and tilt for cameras that can move.

A camera opts in with a `ptz` setting naming what moves it. The apps send relative moves
(-1..1 on each axis, scaled to degrees here) and saved positions; the hub passes them on.

Backends:
  wyze-bridge   docker-wyze-bridge's camera command API (its Python 3.x line; the 4.x Go
                rewrite has no movement commands). {"type": "wyze-bridge",
                "url": "http://127.0.0.1:5000", "camera": "bedroom", "api_key": optional}
"""

import asyncio
import time
from urllib.parse import quote

import aiohttp

TYPES = ("wyze-bridge",)
STEP_DEGREES = 30          # a full nudge (pan=1) turns this far; the Wyze Pan V3 does ~350 x ~110
MIN_INTERVAL = 0.35        # one move at a time per camera: the motor queues anything faster


class PTZError(Exception):
    pass


def validate(cfg):
    """Clean copy of a camera's `ptz` setting, or ValueError."""
    if not isinstance(cfg, dict) or cfg.get("type") not in TYPES:
        raise ValueError(f"ptz.type must be one of {TYPES}")
    url = cfg.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("ptz.url must be the bridge's http(s):// address")
    cam = cfg.get("camera")
    if not isinstance(cam, str) or not cam.strip():
        raise ValueError("ptz.camera must be the camera's name in the bridge")
    out = {"type": cfg["type"], "url": url.rstrip("/"), "camera": cam.strip()}
    if cfg.get("api_key"):
        if not isinstance(cfg["api_key"], str):
            raise ValueError("ptz.api_key must be a string")
        out["api_key"] = cfg["api_key"]
    return out


class WyzeBridge:
    def __init__(self, cfg, session):
        self.cfg = cfg
        self.http = session

    async def _cmd(self, cmd, payload=None):
        url = f"{self.cfg['url']}/api/{quote(self.cfg['camera'])}/{cmd}"
        if payload is not None:
            url += f"/{payload}"
        params = {"api": self.cfg["api_key"]} if self.cfg.get("api_key") else None
        try:
            async with self.http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                body = await r.json(content_type=None) if r.status == 200 else None
        except aiohttp.ClientConnectorError:
            raise PTZError(f"Can't reach the camera bridge at {self.cfg['url']}. Check the address and that it's running.")
        except asyncio.TimeoutError:
            raise PTZError("The camera bridge didn't answer in time. Is the camera on?")
        except (aiohttp.ClientError, ValueError):
            raise PTZError("The camera bridge gave an answer Lookout doesn't understand.")
        if r.status != 200:
            raise PTZError(f"The camera bridge answered {r.status}. Is it version 3.x, and is the camera name right?")
        if isinstance(body, dict) and body.get("status") not in (None, "success", "ok"):
            raise PTZError(str(body.get("response") or body.get("status"))[:200])
        return body

    async def move(self, pan, tilt):
        h, v = round(pan * STEP_DEGREES), round(tilt * STEP_DEGREES)
        return await self._cmd("rotary_degree", f"{h},{v}")

    async def preset(self, n):
        return await self._cmd("cruise_point", str(n))

    async def get(self, cmd):
        """A setting's current value (e.g. "power" -> "on")."""
        body = await self._cmd(cmd)
        return body.get("value") if isinstance(body, dict) else None

    async def set(self, cmd, value):
        return await self._cmd(cmd, value)

    async def presets(self):
        """Saved positions: [{"pan": degrees, "tilt": degrees}], in the bridge's order (1-based)."""
        body = await self._cmd("cruise_points")
        points = body.get("value") if isinstance(body, dict) else None
        return [{"pan": p.get("horizontal"), "tilt": p.get("vertical")} for p in points or [] if isinstance(p, dict)]


async def bridge_cameras(url, api_key, session):
    """The bridge's cameras: {stream name: {"model", "name", "mac"}}. Raises PTZError."""
    params = {"api": api_key} if api_key else None
    try:
        async with session.get(f"{url.rstrip('/')}/api", params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 401:
                raise PTZError("The camera bridge refused the API key.")
            if r.status != 200:
                raise PTZError(f"The camera bridge answered {r.status}. Is it version 3.x?")
            body = await r.json(content_type=None)
    except aiohttp.ClientConnectorError:
        raise PTZError(f"Can't reach the camera bridge at {url}.")
    except asyncio.TimeoutError:
        raise PTZError("The camera bridge didn't answer in time.")
    except (aiohttp.ClientError, ValueError):
        raise PTZError("The camera bridge gave an answer Lookout doesn't understand.")
    cams = (body or {}).get("cameras") if isinstance(body, dict) else None
    if not isinstance(cams, dict):
        raise PTZError("That doesn't look like a docker-wyze-bridge 3.x API.")
    return {name: {"model": c.get("product_model") or "", "name": c.get("nickname") or name, "mac": c.get("mac")}
            for name, c in cams.items() if isinstance(c, dict)}


class PTZ:
    """The hub's movers, one per camera that has a `ptz` setting."""

    def __init__(self, session):
        self.http = session
        self.last = {}           # camera id -> time of the last move
        self.locks = {}

    def backend(self, cfg):
        if cfg["type"] == "wyze-bridge":
            return WyzeBridge(cfg, self.http)
        raise PTZError("unknown ptz type")

    async def run(self, cam_id, cfg, body):
        """body: {"pan": -1..1, "tilt": -1..1} or {"preset": 1..}"""
        lock = self.locks.setdefault(cam_id, asyncio.Lock())
        if lock.locked():
            raise PTZError("busy")
        async with lock:
            wait = self.last.get(cam_id, 0) + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            b = self.backend(cfg)
            if "preset" in body:
                n = body["preset"]
                if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 8:
                    raise ValueError("preset must be 1-8")
                await b.preset(n)
            else:
                pan, tilt = body.get("pan", 0), body.get("tilt", 0)
                for v in (pan, tilt):
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not -1 <= v <= 1:
                        raise ValueError("pan and tilt must be numbers from -1 to 1")
                if pan == 0 and tilt == 0:
                    raise ValueError("nothing to move")
                await b.move(pan, tilt)
            self.last[cam_id] = time.monotonic()
