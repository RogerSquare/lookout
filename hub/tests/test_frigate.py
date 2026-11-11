"""Frigate as the source: the hub stops detecting, imports Frigate's cameras, and turns Frigate's
person reviews into alerts. Runs against a fake Frigate (login, config, stats, /ws, snapshots,
review history). Run in the hub image: python tests/test_frigate.py"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
MEDIA = Path(os.environ.get("LOOKOUT_TEST_MEDIA", "/media"))
PORT, RTSP, API = 18767, 18555, 18985
BASE = f"http://127.0.0.1:{PORT}"
SNAP = b"\xff\xd8FAKE-FRIGATE-SNAPSHOT\xff\xd9"
results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


class FakeFrigate:
    def __init__(self):
        self.sockets = set()
        self.logins = 0
        self.clip_fetches = []

    def authed(self, request):
        return request.cookies.get("frigate_token") == "ok"

    async def login(self, request):
        body = await request.json()
        if body == {"user": "lookout", "password": "pw"}:
            self.logins += 1
            r = web.json_response({})
            r.set_cookie("frigate_token", "ok")
            return r
        return web.Response(status=401)

    def guard(handler):
        async def wrapped(self, request):
            if not self.authed(request):
                return web.Response(status=401)
            return await handler(self, request)
        return wrapped

    @guard
    async def version(self, request):
        return web.Response(text="0.18.2-fake")

    @guard
    async def config(self, request):
        return web.json_response({"go2rtc": {"streams": {"front_door": ["rtsp://cam/x"]}}, "cameras": {
            "kitchen": {"ffmpeg": {"inputs": [{"path": "rtsp://elsewhere/k", "roles": ["record"]}]}},
            "front_door": {"ui": {"name": "Front Door"}, "ffmpeg": {"inputs": [{"path": "rtsp://cam/x", "roles": ["detect"]}]}},
            "garage": {"enabled": False, "ffmpeg": {"inputs": [{"path": "rtsp://cam/g", "roles": ["detect"]}]}}}})

    @guard
    async def stats(self, request):
        return web.json_response({"cameras": {"kitchen": {"camera_fps": 5}, "front_door": {"camera_fps": 5}}})

    @guard
    async def snapshot(self, request):
        return web.Response(body=SNAP + request.match_info["id"].encode(), content_type="image/jpeg")

    @guard
    async def review(self, request):
        items = [
            {"id": "r3", "camera": "front_door", "start_time": 300, "end_time": None, "severity": "alert",
             "data": {"objects": ["person"], "detections": ["d3"]}},
            {"id": "r2", "camera": "kitchen", "start_time": 200, "end_time": 210, "severity": "alert",
             "data": {"objects": ["person"], "detections": ["d2"]}},
            {"id": "r1", "camera": "kitchen", "start_time": 100, "end_time": 110, "severity": "alert",
             "data": {"objects": ["car"], "detections": ["d1"]}}]
        cams = request.query.get("cameras", "").split(",")
        before = float(request.query["before"]) if "before" in request.query else None
        return web.json_response([r for r in items if r["camera"] in cams
                                  and (before is None or r["start_time"] < before)])

    @guard
    async def clip(self, request):
        m = request.match_info
        self.clip_fetches.append((m["camera"], m["start"], m["end"]))
        return web.Response(body=b"CLIP" + bytes(5000), content_type="video/mp4")

    @guard
    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            async for _ in ws:
                pass
        finally:
            self.sockets.discard(ws)
        return ws

    async def push(self, kind, rid, camera="kitchen", objects=("person",), severity="alert", end=None):
        after = {"id": rid, "camera": camera, "start_time": time.time(), "end_time": end, "severity": severity,
                 "data": {"objects": list(objects), "detections": [f"det-{rid}"]}}
        msg = json.dumps({"topic": "reviews", "payload": json.dumps({"type": kind, "before": after, "after": after})})
        for ws in list(self.sockets):
            await ws.send_str(msg)

    async def drop(self):
        for ws in list(self.sockets):
            await ws.close()

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/login", self.login)
        app.router.add_get("/api/version", self.version)
        app.router.add_get("/api/config", self.config)
        app.router.add_get("/api/stats", self.stats)
        app.router.add_get("/api/events/{id}/snapshot.jpg", self.snapshot)
        app.router.add_get("/api/review", self.review)
        app.router.add_get("/api/{camera}/start/{start}/end/{end}/clip.mp4", self.clip)
        app.router.add_get("/ws", self.ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


class Events:
    def __init__(self, s):
        self.s, self.events = s, []

    async def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        async with self.s.get(f"{BASE}/events", timeout=aiohttp.ClientTimeout(total=None)) as r:
            buf = ""
            async for chunk in r.content.iter_any():
                buf += chunk.decode()
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    data = [l[6:] for l in block.splitlines() if l.startswith("data: ")]
                    if data:
                        ev = json.loads(data[0])
                        ev["_at"] = time.monotonic()
                        self.events.append(ev)

    async def wait(self, pred, timeout, since=0.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for ev in self.events:
                if ev["_at"] >= since and pred(ev):
                    return ev
            await asyncio.sleep(0.05)
        return None


def is_(t, cam=None):
    return lambda e: e["type"] == t and (cam is None or e.get("camera") == cam)


async def main():
    fake = FakeFrigate()
    await fake.start()
    data = Path(tempfile.mkdtemp())
    env = {**os.environ, "LOOKOUT_HTTPS_PORT": "18867", "LOOKOUT_BONJOUR": "0", "LOOKOUT_DATA": str(data), "LOOKOUT_CONFIG": str(data / "none.json"), "LOOKOUT_PORT": str(PORT),
           "LOOKOUT_RTSP_PORT": str(RTSP), "LOOKOUT_GO2RTC_API_PORT": str(API), "LOOKOUT_VERSION": "test"}
    proc = subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(150):
                try:
                    async with s.get(f"{BASE}/healthz") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            A = {"Authorization": f"Bearer {(data / 'admin-token').read_text().strip()}"}
            app_token = json.loads((data / "settings.json").read_text())["app_token"]
            # new hubs only take paired apps; these tests use the old shared token
            async with s.put(f"{BASE}/api/admin/settings", json={"allow_legacy_token": True}, headers=A) as r:
                assert r.status == 200
            ev = Events(s)
            await ev.start()
            # A camera full of people, detected by the hub itself at first
            async with s.post(f"{BASE}/api/admin/cameras", headers=A, json={
                    "id": "kitchen", "name": "Kitchen", "role": "door", "url": "unused",
                    "detect_url": str(MEDIA / "people.jpg"), "loop": True}) as r:
                assert r.status == 200, await r.text()
            d = await ev.wait(is_("person_detected", "kitchen"), 30)
            check("built-in source: the hub detects people itself", d is not None and d.get("source") is None)

            async with s.put(f"{BASE}/api/admin/source", headers=A, json={"type": "frigate", "url": fake.url}) as r:
                check("Frigate needing a login, given none: refused with a reason (400)",
                      r.status == 400 and "login" in (await r.text()))
            async with s.put(f"{BASE}/api/admin/source", headers=A,
                             json={"type": "frigate", "url": fake.url, "user": "lookout", "password": "nope"}) as r:
                check("wrong Frigate password refused (400)", r.status == 400)
            t_switch = time.monotonic()
            async with s.put(f"{BASE}/api/admin/source", headers=A,
                             json={"type": "frigate", "url": fake.url, "user": "lookout", "password": "pw"}) as r:
                body = await r.json()
                check("switch to Frigate imports the cameras it doesn't have (not disabled ones)",
                      r.status == 200 and body["imported"] == ["front_door"], json.dumps(body))
            async with s.get(f"{BASE}/api/state") as r:
                st = await r.json()
                cams = {c["id"]: c for c in st["cameras"]}
                check("state reports the Frigate source and both cameras",
                      st["source"] == "frigate" and set(cams) == {"kitchen", "front_door"}
                      and cams["front_door"]["name"] == "Front Door", json.dumps({k: v["name"] for k, v in cams.items()}))
            settings = json.loads((data / "settings.json").read_text())
            fd = next(c for c in settings["cameras"] if c["id"] == "front_door")
            check("imported camera uses Frigate's restream", fd["url"] == "rtsp://127.0.0.1:8554/front_door", fd["url"])
            gone = await ev.wait(lambda e: is_("person_gone", "kitchen")(e) and e["reason"] == "source_changed", 5, t_switch)
            check("switching source withdraws the hub's own alert", gone is not None)
            await asyncio.sleep(4)
            check("no detection by the hub while Frigate is the source (people still in frame)",
                  await ev.wait(is_("person_detected", "kitchen"), 0.1, t_switch) is None)
            for _ in range(50):
                if fake.sockets:
                    break
                await asyncio.sleep(0.1)
            check("hub follows Frigate's live reviews", bool(fake.sockets))

            t = time.monotonic()
            await fake.push("new", "rev-1")
            d = await ev.wait(lambda e: is_("person_detected", "kitchen")(e) and e.get("source") == "frigate", 3, t)
            check("Frigate person review -> Lookout alert", d is not None and d["role"] == "door",
                  f"{(d['_at'] - t) * 1000:.0f} ms" if d else "")
            async with s.get(f"{BASE}/api/watch/cameras/kitchen/snapshot.jpg?token={app_token}") as r:
                snap = await r.read()
                check("alert snapshot is Frigate's snapshot of the person", snap.startswith(SNAP) and b"det-rev-1" in snap)
            await fake.push("update", "rev-1")
            await asyncio.sleep(0.5)
            check("updates to the same review don't alert again",
                  sum(1 for e in ev.events if is_("person_detected", "kitchen")(e) and e["_at"] >= t) == 1)
            t = time.monotonic()
            await fake.push("end", "rev-1", end=time.time())
            g = await ev.wait(lambda e: is_("person_gone", "kitchen")(e) and e["reason"] == "frigate", 3, t)
            check("review end -> person gone", g is not None)

            t = time.monotonic()
            await fake.push("new", "rev-car", objects=("car",))
            await fake.push("new", "rev-det", severity="detection")
            await asyncio.sleep(1)
            check("car reviews and detection-severity reviews don't alert",
                  await ev.wait(is_("person_detected"), 0.1, t) is None)

            async with s.post(f"{BASE}/api/alerts", json={"camera": "kitchen", "enabled": False}) as r:
                pass
            t = time.monotonic()
            await fake.push("new", "rev-off")
            await asyncio.sleep(1)
            check("camera with alerts off ignores Frigate reviews", await ev.wait(is_("person_detected", "kitchen"), 0.1, t) is None)
            await fake.push("end", "rev-off", end=time.time())
            async with s.post(f"{BASE}/api/alerts", json={"camera": "kitchen", "enabled": True}) as r:
                pass

            await fake.push("new", "rev-2", camera="front_door")
            await ev.wait(is_("person_detected", "front_door"), 3)
            t = time.monotonic()
            logins = fake.logins
            await fake.drop()
            g = await ev.wait(lambda e: is_("person_gone", "front_door")(e) and e["reason"] == "frigate_disconnected", 3, t)
            check("Frigate connection lost: its open alerts are closed", g is not None)
            for _ in range(60):
                if fake.sockets:
                    break
                await asyncio.sleep(0.1)
            check("hub reconnects to Frigate (logging in again)", bool(fake.sockets) and fake.logins > logins)
            t = time.monotonic()
            await fake.push("new", "rev-3", camera="front_door")
            check("alerts flow again after reconnecting",
                  await ev.wait(is_("person_detected", "front_door"), 3, t) is not None)

            async with s.get(f"{BASE}/api/watch/cameras/kitchen/visitors") as r:
                check("visitors need the app token", r.status == 401)
            async with s.get(f"{BASE}/api/watch/cameras/kitchen/visitors?token={app_token}") as r:
                v = (await r.json())["visitors"]
                check("recent visitors: person alerts only, with thumbnails",
                      [x["id"] for x in v] == ["r2"] and v[0]["thumbnail"] == "/api/watch/visitors/d2.jpg", json.dumps(v))
            async with s.get(f"{BASE}/api/watch/events") as r:
                check("events need the app token", r.status == 401)
            async with s.get(f"{BASE}/api/watch/events?token={app_token}") as r:
                evs = (await r.json())["events"]
                check("events: person alerts across cameras, newest first, hub camera ids",
                      [(e["id"], e["camera"]) for e in evs] == [("r3", "front_door"), ("r2", "kitchen")], json.dumps(evs))
                check("finished event has a clip, ongoing one doesn't",
                      evs[1]["clip"] == "/api/watch/cameras/kitchen/clip.mp4?start=200.000&end=210.000"
                      and evs[0]["clip"] is None and evs[1]["thumbnail"] == "/api/watch/visitors/d2.jpg")
            async with s.get(f"{BASE}/api/watch/events?camera=kitchen&token={app_token}") as r:
                check("events filtered by camera", [e["id"] for e in (await r.json())["events"]] == ["r2"])
            async with s.get(f"{BASE}/api/watch/events?before=250&token={app_token}") as r:
                check("events page back with ?before", [e["id"] for e in (await r.json())["events"]] == ["r2"])
            async with s.get(f"{BASE}/api/watch/events?camera=nope&token={app_token}") as r:
                check("events for an unknown camera: 404", r.status == 404)
            clip = f"{BASE}/api/watch/cameras/kitchen/clip.mp4?start=200&end=210&token={app_token}"
            async with s.get(clip, headers={"Range": "bytes=0-3"}) as r:
                body = await r.read()
                check("clip fetched from Frigate (with 2 s either side) and served in ranges",
                      r.status == 206 and body == b"CLIP" and fake.clip_fetches == [("kitchen", "198", "212")],
                      f"{r.status} {body[:8]!r} {fake.clip_fetches}")
            async with s.get(clip) as r:
                check("clip cached: whole file, no second fetch",
                      r.status == 200 and len(await r.read()) == 5004 and len(fake.clip_fetches) == 1)
            async with s.get(f"{BASE}/api/watch/cameras/kitchen/clip.mp4?start=200&end={time.time() + 60:.0f}&token={app_token}") as r:
                check("clip of an unfinished event refused", r.status == 400)
            async with s.get(f"{BASE}/api/watch/visitors/d2.jpg?token={app_token}") as r:
                check("visitor thumbnail proxied from Frigate", (await r.read()).startswith(SNAP))
            async with s.get(f"{BASE}/api/admin/source", headers=A) as r:
                src = await r.json()
                check("source settings shown with the password masked",
                      src["password"] == "***" and src["connected"] is True and src["version"] == "0.18.2-fake", json.dumps(src))
            stored = json.loads((data / "settings.json").read_text())["source"]
            check("Frigate password kept in the hub's private settings", stored["password"] == "pw")

            t = time.monotonic()
            async with s.put(f"{BASE}/api/admin/source", headers=A, json={"type": "builtin"}) as r:
                check("switch back to the built-in detector", r.status == 200)
            check("hub detects people itself again",
                  await ev.wait(lambda e: is_("person_detected", "kitchen")(e) and e.get("source") is None, 40, t) is not None)
            check("and stops following Frigate", not fake.sockets or await asyncio.sleep(1) or not fake.sockets)

            # History only: the hub detects, Frigate supplies past events and clips
            async with s.get(f"{BASE}/api/watch/events?token={app_token}") as r:
                check("no history without Frigate", (await r.json())["events"] == [])
            async with s.put(f"{BASE}/api/admin/history", headers=A,
                             json={"url": fake.url, "user": "lookout", "password": "nope"}) as r:
                check("history: wrong Frigate password refused (400)", r.status == 400)
            async with s.put(f"{BASE}/api/admin/history", headers=A,
                             json={"url": fake.url, "user": "lookout", "password": "pw"}) as r:
                check("history from Frigate turned on", r.status == 200)
            async with s.get(f"{BASE}/api/admin/history", headers=A) as r:
                h = await r.json()
                check("history settings shown, password masked", h["enabled"] and h["connected"] and h["password"] == "***")
            async with s.get(f"{BASE}/api/watch/events?camera=kitchen&token={app_token}") as r:
                check("events from Frigate while the hub detects", [e["id"] for e in (await r.json())["events"]] == ["r2"])
            check("history doesn't follow Frigate's live reviews", not fake.sockets)
            check("history setting saved", json.loads((data / "settings.json").read_text())["history"]["password"] == "pw")
            async with s.put(f"{BASE}/api/admin/history", headers=A, json={}) as r:
                check("history turned off", r.status == 200)
            async with s.get(f"{BASE}/api/watch/events?token={app_token}") as r:
                check("no events once it's off", (await r.json())["events"] == [])
            ev.task.cancel()
    finally:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
        await fake.runner.cleanup()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
